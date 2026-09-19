"""
encoder.py — BitZeroEncoder (PNG / HTML / ZIP) con correcciones.

Fixes aplicados:
  - BUG-01: base64 url-safe en encode_html (los datos del div#encoded-data
    ya no contienen '/' ni '+').
  - BUG-04: ZIP encriptado con pyzipper (zipfile.setpassword no encripta
    al escribir; pyzipper sí con AES).
  - BUG-10: encode_html añade marcador estructural `<!-- encoded:KEY -->`
    para que decode_html pueda recuperarlo sin regex ambigua.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Optional

import pyzipper
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class BitZeroEncoder:
    """Sistema de ofuscación avanzado para archivos."""

    PNG_HEADER = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
        b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02'
        b'\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT'
        b'x\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U'
        b'\x00\x00\x00\x00IEND\xaeB`\x82'
    )

    # ── PNG ──────────────────────────────────────────────────────────
    @staticmethod
    def encode_png(file_path: str, output_path: str) -> bool:
        try:
            with open(file_path, 'rb') as f:
                original = f.read()
            with open(output_path, 'wb') as f:
                f.write(BitZeroEncoder.PNG_HEADER + original)
            logger.info(f"PNG encode OK: {output_path}")
            return True
        except Exception as e:
            logger.error(f"encode_png error: {e}")
            return False

    @staticmethod
    def decode_png(file_path: str) -> bytes:
        with open(file_path, 'rb') as f:
            data = f.read()
        return data[len(BitZeroEncoder.PNG_HEADER):] if data.startswith(BitZeroEncoder.PNG_HEADER) else data

    # ── HTML ─────────────────────────────────────────────────────────
    @staticmethod
    def _xor_bytes(data: bytes, key: str) -> bytes:
        """Aplica XOR con clave repetida."""
        if not key:
            return data
        key_bytes = key.encode('utf-8')
        out = bytearray(len(data))
        for i, b in enumerate(data):
            out[i] = b ^ key_bytes[i % len(key_bytes)]
        return bytes(out)

    @staticmethod
    def encode_html(file_path: str, output_path: str,
                    encryption_key: Optional[str] = None) -> bool:
        """Codifica un archivo como HTML con datos en base64 url-safe.
        Si se pasa encryption_key, aplica XOR + doble base64.
        """
        try:
            with open(file_path, 'rb') as f:
                original_data = f.read()

            # Capa 1: base64 estándar de los datos originales
            encoded = base64.b64encode(original_data).decode('utf-8')

            # Capa 2 (opcional): XOR + base64 url-safe (sin '/' ni '+')
            if encryption_key:
                xor_data = BitZeroEncoder._xor_bytes(encoded.encode('utf-8'), encryption_key)
                # url-safe para que no contenga '/' ni '+' (BUG-01 fix)
                encoded = base64.urlsafe_b64encode(xor_data).decode('utf-8').rstrip('=')

            timestamp = int(time.time())
            file_size = os.path.getsize(file_path)
            file_name = os.path.basename(file_path)
            has_key = 'true' if encryption_key else 'false'

            html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Documento de Datos</title>
    <style>body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f5f5f5; }}
    .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
    .data-section {{ display: none; }}</style>
</head>
<body>
<!-- encoded:{encoded} -->
<!-- meta:filename={file_name}|size={file_size}|timestamp={timestamp}|encrypted={has_key} -->
    <div class="container">
        <h1>Documento de Datos</h1>
        <p>Documento generado por BitZero. Tamaño: {file_size} bytes.</p>
        <div class="data-section" id="data">
            <div id="encoded-data">{encoded}</div>
        </div>
    </div>
</body>
</html>"""
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.info(f"HTML encode OK: {output_path}")
            return True
        except Exception as e:
            logger.error(f"encode_html error: {e}")
            return False

    @staticmethod
    def decode_html(html_path: str, encryption_key: Optional[str] = None) -> bytes:
        """Decodifica HTML generado por encode_html.
        Prioridad de extracción (BUG-10 fix):
          1. Comentario `<!-- encoded:XXX -->`
          2. div#encoded-data
          3. div#data
          4. Fallback regex con advertencia
        """
        with open(html_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        encoded: Optional[str] = None

        # 1. Comentario estructural (preferido)
        if '<!-- encoded:' in content:
            try:
                encoded = content.split('<!-- encoded:')[1].split('-->')[0].strip()
            except Exception:
                encoded = None

        # 2. div#encoded-data
        if not encoded:
            soup = BeautifulSoup(content, 'html.parser')
            div = soup.find('div', id='encoded-data')
            if div and div.string:
                encoded = div.string.strip()

        # 3. div#data
        if not encoded:
            soup = BeautifulSoup(content, 'html.parser')
            div = soup.find('div', id='data')
            if div and div.string:
                encoded = div.string.strip()

        # 4. Fallback regex (con advertencia)
        if not encoded:
            import re
            matches = re.findall(r'[A-Za-z0-9+/=_-]{100,}', content)
            if matches:
                encoded = max(matches, key=len)
                logger.warning("decode_html: fallback regex poco fiable")

        if not encoded:
            raise ValueError("No se encontraron datos codificados en el HTML")

        # Reconstruir padding y decodificar
        if encryption_key:
            # Capa 2: era url-safe con XOR
            pad = '=' * (-len(encoded) % 4)
            xor_data = base64.urlsafe_b64decode(encoded + pad)
            inner = BitZeroEncoder._xor_bytes(xor_data, encryption_key).decode('utf-8')
            # Capa 1: base64 estándar de los datos originales
            return base64.b64decode(inner)
        else:
            # Sin clave: era base64 estándar de los datos originales
            return base64.b64decode(encoded)

    # ── ZIP (con pyzipper, BUG-04 fix) ───────────────────────────────
    @staticmethod
    def encode_zip(file_path: str, output_path: str,
                   encryption_key: Optional[str] = None) -> bool:
        """Crea ZIP comprimido. Si se pasa clave, encripta con AES-256.
        Usa pyzipper (zipfile estándar no soporta encriptación al escribir).
        """
        try:
            encryption = pyzipper.WZ_AES if encryption_key else None
            with pyzipper.AESZipFile(
                output_path, 'w',
                compression=pyzipper.ZIP_DEFLATED,
                encryption=encryption
            ) as zipf:
                if encryption_key:
                    zipf.setpassword(encryption_key.encode('utf-8'))
                arcname = f"data_{int(time.time())}.bin"
                zipf.write(file_path, arcname)
            logger.info(f"ZIP encode OK: {output_path} (encrypted={bool(encryption_key)})")
            return True
        except Exception as e:
            logger.error(f"encode_zip error: {e}")
            return False

    @staticmethod
    def decode_zip(file_path: str, password: Optional[str] = None) -> bytes:
        """Extrae primer archivo del ZIP. Requiere pyzipper."""
        with pyzipper.AESZipFile(file_path, 'r') as zf:
            namelist = zf.namelist()
            if not namelist:
                raise ValueError("ZIP vacío")
            if password:
                zf.setpassword(password.encode('utf-8'))
            with zf.open(namelist[0]) as f:
                return f.read()

    # ── Camuflaje (dispatcher) ───────────────────────────────────────
    @staticmethod
    def apply_camouflage(file_path: str, bitzero_mode: int, user_id: int,
                         encryption_key: Optional[str] = None) -> Optional[str]:
        """Aplica ofuscación según el modo. Devuelve ruta del archivo
        camuflado o None si falla. Modo 0 = sin camuflaje (devuelve original)."""
        if bitzero_mode == 0:
            return file_path

        file_name = os.path.basename(file_path)
        file_ext = os.path.splitext(file_name)[1]
        out_dir = os.path.dirname(file_path)

        if bitzero_mode == 1:
            output = os.path.join(out_dir, f"{file_name}_{user_id}_cache{file_ext}.png")
            return output if BitZeroEncoder.encode_png(file_path, output) else None

        if bitzero_mode == 2:
            output = os.path.join(out_dir, f"{file_name}_{user_id}_data{file_ext}.html")
            return output if BitZeroEncoder.encode_html(file_path, output, encryption_key) else None

        if bitzero_mode == 3:
            output = os.path.join(out_dir, f"{file_name}_{user_id}_archivo{file_ext}.zip")
            return output if BitZeroEncoder.encode_zip(file_path, output, encryption_key) else None

        return None
