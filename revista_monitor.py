"""
revista_monitor.py — Monitor periódico de revistas.

Cada N minutos (por defecto 15, configurable con /rev_monitor on <min>)
prueba el login real de cada revista activa (igual que pick_working_revista)
y notifica al admin por DM SOLO cuando el estado CAMBIA:

    🟢 Canel_REV volvió a estar OPERATIVA
    🔴 COMED_REV está CAÍDA (antes: operativa)

Así el admin se entera sin spam (una revista caída no re-notifica cada ciclo).

Temporal: se activa/desactiva en caliente con /rev_monitor on|off.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

from config import config
from database import db
from handlers.upload import probe_login_cached

logger = logging.getLogger(__name__)

# Client de pyrogram y admin a notificar (se inyectan al activar el monitor).
# Variables de módulo: solo hay un monitor a la vez, así que no hace falta
# contextvars (estilo singletons del proyecto: storage, db).
_client = None
_notify_id: Optional[int] = None

# Estado del monitor
_monitor_task: Optional[asyncio.Task] = None
_interval_min: int = 15
_last_state: Dict[str, bool] = {}  # rev_id -> última vez operativa (True/False)


def is_active() -> bool:
    """True si el monitor está corriendo."""
    return _monitor_task is not None and not _monitor_task.done()


def get_interval_min() -> int:
    return _interval_min


async def _notify(text: str) -> None:
    """Envía un DM al admin (best-effort, nunca lanza)."""
    try:
        if not _notify_id or not _client:
            return
        await _client.send_message(chat_id=_notify_id, text=text)
    except Exception as e:
        logger.warning(f"revista_monitor: no se pudo notificar: {e}")


async def _chequear_y_notificar() -> None:
    """Un ciclo: prueba cada revista y notifica SOLO cambios de estado."""
    loop = asyncio.get_running_loop()

    try:
        revistas = await db.list_revistas(only_active=True)
    except Exception as e:
        logger.error(f"revista_monitor: error listando revistas: {e}")
        return

    cambios: list = []
    for r in revistas:
        rev_id = r['rev_id']
        try:
            # probe_login_cached respeta caché e intervalo mínimo: el monitor
            # ya no bombardea las revistas con logins cada ciclo.
            ok, probado = await loop.run_in_executor(None, probe_login_cached, r)
        except Exception as e:
            logger.warning(f"revista_monitor: probe de {rev_id} lanzó excepción: {e}")
            ok = False

        # Reflejar en la DB solo si hubo login real en esta llamada
        if probado:
            try:
                await db.update_revista_login_status(rev_id, ok)
            except Exception:
                pass

        anterior = _last_state.get(rev_id)
        _last_state[rev_id] = ok

        if anterior is not None and ok != anterior:
            cambios.append((rev_id, r['nombre'], ok, anterior))

    # ── Notificar cambios ───────────────────────────────────────────
    if cambios:
        lineas = [f"📡 **Cambio de estado en revistas** ({time.strftime('%H:%M')})\n"]
        for rev_id, nombre, ok, anterior in cambios:
            if ok:
                lineas.append(f"🟢 **{nombre}** (`{rev_id}`) volvió a estar **OPERATIVA**")
            else:
                lineas.append(f"🔴 **{nombre}** (`{rev_id}`) está **CAÍDA**")
        lineas.append("\n📝 Puedes ver el detalle con `/rev_status`")
        await _notify("\n".join(lineas))
        logger.info(f"revista_monitor: {len(cambios)} cambio(s) de estado notificados")

        # Si alguna revista VOLVIÓ a estar operativa, aprovechar para subir
        # ya los vídeos comprimidos que quedaron en espera (en vez de
        # esperar al siguiente ciclo del loop de reintento).
        if any(ok for _, _, ok, _ in cambios) and _client is not None:
            from handlers.upload import retry_pending_compressed_uploads
            asyncio.create_task(retry_pending_compressed_uploads(_client))


async def _bucle(intervalo_min: int) -> None:
    """Bucle principal del monitor."""
    logger.info(f"revista_monitor: iniciado (cada {intervalo_min} min)")
    while True:
        # Primer ciclo: solo establecer línea base (sin notificar)
        await _chequear_y_notificar()
        try:
            await asyncio.sleep(intervalo_min * 60)
        except asyncio.CancelledError:
            logger.info("revista_monitor: detenido")
            raise


def start(client, notify_id: int, intervalo_min: int = 15) -> bool:
    """Arranca el monitor. Devuelve True si se inició (o ya estaba)."""
    global _monitor_task, _interval_min, _client, _notify_id
    if is_active():
        return True
    _interval_min = max(1, intervalo_min)
    _client = client
    _notify_id = notify_id
    _last_state.clear()  # nueva sesión = nueva línea base
    _monitor_task = asyncio.create_task(_bucle(_interval_min))
    return True


async def stop() -> bool:
    """Detiene el monitor (espera la cancelación). Devuelve True si estaba activo."""
    global _monitor_task
    task = _monitor_task
    if task is None or task.done():
        _monitor_task = None
        return False
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    _monitor_task = None
    _last_state.clear()
    return True
