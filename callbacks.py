"""
handlers/callbacks.py — Manejador central de callbacks (inline keyboards).

Rutas:
  - upload_select_<rev_id>     → inicia subida a revista
  - clear_rev_<rev_id>         → pide confirmación para limpiar
  - clear_rev_confirm_<rev_id> → ejecuta limpieza
  - cancel_action              → cancela operación
  - cancel_proc_<user_id>      → cancela el proceso en curso del usuario
                                 (subida a revista / descarga BitZero / envío)
  - cp_edit_<rev_id>           → muestra menú de campos de revista
  - cp_field_<rev_id>_<campo>  → pide nuevo valor
  - cp_test_<rev_id>           → prueba login
  - cp_back / cp_close         → navegación del panel
"""
from __future__ import annotations

import asyncio
import logging

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cancellation import cancel_keyboard, request_cancel
from config import config
from database import db
from storage import storage
from utils import set_admin_state, safe_handler
from handlers.upload import (enqueue_job, get_or_create_uploader,
                              perform_upload_with_bitzero)

logger = logging.getLogger(__name__)

# Campos editables del panel de control de revistas. El orden no importa para
# el parseo: se compara el final del callback contra cada nombre.
PANEL_FIELDS = (
    "username", "password", "submission_id", "bitzero",
    "encryption_key", "base_url", "contexto",
)


def parse_cp_field_data(data: str):
    """Parsea 'cp_field_<rev_id>_<campo>' → (rev_id, campo) o None.

    Tanto el rev_id (ej. KIKI_REV) como algunos campos (submission_id,
    encryption_key) contienen '_', por lo que el campo se detecta
    comparando el final del data contra los nombres de campo válidos.
    """
    if not data.startswith("cp_field_"):
        return None
    campo = next((c for c in PANEL_FIELDS if data.endswith(f"_{c}")), None)
    if not campo:
        return None
    rev_id = data[len("cp_field_"):-len(campo) - 1]
    if not rev_id:
        return None
    return rev_id, campo


async def _safe_answer(callback_query, *args, **kwargs) -> None:
    """Responde al callback sin que un fallo interrumpa el flujo del handler.

    Cuando la sesión de Pyrogram se está reconectando (algo frecuente al
    descargar/subir archivos grandes, que fuerza cambios de DC), Telegram
    puede rechazar el answer con [400 QUERY_ID_INVALID]. Si ese error se
    propagaba, el handler moría a mitad: por ejemplo en los botones
    '📤 Subir original' / '🗜️ Comprimir y subir' el answer se hacía ANTES
    de encolar la subida, así que un fallo aquí perdía la selección y la
    subida nunca arrancaba. Aquí el fallo solo se registra y se sigue.
    """
    try:
        await callback_query.answer(*args, **kwargs)
    except Exception as e:
        logger.debug(f"answer del callback ignorado (no crítico): {e}")


def register(app) -> None:

    @app.on_callback_query()
    @safe_handler
    async def handle_callback(client, callback_query):
        data = callback_query.data or ""
        user_id = callback_query.from_user.id

        # Verificación de autorización
        if not await db.is_authorized(user_id):
            await _safe_answer(callback_query, "❌ No autorizado", show_alert=True)
            return

        # ── Cancelar proceso en curso (botón '❌ Cancelar') ─────────
        # El proceso (subida a revista, descarga BitZero, envío a Telegram,
        # recepción de archivo) comprueba la bandera y aborta limpiamente.
        if data.startswith("cancel_proc_"):
            owner_str = data.replace("cancel_proc_", "")
            try:
                owner = int(owner_str)
            except ValueError:
                await _safe_answer(callback_query, "❌ Datos inválidos", show_alert=True)
                return
            # Solo el dueño del proceso (o un admin) puede cancelarlo
            if user_id != owner and not config.is_admin(user_id):
                await _safe_answer(
                    callback_query,
                    "❌ No puedes cancelar el proceso de otro usuario",
                    show_alert=True
                )
                return
            request_cancel(owner)
            try:
                await callback_query.message.edit_text(
                    "🛑 **Cancelando...**\n\n"
                    "⏳ Deteniendo el proceso en curso..."
                )
            except Exception:
                pass
            await _safe_answer(callback_query, "🛑 Cancelando...", show_alert=True)
            return

        # ── Cancelar decisión de vídeo (borra pendiente y libera turno) ──
        # El botón '❌ Cancelar' de la pregunta '¿comprimir o subir original?'
        # descarta los vídeos pendientes del disco y deja pasar al siguiente
        # vídeo en espera (pipeline serial anti-disco-lleno).
        if data == "cancel_video_decision":
            from handlers.files import discard_pending_videos
            await _safe_answer(callback_query)
            await discard_pending_videos(client, callback_query, user_id)
            return

        # ── Cancelar ────────────────────────────────────────────────
        if data == "cancel_action":
            try:
                await callback_query.message.edit_text("❌ **Operación cancelada.**")
            except Exception as e:
                logger.debug(f"cancel_action: no se pudo editar el mensaje: {e}")
            await _safe_answer(callback_query)
            return

        # ── Subir con modo elegido: original o comprimido ───────────
        # Los botones los muestran /up y la auto-subida (handlers/files.py)
        # cuando hay vídeos: primero se registran los archivos pendientes
        # y aquí se consume la selección para empezar la subida.
        if data in ("upmode_orig", "upmode_comp"):
            from handlers.upload import resolve_pending_upload, enqueue_upload
            # resolve_pending_upload consume la decisión en memoria y, si ya
            # no está (pulsación tardía o reinicio del bot), la recupera de la
            # BD mientras los archivos sigan en disco: así basta con pulsar el
            # botón, sin rehacer /up.
            paths = await resolve_pending_upload(user_id)
            if not paths:
                await _safe_answer(
                    callback_query,
                    "⚠️ La selección expiró. Vuelve a seleccionar los "
                    "archivos (/up) o envíalos de nuevo.",
                    show_alert=True
                )
                return
            comprimir = data == "upmode_comp"
            # El answer puede fallar (QueryIdInvalid) sin que la subida deba
            # perderse: se responde de forma segura y se continúa encolando.
            await _safe_answer(callback_query)
            modo_txt = ("🗜️ **Modo:** Comprimir vídeos antes de subir\n"
                        if comprimir else
                        "📤 **Modo:** Subir originales\n")
            try:
                await callback_query.message.edit_text(
                    f"🔍 **Buscando servidor operativo...**\n\n"
                    f"📦 **Archivos:** {len(paths)}\n"
                    f"{modo_txt}"
                    f"⏳ Probando conexión a los servidores...",
                    reply_markup=cancel_keyboard(user_id)
                )
            except Exception as e:
                logger.warning(f"upmode: no se pudo actualizar el mensaje: {e}")
            # Pipeline serial: mientras se sube este vídeo el usuario queda
            # 'ocupado' (los vídeos nuevos se ponen en espera).
            from handlers.files import (mark_video_busy, clear_video_busy,
                                        release_next_held_video)
            mark_video_busy(user_id)
            outcome = await enqueue_upload(
                client, callback_query.message, user_id, paths,
                compress_videos=comprimir,
            )
            if outcome in ("success", "partial", "parked"):
                # Subido, o comprimido en espera (el original ya se borró) →
                # hueco libre → dejar pasar al siguiente vídeo en espera.
                clear_video_busy(user_id)
                await release_next_held_video(client, user_id)
            else:
                # No se subió: el archivo sigue en disco. Se mantiene 'ocupado'
                # para no llenar el disco; el usuario reintenta con /up o
                # borra con /rm (o cancela la decisión con el botón ❌).
                logger.info(
                    f"Subida de {user_id} terminó en '{outcome}': archivo retenido "
                    f"en disco; los vídeos nuevos seguirán en espera hasta liberarlo."
                )
            return

        # ── Subir a revista ─────────────────────────────────────────
        # ── Subir a revista (por índices) ───────────────────────────
        if data.startswith("up_select_"):
            # formato: up_select_<rev_id>_<idx1>-<idx2>...
            parts = data.split("_")
            # El ID de la revista podría tener guiones, así que tomamos desde la parte 2 hasta la última (que es el índice)
            revista_id = "_".join(parts[2:-1])
            indices_str = parts[-1]
            
            revista = await db.get_revista(revista_id)
            if not revista:
                await _safe_answer(callback_query, "❌ Servidor no encontrado", show_alert=True)
                return
            
            indices = [int(i) for i in indices_str.split("-")]
            files = storage.list_user_files(user_id, sort_by='modified_desc')
            file_paths = [files[i]['path'] for i in indices if 0 <= i < len(files)]
            
            uploader = await get_or_create_uploader(revista)
            try:
                await callback_query.message.edit_text(
                    f"🚀 **Subiendo {len(file_paths)} archivos...**"
                )
            except Exception as e:
                logger.warning(f"up_select: no se pudo actualizar el mensaje: {e}")
            # Pasa por la cola de subidas (control de recursos del VPS)
            await enqueue_job(
                user_id, callback_query.message,
                lambda: perform_upload_with_bitzero(
                    client, callback_query.message, uploader, revista,
                    user_id, file_paths=file_paths
                ),
                label=f"Subida a `{revista_id}`",
            )
            return

        if data.startswith("upload_select_"):
            revista_id = data.replace("upload_select_", "")
            revista = await db.get_revista(revista_id)
            if not revista:
                await _safe_answer(callback_query, "❌ Servidor no encontrado", show_alert=True)
                return

            uploader = await get_or_create_uploader(revista)

            await _safe_answer(callback_query, "Subiendo...")
            try:
                await callback_query.message.edit_text(
                    f"🚀 **Iniciando subida**\n\n"
                    f"🔐 **Ofuscación:** {'✅' if revista.get('bitzero_mode', 0) > 0 else '❌'}"
                    f" (modo {revista.get('bitzero_mode', 0)})\n"
                    f"⏳ Conectando..."
                )
            except Exception as e:
                logger.warning(f"upload_select: no se pudo actualizar el mensaje: {e}")
            # Pasa por la cola de subidas (control de recursos del VPS)
            await enqueue_job(
                user_id, callback_query.message,
                lambda: perform_upload_with_bitzero(
                    client, callback_query.message, uploader, revista, user_id
                ),
                label=f"Subida a `{revista_id}`",
            )
            return

        # ── clear_rev: borrado vía API OJS (solo lo que el bot subió) ──
        # La submission puede estar compartida con otros usuarios, así que se
        # borran ÚNICAMENTE los file_ids que el bot registró en su DB.
        if data.startswith("clear_rev_confirm_"):
            revista_id = data.replace("clear_rev_confirm_", "")
            revista = await db.get_revista(revista_id)
            if not revista:
                await _safe_answer(callback_query, "Revista no encontrada", show_alert=True)
                return

            file_ids = await db.list_revista_upload_file_ids(revista_id)
            if not file_ids:
                await callback_query.message.edit_text(
                    f"✅ **No hay archivos registrados** del bot en `{revista_id}`.\n\n"
                    "No hay nada que borrar (los intentos fallidos no generan archivos)."
                )
                await _safe_answer(callback_query)
                return

            msg = callback_query.message
            await _safe_answer(callback_query, "Borrando...")
            await msg.edit_text(
                f"🗑️ **Borrando {len(file_ids)} archivo(s) de `{revista_id}`**\n\n"
                f"🆔 Submission: `{revista['submission_id']}`\n"
                f"⏳ Solo se borran los archivos que el bot subió (nada de otros usuarios)..."
            )

            uploader = await get_or_create_uploader(revista)
            uploader.reset_batch_state()
            loop = asyncio.get_running_loop()

            total = len(file_ids)
            borrados = 0
            fallidos = 0
            for fid in file_ids:
                if await loop.run_in_executor(None, uploader.delete_submission_file, fid):
                    borrados += 1
                else:
                    fallidos += 1
                if (borrados + fallidos) % 5 == 0:
                    try:
                        await msg.edit_text(
                            f"🗑️ **Borrando...** ({borrados + fallidos}/{total})"
                        )
                    except Exception:
                        pass

            texto = (
                f"🗑️ **Limpieza completada**\n\n"
                f"📚 **Servidor:** `{revista_id}`\n"
                f"🗃️ **Archivos registrados:** {total}\n"
                f"✅ **Borrados:** {borrados}\n"
                f"❌ **Fallidos:** {fallidos}"
            )
            if fallidos:
                texto += (f"\n\n⚠️ **Detalle del último fallo:** "
                          f"`{getattr(uploader, '_last_upload_error', '')[:200]}`")
            await msg.edit_text(texto)
            return

        if data.startswith("clear_rev_"):
            revista_id = data.replace("clear_rev_", "")
            revista = await db.get_revista(revista_id)
            if not revista:
                await _safe_answer(callback_query, "Revista no encontrada", show_alert=True)
                return
            keyboard = [
                [InlineKeyboardButton(
                    f"✅ Sí, eliminar archivos de `{revista_id}`",
                    callback_data=f"clear_rev_confirm_{revista_id}"
                )],
                [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_action")],
            ]
            await callback_query.message.edit_text(
                f"⚠️ **Confirmar limpieza**\n\n"
                f"📚 Servidor: `{revista_id}`\n"
                f"🆔 Submission: `{revista['submission_id']}`\n\n"
                f"Esto eliminará TODOS los archivos subidos a esta submission. "
                f"¿Continuar?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            await _safe_answer(callback_query)
            return

        # ── Panel de control ────────────────────────────────────────
        if not config.is_admin(user_id):
            await _safe_answer(callback_query, "❌ Solo admin", show_alert=True)
            return

        if data.startswith("cp_edit_"):
            rev_id = data.replace("cp_edit_", "")
            revista = await db.get_revista(rev_id)
            if not revista:
                await _safe_answer(callback_query, "Revista no encontrada", show_alert=True)
                return
            keyboard = [
                [InlineKeyboardButton(f"👤 Usuario: {revista['username']}",
                                      callback_data=f"cp_field_{rev_id}_username")],
                [InlineKeyboardButton(f"🔑 Contraseña: {revista['password'][:3]}***",
                                      callback_data=f"cp_field_{rev_id}_password")],
                [InlineKeyboardButton(f"🆔 Submission ID: {revista['submission_id']}",
                                      callback_data=f"cp_field_{rev_id}_submission_id")],
                [InlineKeyboardButton(f"🔐 Modo BitZero: {revista.get('bitzero_mode', 0)}",
                                      callback_data=f"cp_field_{rev_id}_bitzero")],
                [InlineKeyboardButton(
                    f"🔑 Clave: {(revista.get('encryption_key') or 'No configurada')[:10]}",
                    callback_data=f"cp_field_{rev_id}_encryption_key")],
                [InlineKeyboardButton(f"🌐 URL: {revista['base_url']}",
                                      callback_data=f"cp_field_{rev_id}_base_url")],
                [InlineKeyboardButton(f"📝 Contexto: {revista['contexto']}",
                                      callback_data=f"cp_field_{rev_id}_contexto")],
                [InlineKeyboardButton("🧪 Probar conexión",
                                      callback_data=f"cp_test_{rev_id}")],
                [InlineKeyboardButton("🔙 Volver", callback_data="cp_back")],
                [InlineKeyboardButton("❌ Cerrar", callback_data="cp_close")],
            ]
            await callback_query.message.edit_text(
                f"**Editando:** {revista['nombre']}\n\n"
                "Selecciona el campo a modificar:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            await _safe_answer(callback_query)
            return

        if data.startswith("cp_field_"):
            parsed = parse_cp_field_data(data)
            if parsed is None:
                await _safe_answer(callback_query, "Formato inválido", show_alert=True)
                return
            rev_id, field = parsed
            set_admin_state(user_id, {
                "action": "edit_field",
                "rev_id": rev_id,
                "field": field,
            })
            await callback_query.message.edit_text(
                f"✏️ Envía el nuevo valor para **{field}**.\n\n"
                f"Para cancelar, escribe /cancel"
            )
            await _safe_answer(callback_query)
            return

        if data.startswith("cp_test_"):
            rev_id = data.replace("cp_test_", "")
            revista = await db.get_revista(rev_id)
            if not revista:
                await _safe_answer(callback_query, "Revista no encontrada", show_alert=True)
                return
            await _safe_answer(callback_query, "Probando conexión...")
            from uploader import RevistaUploader
            uploader = RevistaUploader(
                username=revista['username'],
                password=revista['password'],
                submission_id=revista['submission_id'],
                base_url=revista['base_url'],
                contexto=revista['contexto'],
                bitzero_mode=revista.get('bitzero_mode', 0),
                encryption_key=revista.get('encryption_key'),
            )
            ok = uploader.login()
            await db.update_revista_login_status(rev_id, ok)
            await callback_query.message.edit_text(
                f"✅ **Conexión exitosa.**" if ok
                else f"❌ **Falló la conexión.** Revisa credenciales."
            )
            return

        if data == "cp_back":
            revistas = await db.list_revistas()
            keyboard = []
            for r in revistas:
                keyboard.append([InlineKeyboardButton(
                    f"📚 {r['nombre']} (Modo {r.get('bitzero_mode', 0)})",
                    callback_data=f"cp_edit_{r['rev_id']}"
                )])
            keyboard.append([InlineKeyboardButton("❌ Cerrar", callback_data="cp_close")])
            await callback_query.message.edit_text(
                "🔧 **Panel de Control de Revistas**\n\n"
                "Selecciona una revista para editar sus parámetros.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            await _safe_answer(callback_query)
            return

        if data == "cp_close":
            try:
                await callback_query.message.delete()
            except Exception:
                pass
            await _safe_answer(callback_query, "Panel cerrado")
            return
