# Powerlifting Clip Extractor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 🌐 **Accede a la herramienta → [raulillo82.duckdns.org](https://raulillo82.duckdns.org)**
> *(Puedes crear tu cuenta gratis desde la propia web mientras haya plazas disponibles / You can register for free directly from the site while slots are available)*

> 🖥️ ¿Eres administrador y quieres montar tu propia instancia? → [Guía de despliegue (ES)](#despliegue-en-producción) &nbsp;·&nbsp; [Deployment guide (EN)](#production-deployment)

---

> 🇬🇧 [Read in English ↓](#english)

---

## Español

### Índice

- [Hoja de ruta](#hoja-de-ruta)
- [Requisitos](#requisitos)
- [Uso](#uso)
- [Opciones principales](#opciones-principales)
- [Archivos generados](#archivos-generados)
- [Despliegue en producción](#despliegue-en-producción)
- [Desarrollo](#desarrollo)

---

### Hoja de ruta

| Estado | Tarea | Responsable |
|---|---|---|
| ✅ | Descarga de clips individuales con `yt-dlp` y `--download-sections` | — |
| ✅ | Vídeo combinado con 3 levantamientos apilados verticalmente | — |
| ✅ | Compatibilidad Instagram / WhatsApp / Telegram (H.264 + AAC + faststart) | — |
| ✅ | Duración configurable por movimiento (sentadilla / banca / peso muerto) | — |
| ✅ | Freeze en el último frame para clips de distinta duración | — |
| ✅ | Previews en baja resolución para dispositivos lentos | — |
| ✅ | Modo interactivo y modo parámetros (CLI) | — |
| ✅ | Música en el combinado con punto de inicio configurable | — |
| ✅ | Tests automatizados (`pytest`, 34 tests, CI con GitHub Actions) | — |
| ✅ | Interfaz web bilingüe ES/EN (Flask) | — |
| ✅ | Sistema de autenticación (registro, login, vinculación de dispositivo) | — |
| ✅ | Panel de administración de usuarios | — |
| ✅ | Cola de jobs con pool de 2 workers paralelos | — |
| ✅ | Modo dry-run para tests sin descargas reales | — |
| ✅ | Tests automatizados de rutas web (80 tests, CI) | — |
| ✅ | Rate limiting (Flask-Limiter): `/register` 3/15 min, `/run` 1/2 min por usuario, `/login` 20/min | — |
| ✅ | Despliegue en producción (RPi5, nginx, gunicorn, HTTPS) | — |
| ✅ | **Modo un solo levantamiento** (1 tiempo, 1 movimiento; audio original / solo música / mezclado) | — |
| ✅ | Entorno staging en RPi5 (`/staging/`): badge STAGING, acceso admin solo, exención de rate limiting | — |
| ✅ | Buscador por nombre dentro del canal (para competiciones fuera de playlist) | — |
| ✅ | Progreso de descarga en tiempo real (ticker cada 5 s) | — |
| ✅ | Música por defecto al 50%; filtrado de vídeos privados/eliminados del cascade | — |
| ✅ | Link de YouTube junto a la sesión seleccionada; navegación hacia atrás en el cascade | — |
| ✅ | Nuevas federaciones en el cascade: USAPL, British Powerlifting, CPU (solo afiliadas IPF) | — |
| ✅ | Historial de extracciones (`/history`): últimos 20 jobs con estado y link de descarga | — |
| ✅ | Tests automatizados (133 tests, CI) | — |
| ✅ | Contenedor Podman (imagen OCI reproducible, base OpenSUSE Tumbleweed) | — |
| ✅ | Ansible playbook para despliegue y failover RPi5↔RPi4 | — |
| ✅ | **Panel de estadísticas** (`/admin/stats`) — mapa Leaflet por ciudad (GeoLite2), gráficos Chart.js (éxito/error, música, federaciones, modo, hora del día), filtro 7/30/90d/todo, cola en tiempo real | — |

Extrae levantamientos individuales de un vídeo de competición de powerlifting en YouTube y genera un vídeo combinado compatible con Instagram con sentadilla, press de banca y peso muerto apilados verticalmente.

---

### Requisitos

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`sudo zypper install yt-dlp` en openSUSE)
- ffmpeg con soporte H.264 — en openSUSE, el paquete oficial **no incluye H.264** por restricciones de patentes; instala desde [Packman](https://packman.links2linux.de/):
  ```bash
  sudo zypper addrepo -cfp 90 https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/Essentials packman-essentials
  sudo zypper --gpg-auto-import-keys refresh packman-essentials
  sudo zypper --non-interactive install --allow-vendor-change --from packman-essentials ffmpeg-7
  ```

---

### Uso

#### Modo interactivo

Ejecuta sin argumentos y responde a las preguntas (pulsa Enter para aceptar los valores por defecto):

```bash
python3 extract_lifts.py
```

#### Modo parámetros — tiempos desde archivo

```bash
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID
```

Lee los tiempos de `times.txt` por defecto (usa `--times otro.txt` para cambiarlo).

#### Modo parámetros — tiempos en la propia llamada

```bash
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --timestamps 0:21:27 0:29:55 0:38:15 1h23:30 1h32:21 1h41:30 2h26:15 2h33:4 2h41:35
```

#### Modo un solo levantamiento (`--single`)

Extrae un único levantamiento: un tiempo, un movimiento, sin combinado.

```bash
# Audio original (sin riesgo de copyright)
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --single --timestamp 2h26:15 --movement deadlift --attempt 2

# Solo música (reemplaza el audio original)
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --single --timestamp 2h26:15 --movement deadlift --attempt 2 \
    --audio-mode music_only --music "nombre de canción o URL de YouTube"

# Mezclado (audio original + música; genera 3 archivos)
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --single --timestamp 2h26:15 --movement deadlift --attempt 2 \
    --audio-mode mixed --music "https://www.youtube.com/watch?v=..."
```

---

### Opciones principales

| Opción | Por defecto | Descripción |
|---|---|---|
| `--times ARCHIVO` | `times.txt` | Archivo con 9 tiempos de inicio |
| `--timestamps t1..t9` | — | 9 tiempos en la llamada (anula `--times`) |
| `--duration SEGS` | `60` | Duración de cada clip en segundos |
| `--squat {1,2,3}` | `3` | Intento de sentadilla en el vídeo combinado |
| `--bench {1,2,3}` | `3` | Intento de press de banca en el vídeo combinado |
| `--deadlift {1,2,3}` | `3` | Intento de peso muerto en el vídeo combinado |
| `--output-dir DIR` | `lifts/` | Carpeta de salida |
| `--skip-individual` | — | Usa clips ya descargados, omite descargas |
| `--skip-combined` | — | Solo descarga clips, omite el vídeo combinado |
| `--music URL_O_BÚSQUEDA` | — | Añade música al combinado (URL de YouTube recomendada) |
| `--music-start MM:SS` | `0:00` | Punto de inicio en la canción |
| `--preview [ANCHO]` | — | Genera copias en baja resolución en `preview/` |
| `--duration-squat SEGS` | igual que `--duration` | Duración específica para sentadillas |
| `--duration-bench SEGS` | igual que `--duration` | Duración específica para banca |
| `--duration-deadlift SEGS` | igual que `--duration` | Duración específica para peso muerto |
| `--no-replay` | — | Usar solo si el vídeo no tiene repeticiones a cámara lenta |
| `--single` | — | Modo un solo levantamiento (requiere `--timestamp`) |
| `--timestamp TS` | — | Tiempo del levantamiento (p.ej. `2h26:15`). Solo con `--single` |
| `--movement` | `squat` | `squat` / `bench` / `deadlift`. Solo con `--single` |
| `--attempt {1,2,3}` | `3` | Número de intento (solo afecta al nombre del archivo). Solo con `--single` |
| `--audio-mode` | `original` | `original` / `music_only` / `mixed`. Solo con `--single` |

### Formato del archivo de tiempos (`times.txt`)

Un tiempo por línea, 9 en total (sentadillas 1–3, banca 1–3, peso muerto 1–3). Se aceptan formatos mixtos:

```
0:21:27
0:29:55
0:38:15
1h23:30
1h32:21
1h41:30
2h26:15
2h33:4
2h41:35
```

---

### Archivos generados

**Modo completo (9 tiempos):**
```
lifts/
├── lift_01_squat_attempt1.mp4        ← clip individual con audio original
│   ...
├── lift_09_deadlift_attempt3.mp4
├── combined_s3_b3_d3_for-instagram.mp4  ← sin música, sube este a Instagram
│                                           (añade la música desde la propia app)
├── combined_s3_b3_d3_with-music.mp4     ← con música, ideal para WhatsApp,
│                                           Telegram o uso personal en el móvil
└── preview/                              ← versiones en baja resolución (--preview)
```

**Modo un solo levantamiento (`--single`):**
```
lifts/
├── deadlift_attempt2_original.mp4   ← audio original (siempre)
├── deadlift_attempt2_music.mp4      ← solo música (audio-mode: music_only o mixed)
└── deadlift_attempt2_mixed.mp4      ← mezcla original+música (audio-mode: mixed)
```

> ⚠️ **No subas el archivo `with-music` a Instagram** (posts, reels ni historias — todos se escanean). Usa el archivo `for-instagram` y añade la música directamente desde la app de Instagram.

Todos los archivos son MP4 / H.264 / AAC con `-movflags +faststart`, compatibles con Instagram, WhatsApp y Telegram.

---

### Despliegue en producción

<details>
<summary>🖥️ Guía de despliegue — para administradores del sistema (haz clic para expandir)</summary>

> Esta sección está dirigida a quien quiera montar su propia instancia del servidor.
> Los usuarios habituales no necesitan leer esto.

#### Software necesario en el servidor

- Linux con systemd (probado en openSUSE Tumbleweed aarch64 / RPi5 y RPi4)
- `podman` — la app corre dentro de un contenedor OCI; `yt-dlp`, `ffmpeg` y Python van dentro de la imagen
- `nginx`
- `acme.sh` (para el certificado TLS)
- `firewalld` o equivalente

En openSUSE:

```bash
sudo zypper install podman nginx firewalld
```

#### 1. Clonar el repositorio

```bash
git clone https://github.com/raulillo82/powerlifting-clip-extractor.git ~/powerlifting-clip-extractor
cd ~/powerlifting-clip-extractor
```

#### 2. Construir la imagen del contenedor

```bash
podman build -t localhost/powerlifting:latest .
```

La imagen usa OpenSUSE Tumbleweed como base e instala `yt-dlp`, `ffmpeg` y todas las dependencias Python. El primer build tarda unos minutos; los siguientes son rápidos gracias al caché de capas.

#### 3. Datos persistentes

`secret.key` y `users.db` se generan automáticamente al arrancar la app por primera vez y se almacenan **fuera del contenedor**, en el directorio del repositorio. `lifts/` también se monta desde el host. **No los subas al repositorio** (ya están en `.gitignore`).

#### 3b. Geolocalización IP (opcional — panel de estadísticas)

El panel `/admin/stats` incluye un mapa Leaflet con la ciudad de origen de cada job. Para activarlo:

1. Regístrate gratis en [MaxMind](https://www.maxmind.com/en/geolite2/signup) y descarga **GeoLite2 City** (formato MMDB).
2. Coloca el archivo en `~/powerlifting-clip-extractor/GeoLite2-City.mmdb`.
3. Añade el bind mount al servicio (ver paso 4).

Sin el archivo el panel carga igualmente; el mapa aparece vacío. MaxMind publica actualizaciones el primer martes de cada mes — el playbook Ansible despliega automáticamente un systemd timer que descarga la nueva versión esa misma madrugada.

#### 4. Servicio systemd con Podman

Crea `~/.config/systemd/user/powerlifting.service`:

```ini
[Unit]
Description=Powerlifting Clip Extractor (podman)
After=network.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/podman rm -f powerlifting
ExecStart=/usr/bin/podman run \
    --name powerlifting \
    --rm \
    -p 127.0.0.1:5000:5000 \
    -v /home/TU_USUARIO/powerlifting-clip-extractor/users.db:/app/users.db:Z \
    -v /home/TU_USUARIO/powerlifting-clip-extractor/secret.key:/app/secret.key:Z \
    -v /home/TU_USUARIO/powerlifting-clip-extractor/lifts:/app/lifts:Z \
    -v /home/TU_USUARIO/powerlifting-clip-extractor/GeoLite2-City.mmdb:/app/GeoLite2-City.mmdb:Z \
    localhost/powerlifting:latest
ExecStop=/usr/bin/podman stop powerlifting
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now powerlifting.service
loginctl enable-linger TU_USUARIO   # arranque automático sin sesión activa
```

> `Restart=always` relanza el contenedor ante cualquier parada inesperada. No añadas `--restart` a `podman run` — conflictiría con la gestión de systemd.

#### 4. nginx como proxy inverso con HTTPS

Crea `/etc/nginx/conf.d/powerlifting.conf`:

```nginx
server {
    listen 80;
    server_name TU_DOMINIO;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name TU_DOMINIO;

    ssl_certificate     /etc/nginx/ssl/TU_DOMINIO.crt;
    ssl_certificate_key /etc/nginx/ssl/TU_DOMINIO.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 20M;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 600;
        proxy_send_timeout 600;
    }
}
```

```bash
sudo systemctl enable --now nginx
```

#### 5. Certificado TLS gratuito con acme.sh

Se recomienda usar un proveedor DNS con API (DuckDNS, Cloudflare…) para el challenge DNS-01, ya que no requiere abrir el puerto 80.

Ejemplo con DuckDNS:

```bash
git clone --depth 1 https://github.com/acmesh-official/acme.sh.git /tmp/acme_src
cd /tmp/acme_src && ./acme.sh --install --home ~/.acme.sh --accountemail TU_EMAIL --no-cron

export DuckDNS_Token="TU_TOKEN"
~/.acme.sh/acme.sh --issue --dns dns_duckdns -d TU_DOMINIO --server zerossl

sudo mkdir -p /etc/nginx/ssl
~/.acme.sh/acme.sh --install-cert -d TU_DOMINIO \
  --key-file /etc/nginx/ssl/TU_DOMINIO.key \
  --fullchain-file /etc/nginx/ssl/TU_DOMINIO.crt \
  --reloadcmd "sudo systemctl reload nginx"
```

La renovación se gestiona automáticamente mediante el cron que instala acme.sh.

#### 6. Ajustes de seguridad

**SELinux** (si está en modo enforcing):

```bash
sudo setsebool -P httpd_can_network_connect 1
```

**firewalld**:

```bash
sudo firewall-cmd --zone=public --add-service=https --permanent
sudo firewall-cmd --zone=trusted --add-interface=lo --permanent
sudo firewall-cmd --reload
```

#### 7. IP dinámica (opcional)

Si el servidor está en una red con IP residencial dinámica, puedes actualizar el DNS automáticamente con un timer systemd cada 5 minutos.

Guarda el token en `~/.config/duckdns.env`:
```
DUCKDNS_TOKEN=tu_token_aqui
```

Crea `~/.config/systemd/user/duckdns.service`:
```ini
[Unit]
Description=DuckDNS IP update
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=%h/.config/duckdns.env
ExecStart=/usr/bin/curl -s "https://www.duckdns.org/update?domains=TU_SUBDOMINIO&token=${DUCKDNS_TOKEN}&ip=" -o /tmp/duckdns.log
```

Crea `~/.config/systemd/user/duckdns.timer`:
```ini
[Unit]
Description=Actualiza IP en DuckDNS cada 5 minutos

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now duckdns.timer
```

#### 8. Actualización y reinicio

Para desplegar cambios en el código:

```bash
cd /home/TU_USUARIO/powerlifting-clip-extractor
git pull
podman build -t localhost/powerlifting:latest .
systemctl --user restart powerlifting.service
```

Para comprobar el estado y ver los logs:

```bash
systemctl --user status powerlifting.service
journalctl --user -u powerlifting.service -n 50 --no-pager
podman logs powerlifting   # logs del contenedor en ejecución
```

#### 9. Despliegue automático con Ansible (opcional)

El repositorio incluye un playbook Ansible en `ansible/` que automatiza todo el despliegue (instalación de podman, build de la imagen, service file, nginx, linger):

```bash
# Verificar sin aplicar cambios
ansible-playbook -i ansible/inventory.yml ansible/deploy.yml -l rpi4 --check

# Desplegar (pide contraseña sudo para las tareas de nginx)
ansible-playbook -i ansible/inventory.yml ansible/deploy.yml -l rpi4 --ask-become-pass
```

El playbook está pensado para el failover RPi5↔RPi4. Consulta los comentarios al inicio de `ansible/deploy.yml` para los pasos previos al failover (copiar `secret.key`, `users.db` y certificados SSL).

</details>

### Desarrollo

<details>
<summary>🛠️ Guía para contribuidores y desarrolladores (haz clic para expandir)</summary>

#### Entorno de desarrollo

```bash
git clone https://github.com/raulillo82/powerlifting-clip-extractor.git
cd powerlifting-clip-extractor
python3 -m venv venv
venv/bin/pip install -r requirements.txt pytest
```

#### Tests

```bash
venv/bin/python3 -m pytest          # todos los tests (133)
venv/bin/python3 -m pytest test_extract_lifts.py   # solo lógica de extracción (41)
venv/bin/python3 -m pytest test_app.py             # solo rutas web y autenticación (92)
```

El hook de pre-commit ejecuta ambos ficheros automáticamente antes de cada commit. Usa el Python del venv si existe, o el del sistema si no.

#### Rate limiting

Los límites se configuran en `app.py` y `auth.py` (decoradores `@limiter.limit`):

| Ruta | Límite | Clave |
|---|---|---|
| `POST /register` | 3 por 15 minutos | IP |
| `POST /login` | 20 por minuto | IP |
| `POST /run` | 1 por 2 minutos | ID de usuario autenticado |

En los tests, el rate limiting se desactiva en el fixture `client` mediante `monkeypatch.setattr(limiter, "enabled", False)`. Los tests de la clase `TestRateLimit` usan fixtures separados (`client_limited`, `anon_client_limited`) que resetean el storage entre pruebas con `limiter.reset()`.

> Nota técnica: Flask-Limiter 4.x almacena `enabled` como atributo de instancia (fijado en `init_app`), no lo lee de `app.config` en cada request. Por eso `RATELIMIT_ENABLED = False` en config no funciona para desactivarlo en tests — hay que usar `monkeypatch` directamente sobre el objeto.

</details>

---

## English <a name="english"></a>

> 🇪🇸 [Leer en español ↑](#español)

---

### Table of contents

- [Roadmap](#roadmap)
- [Requirements](#requirements)
- [Usage](#usage)
- [Options](#options)
- [Output](#output)
- [Production deployment](#production-deployment)
- [Development](#development)

---

### Roadmap

| Status | Task | Owner |
|---|---|---|
| ✅ | Individual clip download with `yt-dlp` and `--download-sections` | — |
| ✅ | Combined video with 3 lifts stacked vertically | — |
| ✅ | Instagram / WhatsApp / Telegram compatibility (H.264 + AAC + faststart) | — |
| ✅ | Per-movement clip duration (squat / bench / deadlift) | — |
| ✅ | Freeze on last frame for clips of different lengths | — |
| ✅ | Low-res previews for slow devices | — |
| ✅ | Interactive mode and parameter (CLI) mode | — |
| ✅ | Music on combined video with configurable start point | — |
| ✅ | Automated tests (`pytest`, 34 tests, CI with GitHub Actions) | — |
| ✅ | Bilingual ES/EN web interface (Flask) | — |
| ✅ | Auth system (registration, login, device binding) | — |
| ✅ | User admin panel | — |
| ✅ | Job queue with pool of 2 parallel workers | — |
| ✅ | Dry-run mode for testing without real downloads | — |
| ✅ | Web route tests (80 tests, CI) | — |
| ✅ | Rate limiting (Flask-Limiter): `/register` 3/15 min, `/run` 1/2 min per user, `/login` 20/min | — |
| ✅ | Production deployment (RPi5, nginx, gunicorn, HTTPS) | — |
| ✅ | **Single lift mode** (1 timestamp, 1 movement; original / music-only / mixed audio) | — |
| ✅ | Staging environment on RPi5 (`/staging/`): STAGING badge, admin-only access, rate limit exemption | — |
| ✅ | In-channel search (for competitions not in any playlist) | — |
| ✅ | Real-time download progress ticker (every 5 s) | — |
| ✅ | Music default 50%; private/deleted video filtering in cascade | — |
| ✅ | YouTube link next to selected session; cascade back-navigation fix | — |
| ✅ | New federations in cascade: USAPL, British Powerlifting, CPU (IPF affiliates only) | — |
| ✅ | Job history page (`/history`): last 20 jobs with status and download link | — |
| ✅ | Automated tests (133 tests, CI) | — |
| ✅ | Podman container (reproducible OCI image, OpenSUSE Tumbleweed base) | — |
| ✅ | Ansible playbook for deployment and RPi5↔RPi4 failover | — |
| ✅ | **Statistics panel** (`/admin/stats`) — Leaflet city map (GeoLite2), Chart.js charts (success/error, music, federations, mode, time of day), 7/30/90d/all filter, live queue snapshot | — |

Extracts individual lifts from a YouTube powerlifting competition and creates an Instagram-compatible combined video with squat, bench and deadlift stacked vertically.

---

### Requirements

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`sudo zypper install yt-dlp` on openSUSE)
- ffmpeg with H.264 support — on openSUSE, the official package **excludes H.264** for patent reasons; install from [Packman](https://packman.links2linux.de/):
  ```bash
  sudo zypper addrepo -cfp 90 https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/Essentials packman-essentials
  sudo zypper --gpg-auto-import-keys refresh packman-essentials
  sudo zypper --non-interactive install --allow-vendor-change --from packman-essentials ffmpeg-7
  ```

---

### Usage

#### Interactive mode

Run without arguments and answer the prompts (press Enter to accept defaults):

```bash
python3 extract_lifts.py
```

#### Parameter mode — timestamps from file

```bash
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID
```

Reads timestamps from `times.txt` by default (use `--times other.txt` to override).

#### Parameter mode — timestamps inline

```bash
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --timestamps 0:21:27 0:29:55 0:38:15 1h23:30 1h32:21 1h41:30 2h26:15 2h33:4 2h41:35
```

#### Single lift mode (`--single`)

Extract one lift only: one timestamp, one movement, no combined video.

```bash
# Original audio only (no copyright risk)
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --single --timestamp 2h26:15 --movement deadlift --attempt 2

# Music only (replaces original audio)
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --single --timestamp 2h26:15 --movement deadlift --attempt 2 \
    --audio-mode music_only --music "song name or YouTube URL"

# Mixed (original + music blended; generates 3 files)
python3 extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --single --timestamp 2h26:15 --movement deadlift --attempt 2 \
    --audio-mode mixed --music "https://www.youtube.com/watch?v=..."
```

---

### Options

| Option | Default | Description |
|---|---|---|
| `--times FILE` | `times.txt` | File with 9 start timestamps |
| `--timestamps t1..t9` | — | 9 timestamps inline (overrides `--times`) |
| `--duration SECS` | `60` | Duration of each clip in seconds |
| `--squat {1,2,3}` | `3` | Squat attempt in combined video |
| `--bench {1,2,3}` | `3` | Bench attempt in combined video |
| `--deadlift {1,2,3}` | `3` | Deadlift attempt in combined video |
| `--output-dir DIR` | `lifts/` | Output directory |
| `--skip-individual` | — | Use existing clips, skip downloads |
| `--skip-combined` | — | Only download clips, skip combined video |
| `--music URL_OR_QUERY` | — | Add music to combined video (YouTube URL recommended) |
| `--music-start MM:SS` | `0:00` | Start point in the song |
| `--preview [WIDTH]` | — | Generate low-res copies in `preview/` |
| `--duration-squat SECS` | same as `--duration` | Clip duration for squats |
| `--duration-bench SECS` | same as `--duration` | Clip duration for bench press |
| `--duration-deadlift SECS` | same as `--duration` | Clip duration for deadlifts |
| `--no-replay` | — | Use only if the video has no slow-motion replays |
| `--single` | — | Single lift mode (requires `--timestamp`) |
| `--timestamp TS` | — | Lift timestamp (e.g. `2h26:15`). Only with `--single` |
| `--movement` | `squat` | `squat` / `bench` / `deadlift`. Only with `--single` |
| `--attempt {1,2,3}` | `3` | Attempt number (output filename only). Only with `--single` |
| `--audio-mode` | `original` | `original` / `music_only` / `mixed`. Only with `--single` |

### Timestamp file format (`times.txt`)

One timestamp per line, 9 total (squats 1–3, bench 1–3, deadlift 1–3). Mixed formats accepted:

```
0:21:27
0:29:55
0:38:15
1h23:30
1h32:21
1h41:30
2h26:15
2h33:4
2h41:35
```

---

### Output

**Full mode (9 timestamps):**
```
lifts/
├── lift_01_squat_attempt1.mp4            ← individual clip with original audio
│   ...
├── lift_09_deadlift_attempt3.mp4
├── combined_s3_b3_d3_for-instagram.mp4  ← no music; upload this to Instagram
│                                           (add music inside the Instagram app)
├── combined_s3_b3_d3_with-music.mp4     ← with music; ideal for WhatsApp,
│                                           Telegram, or personal use on your phone
└── preview/                              ← low-res copies (--preview)
```

**Single lift mode (`--single`):**
```
lifts/
├── deadlift_attempt2_original.mp4   ← original audio (always generated)
├── deadlift_attempt2_music.mp4      ← music only (audio-mode: music_only or mixed)
└── deadlift_attempt2_mixed.mp4      ← original + music blended (audio-mode: mixed)
```

> ⚠️ **Do not upload the `with-music` file to Instagram** (posts, reels or stories — all are scanned). Use the `for-instagram` file and add music directly inside the Instagram app.

All files are MP4 / H.264 / AAC with `-movflags +faststart`, compatible with Instagram, WhatsApp and Telegram.

---

### Production deployment

<details>
<summary>🖥️ Deployment guide — for system administrators (click to expand)</summary>

> This section is intended for anyone who wants to run their own instance of the server.
> Regular users do not need to read this.

#### Server requirements

- Linux with systemd (tested on openSUSE Tumbleweed aarch64 / RPi5 and RPi4)
- `podman` — the app runs inside an OCI container; `yt-dlp`, `ffmpeg` and Python are bundled in the image
- `nginx`
- `acme.sh` (for the TLS certificate)
- `firewalld` or equivalent

On openSUSE:

```bash
sudo zypper install podman nginx firewalld
```

#### 1. Clone the repository

```bash
git clone https://github.com/raulillo82/powerlifting-clip-extractor.git ~/powerlifting-clip-extractor
cd ~/powerlifting-clip-extractor
```

#### 2. Build the container image

```bash
podman build -t localhost/powerlifting:latest .
```

The image uses OpenSUSE Tumbleweed as its base and installs `yt-dlp`, `ffmpeg` and all Python dependencies. The first build takes a few minutes; subsequent builds are fast thanks to layer caching.

#### 3. Persistent data

`secret.key` and `users.db` are generated automatically on first start and stored **outside the container**, in the repository directory. `lifts/` is also mounted from the host. **Do not commit them** (already in `.gitignore`).

#### 3b. IP geolocation (optional — statistics panel)

The `/admin/stats` panel includes a Leaflet map showing the city of origin for each job. To enable it:

1. Sign up for a free account at [MaxMind](https://www.maxmind.com/en/geolite2/signup) and download **GeoLite2 City** (MMDB format).
2. Place the file at `~/powerlifting-clip-extractor/GeoLite2-City.mmdb`.
3. Add the bind mount to your service unit (see step 4).

The panel works without the file — the map simply appears empty. MaxMind publishes updates on the first Tuesday of every month — the Ansible playbook automatically deploys a systemd timer that downloads the new version overnight.

#### 4. Podman systemd user service

Create `~/.config/systemd/user/powerlifting.service`:

```ini
[Unit]
Description=Powerlifting Clip Extractor (podman)
After=network.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/podman rm -f powerlifting
ExecStart=/usr/bin/podman run \
    --name powerlifting \
    --rm \
    -p 127.0.0.1:5000:5000 \
    -v /home/YOUR_USER/powerlifting-clip-extractor/users.db:/app/users.db:Z \
    -v /home/YOUR_USER/powerlifting-clip-extractor/secret.key:/app/secret.key:Z \
    -v /home/YOUR_USER/powerlifting-clip-extractor/lifts:/app/lifts:Z \
    -v /home/YOUR_USER/powerlifting-clip-extractor/GeoLite2-City.mmdb:/app/GeoLite2-City.mmdb:Z \
    localhost/powerlifting:latest
ExecStop=/usr/bin/podman stop powerlifting
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now powerlifting.service
loginctl enable-linger YOUR_USER   # start on boot without an active session
```

> `Restart=always` restarts the container on any unexpected stop. Do not add `--restart` to `podman run` — it would conflict with systemd's restart management.

#### 4. nginx reverse proxy with HTTPS

Create `/etc/nginx/conf.d/powerlifting.conf`:

```nginx
server {
    listen 80;
    server_name YOUR_DOMAIN;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name YOUR_DOMAIN;

    ssl_certificate     /etc/nginx/ssl/YOUR_DOMAIN.crt;
    ssl_certificate_key /etc/nginx/ssl/YOUR_DOMAIN.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 20M;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 600;
        proxy_send_timeout 600;
    }
}
```

```bash
sudo systemctl enable --now nginx
```

#### 5. Free TLS certificate with acme.sh

Using a DNS provider with an API (DuckDNS, Cloudflare…) for the DNS-01 challenge is recommended — it does not require opening port 80.

Example with DuckDNS:

```bash
git clone --depth 1 https://github.com/acmesh-official/acme.sh.git /tmp/acme_src
cd /tmp/acme_src && ./acme.sh --install --home ~/.acme.sh --accountemail YOUR_EMAIL --no-cron

export DuckDNS_Token="YOUR_TOKEN"
~/.acme.sh/acme.sh --issue --dns dns_duckdns -d YOUR_DOMAIN --server zerossl

sudo mkdir -p /etc/nginx/ssl
~/.acme.sh/acme.sh --install-cert -d YOUR_DOMAIN \
  --key-file /etc/nginx/ssl/YOUR_DOMAIN.key \
  --fullchain-file /etc/nginx/ssl/YOUR_DOMAIN.crt \
  --reloadcmd "sudo systemctl reload nginx"
```

Renewal is handled automatically by the cron job acme.sh installs.

#### 6. Security settings

**SELinux** (if running in enforcing mode):

```bash
sudo setsebool -P httpd_can_network_connect 1
```

**firewalld**:

```bash
sudo firewall-cmd --zone=public --add-service=https --permanent
sudo firewall-cmd --zone=trusted --add-interface=lo --permanent
sudo firewall-cmd --reload
```

#### 7. Dynamic DNS (optional)

If the server is on a residential connection with a dynamic IP, you can update DNS automatically every 5 minutes using a systemd timer.

Store the token in `~/.config/duckdns.env`:
```
DUCKDNS_TOKEN=your_token_here
```

Create `~/.config/systemd/user/duckdns.service`:
```ini
[Unit]
Description=DuckDNS IP update
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=%h/.config/duckdns.env
ExecStart=/usr/bin/curl -s "https://www.duckdns.org/update?domains=YOUR_SUBDOMAIN&token=${DUCKDNS_TOKEN}&ip=" -o /tmp/duckdns.log
```

Create `~/.config/systemd/user/duckdns.timer`:
```ini
[Unit]
Description=Update DuckDNS IP every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now duckdns.timer
```

#### 8. Updating and restarting

To deploy code changes:

```bash
cd /home/YOUR_USER/powerlifting-clip-extractor
git pull
podman build -t localhost/powerlifting:latest .
systemctl --user restart powerlifting.service
```

To check status and view logs:

```bash
systemctl --user status powerlifting.service
journalctl --user -u powerlifting.service -n 50 --no-pager
podman logs powerlifting   # logs from the running container
```

#### 9. Automated deployment with Ansible (optional)

The repository includes an Ansible playbook under `ansible/` that automates the full deployment (podman installation, image build, service file, nginx, linger):

```bash
# Dry run — no changes applied
ansible-playbook -i ansible/inventory.yml ansible/deploy.yml -l rpi4 --check

# Deploy (prompts for sudo password for nginx tasks)
ansible-playbook -i ansible/inventory.yml ansible/deploy.yml -l rpi4 --ask-become-pass
```

The playbook is designed for RPi5↔RPi4 failover. See the comments at the top of `ansible/deploy.yml` for the steps required before failing over (copying `secret.key`, `users.db` and SSL certificates).

</details>

### Development

<details>
<summary>🛠️ Guide for contributors and developers (click to expand)</summary>

#### Development environment

```bash
git clone https://github.com/raulillo82/powerlifting-clip-extractor.git
cd powerlifting-clip-extractor
python3 -m venv venv
venv/bin/pip install -r requirements.txt pytest
```

#### Tests

```bash
venv/bin/python3 -m pytest          # all tests (133)
venv/bin/python3 -m pytest test_extract_lifts.py   # extraction logic only (41)
venv/bin/python3 -m pytest test_app.py             # web routes and auth only (92)
```

The pre-commit hook runs both files automatically before each commit, using the venv Python if it exists.

#### Rate limiting

Limits are configured in `app.py` and `auth.py` via `@limiter.limit` decorators:

| Route | Limit | Key |
|---|---|---|
| `POST /register` | 3 per 15 minutes | IP |
| `POST /login` | 20 per minute | IP |
| `POST /run` | 1 per 2 minutes | authenticated user ID |

In tests, rate limiting is disabled in the `client` fixture via `monkeypatch.setattr(limiter, "enabled", False)`. The `TestRateLimit` class uses separate fixtures (`client_limited`, `anon_client_limited`) that call `limiter.reset()` to clear storage between tests.

> Technical note: Flask-Limiter 4.x stores `enabled` as an instance attribute (set during `init_app`), not read from `app.config` on each request. This is why setting `RATELIMIT_ENABLED = False` in config does not work in tests — you must monkeypatch the object directly.

</details>
