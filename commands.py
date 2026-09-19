"""
handlers/commands.py — Comandos generales del bot.

Fixes aplicados aquí:
  - BUG-02: /ls y /rm usan storage.list_user_files con mismo sort_by.
  - BUG-09: /clear_rev implementado con confirmación.
  - Aislamiento por usuario: cada comando sólo opera sobre archivos del usuario.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from pyrogram import filters
from pyrogram.errors import RPCError
from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup)

from cancellation import (UserCancelledError, cancel_keyboard, clear_cancel,
                          is_cancelled)
from config import config
from database import db
from storage import storage
from utils import (admin_only, authorized_only, format_size, progress_bar,
                   safe_handler)

# Límite de Telegram para subir documentos vía Bot API (2 GB)
TG_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024

logger = logging.getLogger(__name__)


def register(app) -> None:
    """Registra todos los handlers de comandos en la app."""

    # ── /start ──────────────────────────────────────────────────────
    @app.on_message(filters.command("start"))
    @safe_handler
    async def start_handler(client, message):
        user_id = message.from_user.id
        authorized = await db.is_authorized(user_id)
        if authorized:
            await message.reply(
                f"👋 **Bienvenido, {message.from_user.first_name}!**\n\n"
                "✅ **Estás autorizado para usar este bot.**\n\n"
                "🤖 **¿Qué hace este bot?**\n"
                "Envías un archivo y el bot te devuelve un enlace de descarga "
                "listo para usar en la aplicación.\n\n"
                "📤 **Cómo usarlo:**\n"
                "1. Envía el archivo que quieras.\n"
                "2. Espera a que termine la subida.\n"
                "3. Copia el enlace que te envía y pégalo en la app.\n\n"
                "¡Empieza enviando un archivo!"
            )
        else:
            await message.reply(
                "🔒 **Bot Privado**\n\n"
                "Este bot es de uso restringido.\n"
                "Para solicitar acceso, contacta al administrador e indica tu ID.\n\n"
                f"📋 **Tu ID:** `{user_id}`"
            )

    # ── /ls — listar archivos del usuario (BUG-02 fix) ──────────────
    @app.on_message(filters.command("ls"))
    @authorized_only
    @safe_handler
    async def list_files(client, message):
        user_id = message.from_user.id
        storage.ensure_user_dirs(user_id)
        files = storage.list_user_files(user_id, sort_by='modified_desc')
        if not files:
            await message.reply(
                f"📭 **Tu carpeta está vacía**\n\n"
                f"📁 **Directorio:** `raiz/{user_id}/`\n"
                "Envía archivos al bot para que aparezcan aquí."
            )
            return

        total_size = sum(f['size'] for f in files)
        text = f"📂 **Tus archivos**\n\n"
        text += f"📊 **Total:** {len(files)} archivos | {format_size(total_size)}\n\n"
        for i, f in enumerate(files[:20], 1):
            mod_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(f['modified']))
            text += f"{i}. **{f['name']}**\n   📏 {format_size(f['size'])} | 📅 {mod_time}\n\n"
        if len(files) > 20:
            text += f"... y {len(files) - 20} archivos más.\n\n"
        text += "💡 **Usa `/rm <número>` para eliminar un archivo.**\n"
        text += f"👨‍💻 **Desarrollador:** {config.DEVELOPER_HANDLE}"
        await message.reply(text)

    # ── /rm — borrar por índice (BUG-02 fix: mismo orden que /ls) ───
    @app.on_message(filters.command("rm"))
    @authorized_only
    @safe_handler
    async def remove_file(client, message):
        user_id = message.from_user.id
        try:
            parts = message.text.split()
            if len(parts) != 2:
                await message.reply("❌ **Uso:** `/rm <número>`")
                return
            idx = int(parts[1])
        except ValueError:
            await message.reply("❌ El número debe ser un entero.")
            return

        # BUG-02 fix: usar storage con mismo sort que /ls
        files = storage.list_user_files(user_id, sort_by='modified_desc')
        if idx < 1 or idx > len(files):
            await message.reply(f"❌ **Índice inválido.** Usa números del 1 al {len(files)}")
            return
        target_path = files[idx - 1]['path']
        deleted = storage.delete_user_file_by_index(user_id, idx)
        if deleted:
            # Mantener la DB (tabla files) consistente con el disco
            await db.delete_file_by_path(user_id, target_path)
            await message.reply(f"✅ **Eliminado:** `{deleted}`")
        else:
            await message.reply("❌ **No se pudo eliminar el archivo.**")

    # ── /deleteall — limpiar carpeta del usuario ────────────────────
    @app.on_message(filters.command("deleteall"))
    @authorized_only
    @safe_handler
    async def delete_all(client, message):
        user_id = message.from_user.id
        count = storage.delete_all_user_files(user_id)
        # Mantener la DB (tabla files) consistente con el disco
        await db.delete_user_files(user_id)
        await message.reply(
            f"✅ **Limpieza completada**\n\n"
            f"🗑️ **Eliminados:** {count} elementos\n"
            f"📁 **Carpeta:** `raiz/{user_id}/`"
        )

    # ── /zips — cambiar tamaño de partes ────────────────────────────
    @app.on_message(filters.command("zips"))
    @authorized_only
    @safe_handler
    async def set_zip_size(client, message):
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("❌ **Uso:** `/zips <tamaño_MB>`")
            return
        try:
            new_size = int(parts[1])
        except ValueError:
            await message.reply("❌ El tamaño debe ser un entero.")
            return
        if not 1 <= new_size <= 100:
            await message.reply("❌ **El tamaño debe estar entre 1 y 100 MB.**")
            return
        config.CHUNK_SIZE_MB = new_size
        await message.reply(f"✅ **Tamaño de partes cambiado a {new_size} MB.**")

    # ── /descargar — descargar URL BitZero y enviarla a Telegram ────
    @app.on_message(filters.command("descargar"))
    @authorized_only
    @safe_handler
    async def descargar_handler(client, message):
        """Descarga el archivo de una URL BitZero y lo envía a Telegram."""
        from bitzero import descargar_bitzero, parse_url
        user_id = message.from_user.id

        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "❌ **Uso:** `/descargar <enlace>`\n\n"
                "📥 Descarga el archivo del enlace y te lo envía a Telegram.\n\n"
                "**Ejemplo:**\n"
                "`/descargar 58780200-5260/1-2-3/1/...`\n\n"
                "⚠️ Límite de Telegram: **2 GB** por archivo."
            )
            return

        url = parts[1]

        # ── Validar URL y tamaño ──────────────────────────────────────
        try:
            info = parse_url(url)
        except Exception as e:
            await message.reply(f"❌ **Enlace de descarga inválido**\n\n`{e}`")
            return

        file_size = info.get('file_size') or 0
        nombre = info.get('original_name') or "archivo.bin"
        if file_size > TG_UPLOAD_LIMIT:
            await message.reply(
                f"❌ **Archivo demasiado grande para Telegram**\n\n"
                f"📄 `{nombre}`\n"
                f"💾 {format_size(file_size)}\n\n"
                f"Telegram solo acepta hasta **2 GB** por archivo.\n"
                f"💡 Usa la app para descargarlo en tu equipo."
            )
            return

        # Proceso nuevo: limpiar bandera de cancelación anterior
        clear_cancel(user_id)

        status_msg = await message.reply(
            f"📥 **Descargando**\n\n"
            f"📄 `{nombre}`\n"
            f"💾 {format_size(file_size)}\n"
            f"🔗 Partes: {len(info['file_ids'])}\n"
            f"⏳ Conectando con el servidor...",
            reply_markup=cancel_keyboard(user_id)
        )

        # ── Descargar a _temp del usuario ────────────────────────────
        temp_dir = storage.get_user_dir(user_id, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = storage.sanitize_filename(nombre)
        if safe_name.startswith('__multi__'):
            safe_name = 'multi_files.tar'
        output_path = storage.unique_path(temp_dir, safe_name)

        loop = asyncio.get_running_loop()
        estado = {"ultima": -1}

        def _progreso(parte_actual: int, total_partes: int, velocidad_mbs: float):
            """Actualiza el mensaje desde el hilo del worker (seguro)."""
            async def _editar():
                try:
                    if parte_actual <= estado["ultima"]:
                        return
                    estado["ultima"] = parte_actual
                    bar = progress_bar(parte_actual, total_partes)
                    await status_msg.edit_text(
                        f"📥 **Descargando**\n\n"
                        f"📄 `{nombre}`\n"
                        f"💾 {format_size(file_size)}\n\n"
                        f"{bar}\n"
                        f"🔗 Parte {parte_actual}/{total_partes}\n"
                        f"⚡ {velocidad_mbs:.1f} MB/s",
                        reply_markup=cancel_keyboard(user_id)
                    )
                except Exception as e:
                    logger.debug(f"progreso descarga: {e}")

            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_editar())
                )
            except Exception as e:
                logger.debug(f"agendar progreso descarga: {e}")

        try:
            resultado = await asyncio.to_thread(
                descargar_bitzero, url, output_path, _progreso,
                cancel_check=lambda: is_cancelled(user_id)
            )
        except UserCancelledError:
            # Usuario pulsó '❌ Cancelar' a mitad de la descarga
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                pass
            await status_msg.edit_text(
                f"🛑 **Descarga cancelada**\n\n"
                f"📄 `{nombre}`\n"
                "⚠️ El archivo **no** se envió a Telegram.\n\n"
                "💡 Puedes volver a intentarlo con `/descargar <URL>` cuando quieras."
            )
            clear_cancel(user_id)
            return
        except Exception as e:
            await status_msg.edit_text(
                f"❌ **Error descargando**\n\n"
                f"📄 `{nombre}`\n"
                f"**Detalle:** `{str(e)[:200]}`\n\n"
                f"🔁 Asegúrate de que el servidor esté operativo e inténtalo de nuevo."
            )
            return
        finally:
            # Bloquear ediciones de progreso pendientes para que no
            # pisen los mensajes que vienen a continuación.
            estado["ultima"] = 10 ** 9

        downloaded_size = resultado.get('file_size', 0)
        if downloaded_size == 0:
            try:
                os.remove(output_path)
            except OSError:
                pass
            await status_msg.edit_text(
                f"❌ **Archivo vacío o incompleto**\n\n"
                f"📄 `{nombre}`\n"
                "La descarga no devolvió datos. Verifica el enlace e inténtalo de nuevo."
            )
            return

        # ── Enviar a Telegram ────────────────────────────────────────
        # Si el usuario canceló justo al terminar la descarga, no enviar
        if is_cancelled(user_id):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                pass
            await status_msg.edit_text(
                f"🛑 **Descarga cancelada**\n\n📄 `{nombre}`\n"
                "⚠️ El archivo **no** se envió a Telegram."
            )
            clear_cancel(user_id)
            return

        await status_msg.edit_text(
            f"📤 **Enviando a Telegram...**\n\n"
            f"📄 `{nombre}`\n"
            f"💾 {format_size(downloaded_size)}\n"
            f"⏳ Subiendo a Telegram (puede tardar)...",
            reply_markup=cancel_keyboard(user_id)
        )

        def _progreso_tg(current: int, total: int):
            """Cancela el envío a Telegram si el usuario pulsa el botón.

            pyrogram captura StopTransmission en send_document y devuelve
            None; después comprobamos la bandera para mostrar 'cancelado'.
            """
            if is_cancelled(user_id):
                client.stop_transmission()

        enviado = False
        try:
            await client.send_document(
                chat_id=message.chat.id,
                document=output_path,
                caption=f"📥 Archivo descargado\n💾 {format_size(downloaded_size)}",
                progress=_progreso_tg
            )
            enviado = True
        except RPCError as e:
            await status_msg.edit_text(
                f"❌ **Telegram rechazó el archivo**\n\n"
                f"**Detalle:** `{e}`\n\n"
                f"Puede superar el límite de 2 GB o el formato no es aceptado."
            )
        except Exception as e:
            await status_msg.edit_text(
                f"❌ **Error enviando a Telegram**\n\n`{str(e)[:200]}`"
            )
        finally:
            # Limpiar el temporal SIEMPRE (éxito, error o cancelación)
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                pass

        if not enviado and is_cancelled(user_id):
            # send_document devuelve None cuando el progress levanta
            # StopTransmission: el envío se detuvo por cancelación.
            # (Solo se muestra 'cancelado' si de verdad NO llegó a enviarse:
            # si el envío ya se completó, se muestra el éxito.)
            await status_msg.edit_text(
                f"🛑 **Envío cancelado**\n\n"
                f"📄 `{nombre}`\n"
                "⚠️ El archivo **no** se envió a Telegram.\n\n"
                "💡 Vuelve a intentarlo con `/descargar <URL>` cuando quieras."
            )
            clear_cancel(user_id)
        elif enviado:
            await status_msg.edit_text(
                f"✅ **Archivo enviado a Telegram**\n\n"
                f"📄 `{nombre}`\n"
                f"💾 {format_size(downloaded_size)}\n"
                f"📥 Revisa el documento arriba ↑"
            )

    # ── /rev_monitor — activar/desactivar monitor de revistas (temporal) ──
    @app.on_message(filters.command("rev_monitor"))
    @admin_only
    @safe_handler
    async def rev_monitor_handler(client, message):
        """Activa/desactiva el monitor periódico de revistas.
        Uso: /rev_monitor on [minutos] | /rev_monitor off
        """
        import revista_monitor as rm

        parts = message.text.split()
        if len(parts) < 2:
            if rm.is_active():
                estado_actual = f"🟢 Activo (cada {rm.get_interval_min()} min)"
            else:
                estado_actual = "⚫ Inactivo"
            await message.reply(
                "❌ **Uso:** `/rev_monitor on [minutos]` o `/rev_monitor off`\n\n"
                "📡 **on** — revisa las revistas cada X minutos (por defecto 15)\n"
                "      y te notifica por DM cuando una cambia de estado\n"
                "      (operativa ↔ caída).\n"
                "📴 **off** — detiene el monitor.\n\n"
                f"📊 **Estado actual:** {estado_actual}\n\n"
                "ℹ️ El monitor arranca **solo** al iniciar el bot (REV_MONITOR_ENABLED="
                "1). Apágalo con `/rev_monitor off`."
            )
            return

        accion = parts[1].lower()
        if accion == "on":
            try:
                intervalo = int(parts[2]) if len(parts) > 2 else 15
            except ValueError:
                intervalo = 15
            if intervalo < 1:
                intervalo = 1
            rm.start(client, message.from_user.id, intervalo)
            await message.reply(
                f"📡 **Monitor de revistas ACTIVADO**\n\n"
                f"⏱️ Revisando cada **{rm.get_interval_min()} minutos**\n"
                f"🔔 Te notificaré aquí por DM solo cuando una revista "
                f"**cambie de estado** (operativa ↔ caída).\n\n"
                f"📴 Para desactivarlo: `/rev_monitor off`"
            )
        elif accion == "off":
            if await rm.stop():
                await message.reply(
                    "📴 **Monitor de revistas DESACTIVADO**\n\n"
                    "Ya no revisaré el estado automáticamente.\n"
                    "Puedes consultarlo manualmente con `/rev_status`."
                )
            else:
                await message.reply(
                    "⚫ **El monitor ya estaba inactivo.**\n"
                    "Actívalo con `/rev_monitor on`."
                )
        else:
            await message.reply("❌ Acción inválida. Usa `on` o `off`.")

    # ── /rev_status — estado actual de las revistas ─────────────────
    @app.on_message(filters.command("rev_status"))
    @admin_only
    @safe_handler
    async def rev_status_handler(client, message):
        """Muestra el estado actual de todas las revistas (con login real)."""
        import revista_monitor as rm
        import asyncio as _asyncio
        from handlers.upload import probe_login_cached

        loop = _asyncio.get_running_loop()
        revistas = await db.list_revistas(only_active=True)
        if not revistas:
            await message.reply("📭 No hay revistas configuradas.")
            return

        status_msg = await message.reply("🔍 **Comprobando revistas...**")
        lineas = ["📡 **Estado de las revistas**\n"]
        for r in revistas:
            # probe_login_cached respeta caché e intervalo mínimo: dos
            # /rev_status seguidos no disparan logins reales.
            ok, probado = await loop.run_in_executor(None, probe_login_cached, r)
            if probado:
                await db.update_revista_login_status(r['rev_id'], ok)
            emoji = "🟢" if ok else "🔴"
            lineas.append(f"{emoji} **{r['nombre']}** (`{r['rev_id']}`) "
                          f"— {'**OPERATIVA**' if ok else '**CAÍDA**'}")

        if rm.is_active():
            lineas.append(
                f"\n📡 Monitor: 🟢 activo (cada {rm.get_interval_min()} min)"
            )
        else:
            lineas.append("\n📡 Monitor: ⚫ inactivo (`/rev_monitor on` para activarlo)")
        lineas.append(f"\n🕐 {time.strftime('%Y-%m-%d %H:%M:%S')}")
        await status_msg.edit_text("\n".join(lineas))

    # ── /status — estado del sistema ────────────────────────────────
    @app.on_message(filters.command("status"))
    @authorized_only
    @safe_handler
    async def status_handler(client, message):
        user_id = message.from_user.id
        user = await db.get_user(user_id)
        usage = storage.get_user_usage_bytes(user_id)
        quota_mb = (user.get('quota_mb', config.DEFAULT_USER_QUOTA_MB) if user
                    else config.DEFAULT_USER_QUOTA_MB)
        is_admin = config.is_admin(user_id)

        stats = await db.get_global_stats()
        revistas = await db.list_revistas()

        text = "📊 **Estado del Sistema**\n\n"
        text += "👤 **Tu cuenta:**\n"
        text += f"   • ID: `{user_id}`\n"
        text += f"   • Admin: {'✅ Sí' if is_admin else '❌ No'}\n"
        text += f"   • Espacio usado: {format_size(usage)}"
        if is_admin:
            text += " (ilimitado)\n"
        else:
            text += f" / {quota_mb} MB\n"
        text += "\n📈 **Global:**\n"
        text += f"   • Usuarios activos: {stats['active_users']}/{stats['total_users']}\n"
        text += f"   • Archivos totales: {stats['total_files']}\n"
        text += f"   • Espacio total: {format_size(stats['total_bytes'])}\n"
        text += f"   • Subidas exitosas: {stats['successful_uploads']}\n\n"
        text += f"📚 **Servidores:** {len(revistas)} activos\n"

        # ── Estado de la cola de subidas (control de recursos) ────────
        from handlers.upload import queue_info
        en_cola, en_curso = queue_info()
        text += f"⏳ **Cola de subidas:** {en_curso} en curso | {en_cola} en cola\n"

        text += f"\n🕐 **Hora:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        text += f"👨‍💻 **Desarrollador:** {config.DEVELOPER_HANDLE}"
        await message.reply(text)

    # ── /bitzero — cambiar modo ─────────────────────────────────────
    @app.on_message(filters.command("bitzero"))
    @authorized_only
    @safe_handler
    async def bitzero_handler(client, message):
        parts = message.text.split()
        if len(parts) < 3:
            revistas = await db.list_revistas()
            rev_list = "\n".join(
                [f"  • `{r['rev_id']}`" for r in revistas]
            )
            await message.reply(
                "❌ **Uso:** `/bitzero <servidor> <modo>`\n\n"
                "**Servidores disponibles:**\n" + rev_list +
                "\n\n**Modos de ofuscación:**\n"
                "  • `0` - Sin ofuscación\n"
                "  • `1` - Ofuscación PNG\n"
                "  • `2` - Ofuscación HTML\n"
                "  • `3` - Ofuscación ZIP (encriptado AES)\n\n"
                f"👨‍💻 **Ejemplo:** `/bitzero KIKI_REV 2`"
            )
            return

        revista_id = parts[1].upper()
        try:
            modo = int(parts[2])
        except ValueError:
            await message.reply("❌ El modo debe ser 0, 1, 2 o 3.")
            return

        revista = await db.get_revista(revista_id)
        if not revista:
            revistas = await db.list_revistas()
            await message.reply(
                f"❌ **Servidor no encontrado:** `{revista_id}`\n\n"
                f"Disponibles: {', '.join(r['rev_id'] for r in revistas)}"
            )
            return

        if modo not in [0, 1, 2, 3]:
            await message.reply("❌ **Modo inválido.** Modos válidos: 0, 1, 2, 3")
            return

        await db.update_revista_field(revista_id, 'bitzero_mode', modo)
        # Invalidar uploader en caché si existe
        from handlers.upload import invalidate_uploader
        invalidate_uploader(revista_id)

        nombres = {0: "Sin ofuscación", 1: "PNG", 2: "HTML", 3: "ZIP (AES)"}
        await message.reply(
            f"✅ **Modo de ofuscación actualizado**\n\n"
            f"📚 **Servidor:** `{revista_id}`\n"
            f"🔐 **Nuevo modo:** {modo} ({nombres[modo]})\n"
            f"🔄 **Próximas subidas** usarán este modo."
        )

    # ── /bitzero_status ──────────────────────────────────────────────
    # ── /up — Subir archivos por índice ─────────────────────────────
    @app.on_message(filters.command("up"))
    @authorized_only
    @safe_handler
    async def up_handler(client, message):
        user_id = message.from_user.id
        parts = message.text.split()
        
        files = storage.list_user_files(user_id, sort_by='modified_desc')
        if not files:
            await message.reply("📭 **No tienes archivos para subir.**")
            return

        # Si no hay argumentos, mostrar listado con índices para seleccionar
        if len(parts) == 1:
            text = "📤 **Selecciona archivos para subir (ej: /up 1,2)**\n\n"
            for i, f in enumerate(files[:20], 1):
                text += f"{i}. **{f['name']}** ({format_size(f['size'])})\n"
            await message.reply(text)
            return

        # Parsear índices (permitir 1,2,3 o 1 2 3)
        indices_str = " ".join(parts[1:]).replace(",", " ")
        try:
            selected_indices = [int(i) - 1 for i in indices_str.split()]
        except ValueError:
            await message.reply("❌ **Índices inválidos.** Usa números separados por espacios o comas.")
            return

        selected_files = []
        for idx in selected_indices:
            if 0 <= idx < len(files):
                selected_files.append(files[idx])
            else:
                await message.reply(f"❌ **Índice inválido:** `{idx + 1}`")
                return

        # Proceso nuevo: limpiar bandera de cancelación anterior
        clear_cancel(user_id)

        file_paths = [f['path'] for f in selected_files]

        # ── Si hay vídeos: preguntar cómo subirlos (original/comprimido) ──
        # La compresión hace que el vídeo pese menos y la subida sea más
        # rápida; el usuario decide por archivo-en-lote.
        from handlers.upload import register_pending_upload, enqueue_upload
        from video_compressor import is_video_file

        videos = [p for p in file_paths if is_video_file(p)]
        if videos:
            register_pending_upload(user_id, file_paths)
            # Persistir la decisión: si el bot se reinicia o el usuario tarda
            # en pulsar, el botón 'Comprimir y subir' sigue funcionando sin
            # rehacer /up (resolve_pending_upload la recupera de la BD).
            try:
                await db.save_pending_decision(user_id, file_paths)
            except Exception as e:
                logger.warning(f"no se pudo persistir decisión de {user_id}: {e}")
            keyboard = [
                [InlineKeyboardButton("📤 Subir original",
                                      callback_data="upmode_orig")],
                [InlineKeyboardButton("🗜️ Comprimir y subir",
                                      callback_data="upmode_comp")],
                [InlineKeyboardButton("❌ Cancelar",
                                      callback_data="cancel_video_decision")],
            ]
            await message.reply(
                f"🎬 **Se detectaron {len(videos)} vídeo(s).**\n\n"
                "🗜️ **Comprimir** re-codifica los vídeos con FFmpeg para "
                "que pesen menos y la **subida sea más rápida** "
                "(algo de pérdida de calidad).\n"
                "📤 **Original** sube los archivos tal cual.\n\n"
                "¿Cómo quieres subirlos?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # ── Sin vídeos: selección automática de la revista + fallback ──
        # enqueue_upload pasa por la COLA de subidas (control de recursos
        # del VPS): prueba el LOGIN y, si la revista elegida falla al SUBIR
        # (ej. HTTP 500 del servidor), la marca en cooldown y prueba
        # automáticamente la siguiente revista operativa. Es la misma
        # lógica que usa la auto-subida al recibir archivos.
        status_msg = await message.reply(
            f"🔍 **Buscando servidor operativo...**\n\n"
            f"📦 **Archivos seleccionados:** {len(selected_files)}\n"
            f"⏳ Probando conexión a los servidores...",
            reply_markup=cancel_keyboard(user_id)
        )

        await enqueue_upload(client, status_msg, user_id, file_paths)

    @app.on_message(filters.command("bitzero_status"))
    @authorized_only
    @safe_handler
    async def bitzero_status_handler(client, message):
        revistas = await db.list_revistas()
        text = "🔐 **Modos de ofuscación por servidor**\n\n"
        for r in revistas:
            modo = r.get('bitzero_mode', 0)
            info = {
                0: "❌ **Desactivado**",
                1: "🖼️ **PNG** (ofuscación básica)",
                2: "🌐 **HTML** (ofuscación avanzada)",
                3: "📦 **ZIP** (encriptado AES-256)"
            }.get(modo, f"❓ Modo {modo}")
            text += f"📚 **`{r['rev_id']}`**\n"
            text += f"   🔐 Modo: {info}\n"
            text += f"   🔑 Clave: {'✅' if r.get('encryption_key') else '❌'}\n\n"
        text += "💡 **Cambiar modo:** `/bitzero <servidor> <modo>`\n"
        text += f"👨‍💻 **Desarrollador:** {config.DEVELOPER_HANDLE}"
        await message.reply(text)

    # ── /history — historial de subidas del usuario ─────────────────
    @app.on_message(filters.command("history"))
    @authorized_only
    @safe_handler
    async def history_handler(client, message):
        user_id = message.from_user.id
        uploads = await db.list_user_uploads(user_id, limit=10)
        if not uploads:
            await message.reply("📭 **No tienes subidas registradas.**")
            return
        text = "📋 **Tus últimas subidas**\n\n"
        for i, u in enumerate(uploads, 1):
            text += (
                f"{i}. **{u['original_name']}**\n"
                f"   📏 {format_size(u['original_size'])}\n"
                f"   📅 {u['uploaded_at']} | ✅ {u['status']}\n\n"
            )
        await message.reply(text)

    # ── /clear_rev — BUG-09 fix: implementado con confirmación ──────
    @app.on_message(filters.command("clear_rev"))
    @authorized_only
    @safe_handler
    async def clear_rev_handler(client, message):
        revistas = await db.list_revistas(only_active=True)
        if not revistas:
            await message.reply("📭 No hay servidores configurados.")
            return
        keyboard = []
        for r in revistas:
            keyboard.append([InlineKeyboardButton(
                f"📚 {r['rev_id']} (sub:{r['submission_id']})",
                callback_data=f"clear_rev_{r['rev_id']}"
            )])
        keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancel_action")])
        await message.reply(
            "⚠️ **Limpiar archivos del servidor**\n\n"
            "Esto eliminará TODOS los archivos subidos a la submission seleccionada.\n"
            "Selecciona el servidor:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── /test_bitzero ────────────────────────────────────────────────
    @app.on_message(filters.command("test_bitzero"))
    @authorized_only
    @safe_handler
    async def test_bitzero_handler(client, message):
        from encoder import BitZeroEncoder
        user_id = message.from_user.id

        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "❌ **Uso:** `/test_bitzero <modo> [archivo_num]`\n\n"
                "**Modos:** 1=PNG, 2=HTML, 3=ZIP\n\n"
                "**Ejemplo:** `/test_bitzero 2 1`"
            )
            return

        try:
            modo = int(parts[1])
            archivo_idx = int(parts[2]) - 1 if len(parts) > 2 else 0
        except ValueError:
            await message.reply("❌ Modo e índice deben ser enteros.")
            return

        if modo not in [1, 2, 3]:
            await message.reply("❌ Modo inválido. Usa 1, 2 o 3.")
            return

        files = storage.list_user_files(user_id, sort_by='modified_desc')
        if not files:
            await message.reply("📭 **No tienes archivos para probar.**")
            return

        if archivo_idx < 0 or archivo_idx >= len(files):
            await message.reply(f"❌ Índice inválido. Usa 1 a {len(files)}")
            return

        file_path = files[archivo_idx]['path']
        file_name = files[archivo_idx]['name']
        original_size = os.path.getsize(file_path)

        status_msg = await message.reply(
            f"🧪 **Probando ofuscación (modo {modo})**\n\n"
            f"📄 **Archivo:** `{file_name}`\n"
            f"💾 **Tamaño:** {format_size(original_size)}\n"
            f"⏳ Procesando..."
        )

        temp_dir = storage.get_user_dir(user_id, 'temp')
        os.makedirs(temp_dir, exist_ok=True)

        try:
            if modo == 1:
                output_path = os.path.join(temp_dir, f"{file_name}.test.png")
                success = BitZeroEncoder.encode_png(file_path, output_path)
                tipo = "PNG"
            elif modo == 2:
                output_path = os.path.join(temp_dir, f"{file_name}.test.html")
                # Buscar primera revista con encryption_key
                revistas = await db.list_revistas()
                enc_key = next((r['encryption_key'] for r in revistas
                                if r.get('encryption_key')), None)
                success = BitZeroEncoder.encode_html(file_path, output_path, enc_key)
                tipo = "HTML"
            else:  # modo 3
                output_path = os.path.join(temp_dir, f"{file_name}.test.zip")
                success = BitZeroEncoder.encode_zip(file_path, output_path, "test_pass_123")
                tipo = "ZIP"

            if success:
                output_size = os.path.getsize(output_path)
                ratio = (output_size / original_size) * 100 if original_size else 0
                await status_msg.edit_text(
                    f"✅ **Prueba de ofuscación completada**\n\n"
                    f"📄 **Archivo:** `{file_name}`\n"
                    f"🔧 **Modo:** {modo} ({tipo})\n"
                    f"📊 **Original:** {format_size(original_size)}\n"
                    f"📈 **Codificado:** {format_size(output_size)}\n"
                    f"📉 **Ratio:** {ratio:.1f}%"
                )
                if output_size < 50 * 1024 * 1024:
                    await client.send_document(
                        chat_id=message.chat.id,
                        document=output_path,
                        caption=f"🧪 Archivo de prueba (modo {modo})"
                    )
            else:
                await status_msg.edit_text(f"❌ **Error en codificación modo {modo}**")
        finally:
            # Limpiar temporal tras 60s
            import asyncio as _aio
            async def _cleanup():
                await _aio.sleep(60)
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except Exception:
                    pass
            _aio.create_task(_cleanup())
