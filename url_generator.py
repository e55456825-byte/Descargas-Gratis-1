"""
url_generator.py — Generador y parser de URLs BitZero.

Fixes aplicados:
  - BUG-01: base64 url-safe (sin '/' ni '+') en todas las partes de la clave.
    Antes: base64.b64encode(...).decode().replace('=', '#')  → contenía '/'
    Ahora: base64.urlsafe_b64encode(...).decode().rstrip('=')
  - BUG-03: el parser detecta dinámicamente si el último segmento es un hash
    (8 hex chars) o el filename, en lugar de asumir parts[-2].
  - BUG-07: cuando hay múltiples archivos, el filename se compone como
    '__multi__<b64manifest>' y el decoder lo reconoce.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _urlsafe_b64encode(data: bytes) -> str:
    """base64 url-safe sin padding (sin '=', sin '/', sin '+')."""
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def _urlsafe_b64decode(s: str) -> bytes:
    """Decodifica base64 url-safe reconstruyendo el padding."""
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _is_hex_hash(s: str) -> bool:
    """True si s es un hash MD5 corto de 8 hex chars."""
    return len(s) == 8 and bool(re.fullmatch(r'[0-9a-f]{8}', s))


class URLGenerator:
    """Genera y parsea URLs ofuscadas tipo BitZero."""

    # ── Encode ───────────────────────────────────────────────────────
    @staticmethod
    def encode_key(host: str, user: str, password: str, repo: str,
                   contexto: str, bitzero_mode: int,
                   timestamp: Optional[int] = None,
                   encryption_key: Optional[str] = None) -> str:
        """Codifica 7-8 partes en base64 url-safe, separadas por '-'.
        Las partes son: host, user, password, repo, contexto, modo, timestamp,
        y opcionalmente encryption_key.
        """
        if timestamp is None:
            timestamp = int(time.time())

        parts = [
            _urlsafe_b64encode(host.encode('utf-8')),
            _urlsafe_b64encode(user.encode('utf-8')),
            _urlsafe_b64encode(password.encode('utf-8')),
            _urlsafe_b64encode(repo.encode('utf-8')),
            _urlsafe_b64encode(contexto.encode('utf-8')),
            _urlsafe_b64encode(str(bitzero_mode).encode('utf-8')),
            _urlsafe_b64encode(str(timestamp).encode('utf-8')),
        ]
        if encryption_key:
            parts.append(_urlsafe_b64encode(encryption_key.encode('utf-8')))
        return "-".join(parts)

    @staticmethod
    def decode_key(encoded_key: str) -> Dict[str, Any]:
        """Decodifica una clave generada por encode_key."""
        try:
            parts = encoded_key.split("-")
            if len(parts) < 5:
                raise ValueError("Clave incompleta (mínimo 5 partes)")

            def dec(i: int) -> str:
                return _urlsafe_b64decode(parts[i]).decode('utf-8')

            result: Dict[str, Any] = {
                'host': dec(0),
                'user': dec(1),
                'password': dec(2),
                'repo': dec(3),
                'contexto': dec(4),
            }
            if len(parts) >= 6:
                result['bitzero_mode'] = int(dec(5))
            if len(parts) >= 7:
                result['timestamp'] = int(dec(6))
            if len(parts) >= 8:
                result['encryption_key'] = dec(7)
            return result
        except Exception as e:
            logger.error(f"decode_key error: {e}")
            return {}

    # ── Generación de URL completa ───────────────────────────────────
    @staticmethod
    def generate_bitzero_url(host: str, user: str, password: str, repo: str,
                              contexto: str, file_ids: List[str],
                              bitzero_mode: int, original_name: str,
                              file_size: int,
                              encryption_key: Optional[str] = None,
                              fake_host: Optional[str] = None) -> str:
        """Genera URL BitZero completa.

        Estructura:
          [<fake_host>/]<size>-<repo>/<token>/<mode>/<key>/<filename>[/<hash>]

        Si fake_host es None o vacío se genera un CÓDIGO sin dominio (solo la
        ruta): no delata ningún servicio y todos los descargadores lo aceptan
        igual, porque el host real de la revista va codificado dentro de <key>.

        - <key> contiene 7-8 partes separadas por '-', cada una base64 url-safe.
        - <filename> es base64 url-safe del nombre original.
        - <hash> (8 hex) sólo se añade si hay encryption_key.
        """
        token = "-".join(file_ids)
        timestamp = int(time.time())
        key = URLGenerator.encode_key(
            host, user, password, repo, contexto, bitzero_mode,
            timestamp, encryption_key
        )
        safe_name = _urlsafe_b64encode(original_name.encode('utf-8'))

        verification_hash = ""
        if encryption_key:
            data_to_hash = f"{original_name}{file_size}{timestamp}{encryption_key}"
            verification_hash = hashlib.md5(data_to_hash.encode('utf-8')).hexdigest()[:8]

        url_parts = [
            f"{file_size}-{repo}",
            token,
            str(bitzero_mode),
            key,
            safe_name,
        ]
        if verification_hash:
            url_parts.append(verification_hash)
        if fake_host:
            url_parts.insert(0, fake_host.rstrip('/'))
        return "/".join(url_parts)

    # ── Parser robusto (BUG-03 fix) ──────────────────────────────────
    @staticmethod
    def parse_bitzero_url(url: str) -> Optional[Dict[str, Any]]:
        """Parsea una URL BitZero generada por generate_bitzero_url.
        Detecta dinámicamente si el último segmento es un hash o el filename.
        """
        try:
            url = url.strip().replace('bitzero ', '').strip()
            parts = url.split('/')
            if len(parts) < 5:
                return None

            # base_url = parts[0] + '//' + parts[2]
            # size_repo = parts[3]
            # token = parts[4]  (variable según presencia de hash)

            # Detectar si último segmento es hash (8 hex) o filename
            last = parts[-1]
            if _is_hex_hash(last):
                verification_hash: Optional[str] = last
                filename_b64 = parts[-2]
                key_idx = -3
            else:
                verification_hash = None
                filename_b64 = last
                key_idx = -2

            key_encoded = parts[key_idx]
            bitzero_mode_str = parts[key_idx - 1]
            token = parts[key_idx - 2]
            size_repo = parts[key_idx - 3]

            if '-' not in size_repo:
                raise ValueError(f"Formato inválido en size-repo: {size_repo}")
            size_str, repo = size_repo.split('-', 1)
            file_size = int(size_str)

            key_info = URLGenerator.decode_key(key_encoded)
            if not key_info:
                raise ValueError("No se pudo decodificar la clave")

            try:
                bitzero_mode = int(bitzero_mode_str)
            except ValueError:
                bitzero_mode = key_info.get('bitzero_mode', 0)

            # Decodificar filename
            try:
                filename = _urlsafe_b64decode(filename_b64).decode('utf-8')
            except Exception:
                filename = filename_b64

            file_ids = token.split('-')

            return {
                'file_size': file_size,
                'repo': repo,
                'token': token,
                'bitzero_mode': bitzero_mode,
                'original_name': filename,
                'filename': filename,
                'key_info': key_info,
                'file_ids': file_ids,
                'verification_hash': verification_hash,
                'encryption_key': key_info.get('encryption_key'),
                'host': key_info.get('host'),
                'username': key_info.get('user'),
                'password': key_info.get('password'),
                'contexto': key_info.get('contexto'),
                'submission_id': key_info.get('repo'),
                'timestamp': key_info.get('timestamp'),
                'full_url': url,
            }
        except Exception as e:
            logger.error(f"parse_bitzero_url error: {e}")
            return None

    # ── Multi-archivo (BUG-07 fix) ───────────────────────────────────
    @staticmethod
    def build_multi_filename(filenames: List[str]) -> str:
        """Genera un nombre especial '__multi__<b64manifest>' que el decoder
        reconoce para indicar que el contenido es un .tar con varios archivos.
        """
        manifest = {
            "type": "multi",
            "files": filenames,
            "count": len(filenames),
            "generated_at": int(time.time()),
        }
        encoded = _urlsafe_b64encode(json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
        return f"__multi__{encoded}"

    @staticmethod
    def is_multi_filename(filename: str) -> bool:
        return filename.startswith("__multi__")

    @staticmethod
    def parse_multi_filename(filename: str) -> Optional[Dict[str, Any]]:
        """Si filename es multi, devuelve el manifest decodificado."""
        if not URLGenerator.is_multi_filename(filename):
            return None
        try:
            b64 = filename[len("__multi__"):]
            manifest = json.loads(_urlsafe_b64decode(b64).decode('utf-8'))
            return manifest
        except Exception as e:
            logger.error(f"parse_multi_filename error: {e}")
            return None
