# Bit Uploader v2

Bot privado de Telegram que **recibe archivos y los sube a las revistas OJS
cubanas**, devolviendo al usuario un enlace de descarga (con ofuscación
BitZero). Incluye cola de subidas, compresión de vídeo opcional, cuotas por
usuario, panel de administración y monitor de estado de las revistas.

> **Esta es una copia de traspaso, sin credenciales.** Se eliminaron el
> `BOT_TOKEN`, `API_HASH` y las contraseñas de las revistas. Tendrás que
> crear tu propio bot en Telegram y rellenar tu `.env` (paso 6).

---

## 1. Qué hace

- Recibe documentos, vídeos, audio y comprime vídeos con FFmpeg (opcional).
- Sube el archivo a una revista OJS (vía `requests` + sesión/cookies) y guarda
  el resultado.
- Genera un **enlace BitZero** de descarga que el usuario pega en su app.
- **Cola de subidas** para no saturar el VPS (1 subida a la vez por defecto).
- **Reintentos automáticos** cuando las revistas (intermitentes) fallan.
- **Cuotas** por usuario y lista de usuarios autorizados (bot privado).
- **Panel de administración** y **broadcast** a los usuarios.
- **Apagado limpio**: al reiniciar, termina las subidas activas y reanuda las
  que quedaron en cola (se guardan en la base de datos).
- **Monitor de revistas** que avisa al admin si una revista deja de responder.

---

## 2. Requisitos

| Requisito | Detalle |
|---|---|
| Docker + Docker Compose | Recomendado (todo incluido, sin instalar Python). |
| Python 3.11+ | Solo para el modo manual (local). |
| FFmpeg | Para la compresión de vídeo. En Docker ya viene en la imagen. |
| Cuenta de Telegram | `API_ID`, `API_HASH` y un bot creado con @BotFather. |
| Acceso a las revistas | Usuario/contraseña de cada revista OJS (las tuyas). |

---

## 3. Instalación rápida (Docker) — recomendada

```bash
# 1. Descomprimir el paquete
tar -xzf BitUploaderBot-handoff.tar.gz
cd BitUploaderBot

# 2. Crear el archivo de configuración
cp .env.example .env
nano .env            # rellena API_ID, API_HASH, BOT_TOKEN, ADMIN_ID... (ver §6)

# 3. Levantar el bot
docker compose up -d --build

# 4. Ver que arrancó bien
docker compose logs -f
```

Cuando en el log aparezca `✅ Bot listo. Esperando comandos...`, abre tu bot
en Telegram y envía `/start`.

Comandos útiles:

```bash
docker compose logs -f          # ver el log en vivo
docker compose restart          # reiniciar (apagado limpio)
docker compose down             # detener
docker compose up -d --build    # reconstruir tras cambiar el código
```

> El `docker-compose.yml` monta `./data`, `./raiz` y `./logs` desde el host,
> así que los datos sobreviven a los reinicios y actualizaciones.

---

## 4. Instalación manual (sin Docker)

Útil para desarrollo o si no quieres Docker. Necesita **FFmpeg** en el
sistema (`apt install ffmpeg` en Debian/Ubuntu).

```bash
cd BitUploaderBot
cp .env.example .env
nano .env

# Opción A: usar el script que crea el venv e instala todo
chmod +x start.sh
./start.sh

# Opción B: a mano
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

---

## 5. Obtener las credenciales de Telegram

1. **API_ID y API_HASH**
   - Entra en https://my.telegram.org e inicia sesión con tu número.
   - `API development tools` → crea una app → copia `api_id` y `api_hash`.
2. **BOT_TOKEN**
   - Abre **@BotFather** en Telegram → `/newbot` → elige nombre y usuario.
   - Copia el token (formato `123456789:AA...`).
3. **ADMIN_ID (tu ID de usuario)**
   - Abre **@userinfobot** en Telegram y copia tu `Id` numérico.
   - Ese ID es tu **súper-admin permanente**; no se puede quitar desde el bot.
4. **Canales (opcionales)**
   - `LOG_CHANNEL_ID`: canal donde se registra lo que se sube (auditoría).
   - `STORAGE_CHANNEL_ID`: canal privado donde se reenvían los archivos.
   - Crea los canales, añade el bot como administrador y usa el ID con el
     prefijo `-100` (p. ej. `-1001234567890`). Deja `0` para desactivarlos.

---

## 6. Configuración (`.env`)

Todas las variables están documentadas en `.env.example`. Las más importantes:

### Obligatorias

| Variable | Descripción |
|---|---|
| `API_ID` | API ID de https://my.telegram.org |
| `API_HASH` | API Hash de https://my.telegram.org |
| `BOT_TOKEN` | Token del bot creado con @BotFather |
| `ADMIN_ID` | Tu ID numérico de Telegram (súper-admin permanente) |

### Recomendadas / operativas

| Variable | Por defecto | Descripción |
|---|---|---|
| `LOG_CHANNEL_ID` | `0` | Canal de auditoría (`-100...` o `0`). |
| `STORAGE_CHANNEL_ID` | `0` | Canal de backup de archivos (`-100...` o `0`). |
| `AUTO_UPLOAD` | `1` | Subir automáticamente al recibir el archivo. |
| `DELETE_AFTER_UPLOAD` | `1` | Borrar el archivo del VPS tras subirlo. |
| `UPLOAD_QUEUE_ENABLED` | `1` | Cola de subidas (1 a la vez). |
| `MAX_CONCURRENT_UPLOADS` | `1` | Subidas simultáneas. |
| `UPLOAD_RETRY_ATTEMPTS` | `3` | Reintentos si todas las revistas fallan. |
| `UPLOAD_RETRY_WAIT_SEC` | `120` | Espera inicial entre rondas (se duplica). |
| `SHUTDOWN_WAIT_SEC` | `1500` | Espera máxima al apagado limpio. |
| `DEFAULT_USER_QUOTA_MB` | `1024` | Cuota por defecto por usuario. |
| `CHUNK_SIZE_MB` | `20` | Tamaño de chunk para subir a Telegram. |
| `REV_MONITOR_ENABLED` | `1` | Monitor automático de revistas. |
| `RETENTION_DAYS` | `3` | Días de retención de archivos (limpieza por cron). |

### Revistas (solo se usan en el PRIMER arranque)

Cada revista tiene 5 variables: `_USER`, `_PASS`, `_SUB`, `_BITZERO`, `_KEY`.
Los grupos son: `KIKI_`, `COMED_`, `LUZ_`, `Canel_`, `Conrado_`.

| Grupo | Revista | URL |
|---|---|---|
| `KIKI_*` | Revista Cardiología | https://revcardiologia.sld.cu |
| `COMED_*` | Revista COMED | https://revcocmed.sld.cu |
| `LUZ_*` | Revista LUZ | https://revistavarela.uclv.edu.cu |
| `Canel_*` | Revista Canel | https://rus.ucf.edu.cu |
| `Conrado_*` | Revista Conrado | https://conrado.ucf.edu.cu |

- `_USER` / `_PASS`: tu cuenta en esa revista.
- `_SUB`: el **submission ID** (el envío al que se adjuntan los archivos).
- `_BITZERO`: `1` para activar la ofuscación BitZero, `0` para desactivar.
- `_KEY`: clave de cifrado asociada.

> Si dejas estas variables vacías, el bot crea las revistas igual, pero el
> login fallará hasta que pongas credenciales. Puedes cambiar
> `_BITZERO`/`_KEY` después; las credenciales de una revista ya creada en la
> base de datos no se re-leen del `.env` (la DB es la fuente de verdad).

---

## 7. Uso — comandos

El bot es **privado**: solo responden a los usuarios autorizados. Los añade un
admin con `/adduser <id>`.

### Usuario

| Comando | Qué hace |
|---|---|
| `/start` | Mensaje de bienvenida / aviso de acceso. |
| *(enviar archivo)* | Con `AUTO_UPLOAD=1` se sube solo; si no, usa `/up`. |
| `/up` | Sube el archivo seleccionado a una revista. |
| `/ls` | Lista tus archivos en el servidor. |
| `/zips` | Lista los ZIP/paquetes generados. |
| `/descargar` | Genera el enlace de descarga BitZero. |
| `/history` | Historial de tus subidas. |
| `/status` | Estado de tu cuenta (cuota, rol, etc.). |
| `/rm <id>` | Borra uno de tus archivos. |
| `/deleteall` | Borra todos tus archivos. |
| `/bitzero` | Info / activación de BitZero. |
| `/bitzero_status` | Estado de BitZero. |

### Administración

| Comando | Qué hace |
|---|---|
| `/control_panel` | Panel de control. |
| `/adduser <id>` | Autoriza a un usuario. |
| `/removeuser <id>` | Revoca a un usuario. |
| `/ban <id>` / `/listusers` | Banear / listar usuarios. |
| `/addadmin <id>` / `/deladmin <id>` | Añadir / quitar admin. |
| `/listadmins` | Listar admins. |
| `/quota <id> <MB>` | Ajustar la cuota de un usuario. |
| `/broadcast <mensaje>` | Enviar un mensaje a todos los usuarios. |
| `/rev_status` | Estado de las revistas (login, sesión...). |
| `/rev_monitor on\|off` | Activar/desactivar el monitor de revistas. |
| `/clear_rev` | Limpiar estado de revistas. |
| `/test_bitzero` | Probar BitZero. |

> Los IDs de usuario se obtienen con **@userinfobot** en Telegram.

---

## 8. Estructura del proyecto

```
BitUploaderBot/
├── main.py                # Punto de entrada: arranca Pyrogram y los handlers
├── config.py              # Carga .env y expone la configuración global
├── database.py            # SQLite asíncrono (aiosqlite) y esquema
├── uploader.py            # Subida a OJS (login, sesión, chunks, submission)
├── bitzero.py             # Ofuscación/desofuscación BitZero
├── url_generator.py       # Generación de enlaces de descarga
├── video_compressor.py    # Compresión FFmpeg (libx264)
├── storage.py             # Almacenamiento/gestión de archivos
├── encoder.py             # Codificación de datos
├── cancellation.py        # Cancelación de tareas
├── revista_monitor.py     # Monitor periódico del estado de las revistas
├── cleanup_retention.py   # Limpieza diaria (retención de archivos)
├── utils.py               # Utilidades comunes
├── handlers/
│   ├── commands.py        # Comandos de usuario
│   ├── admin.py           # Comandos de administración
│   ├── files.py           # Recepción de archivos y decisiones
│   ├── upload.py          # Cola de subidas, reintentos, apagado limpio
│   └── callbacks.py       # Botones inline
├── Dockerfile             # Imagen (Python 3.11 + FFmpeg)
├── docker-compose.yml     # Despliegue (env_file, volúmenes, stop_grace_period)
├── requirements.txt       # Dependencias Python
├── start.sh               # Arranque manual (crea venv e instala)
├── .env.example           # Plantilla de configuración  ← COPIA Y EDITA
├── data/                  # (se crea al arrancar) DB y sesión de Telegram
├── logs/                  # (se crea al arrancar) bot.log, cleanup.log
└── raiz/                  # (se crea al arrancar) archivos en tránsito
```

---

## 9. Datos y estado

| Ruta | Contenido | ¿Backup? |
|---|---|---|
| `data/bot.db` | Base de datos: usuarios, admins, archivos, subidas, revistas, colas. | **Sí** |
| `data/revista_bot_v2.session` | Sesión de Pyrogram (login del bot). | Sí (o se regenera al arrancar) |
| `raiz/` | Archivos en tránsito (se borran tras subir o por retención). | No |
| `logs/bot.log` | Log del bot (rotación 5 MB × 5). | Opcional |
| `.env` | Tus credenciales. | **Sí, en lugar seguro** |

> En Docker estas carpetas están montadas desde el host (`./data`, `./raiz`,
> `./logs`), así que hacer backup es copiar esas carpetas.

---

## 10. Mantenimiento

### Backup

```bash
# Con el bot parado (o aceptando una copia en caliente de SQLite):
cp data/bot.db "backups/bot_$(date +%Y%m%d).db"
cp .env "backups/env_$(date +%Y%m%d)"
```

Para una copia consistente con la DB en marcha (WAL), mejor:

```bash
python3 - <<'PY'
import sqlite3
src = sqlite3.connect("data/bot.db")
dst = sqlite3.connect("backups/bot_backup.db")
with dst:
    src.backup(dst)
PY
```

### Limpieza de disco (retención)

`cleanup_retention.py` borra archivos viejos de `raiz/` y registros huérfanos.
Prográmalo con cron (una vez al día):

```cron
0 4 * * * cd /ruta/a/BitUploaderBot && ./venv/bin/python cleanup_retention.py >> logs/cleanup.log 2>&1
```

Con Docker:

```bash
docker compose exec bot python cleanup_retention.py
```

### Logs

```bash
tail -f logs/bot.log          # manual
docker compose logs -f        # Docker
```

### Actualizar el código

```bash
# Docker
docker compose up -d --build

# Manual
source venv/bin/activate && pip install -r requirements.txt && python main.py
```

---

## 11. Seguridad

- **Nunca** compartas ni subas tu `.env` (contiene el token del bot y tu API_HASH).
- El `.gitignore` ya excluye `.env`, `data/`, `logs/`, `raiz/` y `*.session`.
- Restringe los permisos del `.env`: `chmod 600 .env`.
- Si el token se filtra, revócalo en **@BotFather** (`/revoke`) y genera otro.
- El bot es **privado**: mantenlo así y autoriza solo a quien corresponda con
  `/adduser`.
- En producción, no expongas puertos que no uses (este bot solo hace polling,
  no necesita abrir ninguno).

---

## 12. Solución de problemas

| Síntoma | Causa probable / solución |
|---|---|
| `BOT_TOKEN` vacío o error al arrancar | Falta rellenar `.env`. Copia `.env.example` y complétalo. |
| El bot no responde a nadie | El usuario no está autorizado: `/adduser <id>` (usa @userinfobot). |
| `❌ login fallido` en el log | Credenciales de esa revista incorrectas o el sitio está caído. Revisa `_USER`/`_PASS`/`_SUB`. |
| Las subidas fallan en todas las revistas | Revistas intermitentes: el bot reintenta solo. Revisa `UPLOAD_RETRY_*`. |
| `ffmpeg not found` | Instala FFmpeg en el host (`apt install ffmpeg`) o usa Docker. |
| Se llena el disco | Baja `RETENTION_DAYS` y programa `cleanup_retention.py` en cron. |
| `FloodWait` de Telegram | El bot sube muy rápido; espera o reduce `MAX_CONCURRENT_UPLOADS`. |
| No aparece el enlace de descarga | Revisa `/bitzero_status` y `/test_bitzero`; verifica `BITZERO_SIG`. |
| Reinicio y se pierde una subida | Se guardan en la cola (BD) y se reanudan solas al arrancar. |

---

## 13. Notas del traspaso (cambios respecto al original)

Para poder compartir esta copia sin filtrar datos, se hicieron estos ajustes:

- **Se eliminaron** `.env`, `data/`, `logs/`, `raiz/` y el entorno virtual
  `venv/` del paquete.
- **Se quitaron los secretos embebidos** en el código:
  - `config.py`: ya no hay `BOT_TOKEN`, `API_HASH`, `API_ID`, `ADMIN_ID` ni
    IDs de canales por defecto. Se leen **solo** de `.env`.
  - `main.py`: las revistas por defecto arrancan **sin** usuario/contraseña
    (las pones tú en `.env` o desde el bot).
  - `.env.example`: reescrito con marcadores vacíos (antes traía credenciales
    reales) y alineado con las variables que el código realmente lee
    (`LUZ_`, `Canel_`, `Conrado_`).
- **`cleanup_retention.py`** ya no usa rutas absolutas fijas: resuelve
  `raiz/` y `data/bot.db` de forma relativa al propio archivo (o por env), así
  que funciona en cualquier carpeta/instalación.

### Checklist de puesta en marcha

- [ ] `cp .env.example .env` y completar `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMIN_ID`
- [ ] (Opcional) crear y configurar `LOG_CHANNEL_ID` y `STORAGE_CHANNEL_ID`
- [ ] (Opcional) poner tus credenciales de revistas (`KIKI_`, `COMED_`, `LUZ_`, `Canel_`, `Conrado_`)
- [ ] `docker compose up -d --build` (o `./start.sh` en modo manual)
- [ ] Ver `✅ Bot listo` en los logs
- [ ] Enviar `/start` al bot y autorizar usuarios con `/adduser`
- [ ] Probar una subida real y comprobar el enlace de descarga
- [ ] Programar `cleanup_retention.py` en cron y hacer un backup de `data/bot.db`

---

## 14. Licencia / contacto

Proyecto interno. Ajústalo a tu uso. Para dudas sobre el funcionamiento del
código, revisa los comentarios de cada módulo (están en español y explican el
porqué de cada decisión).
