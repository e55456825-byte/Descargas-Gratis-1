"""
config.py — Configuración central del bot.

Lee variables de entorno, expone parámetros globales y bootstrap de la DB.
La configuración de revistas se carga desde la DB (no desde JSON) para
garantizar consistencia transaccional y concurrencia segura.
"""
from __future__ import annotations

import logging
import os
from typing import List

from dotenv import load_dotenv

# Carga .env si existe (token, admins, revistas...) para que el bot funcione
# igual se lance con start.sh, python main.py o Docker.
load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    """Configuración global cargada desde variables de entorno."""

    def __init__(self) -> None:
        # ── Credenciales Telegram ──────────────────────────────────────
        # Sin valores por defecto: cada despliegue usa sus propias
        # credenciales, definidas en el archivo .env (ver .env.example).
        self.API_ID: int = int(os.getenv("API_ID", "0"))
        self.API_HASH: str = os.getenv("API_HASH", "")
        self.BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

        # ── Administradores iniciales (separados por coma) ────────────
        # Estos son los SUPER-ADMIN semilla. Otros admins se añaden vía
        # /addadmin y se guardan en la tabla `admins` de la DB.
        admin_ids_str = os.getenv("ADMIN_ID", "")
        self.SEED_ADMIN_IDS: List[int] = [
            int(i.strip()) for i in admin_ids_str.split(",") if i.strip()
        ]

        # ── Directorios ────────────────────────────────────────────────
        self.ROOT_DIR: str = os.getenv("ROOT_DIR", "raiz")
        self.DATA_DIR: str = os.getenv("DATA_DIR", "data")
        self.LOGS_DIR: str = os.getenv("LOGS_DIR", "logs")

        # ── Base de datos ──────────────────────────────────────────────
        self.DB_PATH: str = os.path.join(self.DATA_DIR, "bot.db")

        # ── Chunking y descargas ───────────────────────────────────────
        self.CHUNK_SIZE_MB: int = int(os.getenv("CHUNK_SIZE_MB", "20"))
        self.DOWNLOAD_TIMEOUT: int = int(os.getenv("DOWNLOAD_TIMEOUT", "600"))
        self.MAX_DOWNLOAD_ATTEMPTS: int = int(os.getenv("MAX_DOWNLOAD_ATTEMPTS", "5"))
        self.DOWNLOAD_CHUNK_SIZE: int = int(os.getenv("DOWNLOAD_CHUNK_SIZE", "32768"))

        # ── Quotas por usuario ─────────────────────────────────────────
        self.DEFAULT_USER_QUOTA_MB: int = int(os.getenv("DEFAULT_USER_QUOTA_MB", "1024"))

        # ── BitZero ────────────────────────────────────────────────────
        self.BITZERO_SIGNATURE: str = os.getenv("BITZERO_SIG", "@bitzero#2024")
        # Vacío por defecto: las URLs se generan SIN dominio (código de descarga
        # anónimo). Si quieres un dominio visible, pon BITZERO_HOST en .env.
        self.BITZERO_FAKE_HOST: str = os.getenv("BITZERO_HOST", "")

        # ── Estado de admin (TTL) ──────────────────────────────────────
        self.ADMIN_STATE_TTL_SEC: int = int(os.getenv("ADMIN_STATE_TTL_SEC", "300"))

        # ── Monitor de revistas ────────────────────────────────────────
        # Arranca automáticamente con el bot (se apaga con /rev_monitor off)
        self.REV_MONITOR_ENABLED: bool = os.getenv("REV_MONITOR_ENABLED", "1") != "0"
        # Intervalo por defecto en minutos para el monitor automático
        self.REV_MONITOR_INTERVAL_MIN: int = int(os.getenv("REV_MONITOR_INTERVAL_MIN", "15"))

        # ── Tolerancia a fallos de subida (fallback automático) ────────
        # Minutos que una revista queda excluida de /up tras un fallo de
        # subida, para no re-elegir una revista cuya API de subida está rota
        # (ej. HTTP 500 del servidor) mientras otras funcionan.
        self.UPLOAD_FAIL_COOLDOWN_MIN: int = int(os.getenv("UPLOAD_FAIL_COOLDOWN_MIN", "15"))

        # ── Reintentos automáticos cuando TODAS las revistas fallan ────
        # Las revistas (OJS cubanas) son intermitentes: se caen y vuelven en
        # minutos. Si en una ronda ninguna logra subir, el bot espera y
        # reintenta solo en vez de fallar de inmediato. UPLOAD_RETRY_ATTEMPTS
        # = número de reintentos DESPUÉS del primer intento (0 = desactivado);
        # la espera se duplica en cada ronda (120s → 240s → 480s...).
        self.UPLOAD_RETRY_ATTEMPTS: int = max(
            0, int(os.getenv("UPLOAD_RETRY_ATTEMPTS", "3"))
        )
        self.UPLOAD_RETRY_WAIT_SEC: int = max(
            10, int(os.getenv("UPLOAD_RETRY_WAIT_SEC", "120"))
        )

        # ── Auto-subida y borrado del VPS ──────────────────────────────
        # AUTO_UPLOAD=1: al recibir un archivo se sube automáticamente a la
        # primera revista operativa (sin esperar a que el usuario haga /up).
        self.AUTO_UPLOAD: bool = os.getenv("AUTO_UPLOAD", "1") != "0"

        # ── Cola de subidas (control de recursos del VPS) ──────────────
        # La compresión FFmpeg y los chunks HTTP consumen mucha CPU/red:
        # con la cola, solo corren MAX_CONCURRENT_UPLOADS subidas a la vez
        # y el resto espera su turno (con aviso de posición). Por defecto
        # 1 = subidas estrictamente secuenciales.
        self.UPLOAD_QUEUE_ENABLED: bool = os.getenv("UPLOAD_QUEUE_ENABLED", "1") != "0"
        self.MAX_CONCURRENT_UPLOADS: int = max(
            1, int(os.getenv("MAX_CONCURRENT_UPLOADS", "1"))
        )

        # ── Apagado limpio ────────────────────────────────────────────
        # Segundos que el bot espera a que terminen las subidas activas
        # al recibir SIGTERM/SIGINT antes de detenerse. Debe ser MENOR
        # que stop_grace_period de docker-compose.yml (30m).
        self.SHUTDOWN_WAIT_SEC: int = max(
            1, int(os.getenv("SHUTDOWN_WAIT_SEC", "1500"))
        )
        # DELETE_AFTER_UPLOAD=1: tras subir con éxito, borra el archivo del
        # VPS (disco) y su registro en la tabla files para ahorrar espacio.
        self.DELETE_AFTER_UPLOAD: bool = os.getenv("DELETE_AFTER_UPLOAD", "1") != "0"

        # ── Decisión pendiente comprimir/original ──────────────────────
        # Segundos que vale la selección en MEMORIA mientras el usuario
        # decide "📤 Subir original" o "🗜️ Comprimir y subir". La fuente de
        # verdad es la tabla pending_decisions (sobrevive reinicios), así
        # que este TTL solo acota el dict en RAM. Antes eran 10 minutos
        # fijos: pasado ese tiempo, al pulsar el botón el bot decía "la
        # selección expiró" y obligaba a rehacer /up aunque el archivo
        # siguiera en disco. Ahora, por defecto, 24 horas.
        self.PENDING_MODE_TTL_SEC: int = max(
            60, int(os.getenv("PENDING_MODE_TTL_SEC", "86400"))
        )

        # ── Compresión de vídeo antes de subir (opcional) ────────────────
        # Cuando el usuario elige "🗜️ Comprimir y subir", el vídeo se
        # re-codifica con FFmpeg (libx264) antes de subirlo: pesa menos,
        # así que la subida es más rápida (menos chunks / ancho de banda).
        self.FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")
        self.FFPROBE_PATH: str = os.getenv("FFPROBE_PATH", "ffprobe")
        # CRF x264: 18-23 casi sin pérdida, 23-28 estándar web, 28+ más
        # pequeño pero con pérdida visible. 27 = subida rápida con calidad
        # aceptable.
        self.COMPRESS_VIDEO_CRF: int = int(os.getenv("COMPRESS_VIDEO_CRF", "27"))
        # Preset x264: más lento ⇒ menor tamaño a la misma calidad.
        self.COMPRESS_VIDEO_PRESET: str = os.getenv("COMPRESS_VIDEO_PRESET", "veryfast")
        # Altura máxima tras comprimir (0 = conservar resolución original).
        # Bajar de 1080p a 720p es el mayor ahorro de tamaño.
        self.COMPRESS_VIDEO_MAX_HEIGHT: int = int(os.getenv("COMPRESS_VIDEO_MAX_HEIGHT", "720"))
        # Bitrate de audio AAC (solo aplica si el vídeo tiene audio).
        self.COMPRESS_VIDEO_AUDIO_BITRATE: str = os.getenv("COMPRESS_VIDEO_AUDIO_BITRATE", "96k")
        # Solo se comprime (o se ofrece comprimir) vídeos de al menos este
        # tamaño en MB: comprimir vídeos pequeños casi no ahorra y tarda.
        self.COMPRESS_MIN_SIZE_MB: int = int(os.getenv("COMPRESS_MIN_SIZE_MB", "10"))
        # Si el resultado no es al menos un X% más pequeño que el original,
        # se sube el original (evita subir un archivo más grande).
        self.COMPRESS_MIN_SAVING_PCT: int = int(os.getenv("COMPRESS_MIN_SAVING_PCT", "10"))

        # ── Reintento automático de comprimidos en espera ──────────────
        # Si un vídeo ya comprimido no pudo subirse (revista caída, HTTP 500,
        # FloodWait de Telegram...), se borra el ORIGINAL y el comprimido
        # queda en espera. Cada este intervalo (minutos) el bot comprueba si
        # alguna revista está operativa y lo sube solo, sin re-comprimir.
        self.PENDING_COMPRESSED_RETRY_MIN: int = max(
            1, int(os.getenv("PENDING_COMPRESSED_RETRY_MIN", "15"))
        )

        # ── Control de carga de logins (anti-scanner) ──────────────────
        # El bot sondea login de las revistas en cada /up, /rev_status y
        # ciclo del monitor (20-40 peticiones/hora). Cachear resultados y
        # respetar un intervalo mínimo por revista evita saturar los
        # servidores OJS y que sus firewalls bloqueen la IP del bot.
        self.LOGIN_PROBE_CACHE_TTL_SEC: int = int(os.getenv("LOGIN_PROBE_CACHE_TTL_SEC", "300"))
        self.LOGIN_PROBE_MIN_INTERVAL_SEC: int = int(os.getenv("LOGIN_PROBE_MIN_INTERVAL_SEC", "60"))

        # ── Identidad del bot ──────────────────────────────────────────
        self.DEVELOPER_HANDLE: str = os.getenv("DEVELOPER_HANDLE", "@Emanuel14APK")

        # ── Canales Telegram ───────────────────────────────────────────
        # Canal donde se LOG todo lo que se sube (texto/audit)
        # Formato: -1001234567890 (channel ID con -100 prefix)
        self.LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0"))

        # Canal privado donde se REENVÍA todo archivo recibido por el bot,
        # para tener backup sin consumir megas del usuario (file_id ref).
        self.STORAGE_CHANNEL_ID: int = int(os.getenv("STORAGE_CHANNEL_ID", "0"))

        # ── Crear directorios base ─────────────────────────────────────
        self._ensure_dirs()

        # ── Cache en memoria de admins (se llena desde DB en startup) ──
        self._admins_cache: set[int] = set(self.SEED_ADMIN_IDS)

    def _ensure_dirs(self) -> None:
        for d in (self.ROOT_DIR, self.DATA_DIR, self.LOGS_DIR):
            os.makedirs(d, exist_ok=True)
        logger.info(f"Directorios inicializados: root={self.ROOT_DIR} data={self.DATA_DIR} logs={self.LOGS_DIR}")

    # ── Helpers de admins (cache en memoria + DB) ────────────────────
    def is_admin(self, user_id: int) -> bool:
        """Verifica si un user_id es admin (cache en memoria)."""
        return user_id in self._admins_cache

    def add_admin_to_cache(self, user_id: int) -> None:
        self._admins_cache.add(user_id)

    def remove_admin_from_cache(self, user_id: int) -> None:
        # Nunca remover los SUPER-ADMIN semilla
        if user_id in self.SEED_ADMIN_IDS:
            return
        self._admins_cache.discard(user_id)

    def get_all_admins(self) -> List[int]:
        return sorted(self._admins_cache)


# Singleton global
config = Config()
