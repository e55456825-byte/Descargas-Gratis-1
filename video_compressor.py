"""
video_compressor.py — Compresión de vídeo con FFmpeg (libx264).

Se usa cuando el usuario elige "🗜️ Comprimir y subir": antes de subir el
vídeo a la revista se re-codifica con FFmpeg para que pese menos y la
subida sea más rápida (menos chunks, menos ancho de banda).

Parámetros (config, ver config.py):
  - CRF (calidad): 18-23 casi sin pérdida, 23-28 estándar web, 28+ más
    pequeño pero con pérdida visible. Default 27 (subida rápida).
  - Preset x264: más lento ⇒ menor tamaño a la misma calidad.
    Default 'veryfast' (buen equilibrio tiempo/tamaño).
  - Altura máxima: si el vídeo original supera COMPRESS_VIDEO_MAX_HEIGHT,
    se baja de resolución (conservando aspecto) — el mayor ahorro de
    tamaño viene de aquí. 0 = conservar resolución original.
  - Audio: se re-codifica a AAC con COMPRESS_VIDEO_AUDIO_BITRATE.

Si ffmpeg/ffprobe no están instalados, o el resultado no ahorra al menos
un X% (COMPRESS_MIN_SAVING_PCT), el flujo de subida usa el original.

Solo depende de la CLI de ffmpeg (no de librerías Python extra): se lanza
con subprocess y se parsea el progreso con `-progress pipe:1`
(fix barra de progreso): ffmpeg escribe el progreso como líneas
`key=value` terminadas en \n (out_time_us, progress=continue/end) en
stdout, que se leen en tiempo real con readline().

ANTES se parseaba el stderr con un regex sobre `time=HH:MM:SS`, pero
ffmpeg separa esas estadísticas con `\r` (no `\n`) cuando stderr no es
un terminal, así que readline() las acumulaba sin devolverlas y la barra
quedaba congelada en "⏳ Analizando..." hasta el final de la compresión.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

from cancellation import UserCancelledError
from config import config

logger = logging.getLogger(__name__)

# Extensiones tratadas como vídeo (los contenedores más comunes).
VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v',
    '.mpg', '.mpeg', '.3gp', '.ts', '.mts', '.m2ts', '.vob', '.ogv',
}


def is_video_file(path: str) -> bool:
    """True si la extensión del archivo es de un contenedor de vídeo."""
    ext = os.path.splitext(path or "")[1].lower()
    return ext in VIDEO_EXTENSIONS


def ffmpeg_available() -> bool:
    """True si el binario ffmpeg existe en el PATH (o FFMPEG_PATH)."""
    return shutil.which(config.FFMPEG_PATH or "ffmpeg") is not None


def ffprobe_available() -> bool:
    """True si el binario ffprobe existe en el PATH (o FFPROBE_PATH)."""
    return shutil.which(config.FFPROBE_PATH or "ffprobe") is not None


def probe_video(path: str) -> Optional[Dict]:
    """Info del vídeo vía ffprobe (JSON). None si falla o no hay ffprobe.

    Devuelve {duration, width, height, has_audio, size}.
    """
    if not ffprobe_available():
        return None
    try:
        cmd = [
            config.FFPROBE_PATH or "ffprobe", "-v", "error",
            "-print_format", "json", "-show_format", "-show_streams", path,
        ]
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
        )
        if proc.returncode != 0:
            logger.warning(f"ffprobe falló para {path}: {proc.stderr[:200]}")
            return None
        data = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
        streams = data.get("streams", [])
        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = None
        if data.get("format", {}).get("duration"):
            try:
                duration = float(data["format"]["duration"])
            except (TypeError, ValueError):
                duration = None
        if duration is None and vstream and vstream.get("duration"):
            try:
                duration = float(vstream["duration"])
            except (TypeError, ValueError):
                duration = None
        return {
            "duration": duration,
            "width": int(vstream["width"]) if vstream and vstream.get("width") else None,
            "height": int(vstream["height"]) if vstream and vstream.get("height") else None,
            "has_audio": has_audio,
            "size": int(data.get("format", {}).get("size") or 0),
        }
    except Exception as e:
        logger.warning(f"probe_video error en {path}: {e}")
        return None


def build_ffmpeg_command(input_path: str, output_path: str,
                         crf: int, preset: str, audio_bitrate: str,
                         width: Optional[int] = None,
                         height: Optional[int] = None,
                         max_height: Optional[int] = 0) -> List[str]:
    """Construye la línea de comandos ffmpeg (función pura, testeable).

    - Re-codifica vídeo a H.264 (libx264) con CRF + preset.
    - Si el alto original supera max_height (y max_height > 0), escala a
      max_height conservando aspecto y con dimensiones pares (yuv420p).
    - Audio AAC a audio_bitrate (solo si el input tiene audio: -map 0:a:0?).
    - -movflags +faststart para que el MP4 se pueda reproducir/descargar
      mientras se transfiere (moov al inicio).
    """
    cmd = [
        config.FFMPEG_PATH or "ffmpeg",
        "-hide_banner", "-loglevel", "info",
        "-y",
        "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
    ]
    if max_height and height and width and height > max_height:
        new_h = int(max_height)
        new_w = int(round(width * new_h / height))
        if new_w % 2:
            new_w += 1  # yuv420p exige dimensiones pares
        cmd += ["-vf", f"scale={new_w}:{new_h}"]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-sn",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def compress_video(input_path: str, output_path: str,
                   on_progress: Optional[Callable[[float], None]] = None,
                   cancel_check: Optional[Callable[[], bool]] = None,
                   crf: Optional[int] = None,
                   preset: Optional[str] = None,
                   audio_bitrate: Optional[str] = None,
                   max_height: Optional[int] = None) -> Optional[Dict]:
    """Comprime un vídeo con FFmpeg. Devuelve stats o None si falla.

    - on_progress(pct): se invoca (throttled) con el % completado; si
      lanza UserCancelledError se aborta y se propaga.
    - cancel_check(): si devuelve True a mitad de la codificación, se
      mata el proceso y se lanza UserCancelledError.
    - Los valores None usan los defaults de config.
    """
    if not ffmpeg_available():
        logger.error("ffmpeg no está disponible en el servidor")
        return None

    crf = int(crf if crf is not None else config.COMPRESS_VIDEO_CRF)
    preset = preset or config.COMPRESS_VIDEO_PRESET
    audio_bitrate = audio_bitrate or config.COMPRESS_VIDEO_AUDIO_BITRATE
    if max_height is None:
        max_height = int(config.COMPRESS_VIDEO_MAX_HEIGHT)

    orig_size = os.path.getsize(input_path)
    probe = probe_video(input_path)
    duration = probe.get("duration") if probe else None

    cmd = build_ffmpeg_command(
        input_path, output_path, crf, preset, audio_bitrate,
        width=(probe or {}).get("width"),
        height=(probe or {}).get("height"),
        max_height=max_height,
    )
    # Fix barra de progreso: leer el progreso de STDOUT con -progress
    # pipe:1, que escribe líneas `key=value` terminadas en \n
    # (out_time_us, progress=continue/end). Sin esto, ffmpeg escribe las
    # stats en stderr separadas por \r (no \n) cuando no hay terminal, y
    # readline() las acumula sin devolverlas hasta el final → la barra
    # quedaba congelada en "⏳ Analizando..." durante toda la compresión.
    cmd[-1:] = ["-progress", "pipe:1", cmd[-1]]
    logger.info(f"Comprimiendo {os.path.basename(input_path)}: "
                f"crf={crf} preset={preset} -> {os.path.basename(output_path)}")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    # Drenar stderr en un hilo (si no, el pipe se llena y ffmpeg se
    # bloquea). El progreso ya no se lee de ahí.
    stderr_tail: List[str] = []

    def _drain_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            stderr_tail.append(raw.decode("utf-8", errors="ignore"))
            if len(stderr_tail) > 20:
                stderr_tail.pop(0)

    threading.Thread(target=_drain_stderr, daemon=True).start()

    last_update = {"t": 0.0}
    try:
        for raw in iter(proc.stdout.readline, b""):
            if cancel_check is not None and cancel_check():
                logger.info("Compresión cancelada por el usuario (flag)")
                _terminate(proc)
                raise UserCancelledError()
            line = raw.decode("utf-8", errors="ignore").strip()
            # El bloque -progress incluye out_time_us=... (microsegundos)
            if line.startswith("out_time_us="):
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000.0
                except (ValueError, IndexError):
                    continue
                pct = min(100.0, elapsed / duration * 100.0) if duration else 0.0
                now = time.time()
                if on_progress and (now - last_update["t"] >= 0.7 or pct >= 100.0):
                    last_update["t"] = now
                    on_progress(pct)
        proc.wait(timeout=120)
    except UserCancelledError:
        # Limpiar el parcial que ffmpeg ya haya escrito (si no, queda un
        # .mp4 a medias en _temp comiendo disco/quota del usuario).
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
                logger.info(f"Parcial de compresión cancelada eliminado: {output_path}")
            except OSError:
                pass
        raise
    except Exception as e:
        logger.error(f"Compresión de {input_path} error: {e}")
        _terminate(proc)
        return None
    finally:
        if proc.poll() is None:
            _terminate(proc)

    if proc.returncode != 0 or not os.path.exists(output_path) \
            or os.path.getsize(output_path) == 0:
        logger.error(f"ffmpeg salió con código {proc.returncode} para {input_path}: "
                     f"{''.join(stderr_tail)[-500:]}")
        return None

    comp_size = os.path.getsize(output_path)
    stats = {
        "input": input_path,
        "output": output_path,
        "orig_size": orig_size,
        "comp_size": comp_size,
        "duration": duration,
        "width": (probe or {}).get("width"),
        "height": (probe or {}).get("height"),
        "crf": crf,
        "preset": preset,
    }
    if comp_size > 0:
        stats["saved_pct"] = max(0.0, (1 - comp_size / orig_size) * 100)
    logger.info(f"Compresión OK: {os.path.basename(input_path)} "
                f"{orig_size} -> {comp_size} bytes")
    return stats


def _terminate(proc: subprocess.Popen) -> None:
    """Termina el proceso ffmpeg de forma segura (SIGTERM, luego SIGKILL)."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass
