"""
utils.py — Funciones auxiliares y decoradores compartidos.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from typing import Any, Callable

from database import db
from config import config

logger = logging.getLogger(__name__)


# ── Formato de tamaños ──────────────────────────────────────────────
def format_size(size_bytes: int) -> str:
    """Formato humano: 1024 -> '1.00 KB'."""
    if size_bytes == 0:
        return "0 B"
    names = ("B", "KB", "MB", "GB", "TB", "PB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    i = min(i, len(names) - 1)
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {names[i]}"


def sizeof_fmt(num: float, suffix: str = 'B') -> str:
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return f"{num:3.2f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.2f}Yi{suffix}"


def progress_bar(current: int, total: int, length: int = 20) -> str:
    """Barra de progreso ASCII."""
    if total <= 0:
        return "[" + "▁" * length + "] 0%"
    filled = int(length * current / total)
    bar = "█" * filled + "▁" * (length - filled)
    pct = (current / total) * 100
    return f"[{bar}] {pct:.1f}%"


# ── Decoradores ─────────────────────────────────────────────────────
def authorized_only(func: Callable) -> Callable:
    """Verifica que el usuario esté autorizado en la DB."""
    @functools.wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        if not await db.is_authorized(user_id):
            await message.reply(
                "❌ **Acceso Denegado**\n\n"
                "No tienes permisos para usar este bot.\n"
                "Contacta al administrador para solicitar acceso.\n\n"
                f"📋 **Tu ID:** `{user_id}`\n"
                f"👨‍💻 **Soporte:** {config.DEVELOPER_HANDLE}"
            )
            return
        # Actualizar último acceso
        asyncio.create_task(db.update_last_access(user_id))
        return await func(client, message, *args, **kwargs)
    return wrapper


def admin_only(func: Callable) -> Callable:
    """Verifica que el usuario sea administrador."""
    @functools.wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        if not config.is_admin(user_id):
            await message.reply("❌ **Solo administradores pueden usar este comando.**")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper


def safe_handler(func: Callable) -> Callable:
    """Captura excepciones no manejadas y notifica al usuario sin stacktrace."""
    @functools.wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        try:
            return await func(client, message, *args, **kwargs)
        except Exception as e:
            logger.exception(f"Error en handler {func.__name__}: {e}")
            try:
                await message.reply(
                    f"❌ **Error inesperado**\n\n"
                    f"No se pudo completar la operación.\n"
                    f"**Error:** `{str(e)[:100]}`"
                )
            except Exception:
                pass
    return wrapper


# ── Estado de admin (con TTL) ───────────────────────────────────────
# BUG-11 fix: re-verificar admin + TTL
admin_states: dict[int, dict[str, Any]] = {}


def set_admin_state(user_id: int, state: dict) -> None:
    """Crea o actualiza estado de admin con timestamp."""
    state['created_at'] = time.time()
    admin_states[user_id] = state


def get_valid_admin_state(user_id: int) -> dict | None:
    """Devuelve el estado si existe, es admin y no ha expirado."""
    if not config.is_admin(user_id):
        admin_states.pop(user_id, None)
        return None
    state = admin_states.get(user_id)
    if not state:
        return None
    if time.time() - state.get('created_at', 0) > config.ADMIN_STATE_TTL_SEC:
        admin_states.pop(user_id, None)
        return None
    return state


def clear_admin_state(user_id: int) -> None:
    admin_states.pop(user_id, None)
