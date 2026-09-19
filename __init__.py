"""handlers/ — Módulos de manejo de Pyrogram.

Importar todos los submódulos para registrar los handlers en la app:
    from handlers import register_all
    register_all(app)
"""
from __future__ import annotations

from pyrogram import Client


def register_all(app: Client) -> None:
    """Importa todos los submódulos para que sus decoradores registren handlers."""
    # El orden importa: primero commands y admin (text handlers), luego files
    from handlers import commands, admin, files, callbacks, upload  # noqa: F401
    # Pyrogram registra los handlers al importar los módulos gracias al
    # decorador @app.on_message, pero como `app` se importa de main.py,
    # los módulos deben importarse DESPUÉS de crear `app`.
