# Powerlifting Clip Extractor

---

> 🇬🇧 [Read in English ↓](#english)

---

## Español

Extrae levantamientos individuales de un vídeo de competición de powerlifting en YouTube y genera un vídeo combinado compatible con Instagram con sentadilla, press de banca y peso muerto apilados verticalmente.

### Requisitos

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`sudo zypper install yt-dlp` en openSUSE)
- ffmpeg (`sudo zypper install ffmpeg`)

### Uso

#### Modo interactivo

Ejecuta sin argumentos y responde a las preguntas (pulsa Enter para aceptar los valores por defecto):

```bash
python extract_lifts.py
```

#### Modo parámetros — tiempos desde archivo

```bash
python extract_lifts.py https://youtube.com/live/VIDEO_ID
```

Lee los tiempos de `times.txt` por defecto (usa `--times otro.txt` para cambiarlo).

#### Modo parámetros — tiempos en la propia llamada

```bash
python extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --timestamps 0:21:27 0:29:55 0:38:15 1h23:30 1h32:21 1h41:30 2h26:15 2h33:4 2h41:35
```

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

### Archivos generados

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

> ⚠️ **No subas el archivo `with-music` a Instagram** (posts, reels ni historias — todos se escanean). Usa el archivo `for-instagram` y añade la música directamente desde la app de Instagram.

Todos los archivos son MP4 / H.264 / AAC con `-movflags +faststart`, compatibles con Instagram, WhatsApp y Telegram.

---

## English <a name="english"></a>

> 🇪🇸 [Leer en español ↑](#español)

---

Extracts individual lifts from a YouTube powerlifting competition and creates an Instagram-compatible combined video with squat, bench and deadlift stacked vertically.

### Requirements

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`sudo zypper install yt-dlp` on openSUSE)
- ffmpeg (`sudo zypper install ffmpeg`)

### Usage

#### Interactive mode

Run without arguments and answer the prompts (press Enter to accept defaults):

```bash
python extract_lifts.py
```

#### Parameter mode — timestamps from file

```bash
python extract_lifts.py https://youtube.com/live/VIDEO_ID
```

Reads timestamps from `times.txt` by default (use `--times other.txt` to override).

#### Parameter mode — timestamps inline

```bash
python extract_lifts.py https://youtube.com/live/VIDEO_ID \
    --timestamps 0:21:27 0:29:55 0:38:15 1h23:30 1h32:21 1h41:30 2h26:15 2h33:4 2h41:35
```

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

### Output

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

> ⚠️ **Do not upload the `with-music` file to Instagram** (posts, reels or stories — all are scanned). Use the `for-instagram` file and add music directly inside the Instagram app.

All files are MP4 / H.264 / AAC with `-movflags +faststart`, compatible with Instagram, WhatsApp and Telegram.
