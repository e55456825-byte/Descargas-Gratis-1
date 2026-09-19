#!/usr/bin/env python3
"""
bitzero.py — Decodificador/descargador de URLs BitZero (Bit Uploader v2).

Uso:
    python3 bitzero.py "58780200-5260/1,2,3/1/..."

El código puede pegarse SIN dominio (solo la ruta) o como URL completa
(https://...): ambas funcionan. La URL no apunta a un servidor real: es un
paquete que contiene las
credenciales de la revista OJS, los IDs de las partes subidas y el modo de
camuflaje. Este script:

  1. Parsea y decodifica la URL (base64 url-safe).
  2. Inicia sesión en la revista OJS con las credenciales incrustadas.
  3. Descarga cada parte en orden.
  4. Revierte el camuflaje (1=PNG, 2=HTML, 3=ZIP-AES-256).
  5. Reensambla las partes y verifica el tamaño.
  6. Guarda el archivo original.

Requiere: requests. Para modo 2: beautifulsoup4. Para modo 3: pyzipper.
"""
from __future__ import annotations

import base64
import io
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from cancellation import UserCancelledError

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# Cabecera PNG falsa que BitZero antepone en modo 1 (idéntica a encoder.py)
PNG_HEADER = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02'
    b'\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT'
    b'x\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U'
    b'\x00\x00\x00\x00IEND\xaeB`\x82'
)


# ── URL ─────────────────────────────────────────────────────────────
def _urlsafe_b64decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def parse_url(url: str) -> Dict[str, Any]:
    """Replica URLGenerator.parse_bitzero_url (misma lógica de segmentos)."""
    url = url.strip().replace('bitzero ', '').strip()
    parts = url.split('/')
    if len(parts) < 5:
        raise ValueError('Código de descarga inválido (menos de 5 segmentos)')

    # ¿El último segmento es un hash MD5 de 8 hex o el filename?
    last = parts[-1]
    if re.fullmatch(r'[0-9a-f]{8}', last):
        filename_b64 = parts[-2]
        key_idx = -3
    else:
        filename_b64 = last
        key_idx = -2

    key_encoded = parts[key_idx]
    bitzero_mode = int(parts[key_idx - 1])
    token = parts[key_idx - 2]
    size_str, repo = parts[key_idx - 3].split('-', 1)
    file_size = int(size_str)

    key_parts = key_encoded.split('-')
    if len(key_parts) < 5:
        raise ValueError('Clave BitZero incompleta (menos de 5 partes)')

    def dec(i: int) -> str:
        return _urlsafe_b64decode(key_parts[i]).decode('utf-8')

    info: Dict[str, Any] = {
        'host': dec(0),
        'username': dec(1),
        'password': dec(2),
        'submission_id': dec(3),
        'contexto': dec(4),
    }
    if len(key_parts) >= 6:
        info['bitzero_mode'] = int(dec(5))
    if len(key_parts) >= 7:
        info['timestamp'] = int(dec(6))
    if len(key_parts) >= 8:
        info['encryption_key'] = dec(7)

    try:
        original_name = _urlsafe_b64decode(filename_b64).decode('utf-8')
    except Exception:
        original_name = filename_b64

    return {
        'file_size': file_size,
        'submission_id': repo,
        'file_ids': token.split('-'),
        'bitzero_mode': bitzero_mode,
        'original_name': original_name,
        'host': info['host'],
        'username': info['username'],
        'password': info['password'],
        'contexto': info['contexto'],
        'encryption_key': info.get('encryption_key'),
    }


# ── OJS ─────────────────────────────────────────────────────────────
def login(session: requests.Session, base_url: str, contexto: str,
          username: str, password: str) -> bool:
    try:
        login_url = f"{base_url}/index.php/{contexto}/login"
        r = session.get(login_url, timeout=30)
        m = re.search(r'name="csrfToken"[^>]*value="([^"]+)"', r.text)
        if not m:
            print('⚠ No se encontró CSRF (¿revista caída?). Probando igual...')
            csrf = ''
        else:
            csrf = m.group(1)
        session.post(
            f"{base_url}/index.php/{contexto}/login/signIn",
            data={'csrfToken': csrf, 'username': username, 'password': password,
                  'remember': '1', 'source': ''},
            allow_redirects=True, timeout=30,
        )
        d = session.get(f"{base_url}/index.php/{contexto}/user", timeout=30)
        ok = username in d.text
        print(f"🔑 Login en {base_url}: {'OK' if ok else 'FALLIDO'}")
        return ok
    except Exception as e:
        print(f"🔑 Login error: {e}")
        return False


def download_part(session: requests.Session, base_url: str, contexto: str,
                  submission_id: str, file_id: str, timeout: int = 120) -> bytes:
    """Descarga una parte camuflada desde OJS.
    El endpoint $$call$$ necesita el prefijo /index.php/<contexto>;
    sin él OJS devuelve la portada de la revista (HTML) en vez del archivo.
    """
    url = (f"{base_url}/index.php/{contexto}/$$$call$$$/api/file/file-api/download-file"
           f"?submissionFileId={file_id}&submissionId={submission_id}&stageId=1")
    r = session.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Descarga de parte {file_id}: HTTP {r.status_code}")
    return r.content


# ── Decamufalje ─────────────────────────────────────────────────────
def de_camouflage(data: bytes, bitzero_mode: int,
                  encryption_key: Optional[str] = None) -> bytes:
    """Revierte el camuflaje BitZero aplicado en la subida."""
    if bitzero_mode == 1:
        # PNG falso: quitar cabecera
        return data[len(PNG_HEADER):] if data.startswith(PNG_HEADER) else data

    if bitzero_mode == 2:
        # HTML con base64 (+XOR si hay clave)
        return _decode_html(data, encryption_key)

    if bitzero_mode == 3:
        # ZIP (AES-256 si hay clave)
        return _decode_zip(data, encryption_key)

    return data  # modo 0: sin camuflaje


def _decode_html(data: bytes, encryption_key: Optional[str]) -> bytes:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError('Modo HTML requiere beautifulsoup4 (pip install beautifulsoup4)')
    content = data.decode('utf-8', errors='ignore')

    encoded: Optional[str] = None
    if '<!-- encoded:' in content:
        encoded = content.split('<!-- encoded:')[1].split('-->')[0].strip()
    if not encoded:
        soup = BeautifulSoup(content, 'html.parser')
        div = soup.find('div', id='encoded-data')
        if div and div.string:
            encoded = div.string.strip()
    if not encoded:
        matches = re.findall(r'[A-Za-z0-9+/=_-]{100,}', content)
        if matches:
            encoded = max(matches, key=len)
    if not encoded:
        raise RuntimeError('No se encontraron datos codificados en el HTML')

    if encryption_key:
        pad = '=' * (-len(encoded) % 4)
        xor_data = base64.urlsafe_b64decode(encoded + pad)
        inner = _xor_bytes(xor_data, encryption_key).decode('utf-8')
        return base64.b64decode(inner)
    return base64.b64decode(encoded)


def _decode_zip(data: bytes, password: Optional[str]) -> bytes:
    try:
        import pyzipper
    except ImportError:
        raise RuntimeError('Modo ZIP requiere pyzipper (pip install pyzipper)')
    with pyzipper.AESZipFile(io.BytesIO(data), 'r') as zf:
        name = zf.namelist()[0]
        if password:
            zf.setpassword(password.encode('utf-8'))
        with zf.open(name) as f:
            return f.read()


def _xor_bytes(data: bytes, key: str) -> bytes:
    if not key:
        return data
    kb = key.encode('utf-8')
    return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(data))


# ── Descarga reutilizable (para el bot y para la CLI) ───────────────
def descargar_bitzero(url: str, output_path: str,
                      on_progreso: Optional[Callable[[int, int, float], None]] = None,
                      timeout: int = 120,
                      cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Descarga y decamufla un archivo BitZero a disco.

    Escribe cada parte decodificada directamente en output_path (memoria
    acotada aunque el archivo sea enorme). Devuelve info con:
      - original_name, file_size, output_path, parts
    Lanza RuntimeError si la URL es inválida, no se puede autenticar,
    una parte falla tras los reintentos o el ensamblado no coincide.
    Lanza UserCancelledError si cancel_check() devuelve True (el bot la
    usa para abortar cuando el usuario pulsa '❌ Cancelar').

    on_progreso(parte_actual, total_partes, velocidad_mbs) se invoca tras
    cada parte descargada (desde el hilo que llame a esta función).
    """
    info = parse_url(url)
    total = len(info['file_ids'])
    if total == 0:
        raise RuntimeError('La URL no contiene partes (file_ids vacío)')

    session = requests.Session()
    session.verify = False
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    if not login(session, info['host'], info['contexto'],
                 info['username'], info['password']):
        raise RuntimeError('No se pudo autenticar en el servidor '
                           '(credenciales inválidas o sitio caído)')

    # Asegurar directorio destino
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    total_bytes = 0
    parts = []
    try:
        with open(output_path, 'wb') as out:
            for i, fid in enumerate(info['file_ids'], 1):
                # Cancelación por el usuario (el bot comprueba la bandera
                # entre partes; si el usuario la pulsa a mitad de una parte,
                # se aborta en la siguiente)
                if cancel_check and cancel_check():
                    raise UserCancelledError(
                        f'Descarga cancelada por el usuario (parte {i}/{total})'
                    )
                data = None
                last_err = None
                # Reintentos por parte (la revista puede caerse a mitad)
                for intento in range(1, 4):
                    try:
                        data = download_part(session, info['host'], info['contexto'],
                                             info['submission_id'], fid, timeout=timeout)
                        break
                    except Exception as e:
                        last_err = e
                        if intento < 3:
                            time.sleep(2 * intento)
                if data is None:
                    raise RuntimeError(
                        f'Falló la parte {i}/{total} (id {fid}) tras 3 intentos: {last_err}'
                    )

                try:
                    decoded = de_camouflage(data, info['bitzero_mode'], info['encryption_key'])
                except Exception as e:
                    raise RuntimeError(f'Falló el decamuflaje de la parte {i}/{total}: {e}')

                out.write(decoded)
                total_bytes += len(decoded)
                parts.append({'file_id': fid, 'size': len(decoded)})

                if on_progreso:
                    elapsed = time.time() - t0
                    velocidad = total_bytes / elapsed / 1024 / 1024 if elapsed > 0 else 0
                    try:
                        on_progreso(i, total, velocidad)
                    except Exception:
                        pass
    finally:
        try:
            session.close()
        except Exception:
            pass

    # Verificación de tamaño (solo informativa: algunos enlaces reportan
    # el tamaño del .tar multi-archivo, así que no abortamos si difiere)
    if info.get('file_size') and total_bytes != info['file_size']:
        print(f"⚠ Tamaño distinto al esperado: {total_bytes} vs "
              f"{info['file_size']} bytes. El archivo puede estar incompleto.",
              file=sys.stderr)

    return {
        'original_name': info['original_name'],
        'file_size': total_bytes,
        'expected_size': info.get('file_size'),
        'output_path': output_path,
        'parts': parts,
    }


# ── Main ────────────────────────────────────────────────────────────
def main() -> int:
    if len(sys.argv) < 2:
        print('Uso: python3 bitzero.py "URL"')
        return 1

    url = sys.argv[1]
    try:
        info = parse_url(url)
    except Exception as e:
        print(f"❌ URL inválida: {e}")
        return 1

    print('┌─ URL BitZero decodificada')
    print(f"│  Revista     : {info['host']}/index.php/{info['contexto']}")
    print(f"│  Usuario     : {info['username']}")
    print(f"│  Submission  : {info['submission_id']}")
    print(f"│  Modo        : {info['bitzero_mode']} "
          f"({['sin camuflaje', 'PNG', 'HTML', 'ZIP-AES'][info['bitzero_mode']]})")
    print(f"│  Partes      : {len(info['file_ids'])}")
    print(f"│  Archivo     : {info['original_name']}")
    print(f"│  Tamaño      : {info['file_size']} bytes "
          f"({info['file_size'] / 1024 / 1024:.1f} MB)")
    print(f"│  Clave       : {'SÍ' if info['encryption_key'] else 'no'}")
    print('└─')

    session = requests.Session()
    session.verify = False
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                          'AppleWebKit/537.36'})

    if not login(session, info['host'], info['contexto'],
                 info['username'], info['password']):
        print('❌ No se pudo autenticar. ¿Credenciales inválidas o sitio caído?')
        return 1

    total = len(info['file_ids'])
    chunks: List[bytes] = []
    t0 = time.time()
    for i, fid in enumerate(info['file_ids'], 1):
        try:
            data = download_part(session, info['host'], info['contexto'],
                                 info['submission_id'], fid)
        except Exception as e:
            print(f"\n❌ Falló la parte {i}/{total} (id {fid}): {e}")
            print('   Partes descargadas se conservan en memoria; reintenta desde 0.')
            return 1
        try:
            chunks.append(de_camouflage(data, info['bitzero_mode'], info['encryption_key']))
        except Exception as e:
            print(f"\n❌ Falló el decamufalje de la parte {i}/{total}: {e}")
            return 1
        elapsed = time.time() - t0
        rate = sum(len(c) for c in chunks) / elapsed if elapsed > 0 else 0
        print(f"  ⬇ [{i}/{total}] {fid}  ({len(data)/1024/1024:.1f} MB)  "
              f"{rate/1024/1024:.1f} MB/s")

    full = b''.join(chunks)
    print(f"\n✅ {len(info['file_ids'])} partes descargadas y reensambladas "
          f"({len(full)/1024/1024:.1f} MB)")

    if len(full) != info['file_size']:
        print(f"⚠ Tamaño distinto al esperado: {len(full)} vs {info['file_size']} bytes. "
              f"El archivo puede estar incompleto.")

    out_name = info['original_name']
    if out_name.startswith('__multi__'):
        out_name = 'multi_files.tar'
    if os.path.exists(out_name):
        base, ext = os.path.splitext(out_name)
        out_name = f"{base}_{int(time.time())}{ext}"

    with open(out_name, 'wb') as f:
        f.write(full)
    print(f"💾 Guardado: {os.path.abspath(out_name)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
