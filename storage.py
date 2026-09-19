"""
storage.py — Gestión de carpetas y archivos por usuario.

Cada usuario tiene su propia carpeta bajo ROOT_DIR/<user_id>/ con
subcarpetas para documentos, multimedia, temporales y logs. Se aplican
quotas por usuario y aislamiento estricto: ningún handler accede a la
carpeta de otro usuario salvo el admin explícito.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tarfile
import time
from typing import Dict, List, Optional

from config import config

logger = logging.getLogger(__name__)


class UserStorage:
    """Gestor de almacenamiento aislado por usuario."""

    # Subdirectorios estándar dentro de cada carpeta de usuario
    SUBDIRS = {
        'docs': 'documentos',
        'media': 'multimedia',
        'temp': '_temp',
        'logs': '_logs',
    }

    # ── Helpers de ruta ───────────────────────────────────────────────
    def get_user_dir(self, user_id: int, subdir: Optional[str] = None) -> str:
        """Devuelve la ruta absoluta del directorio del usuario.
        Si se pasa subdir ('docs'|'media'|'temp'|'logs'), devuelve esa subcarpeta.
        """
        base = os.path.join(config.ROOT_DIR, str(user_id))
        if subdir:
            if subdir not in self.SUBDIRS:
                raise ValueError(f"Subdir inválido: {subdir}")
            return os.path.join(base, self.SUBDIRS[subdir])
        return base

    def ensure_user_dirs(self, user_id: int) -> None:
        """Crea la estructura completa de carpetas del usuario."""
        for sub in self.SUBDIRS.values():
            os.makedirs(os.path.join(config.ROOT_DIR, str(user_id), sub), exist_ok=True)

    # ── Limpieza de nombres ──────────────────────────────────────────
    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Reemplaza caracteres no seguros para el sistema de archivos."""
        if not name:
            return "file.bin"
        # Caracteres prohibidos en Windows + Unix
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        # Recortar longitud
        if len(name) > 200:
            base, ext = os.path.splitext(name)
            name = base[:200 - len(ext)] + ext
        return name

    @staticmethod
    def unique_path(directory: str, filename: str) -> str:
        """Genera ruta única añadiendo _1, _2, ... si el archivo existe."""
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(filename)
        counter = 1
        while True:
            candidate = os.path.join(directory, f"{base}_{counter}{ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    # ── Listado y borrado (consistencia con /ls y /rm) ────────────────
    def list_user_files(self, user_id: int,
                        sort_by: str = 'modified_desc') -> List[Dict[str, object]]:
        """Lista los archivos de la carpeta principal del usuario.

        sort_by:
          - 'modified_desc' (default, usado por /ls y /rm — BUG-02 fix)
          - 'name_asc'
          - 'size_desc'
        """
        user_dir = self.get_user_dir(user_id)
        if not os.path.isdir(user_dir):
            return []

        files: List[Dict[str, object]] = []
        for item in os.listdir(user_dir):
            path = os.path.join(user_dir, item)
            if not os.path.isfile(path):
                continue
            # Saltar archivos internos
            if item.startswith('.') or item.endswith(('.tmp', '.temp', '.log')):
                continue
            stat = os.stat(path)
            files.append({
                'name': item,
                'path': path,
                'size': stat.st_size,
                'modified': stat.st_mtime,
            })

        if sort_by == 'modified_desc':
            files.sort(key=lambda x: x['modified'], reverse=True)  # type: ignore[arg-type]
        elif sort_by == 'name_asc':
            files.sort(key=lambda x: x['name'])  # type: ignore[arg-type]
        elif sort_by == 'size_desc':
            files.sort(key=lambda x: x['size'], reverse=True)  # type: ignore[arg-type]
        return files

    def delete_user_file_by_index(self, user_id: int, idx: int) -> Optional[str]:
        """Borra archivo por índice (1-based) usando el MISMO orden que /ls.
        Devuelve el nombre del archivo borrado o None si índice inválido.
        """
        files = self.list_user_files(user_id, sort_by='modified_desc')
        if idx < 1 or idx > len(files):
            return None
        target = files[idx - 1]
        try:
            os.remove(target['path'])  # type: ignore[arg-type]
            return target['name']  # type: ignore[return-value]
        except OSError as e:
            logger.error(f"No se pudo borrar {target['path']}: {e}")
            return None

    def delete_all_user_files(self, user_id: int) -> int:
        """Borra todos los archivos de la carpeta principal del usuario.
        Devuelve el número de archivos eliminados.
        """
        user_dir = self.get_user_dir(user_id)
        if not os.path.isdir(user_dir):
            return 0
        count = 0
        for item in os.listdir(user_dir):
            path = os.path.join(user_dir, item)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    count += 1
                elif os.path.isdir(path) and not item.startswith('.'):
                    shutil.rmtree(path)
                    count += 1
            except OSError as e:
                logger.warning(f"No se pudo eliminar {path}: {e}")
        return count

    # ── Quotas ───────────────────────────────────────────────────────
    def get_user_usage_bytes(self, user_id: int) -> int:
        """Suma bytes usados por el usuario en su carpeta."""
        user_dir = self.get_user_dir(user_id)
        if not os.path.isdir(user_dir):
            return 0
        total = 0
        for root, _, files in os.walk(user_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def check_quota(self, user_id: int, additional_bytes: int,
                    quota_mb: Optional[int] = None) -> bool:
        """Devuelve True si el usuario puede añadir additional_bytes más."""
        if quota_mb is None:
            # Si no se pasa, asumir ilimitado (admins)
            return True
        if quota_mb < 0:
            return True  # ilimitado
        current = self.get_user_usage_bytes(user_id)
        limit_bytes = quota_mb * 1024 * 1024
        return (current + additional_bytes) <= limit_bytes

    # ── Multi-archivo: empaquetar en TAR (BUG-07 fix) ────────────────
    def package_multiple_files(self, file_paths: List[str], user_id: int) -> str:
        """Empaqueta múltiples archivos en un .tar dentro de _temp del usuario.
        Devuelve la ruta del tar.
        """
        temp_dir = self.get_user_dir(user_id, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        timestamp = int(time.time())
        tar_path = os.path.join(temp_dir, f"multi_{user_id}_{timestamp}.tar")
        with tarfile.open(tar_path, 'w') as tar:
            for fp in file_paths:
                tar.add(fp, arcname=os.path.basename(fp))
        logger.info(f"Empaquetado {len(file_paths)} archivos en {tar_path}")
        return tar_path

    # ── Limpieza de temporales ───────────────────────────────────────
    def cleanup_temp(self, user_id: int) -> int:
        """Borra todos los archivos temporales del usuario. Devuelve count."""
        temp_dir = self.get_user_dir(user_id, 'temp')
        if not os.path.isdir(temp_dir):
            return 0
        count = 0
        for item in os.listdir(temp_dir):
            path = os.path.join(temp_dir, item)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    count += 1
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    count += 1
            except OSError as e:
                logger.warning(f"No se pudo limpiar {path}: {e}")
        return count


# Singleton global
storage = UserStorage()
