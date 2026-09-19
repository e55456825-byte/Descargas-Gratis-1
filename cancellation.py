"""
cancellation.py — Registro central de cancelación de procesos en curso.

Cada proceso largo (subida a revista, descarga BitZero, envío/recepción de
archivos a Telegram) muestra un botón inline "❌ Cancelar". Al pulsarlo, el
callback marca la bandera del usuario y el proceso en curso la comprueba
entre pasos (partes/chunks) para abortar limpiamente:

  - Subidas a revista: se consulta en el progress de chunks.
  - Descarga BitZero: se consulta vía cancel_check dentro del bucle.
  - Transferencias de Telegram: se levanta pyrogram.StopTransmission en el
    progress callback (pyrogram lo captura y devuelve None; luego se
    comprueba la bandera).

La bandera se limpia al INICIAR cada proceso nuevo (clear_cancel) y al
abortar por cancelación, para que no quede "pegada" cancelando procesos
futuros.
"""
from __future__ import annotations

import time
from typing import Dict


class UserCancelledError(Exception):
    """Se lanza cuando el usuario cancela el proceso en curso.

    Los procesos lo capturan para mostrar un mensaje de cancelación
    distinto de un error real (y para NO marcar la revista en cooldown,
    ni reintentar en otro host).
    """


# user_id -> True cuando el usuario pidió cancelar su proceso en curso.
_flags: Dict[int, bool] = {}
# user_id -> timestamp del último request de cancelación (información/depuración)
_last_request: Dict[int, float] = {}


def cancel_keyboard(user_id: int):
    """Teclado inline con el botón '❌ Cancelar' para un usuario.

    La importación de pyrogram es diferida para que los módulos que solo
    necesitan las banderas (ej. bitzero.py, uploader.py) no carguen
    pyrogram innecesariamente.
    """
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_proc_{user_id}")
    ]])


def request_cancel(user_id: int) -> bool:
    """Marca la cancelación del proceso de user_id.

    Devuelve True si YA estaba marcada (doble pulsación).
    """
    already = _flags.get(user_id, False)
    _flags[user_id] = True
    _last_request[user_id] = time.time()
    return already


def is_cancelled(user_id: int) -> bool:
    """True si el usuario pidió cancelar su proceso en curso."""
    return _flags.get(user_id, False)


def clear_cancel(user_id: int) -> None:
    """Limpia la bandera (al iniciar un proceso nuevo o tras cancelar)."""
    _flags.pop(user_id, None)
    _last_request.pop(user_id, None)


def check_cancel(user_id: int) -> None:
    """Lanza UserCancelledError si el usuario pidió cancelar.

    Se invoca en puntos de control del proceso (antes de subir cada
    archivo, etc.).
    """
    if is_cancelled(user_id):
        raise UserCancelledError()
