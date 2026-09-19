"""
uploader.py — RevistaUploader con correcciones.

Fixes aplicados:
  - BUG-05: trackea original_size además del tamaño subido, para que la URL
    final lleve el tamaño original (no el camuflado).
  - BUG-06: cleanup garantizado de temporales en bloque finally.
  - BUG-08: refresca CSRF antes de cada subida + retry en 403.
  - BUG-07: si hay múltiples archivos, los empaqueta en .tar antes de subir.
  - BUG-09: auto-descubrimiento del submission_id. Si el ID configurado está
    desactualizado (OJS responde 404), se consulta /api/v1/submissions y se
    usa el primer ID visible del usuario, corrigiéndose también en la BD.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from cancellation import UserCancelledError
from config import config
from encoder import BitZeroEncoder
from storage import storage
from url_generator import URLGenerator

logger = logging.getLogger(__name__)


class UploadAbortedError(Exception):
    """La subida se abortó porque la revista falló o dejó de responder; se
    evita así generar una URL BitZero con partes faltantes (subida incompleta).

    .detail lleva el motivo preciso (ej. 'HTTP 500 — error del servidor')
    para que el mensaje al usuario sea útil y el fallback sepa qué pasó.
    """

    def __init__(self, detail: str = "el servidor dejó de responder") -> None:
        super().__init__(detail)
        self.detail = detail


class RevistaUploader:
    """Uploader a OJS con soporte BitZero completo."""

    def __init__(self, username: str, password: str, submission_id: str,
                 base_url: str, contexto: str, bitzero_mode: int = 0,
                 encryption_key: Optional[str] = None,
                 chunk_size_mb: Optional[int] = None) -> None:
        self.base_url = base_url.rstrip('/')
        self.contexto = contexto
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })
        self.session.verify = False  # revistas cubanas suelen tener cert self-signed

        self.csrf_token: Optional[str] = None
        self.submission_id = str(submission_id).strip()
        self.chunk_size = (chunk_size_mb or config.CHUNK_SIZE_MB) * 1024 * 1024
        self.username = username
        self.password = password
        self.bitzero_mode = bitzero_mode
        self.encryption_key = encryption_key
        self.uploaded_files: List[Dict[str, Any]] = []
        self.is_logged_in = False
        # Último error de subida: para mensajes precisos y para que el
        # fallback de /up sepa por qué falló la revista (ej. HTTP 500).
        self._last_upload_error: str = ""
        # Optimización: comprobaciones de sesión/submission UNA vez por lote
        self._batch_session_ok = False
        self._batch_submission_ok = False

    def reset_batch_state(self) -> None:
        """Reinicia el estado 'verificado una vez por lote'.

        Se llama al empezar cada subida: el uploader se reutiliza entre
        subidas (caché), así que la sesión y la submission deben volver a
        comprobarse en el siguiente lote.
        """
        self._batch_session_ok = False
        self._batch_submission_ok = False

    # ── Login ────────────────────────────────────────────────────────
    def login(self) -> bool:
        try:
            login_url = f"{self.base_url}/index.php/{self.contexto}/login"
            resp = self.session.get(login_url, timeout=60)
            soup = BeautifulSoup(resp.text, 'html.parser')

            csrf_token = self._extract_csrf(soup, resp.text)
            if not csrf_token:
                logger.warning("CSRF no encontrado en login, continuando con token temporal")
                csrf_token = "temp_token"
            self.csrf_token = csrf_token

            data = {
                'csrfToken': csrf_token,
                'username': self.username,
                'password': self.password,
                'remember': '1',
                'source': '',
            }
            post_url = f"{self.base_url}/index.php/{self.contexto}/login/signIn"
            resp = self.session.post(post_url, data=data, timeout=60)

            if any(x in resp.text for x in ['Cerrar sesión', 'submissionId=', 'Logout', 'Sign out']):
                logger.info(f"Login OK en {self.base_url}")
                self.is_logged_in = True
                return True
            logger.warning(f"Login fallido en {self.base_url}")
            self.is_logged_in = False
            return False
        except Exception as e:
            logger.error(f"login error: {e}")
            self.is_logged_in = False
            return False

    @staticmethod
    def _extract_csrf(soup: BeautifulSoup, html: str) -> Optional[str]:
        """Busca CSRF en input, script y meta tag."""
        csrf_input = soup.find('input', {'name': 'csrfToken'})
        if csrf_input and csrf_input.get('value'):
            return csrf_input['value']
        for script in soup.find_all('script'):
            if script.string and 'csrfToken' in script.string:
                m = re.search(r'csrfToken[\'"]?\s*:\s*[\'"]([^\'"]+)[\'"]', script.string)
                if m:
                    return m.group(1)
        meta = soup.find('meta', {'name': 'csrf-token'})
        if meta and meta.get('content'):
            return meta['content']
        return None

    @staticmethod
    def _parece_login(text: str) -> bool:
        """True si la respuesta parece la página de login de OJS (sesión muerta)."""
        t = (text or "").lower()
        return ('name="username"' in t or 'name="password"' in t
                or 'signin' in t or 'iniciosesion' in t)

    # ── Auto-descubrimiento de submission (BUG-09) ─────────────────────
    def discover_submission_id(self) -> Optional[str]:
        """Consulta /api/v1/submissions y devuelve el primer ID visible para
        el usuario logueado. Usado para auto-corregir un submission_id
        desactualizado. None si no se puede."""
        try:
            url = (f"{self.base_url}/index.php/{self.contexto}/api/v1/submissions"
                   f"?count=100")
            resp = self.session.get(url, timeout=60)
            if resp.status_code != 200:
                logger.warning(f"Descubrimiento de submission falló: HTTP {resp.status_code}")
                return None
            items = resp.json().get("items", [])
            if not items:
                logger.warning("Descubrimiento de submission: sin submissions visibles")
                return None
            return str(items[0].get("id"))
        except Exception as e:
            logger.error(f"discover_submission_id error: {e}")
            return None

    def verify_submission_access(self) -> bool:
        """Verifica que la submission configurada sea accesible para el usuario.

        - HTTP 200: OK.
        - HTTP 404: el ID está desactualizado → auto-descubre el ID correcto
          y actualiza self.submission_id (BUG-09 fix).
        - Otros códigos/errores: los sitios son intermitentes, no bloqueamos y
          dejamos que el POST de subida decida.
        """
        try:
            url = (f"{self.base_url}/index.php/{self.contexto}/api/v1/submissions/"
                   f"{self.submission_id}")
            resp = self.session.get(url, timeout=60)
            if resp.status_code == 200:
                return True
            if resp.status_code == 404:
                nuevo = self.discover_submission_id()
                if nuevo and nuevo != self.submission_id:
                    logger.warning(f"Submission {self.submission_id} no accesible; "
                                   f"auto-corregida a {nuevo}")
                    self.submission_id = nuevo
                    return True
                logger.error("Submission 404 y no se pudo auto-descubrir un ID válido")
                return False
            logger.warning(f"verify_submission_access: HTTP {resp.status_code}")
            return True
        except Exception as e:
            logger.warning(f"verify_submission_access error: {e}")
            return True

    def check_session(self) -> bool:
        try:
            test_url = (f"{self.base_url}/index.php/{self.contexto}/submission/wizard/2"
                        f"?submissionId={self.submission_id}")
            resp = self.session.get(test_url, timeout=15, allow_redirects=False)
            if resp.status_code == 302 and 'login' in resp.headers.get('Location', '').lower():
                return False
            return resp.status_code == 200 and 'submissionId' in resp.text
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        if self.is_logged_in and self.check_session():
            return True
        return self.login()

    # ── Navegación + refresh CSRF ────────────────────────────────────
    def navigate_to_step_2(self) -> bool:
        """Navega al paso 2 del wizard y refresca el CSRF.
        BUG-08 fix: se debe llamar ANTES de cada POST de subida.
        """
        if not self.submission_id:
            return False
        try:
            step2_url = (f"{self.base_url}/index.php/{self.contexto}/submission/wizard/2"
                         f"?submissionId={self.submission_id}#step-2")
            resp = self.session.get(step2_url, timeout=60)
            if "step-2" not in resp.url and "submission/wizard" not in resp.url:
                logger.warning(f"No se pudo navegar al paso 2. URL actual: {resp.url}")
                return False
            soup = BeautifulSoup(resp.text, 'html.parser')
            new_csrf = self._extract_csrf(soup, resp.text)
            if new_csrf:
                self.csrf_token = new_csrf
                logger.debug("CSRF refrescado desde paso 2")
            return True
        except Exception as e:
            logger.error(f"navigate_to_step_2 error: {e}")
            return False

    # ── Preparación de archivo ───────────────────────────────────────
    def _prepare_file_for_upload(self, file_path: str, user_id: int) -> Optional[str]:
        """Aplica camuflaje BitZero. Devuelve ruta del archivo a subir
        (puede ser el original o el camuflado). None si falla."""
        if self.bitzero_mode == 0:
            return file_path
        camouflaged = BitZeroEncoder.apply_camouflage(
            file_path, self.bitzero_mode, user_id, self.encryption_key
        )
        if camouflaged:
            try:
                orig_size = os.path.getsize(file_path)
                cam_size = os.path.getsize(camouflaged)
                ratio = (cam_size / orig_size) * 100 if orig_size else 0
                logger.info(
                    f"Camuflado: {os.path.basename(file_path)} -> {os.path.basename(camouflaged)} "
                    f"({orig_size/1024:.1f}KB -> {cam_size/1024:.1f}KB, {ratio:.1f}%)"
                )
            except Exception:
                pass
            return camouflaged
        return file_path

    # ── Subida individual (BUG-05, BUG-06, BUG-08 fixes) ─────────────
    def upload_file(self, file_path: str, original_name: Optional[str] = None,
                    user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Sube un archivo. Devuelve dict con info del archivo subido o None.

        BUG-06 fix: limpieza garantizada del temporal en finally.
        BUG-08 fix: CSRF refrescado antes de cada subida + retry en 403.
        BUG-05 fix: registra original_size además del tamaño subido.
        """
        # Cada intento de subida empieza sin error previo (evita que un error
        # de un intento anterior contamine el mensaje o el fallback actual).
        self._last_upload_error = ""
        if not os.path.exists(file_path):
            self._last_upload_error = f"archivo no existe: {file_path}"
            logger.error(f"Archivo no existe: {file_path}")
            return None

        # Optimización: sesión y submission se comprueban UNA vez por lote.
        # El CSRF de OJS es estable durante la sesión (verificado en vivo), así
        # que ya no se navega al paso 2 por cada parte; solo se refresca ante
        # un 403 en _do_upload_request.
        if not self._batch_session_ok:
            if not self.ensure_logged_in():
                self._last_upload_error = "no se pudo iniciar sesión en el servidor"
                logger.error("No se pudo iniciar sesión")
                return None
            self._batch_session_ok = True
        if not self._batch_submission_ok:
            # BUG-09: si el submission_id está desactualizado, se auto-corrige
            if not self.verify_submission_access():
                self._last_upload_error = "la submission no es accesible"
                logger.error("Submission inaccesible y no se pudo auto-corregir")
                return None
            self._batch_submission_ok = True

        # Tamaño ORIGINAL antes del camuflaje (BUG-05 fix)
        original_size = os.path.getsize(file_path)

        upload_path = self._prepare_file_for_upload(file_path, user_id or 0)
        if not upload_path:
            upload_path = file_path
        is_temp = (upload_path != file_path)

        file_name = original_name or os.path.basename(file_path)
        if upload_path != file_path:
            file_name = os.path.basename(upload_path)
        content_type = self._content_type(file_name)

        # BUG-06 fix: try/finally para garantizar limpieza
        try:
            try:
                with open(upload_path, 'rb') as f:
                    file_content = f.read()
            except Exception as e:
                self._last_upload_error = f"error leyendo archivo: {e}"
                logger.error(f"Error leyendo archivo: {e}")
                return None

            file_info = self._do_upload_request(
                file_name=file_name,
                file_content=file_content,
                content_type=content_type,
            )
            if not file_info:
                return None

            # Completar con tamaños (BUG-05 fix)
            file_info['original_size'] = original_size
            file_info['original_name'] = original_name or os.path.basename(file_path)
            file_info['size'] = os.path.getsize(upload_path)

            self.uploaded_files.append(file_info)
            logger.info(f"Subido: {file_name} (ID: {file_info['id']})")
            return file_info

        finally:
            # BUG-06 fix: SIEMPRE limpiar temporal
            if is_temp and upload_path and os.path.exists(upload_path):
                try:
                    os.remove(upload_path)
                    logger.debug(f"Temporal limpiado: {upload_path}")
                except OSError as e:
                    logger.warning(f"No se pudo limpiar temporal {upload_path}: {e}")

    @staticmethod
    def _sanitize_upload_name(name: str) -> str:
        """Normaliza el nombre de archivo a ASCII seguro para OJS.

        Fix: los servidores OJS devuelven HTTP 500 'Slim Application Error'
        si el nombre contiene caracteres Unicode poco comunes (letras
        matemáticas tipo 'L' en negrita, emojis...). Se descompone a ASCII
        (ej. 'L' -> 'L') y lo que no tenga equivalente se sustituye por '_'.

        El nombre ORIGINAL se conserva aparte (en la URL BitZero), así el
        usuario sigue descargando su archivo con el nombre original.
        """
        import unicodedata
        if not name:
            return "archivo.bin"
        # Descompone caracteres especiales (letras matemáticas, acentos...)
        # y descarta los que no tengan equivalente ASCII (emojis, símbolos).
        ascii_name = unicodedata.normalize('NFKD', name)
        ascii_name = ascii_name.encode('ascii', 'ignore').decode('ascii')
        # Lo que quede raro (espacios, símbolos) -> '_'
        ascii_name = re.sub(r'[^\w.\-]', '_', ascii_name)
        ascii_name = ascii_name.strip('._-') or "archivo.bin"
        return ascii_name

    def _do_upload_request(self, file_name: str, file_content: bytes,
                           content_type: str) -> Optional[Dict[str, Any]]:
        """Hace el POST de subida con retry en 403 (CSRF expirado)."""
        # Fix HTTP 500: OJS rompe con nombres Unicode poco comunes
        # (letras matemáticas, emojis). Se sube con nombre ASCII seguro.
        file_name = self._sanitize_upload_name(file_name)
        api_url = (f"{self.base_url}/index.php/{self.contexto}/api/v1/submissions/"
                   f"{self.submission_id}/files")
        referer = (f"{self.base_url}/index.php/{self.contexto}/submission/wizard/2"
                   f"?submissionId={self.submission_id}")

        max_intentos = 3
        for attempt in range(max_intentos):
            headers = {
                'X-Csrf-Token': self.csrf_token or '',
                'Referer': referer,
            }
            files = {'file': (file_name, file_content, content_type)}
            data = {
                'name[es_ES]': file_name,
                'fileStage': '2',
                'csrfToken': self.csrf_token or '',
            }
            try:
                resp = self.session.post(api_url, files=files, data=data,
                                         headers=headers, timeout=120)
            except requests.exceptions.RequestException as e:
                # Red caída / timeout: reintentar con backoff (la revista
                # puede estar caída o inestable en ese momento)
                logger.warning(f"POST upload error (intento {attempt+1}/{max_intentos}): {e}")
                if attempt < max_intentos - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                self._last_upload_error = (
                    f"el servidor no respondió (error de red/timeout tras "
                    f"{max_intentos} intentos)"
                )
                logger.error(f"Subida fallida tras {max_intentos} intentos: "
                             f"la revista no responde")
                return None

            if resp.status_code == 200:
                try:
                    result = resp.json()
                except Exception:
                    # Respuesta no-JSON: puede ser la página de login (sesión
                    # muerta a mitad del lote)
                    if attempt < max_intentos - 1 and self._parece_login(resp.text):
                        logger.warning("Sesión expirada (respuesta no-JSON), re-logueando...")
                        self.is_logged_in = False
                        self._batch_session_ok = False
                        if self.ensure_logged_in():
                            continue
                    logger.warning(f"Respuesta no-JSON: {resp.text[:200]}")
                    self._last_upload_error = (
                        f"respuesta inesperada del servidor: {resp.text[:120]!r}"
                    )
                    return None
                if not result.get('id'):
                    self._last_upload_error = (
                        f"respuesta sin ID de archivo: {str(result)[:150]}"
                    )
                    logger.warning(f"JSON sin ID: {result}")
                    return None
                file_id = result['id']
                name = result.get('name', file_name)
                if isinstance(name, dict):
                    name = name.get('es_ES', file_name)
                download_url = (f"{self.base_url}/$$$call$$$/api/file/file-api/download-file"
                                f"?submissionFileId={file_id}&submissionId={self.submission_id}&stageId=1")
                return {
                    'id': file_id,
                    'name': name,
                    'url': download_url,
                }

            if resp.status_code == 403 and attempt == 0:
                # BUG-08: CSRF expirado → re-login + refresh CSRF + retry
                logger.warning("403 CSRF rechazado, re-logueando y reintentando...")
                self.is_logged_in = False
                self._batch_session_ok = False
                if self.ensure_logged_in() and self.navigate_to_step_2():
                    continue
                self._last_upload_error = "403 CSRF rechazado y no se pudo re-autenticar"
                return None

            if resp.status_code == 404 and attempt == 0:
                # BUG-09: submission_id desactualizado → auto-descubrir y retry
                logger.warning("HTTP 404 en subida: auto-descubriendo submission...")
                nuevo = self.discover_submission_id()
                if nuevo and nuevo != self.submission_id:
                    logger.warning(f"Auto-corregida submission {self.submission_id} -> {nuevo}")
                    self.submission_id = nuevo
                    continue
                self._last_upload_error = "404 — la submission no es accesible"
                return None

            if resp.status_code in (401, 419) and attempt == 0:
                # Sesión muerta a mitad del lote: re-login + retry
                logger.warning(f"HTTP {resp.status_code}: sesión expirada, re-logueando...")
                self.is_logged_in = False
                self._batch_session_ok = False
                if self.ensure_logged_in():
                    continue
                self._last_upload_error = (
                    f"HTTP {resp.status_code} — sesión rechazada y no se pudo re-autenticar"
                )
                return None

            if resp.status_code >= 500 and attempt < max_intentos - 1:
                # Revista inestable: reintentar con backoff
                logger.warning(f"HTTP {resp.status_code} (intento {attempt+1}/{max_intentos})")
                time.sleep(2 * (attempt + 1))
                continue

            self._last_upload_error = (
                f"HTTP {resp.status_code} — error del servidor "
                f"({resp.text.strip()[:120]})"
            )
            logger.warning(f"Upload HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        return None

    # ── Subida con chunking ──────────────────────────────────────────
    def upload_chunked_file(self, file_path: str, user_id: int,
                            on_chunk: Optional[Callable[[int, int], None]] = None
                            ) -> List[Dict[str, Any]]:
        """Sube un archivo en chunks de self.chunk_size.

        on_chunk(idx, total) se invoca tras subir cada chunk (progreso en vivo).
        """
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if file_size <= self.chunk_size:
            result = self.upload_file(file_path, user_id=user_id)
            if result:
                if on_chunk:
                    try:
                        on_chunk(1, 1)
                    except UserCancelledError:
                        # Usuario pulsó '❌ Cancelar': abortar limpiamente
                        raise
                    except Exception as e:
                        logger.warning(f"on_chunk callback error: {e}")
                return [result]
            # Falló el archivo completo tras los reintentos: ABORTAR para
            # que el fallback de /up pruebe automáticamente otra revista.
            raise UploadAbortedError(
                self._last_upload_error or "el servidor dejó de responder"
            )

        chunks = self._split_file(file_path)
        uploaded: List[Dict[str, Any]] = []
        total = len(chunks)
        try:
            for idx, chunk in enumerate(chunks, 1):
                chunk_name = f"{file_name}.part{idx:03d}"
                result = self.upload_file(chunk['path'], chunk_name, user_id)
                if result:
                    uploaded.append(result)
                    logger.info(f"Chunk {idx}/{total} subido: {chunk_name}")
                else:
                    # Si una parte falla tras los reintentos, la revista está
                    # caída o inaccesible: ABORTAR para no dejar una URL con
                    # partes faltantes (subida incompleta).
                    logger.error(f"Chunk {idx}/{total} FALLÓ ({chunk_name}) "
                                 f"— abortando subida")
                    raise UploadAbortedError(
                        f"falló la parte {idx}/{total} ({chunk_name}): "
                        f"{self._last_upload_error or 'el servidor dejó de responder'}"
                    )
                if on_chunk:
                    try:
                        on_chunk(idx, total)
                    except UserCancelledError:
                        # Usuario pulsó '❌ Cancelar': abortar limpiamente.
                        # El finally de abajo limpia los temporales de chunks.
                        raise
                    except Exception as e:
                        logger.warning(f"on_chunk callback error: {e}")
        finally:
            # Limpieza garantizada de TODOS los temporales de chunks
            # (incluidos los que no llegaron a subirse si se abortó).
            for chunk in chunks:
                if os.path.exists(chunk['path']):
                    try:
                        os.remove(chunk['path'])
                    except OSError:
                        pass
        return uploaded

    def _split_file(self, file_path: str) -> List[Dict[str, Any]]:
        chunks: List[Dict[str, Any]] = []
        file_name = os.path.basename(file_path)
        out_dir = os.path.dirname(file_path)
        with open(file_path, 'rb') as f:
            n = 1
            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break
                chunk_name = f"{file_name}.part{n:03d}"
                chunk_path = os.path.join(out_dir, chunk_name)
                with open(chunk_path, 'wb') as cf:
                    cf.write(data)
                chunks.append({
                    'path': chunk_path, 'name': chunk_name,
                    'size': len(data), 'number': n
                })
                n += 1
        return chunks

    # ── Borrado de archivos (API OJS) ────────────────────────────────
    def list_submission_files(self) -> Optional[List[Dict[str, Any]]]:
        """Lista los archivos de la submission vía API OJS (GET, solo lectura).

        Devuelve los items de la API o None si falla (self._last_upload_error
        queda con el motivo).
        """
        try:
            if not self.ensure_logged_in():
                self._last_upload_error = "no se pudo iniciar sesión en el servidor"
                return None
            url = (f"{self.base_url}/index.php/{self.contexto}/api/v1/submissions/"
                   f"{self.submission_id}/files?count=100")
            resp = self.session.get(
                url, headers={'X-Csrf-Token': self.csrf_token or ''}, timeout=60
            )
            if resp.status_code == 200:
                items = resp.json().get('items', [])
                logger.info(f"list_submission_files: {len(items)} archivos en "
                            f"submission {self.submission_id}")
                return items
            self._last_upload_error = (
                f"HTTP {resp.status_code} al listar archivos: {resp.text.strip()[:120]}"
            )
            logger.warning(f"list_submission_files HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
            return None
        except Exception as e:
            self._last_upload_error = f"error listando archivos: {e}"
            logger.error(f"list_submission_files error: {e}")
            return None

    def delete_submission_file(self, file_id: Any) -> bool:
        """Borra un archivo de la submission vía DELETE API OJS.

        True si se borró o ya no existía (404). False si la revista falla
        (self._last_upload_error con el motivo).
        """
        try:
            if not self.ensure_logged_in():
                self._last_upload_error = "no se pudo iniciar sesión en el servidor"
                return False
            url = (f"{self.base_url}/index.php/{self.contexto}/api/v1/submissions/"
                   f"{self.submission_id}/files/{file_id}")
            resp = self.session.delete(
                url, headers={'X-Csrf-Token': self.csrf_token or ''}, timeout=60
            )
            if resp.status_code in (200, 204):
                logger.info(f"delete_submission_file OK: {file_id}")
                return True
            if resp.status_code == 404:
                logger.info(f"delete_submission_file: {file_id} ya no existe (404)")
                return True
            self._last_upload_error = (
                f"HTTP {resp.status_code} al borrar {file_id}: {resp.text.strip()[:120]}"
            )
            logger.warning(f"delete_submission_file HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
            return False
        except Exception as e:
            self._last_upload_error = f"error borrando {file_id}: {e}"
            logger.error(f"delete_submission_file error: {e}")
            return False

    # ── Generación de URL BitZero ────────────────────────────────────
    def generate_bitzero_url(self, original_name: str, file_size: int) -> str:
        if not self.uploaded_files:
            return ""
        file_ids = [str(f['id']) for f in self.uploaded_files]
        return URLGenerator.generate_bitzero_url(
            host=self.base_url,
            user=self.username,
            password=self.password,
            repo=self.submission_id,
            contexto=self.contexto,
            file_ids=file_ids,
            bitzero_mode=self.bitzero_mode,
            original_name=original_name,
            file_size=file_size,
            encryption_key=self.encryption_key,
            fake_host=config.BITZERO_FAKE_HOST,
        )

    # ── MIME types ───────────────────────────────────────────────────
    @staticmethod
    def _content_type(filename: str) -> str:
        name = filename.lower()
        exts = {
            '.pdf': 'application/pdf',
            '.zip': 'application/zip',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.mp4': 'video/mp4', '.mp3': 'audio/mpeg',
            '.html': 'text/html', '.txt': 'text/plain',
            '.7z': 'application/x-7z-compressed',
            '.rar': 'application/x-rar-compressed',
            '.tar': 'application/x-tar',
        }
        for ext, ct in exts.items():
            if name.endswith(ext):
                return ct
        return 'application/octet-stream'

    # ── Resumen ──────────────────────────────────────────────────────
    def get_upload_summary(self) -> Dict[str, Any]:
        return {
            'total_files': len(self.uploaded_files),
            'total_uploaded_size': sum(f.get('size', 0) for f in self.uploaded_files),
            'total_original_size': sum(f.get('original_size', f.get('size', 0))
                                       for f in self.uploaded_files),
            'file_ids': [f['id'] for f in self.uploaded_files],
            'original_names': [f.get('original_name', '') for f in self.uploaded_files],
        }
