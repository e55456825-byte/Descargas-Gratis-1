"""
database.py — Base de datos SQL asíncrona (aiosqlite).

Centraliza toda la persistencia:
  - users       (autorizaciones + quotas)
  - admins      (administradores dinámicos añadidos vía /addadmin)
  - files       (registro de archivos recibidos)
  - uploads     (historial de subidas BitZero)
  - revistas    (configuración de revistas OJS)
  - ojs_sessions(cookies + CSRF persistidos por revista)

El acceso es 100% asíncrono y seguro para concurrencia (sqlite3 maneja
locking interno con WAL activado).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiosqlite

from config import config

logger = logging.getLogger(__name__)


class AsyncDB:
    """Wrapper sobre aiosqlite con API de alto nivel."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    # ── Lifecycle ─────────────────────────────────────────────────────
    async def init(self) -> None:
        """Abre conexión, activa WAL y crea tablas si no existen."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._create_schema()
        await self._conn.commit()
        logger.info(f"DB inicializada en {self.db_path}")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_schema(self) -> None:
        assert self._conn is not None
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                added_by       INTEGER,
                added_date     TEXT NOT NULL,
                active         INTEGER NOT NULL DEFAULT 1,
                last_access    TEXT,
                quota_mb       INTEGER NOT NULL DEFAULT 1024,
                is_admin       INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);

            -- ─── Tabla de administradores dinámicos ───────────────────
            CREATE TABLE IF NOT EXISTS admins (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                added_by       INTEGER,
                added_date     TEXT NOT NULL,
                is_seed        INTEGER NOT NULL DEFAULT 0  -- 1 si viene de ADMIN_ID del .env
            );

            CREATE TABLE IF NOT EXISTS files (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                file_name      TEXT NOT NULL,
                file_path      TEXT NOT NULL,
                file_size      INTEGER NOT NULL,
                mime_type      TEXT,
                received_date  TEXT NOT NULL,
                tg_message_id  INTEGER,
                storage_msg_id INTEGER,  -- ID del mensaje en el canal de almacenamiento
                storage_channel_id INTEGER,  -- Canal donde se respaldó
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id);
            CREATE INDEX IF NOT EXISTS idx_files_date ON files(received_date);

            CREATE TABLE IF NOT EXISTS uploads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                revista_id      TEXT NOT NULL,
                submission_id   TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                original_size   INTEGER NOT NULL,
                uploaded_size   INTEGER NOT NULL,
                file_ids        TEXT NOT NULL,
                bitzero_mode    INTEGER NOT NULL,
                bitzero_url     TEXT,
                encryption_key  TEXT,
                status          TEXT NOT NULL,
                uploaded_at     TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_uploads_user ON uploads(user_id);
            CREATE INDEX IF NOT EXISTS idx_uploads_revista ON uploads(revista_id);

            CREATE TABLE IF NOT EXISTS revistas (
                rev_id          TEXT PRIMARY KEY,
                nombre          TEXT NOT NULL,
                base_url        TEXT NOT NULL,
                contexto        TEXT NOT NULL,
                username        TEXT NOT NULL,
                password        TEXT NOT NULL,
                submission_id   TEXT NOT NULL,
                bitzero_mode    INTEGER NOT NULL DEFAULT 0,
                encryption_key  TEXT,
                active          INTEGER NOT NULL DEFAULT 1,
                last_login_at   TEXT,
                last_login_ok   INTEGER
            );

            CREATE TABLE IF NOT EXISTS ojs_sessions (
                revista_id      TEXT PRIMARY KEY,
                cookies_json    TEXT NOT NULL,
                csrf_token      TEXT,
                created_at      TEXT NOT NULL,
                last_used_at    TEXT NOT NULL,
                FOREIGN KEY (revista_id) REFERENCES revistas(rev_id) ON DELETE CASCADE
            );

            -- ─── Cola de subidas pendientes (sobreviven reinicios) ───
            -- Guarda las subidas en cola para reanudarlas si el bot se
            -- reinicia antes de que les toque (apagado limpio).
            CREATE TABLE IF NOT EXISTS upload_queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                file_paths      TEXT NOT NULL,          -- JSON list
                compress_videos INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            -- ─── Decisión pendiente de modo (comprimir/original) ───
            -- Persiste la decisión que el usuario aún no respondió para que
            -- sobreviva a reinicios: mientras exista una fila para el usuario,
            -- NO se debe descargar ni liberar el siguiente vídeo en espera
            -- (no acumular en el VPS sin haber resuelto el anterior).
            CREATE TABLE IF NOT EXISTS pending_decisions (
                user_id        INTEGER PRIMARY KEY,
                file_paths     TEXT NOT NULL,          -- JSON list
                created_at     TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            -- ─── Vídeos en espera (cola por usuario, SIN descargar) ──
            -- Cuando un usuario ya tiene un vídeo en curso (descargando,
            -- esperando decisión comprimir/original, o subiéndose), los
            -- NUEVOS vídeos que envía NO se bajan al disco (evita llenar
            -- el VPS con varios archivos de ~1 GB a la vez): solo se guarda
            -- la referencia al mensaje y se procesa automáticamente cuando
            -- el anterior termina y libera espacio.
            CREATE TABLE IF NOT EXISTS held_downloads (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                chat_id        INTEGER NOT NULL,
                message_id     INTEGER NOT NULL,
                file_name      TEXT,
                created_at     TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_held_user ON held_downloads(user_id);

            -- ─── Comprimidos en espera de subida ──────────────────────
            -- Cuando el usuario eligió "comprimir y subir" y la compresión
            -- terminó OK pero la subida falló (revista caída, HTTP 500,
            -- FloodWait de Telegram, etc.), el vídeo original se BORRA del
            -- VPS y el comprimido (que pesa mucho menos) se guarda aquí.
            -- Un loop en background lo sube automáticamente en cuanto una
            -- revista vuelva a estar operativa, sin volver a comprimir.
            CREATE TABLE IF NOT EXISTS pending_compressed (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                file_path      TEXT NOT NULL,   -- ruta del vídeo comprimido (_temp)
                original_name  TEXT NOT NULL,   -- nombre visible original
                original_path  TEXT,            -- original borrado (informativo)
                attempts       INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL,
                last_attempt_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pending_compressed_user
                ON pending_compressed(user_id);
        """)

    # ── Admins (dinámicos) ────────────────────────────────────────────
    async def add_admin(self, user_id: int, username: Optional[str],
                        added_by: int, is_seed: bool = False) -> bool:
        """Añade un admin. Devuelve True si se insertó, False si ya existía."""
        assert self._conn is not None
        try:
            await self._conn.execute(
                """INSERT INTO admins (user_id, username, added_by, added_date, is_seed)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, username, added_by,
                 time.strftime('%Y-%m-%d %H:%M:%S'), 1 if is_seed else 0)
            )
            await self._conn.commit()
            config.add_admin_to_cache(user_id)
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_admin(self, user_id: int) -> bool:
        """Quita un admin. Devuelve True si se eliminó.
        Los SEED admins (de .env) no se pueden quitar por DB.
        """
        assert self._conn is not None
        # Verificar que no sea seed
        async with self._conn.execute(
            "SELECT is_seed FROM admins WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row and row['is_seed'] == 1:
                return False
        cur = await self._conn.execute(
            "DELETE FROM admins WHERE user_id = ? AND is_seed = 0",
            (user_id,)
        )
        await self._conn.commit()
        if cur.rowcount > 0:
            config.remove_admin_from_cache(user_id)
            return True
        return False

    async def list_admins(self) -> List[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute("SELECT * FROM admins ORDER BY added_date ASC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def is_admin_in_db(self, user_id: int) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def load_admins_to_cache(self) -> None:
        """Carga todos los admins desde DB al cache en memoria.
        Se llama en startup.
        """
        admins = await self.list_admins()
        for a in admins:
            config.add_admin_to_cache(a['user_id'])
        # Los seeds siempre se mantienen
        for seed_id in config.SEED_ADMIN_IDS:
            config.add_admin_to_cache(seed_id)
        logger.info(f"Admins cargados en cache: {len(config.get_all_admins())} total")

    # ── Users ─────────────────────────────────────────────────────────
    async def add_user(self, user_id: int, username: Optional[str], added_by: int,
                       quota_mb: Optional[int] = None) -> bool:
        """Devuelve True si se insertó, False si ya existía."""
        assert self._conn is not None
        quota = quota_mb if quota_mb is not None else config.DEFAULT_USER_QUOTA_MB
        is_admin = 1 if config.is_admin(user_id) else 0
        try:
            await self._conn.execute(
                """INSERT INTO users (user_id, username, added_by, added_date, active, quota_mb, is_admin)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (user_id, username, added_by, time.strftime('%Y-%m-%d %H:%M:%S'), quota, is_admin)
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_user(self, user_id: int) -> bool:
        assert self._conn is not None
        # No permitir eliminar a admins
        if config.is_admin(user_id):
            return False
        cur = await self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await self._conn.commit()
        return cur.rowcount > 0

    async def is_authorized(self, user_id: int) -> bool:
        """Admins siempre autorizados; resto debe estar activo en DB."""
        if config.is_admin(user_id):
            return True
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM users WHERE user_id = ? AND active = 1", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_users(self) -> List[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute("SELECT * FROM users ORDER BY added_date DESC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def update_last_access(self, user_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE users SET last_access = ? WHERE user_id = ?",
            (time.strftime('%Y-%m-%d %H:%M:%S'), user_id)
        )
        await self._conn.commit()

    async def set_user_quota(self, user_id: int, quota_mb: int) -> bool:
        assert self._conn is not None
        cur = await self._conn.execute(
            "UPDATE users SET quota_mb = ? WHERE user_id = ?",
            (quota_mb, user_id)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    # ── Files ─────────────────────────────────────────────────────────
    async def register_file(self, user_id: int, file_name: str, file_path: str,
                            file_size: int, mime_type: Optional[str],
                            tg_message_id: Optional[int],
                            storage_msg_id: Optional[int] = None,
                            storage_channel_id: Optional[int] = None) -> int:
        assert self._conn is not None
        cur = await self._conn.execute(
            """INSERT INTO files (user_id, file_name, file_path, file_size, mime_type,
                                  received_date, tg_message_id, storage_msg_id, storage_channel_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, file_name, file_path, file_size, mime_type,
             time.strftime('%Y-%m-%d %H:%M:%S'), tg_message_id,
             storage_msg_id, storage_channel_id)
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def list_user_files(self, user_id: int) -> List[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM files WHERE user_id = ? ORDER BY received_date DESC",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def delete_file(self, file_id: int, user_id: int) -> bool:
        """Borra sólo si pertenece al usuario (aislamiento)."""
        assert self._conn is not None
        cur = await self._conn.execute(
            "DELETE FROM files WHERE id = ? AND user_id = ?",
            (file_id, user_id)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_file_by_path(self, user_id: int, file_path: str) -> bool:
        """Borra el registro de un archivo concreto por ruta (aislamiento)."""
        assert self._conn is not None
        cur = await self._conn.execute(
            "DELETE FROM files WHERE user_id = ? AND file_path = ?",
            (user_id, file_path)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_user_files(self, user_id: int) -> int:
        """Borra todos los registros de archivos del usuario (tabla files).
        Devuelve el número de filas eliminadas.
        """
        assert self._conn is not None
        cur = await self._conn.execute(
            "DELETE FROM files WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def get_user_usage(self, user_id: int) -> int:
        """Suma bytes usados por el usuario."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) AS total FROM files WHERE user_id = ?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row['total']) if row else 0

    # ── Cola de subidas pendientes (persistencia entre reinicios) ─────
    async def save_pending_upload(self, user_id: int, file_paths: List[str],
                                  compress_videos: bool = False) -> int:
        """Guarda una subida en cola. Devuelve el row id."""
        assert self._conn is not None
        cur = await self._conn.execute(
            """INSERT INTO upload_queue (user_id, file_paths, compress_videos, created_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, json.dumps(file_paths), 1 if compress_videos else 0,
             time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def list_pending_uploads(self) -> List[Dict[str, Any]]:
        """Todas las subidas pendientes (para reanudarlas en el arranque)."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM upload_queue ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d['file_paths'] = json.loads(d['file_paths'])
            except (ValueError, TypeError):
                d['file_paths'] = []
            result.append(d)
        return result

    async def delete_pending_upload(self, row_id: int) -> None:
        """Elimina una subida pendiente (terminó o se canceló)."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM upload_queue WHERE id = ?", (row_id,)
        )
        await self._conn.commit()

    async def list_pending_upload_user_ids(self) -> set:
        """Usuarios con alguna subida persistida en la cola de subidas."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT DISTINCT user_id FROM upload_queue"
        ) as cur:
            rows = await cur.fetchall()
            return {r['user_id'] for r in rows}

    # ── Decisiones de modo pendientes (persistidas para reinicios) ────
    async def save_pending_decision(self, user_id: int, file_paths: List[str]) -> None:
        """Registra (o actualiza) la decisión comprimir/original pendiente."""
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO pending_decisions (user_id, file_paths, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   file_paths = excluded.file_paths,
                   created_at = excluded.created_at""",
            (user_id, json.dumps(file_paths),
             time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        await self._conn.commit()

    async def get_pending_decision(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Devuelve la decisión pendiente del usuario, o None."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM pending_decisions WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d['file_paths'] = json.loads(d['file_paths'])
        except (ValueError, TypeError):
            d['file_paths'] = []
        return d

    async def clear_pending_decision(self, user_id: int) -> None:
        """Elimina la decisión pendiente (ya respondida o cancelada)."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM pending_decisions WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def list_pending_decision_users(self) -> List[int]:
        """Usuarios con una decisión pendiente sin responder."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT DISTINCT user_id FROM pending_decisions ORDER BY user_id"
        ) as cur:
            rows = await cur.fetchall()
            return [r['user_id'] for r in rows]

    # ── Vídeos en espera de turno (cola por usuario) ──────────────────
    async def add_held_download(self, user_id: int, chat_id: int,
                                message_id: int, file_name: str) -> int:
        """Guarda la referencia de un vídeo que espera turno (sin descargar)."""
        assert self._conn is not None
        cur = await self._conn.execute(
            """INSERT INTO held_downloads (user_id, chat_id, message_id, file_name, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, chat_id, message_id, file_name,
             time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def count_held_downloads(self, user_id: int) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM held_downloads WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row['n']) if row else 0

    async def pop_first_held_download(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Saca (y borra) la entrada más antigua en espera del usuario.
        Devuelve None si no hay nada en espera.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM held_downloads WHERE user_id = ? ORDER BY id ASC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await self._conn.execute(
            "DELETE FROM held_downloads WHERE id = ?", (row['id'],)
        )
        await self._conn.commit()
        return dict(row)

    async def list_users_with_held_downloads(self) -> List[int]:
        """Usuarios que tienen al menos un vídeo en espera."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT DISTINCT user_id FROM held_downloads ORDER BY user_id"
        ) as cur:
            rows = await cur.fetchall()
            return [r['user_id'] for r in rows]

    # ── Comprimidos en espera de subida ───────────────────────────────
    async def save_pending_compressed_upload(self, user_id: int, file_path: str,
                                             original_name: str,
                                             original_path: Optional[str] = None) -> int:
        """Guarda un vídeo comprimido para reintentar su subida más tarde.

        Devuelve el row id. Si ya existía una fila para el mismo file_path
        se reutiliza (evita duplicados si el bot se reinicia a mitad).
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT id FROM pending_compressed WHERE file_path = ? AND user_id = ?",
            (file_path, user_id)
        ) as cur:
            row = await cur.fetchone()
        if row:
            return int(row['id'])
        cur = await self._conn.execute(
            """INSERT INTO pending_compressed
               (user_id, file_path, original_name, original_path, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, file_path, original_name, original_path,
             time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def list_pending_compressed_uploads(self) -> List[Dict[str, Any]]:
        """Todos los comprimidos pendientes de subida (orden de llegada)."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM pending_compressed ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_pending_compressed_upload(self, row_id: int) -> None:
        """Elimina un comprimido pendiente (subido con éxito o descartado)."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM pending_compressed WHERE id = ?", (row_id,)
        )
        await self._conn.commit()

    async def bump_pending_compressed_upload(self, row_id: int) -> None:
        """Marca un intento fallido (para seguimiento en logs)."""
        assert self._conn is not None
        await self._conn.execute(
            """UPDATE pending_compressed
               SET attempts = attempts + 1, last_attempt_at = ?
               WHERE id = ?""",
            (time.strftime('%Y-%m-%d %H:%M:%S'), row_id)
        )
        await self._conn.commit()

    # ── Uploads (historial) ───────────────────────────────────────────
    async def log_upload(self, user_id: int, revista_id: str, submission_id: str,
                         original_name: str, original_size: int, uploaded_size: int,
                         file_ids: List[str], bitzero_mode: int,
                         bitzero_url: Optional[str], encryption_key: Optional[str],
                         status: str) -> int:
        assert self._conn is not None
        cur = await self._conn.execute(
            """INSERT INTO uploads
               (user_id, revista_id, submission_id, original_name, original_size,
                uploaded_size, file_ids, bitzero_mode, bitzero_url, encryption_key,
                status, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, revista_id, submission_id, original_name, original_size,
             uploaded_size, json.dumps(file_ids), bitzero_mode, bitzero_url,
             encryption_key, status, time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def list_revista_upload_file_ids(self, revista_id: str) -> List[str]:
        """Todos los file_ids que el bot registró como subidos a una revista.

        Se usa para borrar SOLO lo que el bot subió (la submission puede estar
        compartida con otros usuarios, así que nunca se borra 'todo').
        """
        assert self._conn is not None
        ids: List[str] = []
        async with self._conn.execute(
            "SELECT file_ids FROM uploads WHERE revista_id = ?", (revista_id,)
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            try:
                parsed = json.loads(r['file_ids']) if r['file_ids'] else []
            except (ValueError, TypeError):
                continue
            ids.extend(str(x) for x in parsed if x not in (None, ""))
        # Únicos, conservando el orden
        vistos = set()
        return [x for x in ids if not (x in vistos or vistos.add(x))]

    async def list_user_uploads(self, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM uploads WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT ?",
            (user_id, limit)
        ) as cur:
            rows = await cur.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d['file_ids'] = json.loads(d['file_ids']) if d['file_ids'] else []
                results.append(d)
            return results

    # ── Revistas ──────────────────────────────────────────────────────
    async def get_revista(self, rev_id: str) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM revistas WHERE rev_id = ?", (rev_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_revistas(self, only_active: bool = False) -> List[Dict[str, Any]]:
        assert self._conn is not None
        sql = "SELECT * FROM revistas"
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY nombre"
        async with self._conn.execute(sql) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def upsert_revista(self, rev_id: str, **fields: Any) -> bool:
        """Inserta o actualiza una revista."""
        assert self._conn is not None
        allowed = {'nombre', 'base_url', 'contexto', 'username', 'password',
                   'submission_id', 'bitzero_mode', 'encryption_key', 'active'}
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return False

        cols = ['rev_id'] + list(clean.keys())
        placeholders = ', '.join('?' * len(cols))
        values = [rev_id] + list(clean.values())
        updates = ', '.join(f"{c}=excluded.{c}" for c in clean.keys())
        sql = f"""
            INSERT INTO revistas ({', '.join(cols)}) VALUES ({placeholders})
            ON CONFLICT(rev_id) DO UPDATE SET {updates}
        """
        cur = await self._conn.execute(sql, values)
        await self._conn.commit()
        return cur.rowcount > 0

    async def update_revista_field(self, rev_id: str, field: str, value: Any) -> bool:
        """Actualiza un campo concreto (con whitelist por seguridad)."""
        allowed = {'nombre', 'base_url', 'contexto', 'username', 'password',
                   'submission_id', 'bitzero_mode', 'encryption_key', 'active'}
        if field not in allowed:
            return False
        assert self._conn is not None
        cur = await self._conn.execute(
            f"UPDATE revistas SET {field} = ? WHERE rev_id = ?",
            (value, rev_id)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def update_revista_login_status(self, rev_id: str, ok: bool) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE revistas SET last_login_at = ?, last_login_ok = ? WHERE rev_id = ?",
            (time.strftime('%Y-%m-%d %H:%M:%S'), 1 if ok else 0, rev_id)
        )
        await self._conn.commit()

    # ── OJS Sessions ──────────────────────────────────────────────────
    async def save_ojs_session(self, revista_id: str, cookies_json: str,
                               csrf_token: Optional[str]) -> None:
        assert self._conn is not None
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        await self._conn.execute(
            """INSERT INTO ojs_sessions (revista_id, cookies_json, csrf_token, created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(revista_id) DO UPDATE SET
                   cookies_json = excluded.cookies_json,
                   csrf_token   = excluded.csrf_token,
                   last_used_at = excluded.last_used_at""",
            (revista_id, cookies_json, csrf_token, now, now)
        )
        await self._conn.commit()

    async def load_ojs_session(self, revista_id: str) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM ojs_sessions WHERE revista_id = ?", (revista_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # ── Stats ─────────────────────────────────────────────────────────
    async def get_global_stats(self) -> Dict[str, Any]:
        assert self._conn is not None
        stats: Dict[str, Any] = {}
        async with self._conn.execute("SELECT COUNT(*) AS n FROM users WHERE active = 1") as cur:
            stats['active_users'] = (await cur.fetchone())['n']
        async with self._conn.execute("SELECT COUNT(*) AS n FROM users") as cur:
            stats['total_users'] = (await cur.fetchone())['n']
        async with self._conn.execute("SELECT COUNT(*) AS n FROM admins") as cur:
            stats['total_admins'] = (await cur.fetchone())['n']
        async with self._conn.execute("SELECT COUNT(*) AS n FROM files") as cur:
            stats['total_files'] = (await cur.fetchone())['n']
        async with self._conn.execute("SELECT COALESCE(SUM(file_size), 0) AS s FROM files") as cur:
            stats['total_bytes'] = (await cur.fetchone())['s']
        async with self._conn.execute("SELECT COUNT(*) AS n FROM uploads WHERE status = 'success'") as cur:
            stats['successful_uploads'] = (await cur.fetchone())['n']
        async with self._conn.execute("SELECT COUNT(*) AS n FROM revistas WHERE active = 1") as cur:
            stats['active_revistas'] = (await cur.fetchone())['n']
        return stats


# Singleton global
db = AsyncDB(config.DB_PATH)
