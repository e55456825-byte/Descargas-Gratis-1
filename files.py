"""
handlers/files.py — Recepción de archivos desde Telegram.

Cada archivo se guarda en la carpeta del usuario (aislamiento).
Se verifica quota antes de aceptar. Se registra en DB.

Mejoras:
  - Si STORAGE_CHANNEL_ID está configurado, se REENVÍA el archivo al
    canal privado de respaldo sin descargarlo (usa file_id del message),
    evitando consumir megas del usuario. Se guarda storage_msg_id en DB.
  - Si LOG_CHANNEL_ID está configurado, se envía un mensaje de auditoría
    (texto) al canal de log por cada archivo recibido.

Pipeline de vídeo SERIAL por usuario:
  Un usuario solo puede tener UN vídeo "en curso" a la vez: descargando,
  esperando su decisión (comprimir / subir original) o subiéndose. Mientras
  esté ocupado, los NUEVOS vídeos que envíe NO se descargan al disco (así el
  VPS no se llena con varios archivos de ~1 GB): se guarda la referencia del
  mensaje en la tabla held_downloads y se procesa automáticamente cuando el
  anterior termina y libera espacio (DELETE_AFTER_UPLOAD borra el archivo al
  subir con éxito).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from pyrogram import filters
from pyrogram.errors import RPCError
from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup)

from cancellation import cancel_keyboard, clear_cancel, is_cancelled
from config import config
from database import db
from storage import storage
from utils import (authorized_only, format_size, progress_bar, safe_handler)

logger = logging.getLogger(__name__)

# Extensiones consideradas vídeo (para decidir ANTES de descargar si un
# mensaje debe entrar en la cola serial).
_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v",
                     ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp")

# Usuarios con un vídeo en curso (descargando / esperando decisión /
# subiéndose). Los vídeos nuevos de estos usuarios se ponen en espera.
_busy_video_users: set[int] = set()


# ── Estado del pipeline serial ──────────────────────────────────────
def mark_video_busy(user_id: int) -> None:
    _busy_video_users.add(user_id)


def clear_video_busy(user_id: int) -> None:
    _busy_video_users.discard(user_id)


def is_video_busy(user_id: int) -> bool:
    """True si el usuario ya tiene un vídeo sin terminar.

    Se considera ocupado si hay un vídeo en curso O si tiene una selección
    pendiente de elegir modo (ej. la dejó a medias con /up).
    """
    if user_id in _busy_video_users:
        return True
    try:
        from handlers.upload import pending_upload_count
        return pending_upload_count(user_id) > 0
    except Exception:
        return False


def _extract_media_info(message):
    """Extrae (file_name, mime_type, es_video) SIN descargar nada.

    'es_video' es una estimación por mime/extensiones usada para la cola
    serial. La comprobación definitiva tras descargar la hace
    video_compressor.is_video_file (contenido real del archivo).
    """
    file_name = None
    mime_type = None

    if message.document:
        file_name = message.document.file_name or f"document_{message.id}"
        mime_type = message.document.mime_type
        if not os.path.splitext(file_name)[1] and mime_type:
            ext_map = {
                'application/pdf': '.pdf',
                'application/zip': '.zip',
                'text/plain': '.txt',
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'video/mp4': '.mp4',
                'audio/mpeg': '.mp3',
            }
            ext = ext_map.get(mime_type, '.bin')
            if not file_name.endswith(ext):
                file_name += ext
    elif message.video:
        file_name = message.video.file_name or f"video_{message.id}.mp4"
        mime_type = 'video/mp4'
    elif message.audio:
        file_name = message.audio.file_name or f"audio_{message.id}.mp3"
        mime_type = 'audio/mpeg'
    elif message.photo:
        file_name = f"photo_{message.photo.file_id[:12]}.jpg"
        mime_type = 'image/jpeg'
    else:
        file_name = f"file_{message.id}.bin"

    es_video = bool((mime_type or "").startswith("video/")) or bool(
        file_name and file_name.lower().endswith(_VIDEO_EXTENSIONS)
    )
    return file_name, mime_type, es_video


# ── Helper del canal de almacenamiento y log ────────────────────────
async def _forward_to_storage_channel(client, message, user_id: int,
                                       file_name: str, file_size: int) -> tuple:
    """Reenvía el mensaje original al canal de almacenamiento.
    Devuelve (storage_msg_id, storage_channel_id) o (None, None) si falla.
    No descarga el archivo: usa forward_message que NO consume ancho de banda
    del usuario (Telegram copia server-side).
    """
    if not config.STORAGE_CHANNEL_ID:
        return None, None
    try:
        forwarded = await client.forward_messages(
            chat_id=config.STORAGE_CHANNEL_ID,
            from_chat_id=message.chat.id,
            message_ids=message.id
        )
        # forwarded puede ser un solo Message o una lista
        if isinstance(forwarded, list):
            forwarded = forwarded[0] if forwarded else None
        if forwarded and forwarded.id:
            return forwarded.id, config.STORAGE_CHANNEL_ID
    except RPCError as e:
        logger.warning(f"No se pudo reenviar al canal de almacenamiento: {e}")
    except Exception as e:
        logger.warning(f"Error inesperado en forward: {e}")
    return None, None


async def _log_to_channel(client, text: str) -> None:
    """Envía un mensaje de texto al canal de log (auditoría)."""
    if not config.LOG_CHANNEL_ID:
        return
    try:
        await client.send_message(
            chat_id=config.LOG_CHANNEL_ID,
            text=text,
            disable_web_page_preview=True
        )
    except RPCError as e:
        logger.warning(f"No se pudo enviar al canal de log: {e}")
    except Exception as e:
        logger.warning(f"Error inesperado en log channel: {e}")


# ── Procesado real de un archivo (descarga + auto-subida) ───────────
async def _process_incoming_media(client, message, user_id: int) -> None:
    """Descarga el archivo del mensaje al VPS y sigue el flujo normal.

    Extraído del handler para poder re-procesar los vídeos que quedaron
    "en espera" (release_next_held_video) cuando el anterior termina.
    """
    storage.ensure_user_dirs(user_id)

    # ── Determinar nombre / tipo (estimación de vídeo) ──────────────
    file_name, mime_type, es_video = _extract_media_info(message)
    file_name = storage.sanitize_filename(file_name)

    # ── Determinar tamaño y verificar quota ──────────────────────
    try:
        expected_size = 0
        for attr in ('document', 'video', 'audio', 'photo'):
            obj = getattr(message, attr, None)
            if obj and hasattr(obj, 'file_size'):
                expected_size = obj.file_size or 0
                break
    except Exception:
        expected_size = 0

    # Marcar el pipeline como ocupado lo antes posible (el handler ya lo
    # marcó de forma atómica; aquí se refuerza también para el flujo de
    # re-proceso de vídeos en espera, sin duplicar).
    marked_busy = False
    if config.AUTO_UPLOAD and es_video:
        mark_video_busy(user_id)
        marked_busy = True

    user = await db.get_user(user_id)
    # Auto-registrar si no existe (ej. admin no registrado aún)
    if user is None:
        username = message.from_user.username or None
        await db.add_user(user_id, username, added_by=0)
        user = await db.get_user(user_id)
    quota_mb = (user.get('quota_mb', config.DEFAULT_USER_QUOTA_MB) if user
                else config.DEFAULT_USER_QUOTA_MB)
    is_admin = config.is_admin(user_id)
    if not is_admin and not storage.check_quota(user_id, expected_size, quota_mb):
        used = storage.get_user_usage_bytes(user_id)
        if marked_busy:
            clear_video_busy(user_id)
        try:
            await message.reply(
                f"❌ **Cuota excedida**\n\n"
                f"📊 Tu quota: {quota_mb} MB\n"
                f"💾 Usado: {format_size(used)}\n"
                f"📥 Este archivo: {format_size(expected_size)}\n\n"
                f"Elimina archivos con `/rm` o pide al admin ampliar tu quota."
            )
        except Exception:
            pass
        return

    # ── Generar ruta única en carpeta del usuario ────────────────
    user_dir = storage.get_user_dir(user_id)
    file_path = storage.unique_path(user_dir, file_name)

    # Proceso nuevo: limpiar bandera de cancelación anterior
    clear_cancel(user_id)

    status_msg = await message.reply(
        f"📥 **Descargando...**\n\n"
        f"📄 `{os.path.basename(file_path)}`\n"
        f"⏳ Iniciando...",
        reply_markup=cancel_keyboard(user_id)
    )

    last_update = time.time()
    last_percent = -1.0

    async def progress(current, total):
        nonlocal last_update, last_percent
        # Cancelación: pyrogram captura StopTransmission en la descarga y
        # devuelve None; luego comprobamos la bandera para mostrar 'cancelado'.
        if is_cancelled(user_id):
            client.stop_transmission()
            return
        now = time.time()
        percent = (current / total) * 100 if total else 0
        if (now - last_update >= 2 or abs(percent - last_percent) >= 5
                or current == total):
            try:
                bar = progress_bar(current, total)
                elapsed = now - status_msg.date.timestamp()
                speed = (current / 1024 / 1024 / elapsed) if elapsed > 0 else 0
                remaining = ((total - current) / (speed * 1024 * 1024)) if speed > 0 else 0
                await status_msg.edit_text(
                    f"📥 **Descargando...**\n\n"
                    f"📄 `{os.path.basename(file_path)}`\n"
                    f"📊 {bar}\n"
                    f"💾 {format_size(current)} / {format_size(total)}\n"
                    f"⚡ {speed:.2f} MB/s\n"
                    f"⏱️ {remaining:.0f}s restantes",
                    reply_markup=cancel_keyboard(user_id)
                )
                last_update = now
                last_percent = percent
            except Exception as e:
                logger.debug(f"progress update error: {e}")

    # ── Descarga con reintentos ─────────────────────────────────
    max_attempts = config.MAX_DOWNLOAD_ATTEMPTS
    attempt = 1
    success = False
    while attempt <= max_attempts:
        try:
            await message.download(
                file_name=file_path,
                progress=progress,
                block=True
            )
            # Si el usuario pulsó '❌ Cancelar' (download devuelve None
            # tras StopTransmission), detenerse sin reintentar
            if is_cancelled(user_id):
                break
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                success = True
                break
            logger.warning(f"Descarga intento {attempt}: archivo 0 bytes o ausente")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout intento {attempt}")
            try:
                await status_msg.edit_text(
                    f"⚠️ **Timeout (intento {attempt}/{max_attempts})**\nReintentando en 5s..."
                )
            except Exception:
                pass
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error intento {attempt}: {e}")
            try:
                await status_msg.edit_text(
                    f"⚠️ **Error (intento {attempt}/{max_attempts})**\n{str(e)[:100]}\n"
                    f"Reintentando en 5s..."
                )
            except Exception:
                pass
            await asyncio.sleep(5)
        attempt += 1

    # Cancelación por el usuario: limpiar parcial y avisar
    if is_cancelled(user_id):
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        try:
            await status_msg.edit_text(
                f"🛑 **Descarga cancelada**\n\n"
                f"📄 `{file_name}`\n"
                "⚠️ El archivo **no** se guardó.\n\n"
                "💡 Envíalo de nuevo cuando quieras."
            )
        except Exception:
            pass
        clear_cancel(user_id)
        # El hueco queda libre: dejar pasar al siguiente vídeo en espera
        if marked_busy:
            clear_video_busy(user_id)
            await release_next_held_video(client, user_id)
        return

    if not success:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        try:
            await status_msg.edit_text(
                f"❌ **No se pudo descargar**\n\n"
                f"📄 `{file_name}`\n"
                f"🔄 Intentos fallidos: {max_attempts}\n\n"
                "Posibles causas: conexión inestable, timeout del servidor."
            )
        except Exception:
            pass
        if marked_busy:
            clear_video_busy(user_id)
            await release_next_held_video(client, user_id)
        return

    # ── Reenviar al canal de almacenamiento (sin consumir megas) ─
    storage_msg_id = None
    storage_channel_id = None
    storage_msg_id, storage_channel_id = await _forward_to_storage_channel(
        client, message, user_id, file_name, expected_size
    )

    # ── Registrar en DB ──────────────────────────────────────────
    file_size = os.path.getsize(file_path)
    await db.register_file(
        user_id=user_id,
        file_name=os.path.basename(file_path),
        file_path=file_path,
        file_size=file_size,
        mime_type=mime_type,
        tg_message_id=message.id,
        storage_msg_id=storage_msg_id,
        storage_channel_id=storage_channel_id,
    )

    # ── Enviar al canal de LOG (auditoría) ──────────────────────
    try:
        user_display = (message.from_user.username
                        or message.from_user.first_name or str(user_id))
    except Exception:
        user_display = str(user_id)
    log_text = (
        f"📥 **Archivo recibido**\n\n"
        f"👤 **Usuario:** @{user_display}\n"
        f"🆔 **ID:** `{user_id}`\n"
        f"📄 **Archivo:** `{os.path.basename(file_path)}`\n"
        f"💾 **Tamaño:** {format_size(file_size)}\n"
        f"🗂️ **MIME:** `{mime_type or 'N/A'}`\n"
        f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"💬 **Mensaje ID:** `{message.id}`"
    )
    if storage_msg_id:
        log_text += f"\n📦 **Respaldo:** canal `{storage_channel_id}` msg `{storage_msg_id}`"
    await _log_to_channel(client, log_text)

    # ── Auto-subida (si está activada) ────────────────────────────
    # En vez de guardar y esperar a que el usuario haga /up, se sube
    # automáticamente a la primera revista operativa. Si la subida
    # termina OK, perform_upload_with_bitzero elimina el archivo del
    # VPS (DELETE_AFTER_UPLOAD). Si falla, el archivo queda guardado
    # y se le indica al usuario que puede reintentar con /up.
    if config.AUTO_UPLOAD:
        from handlers.upload import (register_pending_upload,
                                      pending_upload_count, enqueue_upload)
        from video_compressor import is_video_file

        try:
            await status_msg.edit_text(
                f"📥 **Archivo recibido**\n\n"
                f"📄 `{os.path.basename(file_path)}`\n"
                f"💾 {format_size(file_size)}\n\n"
                f"🚀 Subiendo automáticamente...",
                reply_markup=cancel_keyboard(user_id)
            )
        except Exception:
            pass

        # ── Vídeo: preguntar cómo subirlo (original o comprimido) ──
        # Comprimir pesa menos → subida más rápida. El usuario decide.
        if is_video_file(file_path):
            register_pending_upload(user_id, [file_path])
            # Persistir la decisión pendiente: si el bot se reinicia antes de
            # que respondas, se vuelve a preguntar (y NO se descarga el
            # siguiente) en lugar de perder el estado en memoria.
            try:
                await db.save_pending_decision(user_id, [file_path])
            except Exception as e:
                logger.warning(f"no se pudo persistir decisión de {user_id}: {e}")
            pend_count = pending_upload_count(user_id)
            keyboard = [
                [InlineKeyboardButton("📤 Subir original",
                                      callback_data="upmode_orig")],
                [InlineKeyboardButton("🗜️ Comprimir y subir",
                                      callback_data="upmode_comp")],
                [InlineKeyboardButton("❌ Cancelar",
                                      callback_data="cancel_video_decision")],
            ]
            try:
                await status_msg.edit_text(
                    f"🎬 **Vídeo recibido**\n\n"
                    f"📄 `{os.path.basename(file_path)}`\n"
                    f"💾 {format_size(file_size)}\n\n"
                    "🗜️ **Comprimir** re-codifica el vídeo con FFmpeg para "
                    "que pese menos y la **subida sea más rápida** "
                    "(algo de pérdida de calidad).\n"
                    "📤 **Original** lo sube tal cual.\n\n"
                    "¿Cómo quieres subirlo?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                logger.debug(f"vídeo recibido: no se pudo editar mensaje: {e}")
            # El estado 'ocupado' se mantiene: el hueco no se libera hasta que
            # este vídeo se suba (y se borre del disco) o se cancele.
            return

        # La subida pasa por la COLA (control de recursos del VPS):
        # si hay otra subida en curso, esta espera con aviso de posición.
        outcome = await enqueue_upload(
            client, status_msg, user_id, [file_path]
        )
        if outcome in ("failed", "cancelled"):
            # No se subió: el archivo sigue guardado en el VPS.
            # enqueue_upload ya editó el mensaje con el motivo
            # (revista caída / cancelado); aclarar que el archivo NO
            # se perdió.
            try:
                await status_msg.edit_text(
                    status_msg.text
                    + f"\n\n📄 **El archivo quedó guardado** en `raiz/{user_id}/`.\n"
                    + ("💡 Reintenta con `/up` cuando el servidor esté operativo."
                       if outcome == "failed" else
                       "💡 Reintenta con `/up` cuando quieras.")
                )
            except Exception as e:
                logger.debug(f"auto-upload: no se pudo añadir nota: {e}")
        # Si este archivo marcó el pipeline como ocupado (un falso positivo
        # de vídeo que resultó no serlo), liberar el estado; y si se subió
        # (archivo borrado), dejar pasar al siguiente vídeo en espera.
        if marked_busy:
            clear_video_busy(user_id)
            if outcome in ("success", "partial", "parked"):
                await release_next_held_video(client, user_id)
    else:
        try:
            await status_msg.edit_text(
                f"✅ **Archivo guardado**\n\n"
                f"📄 `{os.path.basename(file_path)}`\n"
                f"💾 {format_size(file_size)}\n"
                f"📁 `raiz/{user_id}/`\n"
                f"🔄 Intentos: {attempt}"
                + (f"\n📦 Respaldo en canal" if storage_msg_id else "")
            )
        except Exception:
            pass


# ── Cola de vídeos en espera (pipeline serial) ──────────────────────
async def release_next_held_video(client, user_id: int) -> None:
    """Procesa el siguiente vídeo en espera del usuario, si hay alguno.

    Se llama cuando el vídeo en curso termina y libera espacio: se recupera
    el mensaje guardado (chat_id + message_id) y se descarga/procesa como si
    acabara de llegar. Las entradas huérfanas (mensaje ya no accesible) se
    descartan.
    """
    if not config.AUTO_UPLOAD:
        return
    # Seguridad: si el usuario ya no está autorizado, descartar su cola.
    try:
        if not await db.is_authorized(user_id):
            while await db.pop_first_held_download(user_id):
                pass
            return
    except Exception:
        pass

    for _ in range(20):  # saltar entradas huérfanas sin bucle infinito
        held = await db.pop_first_held_download(user_id)
        if not held:
            return
        try:
            msg = await client.get_messages(held["chat_id"], held["message_id"])
        except Exception as e:
            logger.warning(
                f"release: no se pudo recuperar mensaje {held.get('message_id')} "
                f"de {user_id}: {e}"
            )
            continue
        if msg is None:
            logger.info(f"release: mensaje en espera de {user_id} ya no existe")
            continue
        try:
            await _process_incoming_media(client, msg, user_id)
        except Exception as e:
            logger.exception(f"release: error procesando vídeo en espera de {user_id}: {e}")
        return


async def restore_pending_decisions(client) -> int:
    """Al arrancar el bot, re-pregunta las decisiones de modo pendientes.

    Mientras un usuario tenga una decisión sin responder (su vídeo sigue en
    disco esperando 'comprimir / subir original'), queda marcado como ocupado
    y NO se descarga ningún vídeo en espera. Se llama desde main.py ANTES de
    restore_held_uploads. Devuelve cuántas decisiones se re-anunciaron.
    """
    reanunciadas = 0
    try:
        users = await db.list_pending_decision_users()
    except Exception as e:
        logger.warning(f"restore_decisions: no se pudo leer la BD: {e}")
        return 0
    from handlers.upload import register_pending_upload, pending_upload_count
    for uid in users:
        try:
            row = await db.get_pending_decision(uid)
            if not row:
                continue
            paths = [p for p in row.get('file_paths', []) if os.path.exists(p)]
            if not paths:
                logger.info(f"restore_decisions: vídeos de {uid} ya no existen; fila descartada")
                await db.clear_pending_decision(uid)
                continue
            # Mantener ocupado (no liberar vídeos en espera) y volver a preguntar
            mark_video_busy(uid)
            register_pending_upload(uid, paths)
            try:
                nombre = os.path.basename(paths[0])
                keyboard = [
                    [InlineKeyboardButton("📤 Subir original",
                                          callback_data="upmode_orig")],
                    [InlineKeyboardButton("🗜️ Comprimir y subir",
                                          callback_data="upmode_comp")],
                    [InlineKeyboardButton("❌ Cancelar",
                                          callback_data="cancel_video_decision")],
                ]
                await client.send_message(
                    uid,
                    f"🎬 **Vídeo pendiente de tu decisión**\n\n"
                    f"📄 `{nombre}`\n"
                    "El bot se reinició antes de que eligieras cómo subirlo.\n\n"
                    "🗜️ **Comprimir** re-codifica (pesa menos, sube más rápido).\n"
                    "📤 **Original** lo sube tal cual.\n\n"
                    "¿Cómo quieres subirlo?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                reanunciadas += 1
                logger.info(f"restore_decisions: decisión de {uid} re-anunciada")
            except Exception as e:
                logger.warning(f"restore_decisions: no se pudo avisar a {uid}: {e}")
        except Exception as e:
            logger.warning(f"restore_decisions: error con {uid}: {e}")
    return reanunciadas


async def restore_held_uploads(client) -> int:
    """Al arrancar el bot, reanuda UN vídeo en espera por usuario.

    Se llama desde main.py después de app.start() y después de
    restore_pending_decisions (para no liberar nada mientras un usuario tenga
    una decisión sin responder). Devuelve cuántos se reanudaron.
    """
    reanudados = 0
    try:
        users = await db.list_users_with_held_downloads()
    except Exception as e:
        logger.warning(f"restore_held: no se pudo leer la BD: {e}")
        return 0
    # Usuarios con una subida reanudándose en la cola también se consideran
    # ocupados (su vídeo anterior aún no se resolvió / liberó espacio).
    try:
        subiendo = await db.list_pending_upload_user_ids()
    except Exception:
        subiendo = set()
    for user_id in users:
        try:
            if is_video_busy(user_id) or user_id in subiendo:
                continue
            before = await db.count_held_downloads(user_id)
            await release_next_held_video(client, user_id)
            after = await db.count_held_downloads(user_id)
            if after < before:
                reanudados += 1
                logger.info(f"restore_held: vídeo en espera de {user_id} reanudado")
        except Exception as e:
            logger.warning(f"restore_held: error con {user_id}: {e}")
    return reanudados


async def discard_pending_videos(client, callback_query, user_id: int) -> None:
    """Botón '❌ Cancelar' en la decisión comprimir/original.

    Descarta la selección pendiente: borra del VPS los vídeos que estaban
    esperando tu decisión (y su registro en DB) y libera el hueco para el
    siguiente vídeo en espera. Así no se acumula espacio sin liberar.
    """
    from handlers.upload import consume_pending_upload
    paths = consume_pending_upload(user_id) or []
    # Respaldo en BD: si el estado en memoria ya no está (reinicio o pulsación
    # tardía del botón), la decisión persistida sigue teniendo los archivos y
    # hay que borrarlos igual para no dejar espacio ocupado en el VPS.
    try:
        row = await db.get_pending_decision(user_id)
        for p in (row or {}).get('file_paths') or []:
            if p and p not in paths:
                paths.append(p)
    except Exception as e:
        logger.debug(f"discard: no se pudo leer la decisión en BD: {e}")
    try:
        await db.clear_pending_decision(user_id)
    except Exception as e:
        logger.debug(f"discard: no se pudo limpiar decisión en BD: {e}")
    borrados = 0
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
                borrados += 1
        except OSError as e:
            logger.warning(f"discard: no se pudo borrar {p}: {e}")
        try:
            await db.delete_file_by_path(user_id, p)
        except Exception as e:
            logger.debug(f"discard: no se pudo borrar registro de {p}: {e}")
    clear_video_busy(user_id)
    texto = "❌ **Operación cancelada.**"
    if borrados:
        texto += (
            f"\n\n🗑️ **{borrados} vídeo(s)** pendiente(s) borrado(s) del VPS.\n"
            "💡 Puedes enviarlos de nuevo cuando quieras."
        )
    try:
        await callback_query.message.edit_text(texto)
    except Exception as e:
        logger.debug(f"discard: no se pudo editar mensaje: {e}")
    # Hueco libre → siguiente vídeo en espera
    await release_next_held_video(client, user_id)


def register(app) -> None:

    @app.on_message(filters.document | filters.video | filters.audio | filters.photo)
    @authorized_only
    @safe_handler
    async def save_received_file(client, message):
        user_id = message.from_user.id

        # ── Pipeline serial: si el usuario ya tiene un vídeo en curso,
        #    los nuevos NO se descargan (evita llenar el disco); se guarda
        #    la referencia y se procesan al terminar el anterior.
        if config.AUTO_UPLOAD:
            _, file_name, es_video = _extract_media_info(message)
            if es_video:
                # Chequeo + marca ATOMICOS: no hay ningún await entre medias,
                # así que aunque lleguen varios vídeos casi a la vez (mismo
                # lote/álbum), solo el PRIMERO entra a descargarse; el resto
                # se pone en espera. Antes esto tenía una condición de carrera
                # y varios se descargaban a la vez sin haberse resuelto el
                # anterior.
                if is_video_busy(user_id):
                    try:
                        await db.add_held_download(
                            user_id, message.chat.id, message.id, file_name
                        )
                        n = await db.count_held_downloads(user_id)
                    except Exception as e:
                        logger.warning(f"no se pudo poner vídeo en espera de {user_id}: {e}")
                        return
                    try:
                        await message.reply(
                            f"⏳ **Vídeo en espera de turno**\n\n"
                            f"📄 `{file_name}`\n\n"
                            "Ya tienes otro vídeo en proceso (descargando, esperando "
                            "tu decisión de **comprimir / subir original**, o "
                            "subiéndose).\n"
                            "Para **no llenar el disco**, este vídeo **no se ha "
                            "descargado** todavía.\n\n"
                            f"🕒 Se procesará automáticamente cuando termines el anterior.\n"
                            f"📶 **En espera:** vídeo nº {n}"
                        )
                    except Exception as e:
                        logger.debug(f"aviso de espera: {e}")
                    return
                mark_video_busy(user_id)

        await _process_incoming_media(client, message, user_id)
