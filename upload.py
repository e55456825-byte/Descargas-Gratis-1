"""
handlers/upload.py — Orquestador de subidas BitZero.

Mejoras:
  - Si LOG_CHANNEL_ID está configurado, envía un mensaje de log al canal
    por cada subida completada (con URL BitZero, usuario, revista, tamaño).
  - Cache de uploaders por revista con invalidación.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from pyrogram.errors import RPCError

from cancellation import (UserCancelledError, cancel_keyboard, check_cancel,
                          clear_cancel, is_cancelled)
from config import config
from database import db
from storage import storage
from uploader import RevistaUploader, UploadAbortedError
from url_generator import URLGenerator
from utils import format_size

logger = logging.getLogger(__name__)


async def _safe_edit(message, text: str, **kwargs):
    """Edita un mensaje ignorando fallos no críticos de Telegram.

    Un 'FloodWait' (rate-limit de ediciones) o 'MessageNotModified' al
    actualizar el mensaje de progreso NO debe abortar la subida en curso:
    el texto es solo informativo y la subida real (upload_chunked_file)
    puede seguir. Antes, ese FloodWait se propagaba y el job entero
    terminaba en 'failed'. Devuelve el mensaje editado o None.
    """
    try:
        return await message.edit_text(text, **kwargs)
    except RPCError as e:
        # Esperado y transitorio: FloodWait (rate-limit) o MessageNotModified
        # (mismo texto). No debe abortar la subida.
        logger.debug(f"edit del mensaje ignorado (Telegram) "
                     f"[{type(e).__name__}]: {e}")
        return None
    except Exception as e:
        # Inesperado: registrarlo visible para poder diagnosticarlo.
        logger.warning(f"edit del mensaje ignorado "
                       f"[{type(e).__name__}]: {e}")
        return None


# Cache de uploaders por revista
_journal_uploaders: Dict[str, RevistaUploader] = {}

# Cooldown de revistas con fallo de subida: rev_id -> timestamp UNIX hasta el
# que queda excluida de la selección automática (/up). Evita re-elegir una y
# otra vez la misma revista cuya API de subida está rota (ej. HTTP 500).
_upload_failure_until: Dict[str, float] = {}


def mark_upload_failure(revista_id: str) -> None:
    """Excluye temporalmente una revista de /up tras un fallo de subida.

    Durante UPLOAD_FAIL_COOLDOWN_MIN minutos (config, por defecto 15)
    pick_working_revista la saltará, de modo que el fallback pruebe otras.
    """
    cooldown_sec = max(1, int(getattr(config, 'UPLOAD_FAIL_COOLDOWN_MIN', 15))) * 60
    _upload_failure_until[revista_id] = time.time() + cooldown_sec
    logger.warning(
        f"mark_upload_failure: {revista_id} excluida de /up hasta las "
        f"{time.strftime('%H:%M:%S', time.localtime(_upload_failure_until[revista_id]))}"
    )


def _in_upload_cooldown(revista_id: str) -> bool:
    """True si la revista está en cooldown por fallos recientes de subida."""
    until = _upload_failure_until.get(revista_id, 0.0)
    return time.time() < until


# ── Control de carga de logins hacia las revistas (anti-scanner) ────
# El bot sondea login en cada /up, en cada ciclo del monitor y en
# /rev_status. Sin control, eso son 20-40 peticiones de login/hora por
# revista, lo que puede saturar los servidores OJS o hacer que sus
# firewalls bloqueen la IP del bot. Por eso se cachea el resultado unos
# minutos y se respeta un intervalo mínimo entre logins reales por revista.
_login_probe_cache: Dict[str, Tuple[bool, float]] = {}  # rev_id -> (ok, ts)


def _login_cache_ttl() -> int:
    return max(10, int(getattr(config, 'LOGIN_PROBE_CACHE_TTL_SEC', 300)))


def _login_min_interval() -> int:
    return max(5, int(getattr(config, 'LOGIN_PROBE_MIN_INTERVAL_SEC', 60)))


def probe_login_cached(revista: Dict) -> Tuple[bool, bool]:
    """Prueba el login de una revista sin bombardearla.

    - Resultado en caché reciente (< TTL) → se devuelve sin tocar la red.
    - Si no, hace login REAL solo si pasó el intervalo mínimo desde el
      último intento a ESA revista.

    Devuelve (ok, probado_ahora): probado_ahora indica si se hizo login
    real en esta llamada (para refrescar la DB solo cuando corresponde).
    """
    rev_id = revista['rev_id']
    now = time.time()
    cached = _login_probe_cache.get(rev_id)
    if cached:
        ok, ts = cached
        if now - ts < _login_cache_ttl():
            logger.debug(f"probe_login_cached: {rev_id} desde caché ({int(now - ts)}s)")
            return ok, False
        if now - ts < _login_min_interval():
            logger.debug(f"probe_login_cached: {rev_id} en intervalo mínimo, "
                         f"usando caché ({int(now - ts)}s)")
            return ok, False

    uploader = RevistaUploader(
        username=revista['username'], password=revista['password'],
        submission_id=revista['submission_id'], base_url=revista['base_url'],
        contexto=revista['contexto'], bitzero_mode=revista.get('bitzero_mode', 0),
        encryption_key=revista.get('encryption_key'),
    )
    try:
        ok = uploader.login()
    except Exception as e:
        logger.warning(f"probe_login_cached: error en login de {rev_id}: {e}")
        ok = False
    _login_probe_cache[rev_id] = (ok, now)
    return ok, True


def invalidate_uploader(revista_id: str) -> None:
    """Invalida el uploader cacheado para que se recree con la nueva config."""
    _journal_uploaders.pop(revista_id, None)


async def get_or_create_uploader(revista: Dict) -> RevistaUploader:
    """Devuelve uploader cacheado o crea uno nuevo con la config actual."""
    rev_id = revista['rev_id']
    if rev_id in _journal_uploaders:
        return _journal_uploaders[rev_id]
    uploader = RevistaUploader(
        username=revista['username'],
        password=revista['password'],
        submission_id=revista['submission_id'],
        base_url=revista['base_url'],
        contexto=revista['contexto'],
        bitzero_mode=revista.get('bitzero_mode', 0),
        encryption_key=revista.get('encryption_key'),
    )
    _journal_uploaders[rev_id] = uploader
    return uploader


async def pick_working_revista() -> Optional[Tuple[Dict[str, Any], RevistaUploader]]:
    """Selecciona automáticamente la primera revista activa que responde al login.

    Orden de preferencia: revistas con último login exitoso primero
    (y entre ellas, la más reciente). Devuelve (revista, uploader_logueado)
    o None si ninguna está operativa en ese momento.

    Las revistas en cooldown por fallos recientes de subida se saltan.
    """
    revistas = await db.list_revistas(only_active=True)
    if not revistas:
        return None

    # Saltar revistas en cooldown por fallos recientes de subida
    revistas = [r for r in revistas if not _in_upload_cooldown(r['rev_id'])]
    if not revistas:
        logger.warning("pick_working_revista: todas las revistas operativas "
                       "están en cooldown por fallos de subida")
        return None

    revistas.sort(
        key=lambda r: (1 if r.get('last_login_ok') else 0, r.get('last_login_at') or ''),
        reverse=True,
    )

    loop = asyncio.get_running_loop()
    # Lanzar las pruebas en paralelo. probe_login_cached evita hacer login
    # real si hay un resultado fresco en caché o si no ha pasado el
    # intervalo mínimo (anti-scanner: no saturar las revistas con logins).
    probes: List[Tuple[Dict[str, Any], RevistaUploader, Any]] = []
    for r in revistas:
        uploader = RevistaUploader(
            username=r['username'],
            password=r['password'],
            submission_id=r['submission_id'],
            base_url=r['base_url'],
            contexto=r['contexto'],
            bitzero_mode=r.get('bitzero_mode', 0),
            encryption_key=r.get('encryption_key'),
        )
        probes.append((r, uploader, loop.run_in_executor(None, probe_login_cached, r)))

    # Esperar resultados conforme llegan, respetando el orden de preferencia
    while probes:
        done, _ = await asyncio.wait(
            [f for _, _, f in probes], return_when=asyncio.FIRST_COMPLETED
        )
        for r, uploader, fut in probes:  # orden de preferencia
            if fut not in done:
                continue
            try:
                ok, probado = fut.result()
            except Exception as e:
                logger.warning(f"pick_working_revista: error en login de {r['rev_id']}: {e}")
                ok, probado = False, True
            # Refrescar la DB solo si hubo login real en esta llamada
            if probado:
                try:
                    await db.update_revista_login_status(r['rev_id'], ok)
                except Exception:
                    pass
            if ok:
                for _, _, f2 in probes:
                    if not f2.done():
                        f2.cancel()
                logger.info(f"pick_working_revista: {r['rev_id']} operativa → elegida")
                return r, uploader
        # Quitar las ya terminadas y seguir esperando las restantes
        probes = [(r, up, f) for r, up, f in probes if f not in done]

    logger.warning("pick_working_revista: ninguna revista operativa en este momento")
    return None


async def _log_upload_to_channel(client, user_id: int, revista: Dict,
                                  uploader: RevistaUploader,
                                  uploaded_count: int, total_files: int,
                                  all_uploaded: list, bitzero_url: str,
                                  is_multi: bool, original_names_list: list,
                                  status: str) -> None:
    """Envía un mensaje de log al canal de log con el resumen de la subida."""
    if not config.LOG_CHANNEL_ID:
        return
    try:
        user_display = original_names_list[0] if original_names_list else "N/A"
        text = (
            f"📤 **Subida BitZero — {status.upper()}**\n\n"
            f"👤 **Usuario ID:** `{user_id}`\n"
            f"📚 **Revista:** {revista['nombre']} (`{revista['rev_id']}`)\n"
            f"🆔 **Submission:** `{revista['submission_id']}`\n"
            f"📦 **Archivos:** {uploaded_count}/{total_files}"
            + (f" (empaquetado .tar de {len(original_names_list)})" if is_multi else "")
            + f"\n🔗 **Partes:** {len(all_uploaded)}\n"
            f"🔢 **Modo BitZero:** {uploader.bitzero_mode}\n"
            f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        total_orig = sum(f.get('original_size', f.get('size', 0)) for f in all_uploaded)
        total_up = sum(f.get('size', 0) for f in all_uploaded)
        text += f"💾 **Tamaño original:** {format_size(total_orig)}\n"
        text += f"📤 **Tamaño subido:** {format_size(total_up)}\n"
        if bitzero_url:
            text += f"\n🔗 **Código de descarga:**\n`{bitzero_url}`\n"
        if uploader.encryption_key:
            text += f"\n🔑 **Clave:** `{uploader.encryption_key}`"
        await client.send_message(
            chat_id=config.LOG_CHANNEL_ID,
            text=text,
            disable_web_page_preview=True
        )
    except RPCError as e:
        logger.warning(f"No se pudo enviar log de subida al canal: {e}")
    except Exception as e:
        logger.warning(f"Error inesperado en log de subida: {e}")


# ── Subidas pendientes de elegir el modo (original vs comprimido) ──
# Se registra cuando el usuario selecciona archivos (/up) o envía un vídeo
# (auto-subida): el bot muestra "¿Subir original o comprimir?" y el callback
# correspondiente consume la lista. Evita que los índices se desactualicen
# si llega otro archivo mientras tanto.
_pending_mode_uploads: Dict[int, Dict[str, Any]] = {}


def _pending_ttl_sec() -> int:
    """TTL del estado en memoria (configurable, por defecto 24 h).

    La fuente de verdad es la BD (`pending_decisions`), que sobrevive a los
    reinicios; este TTL solo acota cuánto vale el diccionario en RAM.
    """
    try:
        return max(60, int(getattr(config, 'PENDING_MODE_TTL_SEC', 86400)))
    except (TypeError, ValueError):
        return 86400


def register_pending_upload(user_id: int, paths: List[str]) -> None:
    """Guarda los archivos seleccionados hasta que el usuario elija el modo.

    Si el usuario envía varios vídeos seguidos sin pulsar ningún botón, se van
    ACUMULANDO: al pulsar '📤 Subir original' o '🗜️ Comprimir y subir' se
    procesan TODOS los que quedaron pendientes (no solo el último). Se evitan
    duplicados por ruta.
    """
    prev = _pending_mode_uploads.get(user_id)
    if prev:
        combinados = list(prev["paths"])
        for p in paths:
            if p not in combinados:
                combinados.append(p)
        _pending_mode_uploads[user_id] = {"paths": combinados, "ts": time.time()}
    else:
        _pending_mode_uploads[user_id] = {"paths": list(paths), "ts": time.time()}


def pending_upload_count(user_id: int) -> int:
    """Cuántos archivos tiene el usuario pendientes de elegir modo."""
    pend = _pending_mode_uploads.get(user_id)
    return len(pend["paths"]) if pend else 0


def consume_pending_upload(user_id: int) -> Optional[List[str]]:
    """Recupera (y elimina) la selección pendiente EN MEMORIA del usuario.
    None si no hay selección o si superó el TTL.

    Ojo: NO consulta la BD. Para el flujo del botón usa
    `resolve_pending_upload`, que además la recupera de `pending_decisions`.
    """
    pend = _pending_mode_uploads.pop(user_id, None)
    if not pend:
        return None
    if time.time() - pend["ts"] > _pending_ttl_sec():
        logger.info(f"Selección pendiente de {user_id} expirada en memoria")
        return None
    return pend["paths"]


async def resolve_pending_upload(user_id: int) -> Optional[List[str]]:
    """Devuelve los archivos de la decisión pendiente y la consume.

    Se usa al pulsar "📤 Subir original" / "🗜️ Comprimir y subir". Va en dos
    pasos para que el botón funcione siempre que el archivo exista:

      1. Estado en memoria (lo normal: el usuario pulsa enseguida).
      2. Respaldo en BD (`pending_decisions`): la decisión se persiste al
         recibir el vídeo, así que sobrevive a reinicios y a pulsaciones
         tardías. Antes solo se miraba el paso 1 con un TTL de 10 min fijos:
         pasado ese tiempo (o tras un reinicio sin re-anuncio), el botón
         decía "la selección expiró" y obligaba a rehacer /up aunque el
         vídeo siguiera en disco.

    Solo se devuelven rutas que AÚN EXISTEN en disco: si el archivo ya se
    subió y se borró (DELETE_AFTER_UPLOAD), no hay nada que reintentar.
    Devuelve None si no queda nada recuperable.
    """
    paths = consume_pending_upload(user_id)
    if paths:
        # Decisión respondida: limpiar la copia persistida.
        try:
            await db.clear_pending_decision(user_id)
        except Exception as e:
            logger.debug(f"resolve_pending: no se pudo limpiar la BD: {e}")
        return paths

    # Paso 2: respaldo persistido (pulsación tardía o bot reiniciado).
    try:
        row = await db.get_pending_decision(user_id)
    except Exception as e:
        logger.warning(f"resolve_pending: no se pudo leer la decisión de {user_id}: {e}")
        row = None
    candidatos = [p for p in list((row or {}).get("file_paths") or []) if p]
    existentes = [p for p in candidatos if os.path.exists(p)]
    try:
        await db.clear_pending_decision(user_id)
    except Exception as e:
        logger.debug(f"resolve_pending: no se pudo limpiar la BD: {e}")
    if existentes:
        logger.info(
            f"resolve_pending: decisión de {user_id} recuperada de la BD "
            f"({len(existentes)} archivo(s)); el estado en memoria estaba vacío o expirado"
        )
        return existentes
    if candidatos:
        logger.info(
            f"resolve_pending: decisión de {user_id} descartada; sus archivos "
            f"ya no están en disco"
        )
    return None


# ── Compresión de vídeos antes de subir ────────────────────────────
def _cleanup_compressed_temps(temps: list) -> None:
    """Borra los temporales de vídeo comprimido (siempre, incluso al abortar)."""
    for t in temps:
        try:
            if os.path.exists(t):
                os.remove(t)
                logger.debug(f"Temporal comprimido limpiado: {t}")
        except OSError as e:
            logger.warning(f"No se pudo limpiar temporal comprimido {t}: {e}")


async def _compress_videos_before_upload(client, message, user_id: int,
                                          file_paths: list) -> dict:
    """Comprime los VÍDEOS de file_paths con FFmpeg (en _temp del usuario).

    Devuelve dict con:
      - paths:    rutas a subir (comprimidas si el vídeo se comprimió y
                  ahorró lo suficiente; original en caso contrario)
      - display:  {ruta_final: nombre_original_visible}
      - stats:    {ruta_original: stats de compresión}
      - temps:    temporales comprimidos a limpiar al terminar

    Lanza UserCancelledError si el usuario pulsa '❌ Cancelar' a mitad.
    """
    from video_compressor import (is_video_file, ffmpeg_available,
                                  compress_video)

    if not ffmpeg_available():
        logger.warning("ffmpeg no está disponible: subiendo originales")
        try:
            await _safe_edit(message, 
                "⚠️ **FFmpeg no está instalado en el servidor.**\n\n"
                "No se puede comprimir, así que se subirán los archivos "
                "**originales**.\n\n"
                "💡 Instala ffmpeg (`apt install ffmpeg`) para usar la "
                "compresión.",
                reply_markup=cancel_keyboard(user_id)
            )
        except Exception:
            pass
        return {"paths": file_paths, "display": {}, "stats": {},
                "temps": []}

    videos = [p for p in file_paths if is_video_file(p)]
    no_videos = [p for p in file_paths if not is_video_file(p)]
    if not videos:
        try:
            await _safe_edit(message, 
                "ℹ️ **Ninguno de los archivos seleccionados es un vídeo.**\n\n"
                "Se subirán los archivos **tal cual** (la compresión solo "
                "aplica a vídeos).",
                reply_markup=cancel_keyboard(user_id)
            )
        except Exception:
            pass
        return {"paths": file_paths, "display": {}, "stats": {},
                "temps": []}

    temp_dir = storage.get_user_dir(user_id, 'temp')
    os.makedirs(temp_dir, exist_ok=True)

    loop = asyncio.get_running_loop()
    nuevas_rutas: list = list(no_videos)
    display: dict = {p: os.path.basename(p) for p in no_videos}
    stats_por_original: dict = {}
    temporales: list = []
    min_size = int(getattr(config, 'COMPRESS_MIN_SIZE_MB', 10)) * 1024 * 1024
    min_saving = max(0, int(getattr(config, 'COMPRESS_MIN_SAVING_PCT', 10)))

    for i, video in enumerate(videos, 1):
        check_cancel(user_id)
        orig_name = os.path.basename(video)

        # Vídeos pequeños: comprimir casi no ahorra y tarda más
        if os.path.getsize(video) < min_size:
            logger.info(f"{orig_name} < {min_size//(1024*1024)}MB: subiendo original")
            nuevas_rutas.append(video)
            display[video] = orig_name
            continue

        base = os.path.splitext(orig_name)[0]
        output = os.path.join(temp_dir, f"{base}_comprimido.mp4")

        estado = {"ultima": 0.0}

        def _on_progress(pct: float, _i=i, _orig=orig_name, _video=video,
                         _estado=estado) -> None:
            """Actualiza el mensaje con la barra de progreso de la compresión.
            Se llama desde el hilo del worker → se agenda la edición en el
            event loop (mismo patrón que el progreso de chunks)."""
            if is_cancelled(user_id):
                raise UserCancelledError()
            ahora = time.time()
            if ahora - _estado["ultima"] < 1.0 and pct < 100:
                return
            _estado["ultima"] = ahora
            try:
                fill = "█" * int(pct // 5)
                empty = "░" * (20 - int(pct // 5))
                texto = (
                    f"🗜️ **Comprimiendo vídeo** ({_i}/{len(videos)})\n\n"
                    f"📄 {_orig}\n"
                    f"💾 {format_size(os.path.getsize(_video))}\n\n"
                    f"{fill}{empty} {pct:.0f}%\n"
                    f"⏳ Optimizando para que suba más rápido..."
                )
            except Exception:
                return

            async def _editar() -> None:
                try:
                    await _safe_edit(message, 
                        texto, reply_markup=cancel_keyboard(user_id)
                    )
                except Exception as e:
                    logger.debug(f"progreso compresión (edit): {e}")

            def _agendar() -> None:
                try:
                    asyncio.create_task(_editar())
                except Exception:
                    pass

            try:
                loop.call_soon_threadsafe(_agendar)
            except Exception as e:
                logger.debug(f"progreso compresión (agendar): {e}")

        try:
            await _safe_edit(message, 
                f"🗜️ **Comprimiendo vídeo** ({i}/{len(videos)})\n\n"
                f"📄 {orig_name}\n"
                f"💾 {format_size(os.path.getsize(video))}\n\n"
                f"⏳ Analizando...",
                reply_markup=cancel_keyboard(user_id)
            )
            stats = await asyncio.to_thread(
                compress_video, video, output,
                on_progress=_on_progress,
                cancel_check=lambda: is_cancelled(user_id),
            )
        except UserCancelledError:
            raise
        except Exception as e:
            logger.warning(f"Compresión de {orig_name} error inesperado: {e}")
            stats = None

        if not stats or not os.path.exists(output):
            logger.warning(f"Compresión falló para {orig_name}: subiendo original")
            nuevas_rutas.append(video)
            display[video] = orig_name
            if os.path.exists(output):
                try:
                    os.remove(output)
                except OSError:
                    pass
            continue

        comp_size = stats.get("comp_size", 0)
        orig_size = stats.get("orig_size", 0)
        # Si no ahorra al menos el % mínimo → subir el original (un MP4
        # re-codificado puede salir MÁS grande que el original).
        if orig_size and comp_size >= orig_size * (1 - min_saving / 100.0):
            logger.info(f"{orig_name}: compresión no ahorra ({comp_size} >= "
                        f"{orig_size}); subiendo original")
            nuevas_rutas.append(video)
            display[video] = orig_name
            try:
                os.remove(output)
            except OSError:
                pass
            continue

        nuevas_rutas.append(output)
        display[output] = orig_name
        stats_por_original[video] = stats
        temporales.append(output)

    return {
        "paths": nuevas_rutas,
        "display": display,
        "stats": stats_por_original,
        "temps": temporales,
    }


async def perform_upload_with_bitzero(client, message, uploader: RevistaUploader,
                                       revista: Dict, user_id: int,
                                       file_paths: list[str] | None = None,
                                       display_names: dict | None = None,
                                       compression_stats: dict | None = None) -> str:
    """Ejecuta la subida completa de los archivos del usuario a la revista.
    Genera URL BitZero al final y actualiza el historial en DB.

    Los archivos ya vienen PREPARADOS por el llamador
    (upload_files_with_fallback): la compresión de vídeos se hace UNA sola
    vez allí y aquí solo se sube el resultado. `display_names` mapea ruta
    final → nombre ORIGINAL visible (la URL de descarga conserva el nombre
    del archivo, no el del temporal comprimido) y `compression_stats` se
    usa solo para el resumen final.

    Devuelve el estado del intento para que /up pueda hacer fallback:
      - 'success' / 'partial' → subida completada (o con partes subidas)
      - 'aborted'  → la revista falló a mitad de la subida (UploadAbortedError)
      - 'cancelled' → el usuario pulsó '❌ Cancelar' (no es un fallo de la
        revista: NO se marca cooldown y el fallback no prueba otras revistas)
      - 'failed'   → no se subió nada (auth, sin archivos, etc.)
    """
    # Proceso nuevo = bandera de cancelación limpia (una cancelación de un
    # proceso anterior no debe afectar a este).
    clear_cancel(user_id)
    if not uploader.ensure_logged_in():
        uploader._last_upload_error = "no se pudo iniciar sesión en el servidor"
        await _safe_edit(message, "❌ **Error de autenticación.** No se pudo iniciar sesión.")
        await db.update_revista_login_status(revista['rev_id'], False)
        mark_upload_failure(revista['rev_id'])
        return "failed"
    await db.update_revista_login_status(revista['rev_id'], True)

    # BUG-09 fix: si el submission_id configurado está desactualizado (404),
    # el uploader lo auto-descubre; persistimos la corrección en la BD para
    # que sobreviva a reinicios del bot.
    uploader.verify_submission_access()
    if uploader.submission_id != revista['submission_id']:
        await db.update_revista_field(
            revista['rev_id'], 'submission_id', uploader.submission_id
        )
        revista['submission_id'] = uploader.submission_id
        logger.info(f"Submission de {revista['rev_id']} auto-corregida a "
                    f"{uploader.submission_id} en BD")

    uploader.uploaded_files = []
    # Nueva subida = nuevo lote: re-verificar sesión/submission una vez
    # (el uploader se cachea entre subidas, y la revista puede haberse caído
    # o reiniciado en el medio).
    uploader.reset_batch_state()

    # Listar archivos del usuario (aislamiento)
    if file_paths is not None:
        files_to_upload = list(file_paths)
    else:
        files_to_upload = [f['path'] for f in storage.list_user_files(user_id, sort_by='modified_desc')]

    if not files_to_upload:
        await _safe_edit(message, "📭 **No tienes archivos para subir.**")
        return "failed"

    # Rutas a subir (ya preparadas por el llamador: comprimidas si aplica).
    # Son las que se borran del VPS al terminar con DELETE_AFTER_UPLOAD. La
    # compresión y su limpieza se gestionan en upload_files_with_fallback.
    original_user_paths = list(files_to_upload)
    total_files = len(files_to_upload)
    uploaded_count = 0
    all_uploaded: list = []
    # Rutas subidas con éxito: se borrarán del VPS al terminar (DELETE_AFTER_UPLOAD)
    # para ahorrar espacio en disco.
    uploaded_original_paths: list = []

    # display_name_by_path: ruta final → nombre ORIGINAL visible (la URL de
    # descarga debe conservar el nombre del archivo, no el del temporal
    # comprimido) y stats de compresión para el resumen final.
    display_name_by_path: dict = dict(display_names or {})
    compression_stats: dict = dict(compression_stats or {})

    # ── Múltiples archivos → empaquetar en .tar ─────────────────────
    if total_files > 1:
        await _safe_edit(message, 
            f"📦 **Empaquetando {total_files} archivos**\n\n"
            f"⏳ Generando .tar...",
            reply_markup=cancel_keyboard(user_id)
        )
        tar_path = storage.package_multiple_files(files_to_upload, user_id)
        files_to_upload_processed = [tar_path]
        # Nombre visible = nombre ORIGINAL (no el del temporal comprimido)
        original_names_list = [
            display_name_by_path.get(f, os.path.basename(f))
            for f in files_to_upload
        ]
        is_multi = True
    else:
        files_to_upload_processed = files_to_upload
        original_names_list = [
            display_name_by_path.get(files_to_upload[0], os.path.basename(files_to_upload[0]))
        ]
        is_multi = False
        tar_path = None

    # ── Subir cada archivo ──────────────────────────────────────────
    for idx, file_path in enumerate(files_to_upload_processed, 1):
        file_name = display_name_by_path.get(file_path, os.path.basename(file_path))
        file_size = os.path.getsize(file_path)

        if file_size > uploader.chunk_size:
            total_est = (file_size + uploader.chunk_size - 1) // uploader.chunk_size
            bar_inicial = "\n" + "░" * 20 + f" 0%  (0/{total_est} partes)"
        else:
            bar_inicial = ""

        await _safe_edit(message, 
            f"📤 **Subiendo**\n\n"
            f"📂 Progreso: {idx}/{len(files_to_upload_processed)}\n"
            f"📄 {file_name}\n"
            f"💾 {format_size(file_size)}\n"
            f"⏳ Procesando...{bar_inicial}",
            reply_markup=cancel_keyboard(user_id)
        )

        es_chunked = file_size > uploader.chunk_size
        if es_chunked:
            loop = asyncio.get_running_loop()
            # Guarda anti-ediciones-obsoletas: solo se aplica el progreso más
            # reciente. Se toca únicamente desde el event loop (serializado).
            estado_progreso = {"idx": -1}

            def _progreso_chunk(chunk_idx: int, total_chunks: int) -> None:
                """Edita el mensaje con la barra de progreso tras cada chunk.

                Fix del error 'A coroutine object is required': la edición se
                envuelve en una coroutina propia y se agenda dentro del event
                loop del bot con loop.call_soon_threadsafe (seguro desde el
                hilo del worker), en lugar de pasar message.edit_text
                directamente a asyncio.run_coroutine_threadsafe.

                Si el usuario pulsó '❌ Cancelar', lanza UserCancelledError:
                uploader.py lo deja propagarse (el callback on_chunk está
                protegido con try/except UserCancelledError: raise) y la
                subida aborta limpiamente.
                """
                if is_cancelled(user_id):
                    raise UserCancelledError()
                try:
                    pct = min(100, int(chunk_idx / total_chunks * 100))
                    fill = "█" * (pct // 5)
                    empty = "░" * (20 - pct // 5)
                    texto = (
                        f"📤 **Subiendo**\n\n"
                        f"📂 Progreso: {idx}/{len(files_to_upload_processed)}\n"
                        f"📄 {file_name}\n"
                        f"💾 {format_size(file_size)}\n\n"
                        f"{fill}{empty} {pct}%  ({chunk_idx}/{total_chunks} partes)\n"
                        f"⏳ Subiendo parte {chunk_idx} de {total_chunks}..."
                    )
                except Exception as e:
                    logger.warning(f"Progreso: no se pudo construir el texto: {e}")
                    return

                async def _editar() -> None:
                    try:
                        # Ignorar ediciones obsoletas (carrera con el resumen)
                        if chunk_idx <= estado_progreso["idx"]:
                            return
                        estado_progreso["idx"] = chunk_idx
                        await _safe_edit(message, 
                            texto, reply_markup=cancel_keyboard(user_id)
                        )
                    except Exception as e:
                        logger.debug(f"progreso chunk (edit): {e}")

                def _agendar() -> None:
                    try:
                        asyncio.create_task(_editar())
                    except Exception as e:
                        logger.debug(f"progreso chunk (agendar): {e}")

                try:
                    loop.call_soon_threadsafe(_agendar)
                except Exception as e:
                    logger.warning(f"Progreso: no se pudo actualizar el mensaje: {e}")

        try:
            try:
                # Punto de control DENTRO del try/except: si el usuario pulsó
                # '❌ Cancelar' durante el empaquetado .tar, entre archivos o
                # a mitad de la subida anterior, abortar limpiamente con el
                # mismo mensaje/log que una cancelación a mitad de parte.
                check_cancel(user_id)
                # upload_chunked_file maneja también archivos pequeños y, si la
                # revista falla (incluso en un archivo no fraccionado), lanza
                # UploadAbortedError para que el fallback pruebe otra revista.
                uploaded = await asyncio.to_thread(
                    uploader.upload_chunked_file, file_path, user_id,
                    _progreso_chunk if es_chunked else None
                )
            except UserCancelledError:
                # El usuario pulsó '❌ Cancelar' a mitad de la subida.
                # NO es un fallo de la revista: no se marca cooldown y el
                # fallback de /up no debe probar otras revistas.
                logger.info(f"Subida cancelada por el usuario {user_id}")
                await _safe_edit(message, 
                    f"🛑 **Subida cancelada**\n\n"
                    f"📄 {file_name}\n"
                    f"⚠️ **No se generó URL**: la subida se detuvo antes de completarse.\n"
                    f"📂 Partes subidas en esta sesión: {len(all_uploaded)}\n\n"
                    f"💡 Puedes volver a intentarlo con `/up` cuando quieras."
                )
                await db.log_upload(
                    user_id=user_id, revista_id=revista['rev_id'],
                    submission_id=revista['submission_id'],
                    original_name=original_names_list[0] if original_names_list else "unknown",
                    original_size=0, uploaded_size=0,
                    file_ids=[str(f['id']) for f in all_uploaded],
                    bitzero_mode=uploader.bitzero_mode,
                    bitzero_url=None, encryption_key=uploader.encryption_key,
                    status='cancelled'
                )
                # Log al canal de LOG (paridad con aborted/failed)
                await _log_upload_to_channel(
                    client, user_id, revista, uploader, len(all_uploaded),
                    total_files, all_uploaded, "", is_multi,
                    original_names_list, "cancelled"
                )
                clear_cancel(user_id)
                return "cancelled"
            except UploadAbortedError as e:
                # La revista falló a mitad del lote: NO seguir con el siguiente
                # archivo (dejaría una URL incompleta). El /up con fallback
                # probará automáticamente otra revista.
                detalle = getattr(e, 'detail', None) or str(e)
                logger.error(f"Subida abortada: {e}")
                await _safe_edit(message, 
                    f"🛑 **Subida abortada**\n\n"
                    f"📄 {file_name}\n"
                    f"❌ {detalle[:300]}\n\n"
                    f"⚠️ **No se generó URL** porque la subida quedó incompleta.\n"
                    f"📂 Partes subidas con éxito en esta sesión: {len(all_uploaded)}"
                )
                # Historial: registrar el intento como fallido
                await db.log_upload(
                    user_id=user_id, revista_id=revista['rev_id'],
                    submission_id=revista['submission_id'],
                    original_name=original_names_list[0] if original_names_list else "unknown",
                    original_size=0, uploaded_size=0,
                    file_ids=[str(f['id']) for f in all_uploaded],
                    bitzero_mode=uploader.bitzero_mode,
                    bitzero_url=None, encryption_key=uploader.encryption_key,
                    status='aborted'
                )
                await _log_upload_to_channel(
                    client, user_id, revista, uploader, len(all_uploaded),
                    total_files, all_uploaded, "", is_multi,
                    original_names_list, "aborted"
                )
                mark_upload_failure(revista['rev_id'])
                return "aborted"
        finally:
            # Bloquear ediciones de progreso pendientes para que no pisen
            # el mensaje de resumen ("✅ Subido") que viene a continuación
            # (también si la subida lanza una excepción).
            if es_chunked:
                estado_progreso["idx"] = 10 ** 9

        if uploaded:
            uploaded_count += 1
            all_uploaded.extend(uploaded)
            if is_multi:
                # El .tar empaquetado cubre TODOS los originales
                uploaded_original_paths.extend(original_user_paths)
            else:
                uploaded_original_paths.append(original_user_paths[0])
            await _safe_edit(message, 
                f"✅ **Subido**\n\n"
                f"📂 Progreso: {idx}/{len(files_to_upload_processed)}\n"
                f"📄 {file_name}\n"
                f"🔗 Partes: {len(uploaded)}"
            )
        else:
            await _safe_edit(message, 
                f"⚠️ **Error subiendo** {file_name}\n"
                f"⏳ Continuando con el siguiente..."
            )
        await asyncio.sleep(1)

    # ── Limpiar tar temporal si se creó ─────────────────────────────
    # (Los temporales comprimidos los gestiona upload_files_with_fallback:
    #  si la subida falla, el comprimido queda en espera y NO se borra.)
    if tar_path and os.path.exists(tar_path):
        try:
            os.remove(tar_path)
        except OSError:
            pass

    if not all_uploaded:
        await _safe_edit(message, "❌ **Subida fallida.** No se subió ningún archivo.")
        await db.log_upload(
            user_id=user_id, revista_id=revista['rev_id'],
            submission_id=revista['submission_id'],
            original_name=original_names_list[0] if original_names_list else "unknown",
            original_size=0, uploaded_size=0,
            file_ids=[], bitzero_mode=uploader.bitzero_mode,
            bitzero_url=None, encryption_key=uploader.encryption_key,
            status='failed'
        )
        await _log_upload_to_channel(
            client, user_id, revista, uploader, 0, total_files,
            [], "", is_multi, original_names_list, "failed"
        )
        mark_upload_failure(revista['rev_id'])
        return "failed"

    # ── Generar URL BitZero ────────────────────────────────────────
    bitzero_url = ""
    if uploader.bitzero_mode > 0:
        total_original_size = sum(
            f.get('original_size', f.get('size', 0)) for f in all_uploaded
        )
        if is_multi:
            original_name = URLGenerator.build_multi_filename(original_names_list)
        else:
            original_name = original_names_list[0]

        bitzero_url = uploader.generate_bitzero_url(
            original_name=original_name,
            file_size=total_original_size,
        )

    # ── Registrar en DB ─────────────────────────────────────────────
    total_uploaded_size = sum(f.get('size', 0) for f in all_uploaded)
    total_original_size_db = sum(
        f.get('original_size', f.get('size', 0)) for f in all_uploaded
    )
    status = 'success' if uploaded_count == len(files_to_upload_processed) else 'partial'
    await db.log_upload(
        user_id=user_id, revista_id=revista['rev_id'],
        submission_id=revista['submission_id'],
        original_name=original_names_list[0] if original_names_list else "multi",
        original_size=total_original_size_db,
        uploaded_size=total_uploaded_size,
        file_ids=[str(f['id']) for f in all_uploaded],
        bitzero_mode=uploader.bitzero_mode,
        bitzero_url=bitzero_url,
        encryption_key=uploader.encryption_key,
        status=status
    )

    # ── Enviar al canal de LOG ─────────────────────────────────────
    await _log_upload_to_channel(
        client, user_id, revista, uploader, uploaded_count, total_files,
        all_uploaded, bitzero_url, is_multi, original_names_list, status
    )

    # ── Eliminar del VPS los archivos subidos (ahorrar espacio) ────
    deleted_count = 0
    if config.DELETE_AFTER_UPLOAD and uploaded_original_paths:
        for fp in uploaded_original_paths:
            try:
                if os.path.exists(fp):
                    os.remove(fp)
                    deleted_count += 1
                    logger.info(f"🗑️ Eliminado del VPS tras subir: {fp}")
            except OSError as e:
                logger.warning(f"No se pudo eliminar {fp} del VPS: {e}")
            # Mantener la tabla files consistente con el disco
            try:
                await db.delete_file_by_path(user_id, fp)
            except Exception as e:
                logger.warning(f"No se pudo borrar registro DB de {fp}: {e}")

    # ── Mensaje final (simple: archivo + código + cómo usarlo) ─────
    text = f"✅ **Subida Completada**\n\n"
    # Nombre del archivo en el MISMO mensaje que el código: con varias
    # subidas seguidas (o varios archivos) es imposible saber qué código
    # corresponde a qué archivo sin esto.
    if original_names_list:
        if len(original_names_list) == 1:
            text += f"📄 **Archivo:** `{original_names_list[0]}`\n\n"
        else:
            text += f"📄 **Archivos ({len(original_names_list)}):**\n"
            for nombre in original_names_list[:10]:
                text += f"  • `{nombre}`\n"
            if len(original_names_list) > 10:
                text += f"  … y {len(original_names_list) - 10} más\n"
            text += "\n"
    if compression_stats:
        orig_total = sum(s.get('orig_size', 0) for s in compression_stats.values())
        comp_total = sum(s.get('comp_size', 0) for s in compression_stats.values())
        if orig_total and comp_total:
            saved = max(0.0, (1 - comp_total / orig_total) * 100)
            text += (
                f"🗜️ **Comprimido:** {format_size(orig_total)} → "
                f"{format_size(comp_total)} ({saved:.0f}% menos)\n"
                f"⚡ La subida fue más rápida porque pesa menos.\n\n"
            )
    if bitzero_url:
        text += f"🔗 **Código de descarga:**\n`{bitzero_url}`\n\n"
        text += (
            "📱 **Cómo usarlo en la app:**\n"
            "1. Copia el código de arriba.\n"
            "2. Pégalo en la app (la app reconoce el código tal cual).\n"
            "3. Toca **Iniciar descarga** y listo.\n"
        )
    else:
        text += "🔗 **No se generó código de descarga.**\n"
    await _safe_edit(message, text)
    return status


async def _sleep_cancelable(seconds: int, user_id: int,
                             status_msg) -> bool:
    """Duerme en tramos cortos abortando si el usuario pulsa '❌ Cancelar'.

    Devuelve False si el usuario canceló durante la espera (el mensaje ya
    se editó con el aviso de cancelación). True si se completó la espera.
    """
    while seconds > 0:
        if is_cancelled(user_id):
            try:
                await status_msg.edit_text(
                    "🛑 **Proceso cancelado.**\n\nLa subida se detuvo antes de comenzar."
                )
            except Exception:
                pass
            clear_cancel(user_id)
            return False
        tramo = min(5, seconds)
        await asyncio.sleep(tramo)
        seconds -= tramo
    return True


async def upload_files_with_fallback(client, status_msg, user_id: int,
                                      file_paths: list[str],
                                      compress_videos: bool = False) -> str:
    """Selecciona la primera revista operativa y sube los archivos.

    Encapsula la lógica de fallback que antes vivía en /up: si la revista
    elegida falla al SUBIR (no solo al login), se marca en cooldown y se
    prueba automáticamente la siguiente revista operativa. También la usa
    la auto-subida al recibir un archivo (handlers/files.py).

    compress_videos=True: los vídeos se comprimen (FFmpeg) antes de subir
    para que pesen menos y la subida sea más rápida.

    Resiliencia: las revistas OJS cubanas son intermitentes (se caen y
    vuelven en minutos). Si en una ronda ninguna revista logra subir, se
    espera (UPLOAD_RETRY_WAIT_SEC, duplicado en cada ronda) y se reintenta
    sola hasta UPLOAD_RETRY_ATTEMPTS veces antes de rendirse. Al agotarse,
    el mensaje final detalla QUÉ revista falló y POR QUÉ.

    Devuelve el outcome final: 'success' / 'partial' / 'cancelled' / 'failed'
    / 'parked'.

    'parked' = no se pudo subir, pero los vídeos comprimidos quedaron en
    espera para reintento automático (y se borraron los originales).
    """
    clear_cancel(user_id)

    # ── Compresión (UNA sola vez, antes de buscar revistas) ───────────
    # Antes se comprimía dentro de cada intento de subida, así que un vídeo
    # que no lograba subir se re-comprimía en cada ronda (minutos de CPU
    # tirados). Ahora se comprime una vez y el mismo comprimido se reutiliza
    # en todos los reintentos y revistas.
    upload_paths: List[str] = list(file_paths)
    display_names: dict = {}
    comp_stats: dict = {}
    compressed_temps: list = []
    if compress_videos:
        try:
            res = await _compress_videos_before_upload(
                client, status_msg, user_id, upload_paths
            )
            upload_paths = res["paths"]
            display_names = res["display"]
            comp_stats = res["stats"]
            compressed_temps = res["temps"]
        except UserCancelledError:
            _cleanup_compressed_temps(compressed_temps)
            await status_msg.edit_text(
                "🛑 **Compresión cancelada**\n\n"
                "⚠️ **No se subió nada.** El archivo original sigue guardado.\n\n"
                "💡 Puedes reintentar con `/up` cuando quieras."
            )
            clear_cancel(user_id)
            return "cancelled"
        if not upload_paths:
            _cleanup_compressed_temps(compressed_temps)
            await status_msg.edit_text("📭 **No tienes archivos para subir.**")
            return "failed"

    async def _finalizar_ok() -> None:
        """Subida OK: borra del VPS los originales y los temporales."""
        for p in file_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
                    logger.info(f"🗑️ Eliminado del VPS tras subir: {p}")
            except OSError as e:
                logger.warning(f"No se pudo eliminar {p} del VPS: {e}")
            try:
                await db.delete_file_by_path(user_id, p)
            except Exception as e:
                logger.debug(f"No se pudo borrar registro DB de {p}: {e}")
        _cleanup_compressed_temps(compressed_temps)

    async def _fallo_final() -> int:
        """Tras agotar reintentos: deja el comprimido EN ESPERA y borra el
        original, para no re-comprimir ni llenar el disco con el archivo
        grande. Los comprimidos que quedan en espera los sube más tarde
        retry_pending_compressed_uploads() cuando haya revista operativa.
        Devuelve cuántos quedaron en espera.
        """
        parkeados: list = []
        if comp_stats:
            for original_path, stats in comp_stats.items():
                compressed = stats.get("output")
                if not compressed or not os.path.exists(compressed):
                    continue
                nombre = (display_names.get(compressed)
                          or os.path.basename(original_path))
                try:
                    await db.save_pending_compressed_upload(
                        user_id, compressed, nombre, original_path
                    )
                except Exception as e:
                    logger.warning(f"No se pudo dejar en espera {compressed}: {e}")
                    continue
                try:
                    if os.path.exists(original_path):
                        os.remove(original_path)
                        logger.info(
                            f"🗑️ Original borrado (comprimido en espera): "
                            f"{original_path}"
                        )
                except OSError as e:
                    logger.warning(f"No se pudo borrar original {original_path}: {e}")
                try:
                    await db.delete_file_by_path(user_id, original_path)
                except Exception:
                    pass
                parkeados.append(compressed)
                logger.info(f"🗜️ Comprimido en espera de subida: {compressed} "
                            f"({nombre})")
        # Los comprimidos que SÍ quedaron en espera no se limpian
        _cleanup_compressed_temps([t for t in compressed_temps if t not in parkeados])
        return len(parkeados)

    # ── Intentar subir (con fallback de revistas) atrapando CUALQUIER error
    # de Telegram (p. ej. FloodWait al editar el mensaje, que es lo que dejó
    # tirado el último intento): si algo revienta después de haber
    # comprimido, el comprimido NO se pierde: se parkea.
    outcome = "failed"
    try:
        outcome = await _upload_prepared_with_retries(
            client, status_msg, user_id, file_paths,
            upload_paths, display_names, comp_stats,
        )
    except Exception as e:
        logger.exception(f"upload_files_with_fallback: error inesperado: {e}")

    if outcome == "success":
        await _finalizar_ok()
        return outcome
    if outcome in ("partial", "cancelled"):
        # 'partial': perform ya borró del VPS los originales que SÍ se
        # subieron; los que fallaron se conservan para poder reintentar.
        # 'cancelled': el usuario abortó; se conservan todos los originales.
        # En ambos casos solo se descartan los temporales comprimidos.
        _cleanup_compressed_temps(compressed_temps)
        return outcome

    # Falló (o hubo una excepción): dejar los comprimidos en espera y
    # borrar los originales para no llenar el disco ni re-comprimir.
    parkeados = await _fallo_final()
    if parkeados:
        try:
            await status_msg.edit_text(
                "⏸️ **La subida no se pudo completar ahora mismo.**\n\n"
                f"🗜️ **{parkeados} vídeo(s) comprimido(s) quedaron en espera.**\n"
                "El original se borró del VPS y el comprimido se subirá "
                "**automáticamente** en cuanto una revista esté operativa.\n\n"
                "💡 No hace falta que hagas nada."
            )
        except Exception as e:
            logger.debug(f"no se pudo avisar del parked: {e}")
        return "parked"
    return "failed"


async def _upload_prepared_with_retries(client, status_msg, user_id: int,
                                        file_paths: list[str],
                                        upload_paths: list[str],
                                        display_names: dict,
                                        comp_stats: dict) -> str:
    """Bucle de reintentos con fallback entre revistas (ya sin comprimir).

    Los archivos vienen preparados (comprimidos si aplica). Devuelve
    'success' / 'partial' / 'cancelled' / 'failed'. El ciclo de vida de la
    compresión (borrar originales o parkear) lo gestiona el llamador
    `upload_files_with_fallback`.
    """
    max_retries = int(getattr(config, 'UPLOAD_RETRY_ATTEMPTS', 3))
    base_wait = int(getattr(config, 'UPLOAD_RETRY_WAIT_SEC', 120))
    fallos: List[str] = []

    for intento in range(max_retries + 1):
        result = await pick_working_revista()
        if is_cancelled(user_id):
            await status_msg.edit_text(
                "🛑 **Proceso cancelado.**\n\nLa subida se detuvo antes de comenzar."
            )
            clear_cancel(user_id)
            return "cancelled"
        if result is None:
            # Ninguna revista respondió al login en esta ronda
            if intento < max_retries:
                espera = base_wait * (2 ** intento)
                await status_msg.edit_text(
                    "⚠️ **No hay ningún servidor operativo en este momento.**\n\n"
                    f"🔄 Reintentando en ~{espera // 60} min "
                    f"(intento {intento + 1}/{max_retries + 1})...",
                    reply_markup=cancel_keyboard(user_id)
                )
                if not await _sleep_cancelable(espera, user_id, status_msg):
                    return "cancelled"
                continue
            await status_msg.edit_text(
                "❌ **No hay ningún servidor operativo en este momento.**\n\n"
                "Intenta más tarde o avisa al administrador."
            )
            return "failed"

        revista, uploader = result
        probadas: List[str] = []

        while True:
            if is_cancelled(user_id):
                await status_msg.edit_text(
                    "🛑 **Proceso cancelado.**\n\nLa subida se detuvo antes de comenzar."
                )
                clear_cancel(user_id)
                return "cancelled"
            probadas.append(revista['rev_id'])
            await status_msg.edit_text(
                "✅ **Servidor operativo encontrado.**\n\n"
                f"🚀 Subiendo {len(file_paths)} archivo(s)...",
                reply_markup=cancel_keyboard(user_id)
            )
            outcome = await perform_upload_with_bitzero(
                client, status_msg, uploader, revista, user_id,
                file_paths=upload_paths,
                display_names=display_names,
                compression_stats=comp_stats,
            )
            if outcome in ("success", "partial", "cancelled"):
                return outcome

            # Registrar el motivo del fallo para el mensaje final (y para
            # el log): lo que dejó uploader._last_upload_error (HTTP 500,
            # submission 404, red caída, auth...).
            motivo = (getattr(uploader, '_last_upload_error', '') or
                      'la revista dejó de responder durante la subida')
            fallos.append(f"{revista['nombre']} ({revista['rev_id']}): {motivo}")
            logger.warning(f"Ronda {intento + 1}: {revista['rev_id']} falló al subir: {motivo}")

            # La revista falló al subir (ya marcada en cooldown por
            # perform_upload_with_bitzero): probar la siguiente operativa
            result = await pick_working_revista()
            if is_cancelled(user_id):
                await status_msg.edit_text(
                    "🛑 **Proceso cancelado.**\n\nLa subida se detuvo antes de comenzar."
                )
                clear_cancel(user_id)
                return "cancelled"
            if result is None:
                break
            siguiente, siguiente_uploader = result
            if siguiente['rev_id'] in probadas:
                break
            revista, uploader = siguiente, siguiente_uploader
            await asyncio.sleep(1)
            await status_msg.edit_text(
                "🔄 **La subida falló en esta opción — probando otra...**",
                reply_markup=cancel_keyboard(user_id)
            )

        # Ronda agotada (se probaron todas las revistas operativas):
        # si quedan reintentos, esperar (con backoff) y volver a intentar.
        if intento < max_retries:
            espera = base_wait * (2 ** intento)
            await status_msg.edit_text(
                f"⚠️ **La subida falló en esta ronda** "
                f"(intento {intento + 1}/{max_retries + 1}).\n\n"
                f"🔄 Reintentando en ~{espera // 60} min...",
                reply_markup=cancel_keyboard(user_id)
            )
            if not await _sleep_cancelable(espera, user_id, status_msg):
                return "cancelled"
            continue

    # Agotados todos los reintentos: mensaje final con el detalle de fallos.
    # (Si había comprimidos, el llamador los parkea y sobrescribe el aviso.)
    texto = (
        "❌ **La subida falló.**\n\n"
        "🔄 Espera unos minutos y vuelve a intentarlo."
    )
    if fallos:
        # Deduplicar conservando el orden (una revista puede fallar igual
        # en varias rondas) y acotar a las primeras para no saturar el mensaje.
        unicos: List[str] = []
        for f in fallos:
            if f not in unicos:
                unicos.append(f)
        texto += "\n\n📋 **Fallos de esta ronda:**\n"
        texto += "\n".join(f"• {f}" for f in unicos[:5])
        if len(unicos) > 5:
            texto += f"\n... y {len(unicos) - 5} más."
    try:
        await status_msg.edit_text(texto)
    except Exception as e:
        logger.debug(f"no se pudo editar el mensaje final: {e}")
    return "failed"


# ── Cola de subidas (control de recursos del VPS) ──────────────────
# La compresión FFmpeg y las subidas por chunks consumen mucha CPU y
# ancho de banda. Con UPLOAD_QUEUE_ENABLED las subidas se encolan y
# corren como máximo MAX_CONCURRENT_UPLOADS a la vez (por defecto 1 =
# estrictamente secuencial); el usuario ve su posición en el mensaje y
# el bot le avisa cuando le toca. Todo el estado vive en el event loop
# (los handlers son async), así que no se necesitan locks.
class _UploadJob:
    """Un trabajo de subida esperando (o en ejecución) en la cola."""

    __slots__ = ("user_id", "status_msg", "run", "label", "done", "outcome",
                 "db_id")

    def __init__(self, user_id: int, status_msg, run, label: str,
                 db_id: Optional[int] = None) -> None:
        self.user_id = user_id
        self.status_msg = status_msg
        self.run = run          # async callable () -> str (outcome)
        self.label = label
        self.done: asyncio.Event = asyncio.Event()
        self.outcome: str = "failed"
        # Fila en upload_queue si la subida está persistida (sobrevive a
        # reinicios del bot). Se borra al terminar o cancelarse.
        self.db_id = db_id


_upload_jobs: "collections.deque[_UploadJob]" = collections.deque()
_active_jobs: List[_UploadJob] = []
_worker_tasks: List[asyncio.Task] = []
_wake: Optional[asyncio.Event] = None
# Apagado limpio: al ponerse True, los workers terminan la subida activa
# y NO arrancan nuevas (las pendientes quedan persistidas en BD y se
# reanudan en el próximo arranque).
_shutting_down: bool = False


def request_shutdown() -> None:
    """Solicita el apagado limpio de la cola de subidas.

    Los workers terminan la subida en curso y dejan de arrancar nuevas;
    las que estaban en cola quedan persistidas (si tenían persistencia)
    y se reanudan al próximo arranque. main.py llama a wait_until_idle()
    para esperar a que terminen las activas antes de detenerse.
    """
    global _shutting_down
    _shutting_down = True
    _get_wake().set()


def is_shutting_down() -> bool:
    return _shutting_down


def _get_wake() -> asyncio.Event:
    """Evento de aviso de la cola, creado dentro del event loop en uso
    (en Python 3.9 crearlo a nivel de módulo lo ata a un loop inexistente).
    """
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def _queue_enabled() -> bool:
    return bool(getattr(config, 'UPLOAD_QUEUE_ENABLED', True))


def _queue_max_concurrent() -> int:
    return max(1, int(getattr(config, 'MAX_CONCURRENT_UPLOADS', 1)))


def queue_info() -> Tuple[int, int]:
    """Devuelve (en_cola, en_curso) para mostrar en /status."""
    return len(_upload_jobs), len(_active_jobs)


def _job_position(job: _UploadJob) -> int:
    """Posición 1-based del job (1 = ya en ejecución)."""
    pos = len(_active_jobs)
    for j in _upload_jobs:
        pos += 1
        if j is job:
            return pos
    return max(1, pos)


async def _update_queued_positions() -> None:
    """Actualiza el mensaje de cada subida en cola con su posición."""
    total = len(_active_jobs) + len(_upload_jobs)
    for idx, job in enumerate(_upload_jobs, start=len(_active_jobs) + 1):
        try:
            await job.status_msg.edit_text(
                f"⏳ **En cola de subida**\n\n"
                f"📦 {job.label}\n"
                f"📶 **Posición:** {idx}/{total}\n\n"
                f"Hay otra subida en curso ahora mismo.\n"
                f"Te avisaré cuando empiece la tuya.",
                reply_markup=cancel_keyboard(job.user_id)
            )
        except Exception as e:
            logger.debug(f"cola: no se pudo actualizar posición: {e}")


async def _upload_worker() -> None:
    """Procesa la cola FIFO: saca un job, lo ejecuta y repite."""
    while True:
        # Esperar hasta que haya trabajo
        while not _upload_jobs:
            if _shutting_down:
                return
            _get_wake().clear()
            if not _upload_jobs and not _shutting_down:
                await _get_wake().wait()
        job = _upload_jobs.popleft()
        # Apagado limpio: no arrancar subidas nuevas; las pendientes quedan
        # persistidas en BD y se reanudan en el próximo arranque. Las que el
        # usuario canceló sí se procesan para liberarlo (y se borran de BD).
        if _shutting_down and not is_cancelled(job.user_id):
            _upload_jobs.appendleft(job)
            return
        _active_jobs.append(job)
        try:
            await _update_queued_positions()
        except Exception:
            pass

        # El usuario pudo cancelar mientras esperaba en la cola
        if is_cancelled(job.user_id):
            clear_cancel(job.user_id)
            job.outcome = "cancelled"
            if job.db_id:
                try:
                    await db.delete_pending_upload(job.db_id)
                except Exception:
                    pass
            try:
                await job.status_msg.edit_text(
                    "🛑 **Subida cancelada**\n\n"
                    "⚠️ Estaba en cola y **no llegó a empezar**.\n\n"
                    "💡 Reintenta cuando quieras."
                )
            except Exception:
                pass
            _active_jobs.remove(job)
            job.done.set()
            continue

        try:
            await job.status_msg.edit_text(
                "🚀 **¡Te toca! Subiendo...**",
                reply_markup=cancel_keyboard(job.user_id)
            )
        except Exception:
            pass

        try:
            job.outcome = await job.run()
        except Exception as e:
            logger.exception(f"cola: error en subida de {job.user_id}: {e}")
            job.outcome = "failed"
        finally:
            # Terminó (o se canceló a mitad): ya no está pendiente
            if job.db_id:
                try:
                    await db.delete_pending_upload(job.db_id)
                except Exception:
                    pass
            _active_jobs.remove(job)
            job.done.set()


def _ensure_workers() -> None:
    """Arranca (o repara) los workers según MAX_CONCURRENT_UPLOADS."""
    target = _queue_max_concurrent()
    while len(_worker_tasks) < target:
        _worker_tasks.append(asyncio.create_task(_upload_worker()))
    _worker_tasks[:] = [t for t in _worker_tasks if not t.done()]


async def enqueue_job(user_id: int, status_msg, run, label: str,
                      persist_row: Optional[dict] = None,
                      db_id: Optional[int] = None) -> str:
    """Encola una subida y espera a que se ejecute.

    - run: async callable sin argumentos que ejecuta la subida y devuelve
      el outcome ('success' / 'partial' / 'cancelled' / 'failed').
    - label: texto corto que identifica el lote en el aviso de cola
      (ej. '3 archivo(s)', 'Subida a KIKI_REV').
    - persist_row: dict con {"file_paths": [...], "compress_videos": bool}.
      Si se da, la subida se guarda en la tabla upload_queue para que
      sobreviva a un reinicio del bot mientras espera su turno (la fila
      se borra al terminar o cancelarse).
    - db_id: si ya existe la fila (restauración tras reinicio), se usa en
      vez de crear una nueva.

    Si UPLOAD_QUEUE_ENABLED=0 se ejecuta directamente (sin cola).
    """
    if not _queue_enabled():
        return await run()

    if persist_row and not db_id:
        try:
            db_id = await db.save_pending_upload(
                user_id, persist_row.get("file_paths", []),
                persist_row.get("compress_videos", False),
            )
        except Exception as e:
            logger.warning(f"cola: no se pudo persistir subida de {user_id}: {e}")

    job = _UploadJob(user_id=user_id, status_msg=status_msg, run=run,
                     label=label, db_id=db_id)
    _upload_jobs.append(job)
    _ensure_workers()
    pos = _job_position(job)
    if pos > 1:
        total = len(_active_jobs) + len(_upload_jobs)
        try:
            await status_msg.edit_text(
                f"⏳ **En cola de subida**\n\n"
                f"📦 {label}\n"
                f"📶 **Posición:** {pos}/{total}\n\n"
                f"Hay otra subida en curso ahora mismo.\n"
                f"Te avisaré cuando empiece la tuya.",
                reply_markup=cancel_keyboard(user_id)
            )
        except Exception as e:
            logger.debug(f"cola: no se pudo mostrar posición: {e}")
    _get_wake().set()
    await job.done.wait()
    return job.outcome


async def enqueue_upload(client, status_msg, user_id: int,
                          file_paths: list[str],
                          compress_videos: bool = False) -> str:
    """Encola una subida con fallback automático de revistas.

    Punto de entrada único para /up, la auto-subida y los botones de modo
    (original/comprimir). El usuario ve su posición en la cola y el bot
    le avisa cuando le toca.
    """
    async def _run() -> str:
        return await upload_files_with_fallback(
            client, status_msg, user_id, file_paths,
            compress_videos=compress_videos
        )

    return await enqueue_job(
        user_id, status_msg, _run,
        label=f"{len(file_paths)} archivo(s)",
        persist_row={"file_paths": file_paths,
                     "compress_videos": compress_videos},
    )


async def restore_pending_uploads(app) -> int:
    """Reanuda las subidas que quedaron en cola por un reinicio del bot.

    Se llama en el arranque (main.py), después de app.start(). Para cada
    subida pendiente en la BD: si los archivos siguen existiendo, se avisa
    al usuario y se vuelve a encolar (la fila de BD se reutiliza y se
    borra cuando la subida termine). Devuelve cuántas se reanudaron.
    """
    if not _queue_enabled():
        return 0
    try:
        pendientes = await db.list_pending_uploads()
    except Exception as e:
        logger.warning(f"restore_pending_uploads: no se pudo leer la BD: {e}")
        return 0
    if not pendientes:
        return 0

    reanudadas = 0
    for p in pendientes:
        paths = [x for x in p.get("file_paths", []) if os.path.exists(x)]
        if not paths:
            logger.info(f"restore: subida pendiente de {p['user_id']} sin "
                        f"archivos (borrados?) — descartada")
            try:
                await db.delete_pending_upload(p["id"])
            except Exception:
                pass
            continue
        user_id = p["user_id"]
        compress = bool(p.get("compress_videos", 0))
        try:
            status_msg = await app.send_message(
                user_id,
                "↩️ **Subida pendiente reanudada**\n\n"
                "Tu subida quedó en cola cuando el bot se reinició.\n"
                "⏳ Se reanuda automáticamente...",
                reply_markup=cancel_keyboard(user_id)
            )
        except Exception as e:
            logger.warning(f"restore: no se pudo avisar a {user_id}: {e} — "
                           f"descartando pendiente")
            try:
                await db.delete_pending_upload(p["id"])
            except Exception:
                pass
            continue

        async def _run(user=user_id, paths=paths, compress=compress) -> str:
            return await upload_files_with_fallback(
                app, status_msg, user, paths, compress_videos=compress
            )

        _upload_jobs.append(_UploadJob(
            user_id=user_id, status_msg=status_msg, run=_run,
            label=f"{len(paths)} archivo(s)", db_id=p["id"],
        ))
        _ensure_workers()
        _get_wake().set()
        reanudadas += 1
        logger.info(f"restore: subida de {user_id} ({len(paths)} archivo(s)) "
                    f"reanudada en cola")
    return reanudadas


# ── Reintento automático de comprimidos en espera ─────────────────
# Si un vídeo ya comprimido no pudo subirse (revista caída / HTTP 500 /
# FloodWait de Telegram...), el original se borra y el comprimido queda en
# la tabla pending_compressed. Estas funciones lo reintentan solas cuando
# alguna revista vuelve a estar operativa, sin re-comprimir.
_retry_in_progress: bool = False

# Avisos "sigue en espera" ya enviados en este proceso (por id de fila de
# pending_compressed): evita reenviar el mismo mensaje en cada ciclo
# mientras no haya ninguna revista operativa.
_pending_wait_notified: set = set()


async def _retry_one_pending(client, row: Dict[str, Any]) -> bool:
    """Intenta subir UN comprimido en espera. True si se subió.

    Usa la cola de subidas (respeta MAX_CONCURRENT_UPLOADS) y prueba hasta
    3 revistas operativas; si no hay ninguna, no hace nada y espera al
    siguiente ciclo.
    """
    user_id = row["user_id"]
    path = row["file_path"]
    if not os.path.exists(path):
        logger.info(f"pending_compressed: archivo en espera ya no existe: {path}")
        try:
            await db.delete_pending_compressed_upload(row["id"])
        except Exception:
            pass
        _pending_wait_notified.discard(row.get("id"))
        return False

    # Apagado limpio en curso: no arrancar subidas nuevas (la cola no las
    # procesaría y este await se quedaría colgado).
    if is_shutting_down():
        return False

    # Sin revistas operativas ahora mismo → esperar al próximo ciclo
    # (pick_working_revista usa caché/intervalo mínimo: no satura las OJS).
    if await pick_working_revista() is None:
        # Avisar UNA vez de que la subida sigue en espera, para que el usuario
        # no crea que el bot la olvidó. El aviso de "empieza la subida" se
        # envía aparte (más abajo) justo antes de empezar a subir de verdad.
        row_id = row.get("id")
        if row_id is not None and row_id not in _pending_wait_notified:
            _pending_wait_notified.add(row_id)
            try:
                await client.send_message(
                    user_id,
                    "⏳ **Tu subida sigue en espera**\n\n"
                    f"📄 `{row.get('original_name') or os.path.basename(path)}`\n\n"
                    "Ninguna revista está operativa ahora mismo. El vídeo ya "
                    "está comprimido y se subirá **automáticamente** en cuanto "
                    "una revista vuelva a responder.\n\n"
                    "🔔 Te avisaré por aquí en cuanto **empiece la subida**.",
                    reply_markup=cancel_keyboard(user_id),
                )
            except Exception as e:
                logger.warning(f"pending_compressed: no se pudo avisar (en "
                               f"espera) a {user_id}: {e}")
        return False

    nombre = row.get("original_name") or os.path.basename(path)
    try:
        status_msg = await client.send_message(
            user_id,
            "♻️ **Reintentando subida en espera**\n\n"
            f"📄 `{nombre}`\n"
            "El vídeo ya estaba comprimido y esperaba a que una revista "
            "estuviera operativa. Reintentando ahora...",
            reply_markup=cancel_keyboard(user_id),
        )
    except Exception as e:
        logger.warning(f"pending_compressed: no se pudo avisar a {user_id}: {e}")
        return False

    async def _run() -> str:
        for _ in range(3):
            if is_cancelled(user_id):
                clear_cancel(user_id)
                return "cancelled"
            result = await pick_working_revista()
            if result is None:
                return "failed"
            revista, uploader = result
            outcome = await perform_upload_with_bitzero(
                client, status_msg, uploader, revista, user_id,
                file_paths=[path],
                display_names={path: nombre},
            )
            if outcome in ("success", "partial", "cancelled"):
                return outcome
        return "failed"

    outcome = await enqueue_job(
        user_id, status_msg, _run,
        label=f"Reintento: {nombre}",
    )

    if outcome in ("success", "partial"):
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"🗑️ Comprimido en espera eliminado tras subir: {path}")
        except OSError as e:
            logger.warning(f"No se pudo borrar el comprimido en espera {path}: {e}")
        try:
            await db.delete_pending_compressed_upload(row["id"])
        except Exception as e:
            logger.warning(f"No se pudo borrar fila en espera {row.get('id')}: {e}")
        _pending_wait_notified.discard(row.get("id"))
        # Hueco libre → dejar pasar al siguiente vídeo en espera del usuario
        try:
            from handlers.files import clear_video_busy, release_next_held_video
            clear_video_busy(user_id)
            await release_next_held_video(client, user_id)
        except Exception as e:
            logger.debug(f"pending_compressed: no se pudo liberar a {user_id}: {e}")
        return True

    try:
        await db.bump_pending_compressed_upload(row["id"])
    except Exception:
        pass
    return False


async def retry_pending_compressed_uploads(client) -> int:
    """Reintenta subir todos los comprimidos en espera. Devuelve cuántos subió.

    Se llama desde el loop periódico y cuando el monitor detecta que una
    revista volvió a estar operativa.
    """
    global _retry_in_progress
    if _retry_in_progress:
        return 0
    _retry_in_progress = True
    try:
        try:
            pendientes = await db.list_pending_compressed_uploads()
        except Exception as e:
            logger.warning(f"pending_compressed: no se pudo leer la BD: {e}")
            return 0
        if not pendientes:
            return 0
        subidas = 0
        for row in pendientes:
            try:
                if await _retry_one_pending(client, row):
                    subidas += 1
            except Exception as e:
                logger.warning(
                    f"pending_compressed: error reintentando {row.get('id')}: {e}"
                )
        return subidas
    finally:
        _retry_in_progress = False


async def pending_compressed_retry_loop(client, interval_min: int) -> None:
    """Loop de fondo: reintenta los comprimidos en espera cada X minutos."""
    intervalo = max(1, int(interval_min))
    logger.info(f"pending_compressed: loop de reintento iniciado "
                f"(cada {intervalo} min)")
    while True:
        if is_shutting_down():
            logger.info("pending_compressed: apagado en curso, loop detenido")
            return
        try:
            n = await retry_pending_compressed_uploads(client)
            if n:
                logger.info(f"pending_compressed: {n} subida(s) en espera completada(s)")
        except asyncio.CancelledError:
            logger.info("pending_compressed: loop detenido")
            raise
        except Exception as e:
            logger.warning(f"pending_compressed: error en el ciclo: {e}")
        await asyncio.sleep(intervalo * 60)


async def wait_until_idle(timeout: float) -> None:
    """Espera (máx. timeout segundos) a que terminen las subidas activas.

    Se usa en el apagado limpio: al recibir SIGTERM, la cola deja de
    arrancar subidas nuevas y este método espera a que las en curso
    terminen antes de detener el bot (docker respeta stop_grace_period).
    """
    inicio = time.monotonic()
    while _active_jobs:
        if time.monotonic() - inicio > timeout:
            logger.warning(
                f"Apagado: {len(_active_jobs)} subida(s) activa(s) tras "
                f"{timeout:.0f}s — se cortarán."
            )
            return
        await asyncio.sleep(1)
    logger.info("Apagado limpio: sin subidas activas, deteniéndose.")
