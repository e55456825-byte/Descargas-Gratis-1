#!/usr/bin/env python3
"""Limpieza de retención del BitUploaderBot.

Evita que el disco se llene con archivos acumulados:
  1) Borra archivos de raiz/ con más de RETENTION_DAYS días.
  2) Elimina registros huérfanos de la tabla files (archivo ya no existe).

Se ejecuta desde cron una vez al día. Ejemplo de entrada en crontab
(ajusta la ruta a donde tengas el bot; con Docker puedes ejecutarlo con
`docker compose exec bot python cleanup_retention.py`):

    0 4 * * * cd /ruta/a/BitUploaderBot && ./venv/bin/python cleanup_retention.py >> logs/cleanup.log 2>&1

Las rutas se resuelven de forma relativa a este archivo, por lo que no
depende de una ubicación concreta en el disco.
"""
import os
import sqlite3
import time

# ── Cargar .env si está disponible (mismos valores que usa el bot) ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:
    pass


def _resolve(path: str) -> str:
    """Devuelve una ruta absoluta; si es relativa, la cuelga de BASE_DIR."""
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


RAIZ = _resolve(os.getenv("ROOT_DIR", "raiz"))
DATA_DIR = _resolve(os.getenv("DATA_DIR", "data"))
DB = _resolve(os.getenv("DB_PATH", os.path.join(DATA_DIR, "bot.db")))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "3"))
CUTOFF = time.time() - RETENTION_DAYS * 86400


def _host_path(file_path: str) -> str:
    """Traduce la ruta guardada en BD a la ruta real en este host.

    En Docker la tabla `files` guarda rutas tipo `/app/raiz/...`; en local
    pueden ser relativas (`raiz/...`). Se acepta cualquiera de las dos.
    """
    if not file_path:
        return ""
    if file_path.startswith("/app/raiz/"):
        return os.path.join(RAIZ, file_path[len("/app/raiz/"):])
    return _resolve(file_path)


# 1) Archivos viejos en todo raiz/ (incluye subcarpetas _temp, multimedia, etc.)
removed = 0
freed = 0
for root, _dirs, files in os.walk(RAIZ):
    for name in files:
        path = os.path.join(root, name)
        try:
            st = os.stat(path)
            if st.st_mtime < CUTOFF:
                freed += st.st_size
                os.remove(path)
                removed += 1
        except OSError:
            pass

# 2) Registros huérfanos en la tabla files (el archivo ya no existe)
orphans = 0
if os.path.exists(DB):
    try:
        con = sqlite3.connect(DB, timeout=5)
        cur = con.cursor()
        rows = cur.execute("SELECT id, file_path FROM files").fetchall()
        for fid, fp in rows:
            if not os.path.exists(_host_path(fp)):
                cur.execute("DELETE FROM files WHERE id = ?", (fid,))
                orphans += 1
        con.commit()
        con.close()
    except sqlite3.Error as e:
        print(f"Error limpiando la tabla files: {e}")

print(f"Retención {RETENTION_DAYS}d → archivos viejos borrados: {removed} ({freed / 1048576:.1f} MB)")
print(f"Registros huérfanos eliminados: {orphans}")
