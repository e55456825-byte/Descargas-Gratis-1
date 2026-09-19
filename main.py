"""
main.py — Entry point del Bit Uploader v2.

Inicializa:
  1. Logging estructurado con rotación.
  2. Base de datos SQLite asíncrona.
  3. Admins semilla (de .env) + admins dinámicos (de DB).
  4. Revistas por defecto (si la DB está vacía).
  5. Cliente Pyrogram y registro de handlers.
  6. Pre-login de revistas para detectar credenciales malas temprano.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys

from config import config
from database import db

# ── Logging estructurado con rotación ───────────────────────────────
os.makedirs(config.LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            os.path.join(config.LOGS_DIR, 'bot.log'),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding='utf-8'
        ),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


async def seed_admins() -> None:
    """Asegura que todos los ADMIN_ID (semilla) estén en la tabla admins."""
    for admin_id in config.SEED_ADMIN_IDS:
        # Insertar como seed si no existe
        await db.add_admin(admin_id, username=None, added_by=admin_id, is_seed=True)
    # Cargar todos los admins (semilla + dinámicos) al cache
    await db.load_admins_to_cache()
    logger.info(f"👑 Admins activos: {config.get_all_admins()}")


async def seed_default_revistas() -> None:
    """Si la DB no tiene revistas, inserta las 4 por defecto desde env vars."""
    existing = await db.list_revistas()
    if existing:
        logger.info(f"DB ya tiene {len(existing)} revistas, saltando seed.")
        return

    # Las credenciales se toman de .env (ver .env.example). Sin valores por
    # defecto: cada quien configura las suyas. También se pueden editar luego
    # desde el bot con /rev_status y los comandos de administración.
    defaults = [
        ("KIKI_REV", "Revista Cardiología", "https://revcardiologia.sld.cu",
         "revcardiologia", os.getenv("KIKI_USER", ""),
         os.getenv("KIKI_PASS", ""), os.getenv("KIKI_SUB", ""),
         int(os.getenv("KIKI_BITZERO", "1")), os.getenv("KIKI_KEY", "default_key_1")),
        ("COMED_REV", "Revista COMED", "https://revcocmed.sld.cu",
         "cocmed", os.getenv("COMED_USER", ""),
         os.getenv("COMED_PASS", ""), os.getenv("COMED_SUB", ""),
         int(os.getenv("COMED_BITZERO", "1")), os.getenv("COMED_KEY", "default_key_2")),
        ("LUZ_REV", "Revista LUZ", "https://revistavarela.uclv.edu.cu",
         "uclv", os.getenv("LUZ_USER", ""),
         os.getenv("LUZ_PASS", ""), os.getenv("LUZ_SUB", ""),
         int(os.getenv("LUZ_BITZERO", "1")), os.getenv("LUZ_KEY", "default_key_3")),
        ("Canel_REV", "Revista Canel", "https://rus.ucf.edu.cu",
         "rus", os.getenv("Canel_USER", ""),
         os.getenv("Canel_PASS", ""), os.getenv("Canel_SUB", ""),
         int(os.getenv("Canel_BITZERO", "1")), os.getenv("Canel_KEY", "default_key_4")),
        ("Conrado_REV", "Revista Conrado", "https://conrado.ucf.edu.cu",
         "conrado", os.getenv("Conrado_USER", ""),
         os.getenv("Conrado_PASS", ""), os.getenv("Conrado_SUB", ""),
         int(os.getenv("Conrado_BITZERO", "1")), os.getenv("Conrado_KEY", "default_key_5")),
    ]
    for (rev_id, nombre, base_url, contexto, user, pwd, sub, bz, key) in defaults:
        await db.upsert_revista(
            rev_id, nombre=nombre, base_url=base_url, contexto=contexto,
            username=user, password=pwd, submission_id=sub,
            bitzero_mode=bz, encryption_key=key, active=1
        )
    logger.info(f"Seed completado: {len(defaults)} revistas insertadas.")


async def prelogin_revistas() -> None:
    """Intenta login en cada revista para detectar fallos temprano."""
    from uploader import RevistaUploader
    revistas = await db.list_revistas(only_active=True)
    logger.info(f"Pre-login de {len(revistas)} revistas...")

    loop = asyncio.get_event_loop()
    for r in revistas:
        uploader = RevistaUploader(
            username=r['username'], password=r['password'],
            submission_id=r['submission_id'], base_url=r['base_url'],
            contexto=r['contexto'], bitzero_mode=r.get('bitzero_mode', 0),
            encryption_key=r.get('encryption_key'),
        )
        ok = await loop.run_in_executor(None, uploader.login)
        await db.update_revista_login_status(r['rev_id'], ok)
        if ok:
            logger.info(f"  ✅ {r['nombre']}: login OK")
        else:
            logger.warning(f"  ❌ {r['nombre']}: login fallido")


async def main_async() -> None:
    logger.info("=" * 60)
    logger.info("🚀 Iniciando Bit Uploader v2 (modular)")
    logger.info("=" * 60)

    # 1. Inicializar DB
    await db.init()

    # 2. Seed de admins (semilla + dinámicos) y revistas
    await seed_admins()
    await seed_default_revistas()

    # 3. Pyrogram client
    from pyrogram import Client
    app = Client(
        "revista_bot_v2",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workdir=config.DATA_DIR,
    )

    # 4. Registrar handlers
    from handlers.commands import register as reg_commands
    from handlers.admin import register as reg_admin
    from handlers.files import register as reg_files
    from handlers.callbacks import register as reg_callbacks

    reg_commands(app)
    reg_admin(app)
    reg_files(app)
    reg_callbacks(app)
    logger.info("Handlers registrados: commands, admin, files, callbacks")

    # 5. Pre-login (no bloquear arranque)
    asyncio.create_task(prelogin_revistas())

    # 5b. Monitor de revistas (automático; apagable con /rev_monitor off)
    if config.REV_MONITOR_ENABLED and config.SEED_ADMIN_IDS:
        import revista_monitor
        revista_monitor.start(app, config.SEED_ADMIN_IDS[0],
                             config.REV_MONITOR_INTERVAL_MIN)
        logger.info(f"📡 Monitor de revistas ACTIVO (cada "
                    f"{config.REV_MONITOR_INTERVAL_MIN} min, notifica al admin "
                    f"{config.SEED_ADMIN_IDS[0]})")

    # 6. Stats
    stats = await db.get_global_stats()
    logger.info(f"📊 Stats: {stats['active_users']} usuarios activos, "
                f"{stats['total_admins']} admins, "
                f"{stats['active_revistas']} revistas activas")
    if config.LOG_CHANNEL_ID:
        logger.info(f"📝 Canal de LOG: {config.LOG_CHANNEL_ID}")
    if config.STORAGE_CHANNEL_ID:
        logger.info(f"📦 Canal de almacenamiento: {config.STORAGE_CHANNEL_ID}")

    # 7. Run
    logger.info("✅ Bot listo. Esperando comandos...")
    logger.info("=" * 60)
    await app.start()

    # 7b. Reanudar subidas que quedaron en cola por un reinicio anterior
    from handlers.upload import restore_pending_uploads
    try:
        restauradas = await restore_pending_uploads(app)
        if restauradas:
            logger.info(f"↩️ {restauradas} subida(s) pendiente(s) reanudada(s)")
    except Exception as e:
        logger.warning(f"No se pudieron restaurar subidas pendientes: {e}")

    # 7b2. Re-preguntar decisiones de modo pendientes (para no descargar el
    #      siguiente vídeo mientras el anterior no se haya resuelto)
    from handlers.files import restore_pending_decisions, restore_held_uploads
    from handlers.upload import pending_compressed_retry_loop
    try:
        decisiones = await restore_pending_decisions(app)
        if decisiones:
            logger.info(f"❓ {decisiones} decisión(es) pendiente(s) re-anunciada(s)")
    except Exception as e:
        logger.warning(f"No se pudieron restaurar decisiones pendientes: {e}")

    # 7b3. Reanudar vídeos en espera (cola serial anti-disco-lleno): solo si el
    #      usuario no tiene una decisión sin responder (ya marcado ocupado).
    try:
        reanudados = await restore_held_uploads(app)
        if reanudados:
            logger.info(f"↩️ {reanudados} vídeo(s) en espera reanudado(s)")
    except Exception as e:
        logger.warning(f"No se pudieron reanudar vídeos en espera: {e}")

    # 7b4. Loop que reintenta automáticamente los vídeos ya comprimidos que
    #      no pudieron subirse (revista caída, HTTP 500, FloodWait...): el
    #      original se borró y el comprimido se sube solo en cuanto una
    #      revista vuelva a estar operativa, sin re-comprimir.
    asyncio.create_task(
        pending_compressed_retry_loop(app, config.PENDING_COMPRESSED_RETRY_MIN)
    )

    # 7c. Bucle principal con APAGADO LIMPIO
    # SIGTERM/SIGINT → la cola deja de arrancar subidas nuevas; main espera
    # a que terminen las activas (máx. SHUTDOWN_WAIT_SEC) antes de detenerse.
    # docker-compose tiene stop_grace_period para respetar esa espera.
    from handlers.upload import request_shutdown, wait_until_idle
    loop = asyncio.get_running_loop()
    parar = asyncio.Event()

    def _on_signal(signame: str) -> None:
        logger.info(f"Señal {signame} recibida — apagado limpio en curso...")
        request_shutdown()
        parar.set()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        try:
            loop.add_signal_handler(sig, _on_signal, name)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda _s, _f, n=name: _on_signal(n))

    try:
        while not parar.is_set():
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        request_shutdown()
    finally:
        await wait_until_idle(config.SHUTDOWN_WAIT_SEC)
        # El stop del cliente puede lanzar TimeoutError/ConnectionError si la
        # sesión de Pyrogram ya estaba caída (DC cambiado, red inestable):
        # eso no debe convertirse en un "Error fatal" ni en exit(1) al apagar.
        try:
            await app.stop()
        except Exception as e:
            logger.warning(f"No se pudo detener el cliente limpiamente: {e}")
        try:
            await db.close()
        except Exception as e:
            logger.warning(f"No se pudo cerrar la base de datos limpiamente: {e}")
        logger.info("Bot detenido limpiamente.")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Interrumpido por usuario.")
    except Exception as e:
        logger.exception(f"Error fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
