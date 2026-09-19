"""
handlers/admin.py — Comandos administrativos y panel de control.

Incluye:
  - /addadmin <id> [@username]   → Añade un administrador dinámico
  - /listadmins                  → Lista todos los administradores
  - /deladmin <id>               → Elimina un administrador (no seeds)
  - /adduser /removeuser /listusers /quota /control_panel (heredados)
  - handle_admin_input (panel de revistas)
"""
from __future__ import annotations

import asyncio
import logging
import time

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import db
from utils import (admin_only, clear_admin_state, get_valid_admin_state,
                   safe_handler, set_admin_state)

logger = logging.getLogger(__name__)


def register(app) -> None:

    # ════════════════════════════════════════════════════════════════
    # COMANDOS DE GESTIÓN DE ADMINISTRADORES
    # ════════════════════════════════════════════════════════════════

    @app.on_message(filters.command("addadmin"))
    @admin_only
    @safe_handler
    async def add_admin_handler(client, message):
        """Añade un nuevo administrador dinámico."""
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "❌ **Uso:** `/addadmin <user_id> [@username]`\n\n"
                "**Ejemplo:** `/addadmin 123456789 @usuario`"
            )
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return
        target_username = parts[2].lstrip('@') if len(parts) > 2 else None

        # No permitir añadirse a sí mismo como duplicado
        if target_id == message.from_user.id:
            await message.reply("❌ Ya eres administrador.")
            return

        added = await db.add_admin(target_id, target_username, message.from_user.id)
        if added:
            await message.reply(
                f"✅ **Administrador añadido**\n\n"
                f"👤 **ID:** `{target_id}`\n"
                f"📛 **Usuario:** @{target_username or 'N/A'}\n"
                f"👑 **Añadido por:** `{message.from_user.id}`\n"
                f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Este usuario ahora tiene acceso total al bot."
            )
        else:
            await message.reply(f"❌ El usuario `{target_id}` ya es administrador.")

    # ── /listadmins — ver todos los administradores ──────────────────
    @app.on_message(filters.command("listadmins"))
    @admin_only
    @safe_handler
    async def list_admins_handler(client, message):
        """Lista todos los administradores (seeds + dinámicos)."""
        admins = await db.list_admins()
        if not admins:
            await message.reply("📭 No hay administradores registrados.")
            return

        text = f"👑 **Administradores ({len(admins)}):**\n\n"
        for i, a in enumerate(admins, 1):
            seed_badge = " 🌱" if a.get('is_seed', 0) == 1 else ""
            text += (
                f"{i}. **ID:** `{a['user_id']}`{seed_badge}\n"
                f"   **Usuario:** @{a.get('username') or 'N/A'}\n"
                f"   **Añadido por:** `{a.get('added_by', 'N/A')}`\n"
                f"   **Fecha:** {a.get('added_date', 'N/A')}\n\n"
            )
        text += "🌱 = Admin semilla (de .env, no eliminable)\n"
        text += f"\n💡 **Para quitar:** `/deladmin <user_id>`"
        await message.reply(text)

    # ── /deladmin — eliminar administrador ───────────────────────────
    @app.on_message(filters.command("deladmin"))
    @admin_only
    @safe_handler
    async def del_admin_handler(client, message):
        """Elimina un administrador (no seeds)."""
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                "❌ **Uso:** `/deladmin <user_id>`\n\n"
                "⚠️ No se pueden eliminar administradores semilla (de .env)"
            )
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return

        if target_id in config.SEED_ADMIN_IDS:
            await message.reply(
                "❌ **No se puede eliminar un administrador semilla.**\n\n"
                "Los administradores definidos en `.env` (ADMIN_ID) son permanentes."
            )
            return

        if target_id == message.from_user.id:
            await message.reply("❌ No puedes eliminarte a ti mismo.")
            return

        removed = await db.remove_admin(target_id)
        if removed:
            await message.reply(
                f"✅ **Administrador eliminado**\n\n"
                f"👤 **ID:** `{target_id}`\n"
                f"👑 **Eliminado por:** `{message.from_user.id}`\n"
                f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            await message.reply(
                f"❌ El usuario `{target_id}` no es administrador o es semilla."
            )

    # Alias /removeadmin
    @app.on_message(filters.command("removeadmin"))
    @admin_only
    @safe_handler
    async def remove_admin_alias(client, message):
        await del_admin_handler(client, message)

    # ════════════════════════════════════════════════════════════════
    # COMANDOS DE GESTIÓN DE USUARIOS (heredados)
    # ════════════════════════════════════════════════════════════════

    @app.on_message(filters.command("adduser"))
    @admin_only
    @safe_handler
    async def add_user_admin(client, message):
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("❌ **Uso:** `/adduser <user_id> [@username]`")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return
        target_username = parts[2].lstrip('@') if len(parts) > 2 else None

        added = await db.add_user(target_id, target_username, message.from_user.id)
        if added:
            await message.reply(
                f"✅ **Usuario añadido**\n\n"
                f"👤 **ID:** `{target_id}`\n"
                f"📛 **Usuario:** @{target_username or 'N/A'}\n"
                f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            await message.reply(f"❌ El usuario `{target_id}` ya está autorizado.")

    @app.on_message(filters.command("add"))
    @admin_only
    @safe_handler
    async def add_user_alias(client, message):
        await add_user_admin(client, message)

    @app.on_message(filters.command("removeuser"))
    @admin_only
    @safe_handler
    async def remove_user_admin(client, message):
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("❌ **Uso:** `/removeuser <user_id>`")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return
        if config.is_admin(target_id):
            await message.reply("❌ No puedes eliminar a un administrador. Usa `/deladmin` primero.")
            return
        removed = await db.remove_user(target_id)
        if removed:
            await message.reply(f"✅ **Usuario `{target_id}` eliminado.**")
        else:
            await message.reply(f"❌ El usuario `{target_id}` no existe.")

    @app.on_message(filters.command("ban"))
    @admin_only
    @safe_handler
    async def ban_user_alias(client, message):
        await remove_user_admin(client, message)

    @app.on_message(filters.command("listusers"))
    @admin_only
    @safe_handler
    async def list_users_admin(client, message):
        users = await db.list_users()
        if not users:
            await message.reply("📭 **No hay usuarios autorizados.**")
            return
        text = f"👥 **Usuarios Autorizados ({len(users)}):**\n\n"
        for i, u in enumerate(users, 1):
            status = "✅" if u.get('active', 1) else "❌"
            admin_badge = " 👑" if config.is_admin(u['user_id']) else ""
            text += (
                f"{i}. **ID:** `{u['user_id']}`{admin_badge}\n"
                f"   **Usuario:** @{u.get('username') or 'N/A'}\n"
                f"   **Estado:** {status}\n"
                f"   **Agregado:** {u.get('added_date', 'N/A')}\n"
                f"   **Quota:** {u.get('quota_mb', config.DEFAULT_USER_QUOTA_MB)} MB\n\n"
            )
        await message.reply(text)

    @app.on_message(filters.command("control_panel"))
    @admin_only
    @safe_handler
    async def control_panel(client, message):
        revistas = await db.list_revistas()
        if not revistas:
            await message.reply("📭 No hay revistas configuradas.")
            return
        keyboard = []
        for r in revistas:
            keyboard.append([InlineKeyboardButton(
                f"📚 {r['nombre']} (Modo {r.get('bitzero_mode', 0)})",
                callback_data=f"cp_edit_{r['rev_id']}"
            )])
        keyboard.append([InlineKeyboardButton("❌ Cerrar", callback_data="cp_close")])
        await message.reply(
            "🔧 **Panel de Control de Revistas**\n\n"
            "Selecciona una revista para editar sus parámetros.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    @app.on_message(filters.command("quota"))
    @admin_only
    @safe_handler
    async def quota_handler(client, message):
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("❌ **Uso:** `/quota <user_id> <mb>` (usa -1 para ilimitado)")
            return
        try:
            target_id = int(parts[1])
            new_quota = int(parts[2])
        except ValueError:
            await message.reply("❌ Valores deben ser enteros.")
            return
        if not await db.set_user_quota(target_id, new_quota):
            await message.reply(f"❌ Usuario `{target_id}` no encontrado.")
            return
        await message.reply(f"✅ Quota de `{target_id}` actualizada a {new_quota} MB.")

    # ── handle_admin_input — editor de campos de revistas ────────────
    # ── /broadcast — enviar mensaje a todos los usuarios ──────────────
    @app.on_message(filters.command("broadcast"))
    @admin_only
    @safe_handler
    async def broadcast_handler(client, message):
        """Envía un mensaje a todos los usuarios autorizados del bot."""
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "❌ **Uso:** `/broadcast <mensaje>`\n\n"
                "**Ejemplo:** `/broadcast Hola a todos, el bot se actualizará mañana.`"
            )
            return

        broadcast_text = parts[1]
        users = await db.list_users()
        if not users:
            await message.reply("📭 **No hay usuarios registrados en la base de datos.**")
            return

        status_msg = await message.reply(
            f"📢 **Enviando mensaje a {len(users)} usuarios...**\n\n"
            f"📝 **Mensaje:**\n{broadcast_text[:200]}{'...' if len(broadcast_text) > 200 else ''}"
        )

        success = 0
        failed = 0
        errors = []

        for user in users:
            user_id = user['user_id']
            try:
                await client.send_message(
                    chat_id=user_id,
                    text=(
                        f"📢 **Mensaje del Administrador**\n\n"
                        f"{broadcast_text}\n\n"
                        f"👨‍💻 {config.DEVELOPER_HANDLE}"
                    ),
                    disable_web_page_preview=True
                )
                success += 1
            except Exception as e:
                failed += 1
                errors.append(f"`{user_id}`: {str(e)[:50]}")
                logger.warning(f"Broadcast falló para {user_id}: {e}")

            # Pequeña pausa para evitar rate limiting
            if (success + failed) % 30 == 0:
                await asyncio.sleep(1)

        # Reporte final
        report = (
            f"✅ **Broadcast completado**\n\n"
            f"📊 **Resultados:**\n"
            f"✅ **Enviados:** {success}\n"
            f"❌ **Fallidos:** {failed}\n"
            f"👥 **Total:** {len(users)}\n"
            f"📝 **Mensaje:**\n{broadcast_text[:300]}"
        )

        if errors and len(errors) <= 5:
            report += f"\n\n⚠️ **Errores:**\n" + "\n".join(errors)
        elif errors:
            report += f"\n\n⚠️ **Errores (primeros 5):**\n" + "\n".join(errors[:5])

        await status_msg.edit_text(report)

    @app.on_message(filters.text & filters.private)
    @safe_handler
    async def handle_admin_input(client, message):
        user_id = message.from_user.id

        # Re-verificar admin + TTL
        state = get_valid_admin_state(user_id)
        if not state:
            return

        # Ignorar si parece comando
        if message.text.startswith('/'):
            clear_admin_state(user_id)
            return

        if state.get("action") != "edit_field":
            return

        rev_id = state["rev_id"]
        field = state["field"]
        new_value = message.text.strip()

        revista = await db.get_revista(rev_id)
        if not revista:
            await message.reply("❌ Revista no encontrada.")
            clear_admin_state(user_id)
            return

        if field in ["username", "password", "submission_id", "encryption_key", "nombre",
                     "base_url", "contexto"]:
            await db.update_revista_field(rev_id, field, new_value)
        elif field == "bitzero":
            try:
                val = int(new_value)
                if val not in [0, 1, 2, 3]:
                    raise ValueError
            except ValueError:
                await message.reply("❌ El modo BitZero debe ser 0, 1, 2 o 3. Intenta de nuevo.")
                return
            await db.update_revista_field(rev_id, field, val)
        else:
            await message.reply("❌ Campo no válido.")
            clear_admin_state(user_id)
            return

        # Invalidar uploader cacheado
        from handlers.upload import invalidate_uploader
        invalidate_uploader(rev_id)

        await message.reply(f"✅ Campo **{field}** actualizado correctamente.")
        clear_admin_state(user_id)

        # Re-abrir panel
        await control_panel(client, message)
