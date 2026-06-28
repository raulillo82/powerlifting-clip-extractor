#!/usr/bin/env python3
"""
find_lifter.py — Detección automática de timestamps de un levantador en vídeo de powerlifting.

Uso:
    python3 find_lifter.py <youtube_url> <apellido> [--federation IPF] [--work-dir /tmp/find_lifter]

Salida (stdout): JSON con los timestamps detectados en segundos.
    {
        "squat":     [t1, t2, t3],
        "bench":     [t1, t2, t3],
        "deadlift":  [t1, t2, t3],
        "comp_start": t,
        "elapsed_s":  t
    }

Progreso (stderr): una línea por frame procesado.

Requiere:
    ffmpeg, yt-dlp, tesseract-ocr (+ traindata spa), python3-pytesseract, Pillow, numpy

Instalación en OpenSUSE Tumbleweed:
    sudo zypper install -y tesseract-ocr tesseract-ocr-traineddata-spa \
        python3-pytesseract python3-Pillow python3-numpy
"""

import os, sys, argparse, subprocess, time, difflib, re, json, unicodedata, threading

# Tesseract lanza hilos OpenMP internos. Con varios procesos OCR concurrentes
# (ver SCAN_WORKERS) eso sobre-suscribe la CPU y dispara el tiempo por frame
# (~140ms → ~1500ms medido). Limitar a 1 hilo por proceso lo evita: sobre estos
# crops minúsculos OMP no aporta nada, y además hace el OCR más reproducible.
# setdefault para respetar un valor externo si se fija a propósito.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

# Read-write lock: múltiples ffmpeg OK (lectura), OCR exclusivo (escritura).
# El engine LSTM bajo carga CPU concurrente (varios ffmpeg simultáneos) produce
# resultados distintos en ARM (RPi5). OCR espera a que todos los ffmpeg en curso
# terminen, y bloquea nuevos ffmpeg mientras corre Tesseract.
class _FfmpegOcrLock:
    def __init__(self):
        self._cond = threading.Condition()
        self._ffmpeg = 0
        self._ocr = False

    def ffmpeg_enter(self):
        with self._cond:
            while self._ocr:
                self._cond.wait()
            self._ffmpeg += 1

    def ffmpeg_exit(self):
        with self._cond:
            self._ffmpeg -= 1
            self._cond.notify_all()

    def ocr_enter(self):
        with self._cond:
            while self._ffmpeg > 0 or self._ocr:
                self._cond.wait()
            self._ocr = True

    def ocr_exit(self):
        with self._cond:
            self._ocr = False
            self._cond.notify_all()

_scan_rw = _FfmpegOcrLock()

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from PIL import Image
import numpy as np, pytesseract

# ── Configuración por federación ──────────────────────────────────────────────

FORMATS = {
    "AEP": {
        "banner_crop":       (0.00, 0.78, 0.45, 1.00),  # inferior-izquierdo
        "banner_color":      "yellow",
        "banner_min_px":     150,
        "timer_crops":       [(0.78, 0.88, 1.00, 1.00),  # esquina inf-der
                              (0.78, 0.00, 1.00, 0.12)], # esquina sup-der
        "timer_color":       "red",
        "has_precomp_timer": True,
    },
    "IPF": {
        # Overlay inferior: fondo azul oscuro (R≈34, G≈64, B≈118), texto blanco.
        # Nombre en dos líneas: "Firstname" / "APELLIDO(S)".
        # Timer del intento (01:00) en la zona derecha del mismo banner.
        # No hay timer pre-competición visible → comp_start=0 como fallback.
        # require_timer_in_banner: filtra falsos positivos de clasificaciones (tablas de
        # resultados que muestran el nombre del levantador pero sin timer en directo).
        "banner_crop":            (0.00, 0.73, 0.80, 0.96),
        "banner_color":           "blue",
        "banner_min_px":          3000,
        "timer_crops":            [(0.00, 0.87, 0.14, 0.99)],
        "timer_color":            "blue",
        "has_precomp_timer":      False,
        "require_timer_in_banner": True,
    },
}

# ── Parámetros de detección ────────────────────────────────────────────────────

# Los valores hardcoded a continuación corresponden al formato AEP (por defecto).
# El formato activo se selecciona vía --federation y sobreescribe estos valores en main().
BANNER_CROP      = FORMATS["AEP"]["banner_crop"]
TIMER_CROPS      = FORMATS["AEP"]["timer_crops"]
TIMER_CROP       = TIMER_CROPS[0]  # alias de compatibilidad
YELLOW_H_RANGE   = (10, 33)                   # hue en rango HSV escalado 0-180
YELLOW_MIN_S     = 100
YELLOW_MIN_V     = 100
YELLOW_MIN_PX    = 150                        # píxeles mínimos para activar OCR
OCR_SCALE        = 3                          # escala de la imagen binarizada para tesseract
TIMER_SCALE      = 4                          # escala del crop del timer
FUZZY_RATIO      = 0.70
GROUP_GAP_S      = 90                         # segundos de gap para separar grupos
SCAN_STEP_S      = 10                         # step del scan denso
TIMER_STEP_S     = 60                         # step del scan del timer de descanso
BREAK_TIMER_MIN  = 120                        # segundos mínimos para considerar timer de descanso
EARLY_STOP_N     = 3                          # detener el scan al completar N grupos
REFINE_BEFORE_S  = 12                         # segundos antes de min(g) para refinar inicio
REFINE_AFTER_S   = 20                         # segundos después de max(g) para refinar fin
REFINE_STEP_S    = 2                          # step del scan de refinamiento
ISOLATED_HIT_GAP_S = SCAN_STEP_S + 5         # gap mínimo para considerar el primer hit aislado
SCAN_WORKERS     = 6                          # hilos para extraer+OCR frames en paralelo
SCAN_BATCH       = 12                         # frames por lote antes de evaluar early-stop
MAX_CONSECUTIVE_ERRORS = 5                    # bail-out si la URL ha expirado

# Acumulador global de coste (se actualiza solo desde el hilo principal → sin race).
_STATS = {"frames": 0, "ff_ms": 0, "ocr_ms": 0}


def _record(frames, ff_ms, ocr_ms):
    _STATS["frames"] += frames
    _STATS["ff_ms"]  += ff_ms
    _STATS["ocr_ms"] += ocr_ms


def err(msg):
    ts = datetime.now().astimezone().strftime('%H:%M:%S %Z')
    print(f'[{ts}] {msg}', file=sys.stderr, flush=True)


def _hms(secs):
    s = int(secs)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def _normalize(text: str) -> str:
    """Quita tildes y convierte a mayúsculas para comparación robusta."""
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").upper()


def extract_frame(url, secs, out):
    _scan_rw.ffmpeg_enter()
    try:
        try:
            subprocess.run(
                ["ffmpeg", "-ss", str(secs), "-i", url, "-frames:v", "1",
                 "-q:v", "3", "-vf", "scale=1280:-1", str(out), "-y"],
                capture_output=True, timeout=30)
            return out.exists() and out.stat().st_size > 0
        except subprocess.TimeoutExpired:
            return False
    finally:
        _scan_rw.ffmpeg_exit()


def _hsv_hue(arr):
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(mx == 0, 0, d / mx * 255)
    v = mx
    hh = np.zeros_like(r)
    mr = (mx == r) & (d != 0)
    mg = (mx == g) & (d != 0)
    mb = (mx == b) & (d != 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        hh[mr] = (60 * ((g[mr] - b[mr]) / d[mr]) % 360) / 2
        hh[mg] = (60 * ((b[mg] - r[mg]) / d[mg] + 2)) / 2
        hh[mb] = (60 * ((r[mb] - g[mb]) / d[mb] + 4)) / 2
    return hh, s, v


def yellow_mask(path):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    x0, y0, x1, y1 = BANNER_CROP
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    arr = np.array(crop).astype(float)
    hh, s, v = _hsv_hue(arr)
    lo, hi = YELLOW_H_RANGE
    return (hh >= lo) & (hh <= hi) & (s >= YELLOW_MIN_S) & (v >= YELLOW_MIN_V)


def blue_mask(path, banner_crop):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    x0, y0, x1, y1 = banner_crop
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    arr = np.array(crop).astype(float)
    # Umbral permisivo: cubre el azul oscuro IPF (R≈34, G≈64, B≈118) y azules más saturados.
    return (arr[:, :, 2] > 80) & (arr[:, :, 0] < 80) & (arr[:, :, 1] < 100)


def _token_matches_word(tok, word):
    """True si tok encaja con word mediante ratio difuso o subconjunto.

    Incluye sliding-window fuzzy: busca tok dentro de una palabra más larga
    comparando ventanas de la misma longitud. Esto detecta casos donde el OCR
    pega dos apellidos como uno solo ("EAMPANODIAZ" → contiene "CAMPANO").
    """
    if len(word) < max(4, len(tok) - 2):
        return False
    if difflib.SequenceMatcher(None, tok, word).ratio() >= FUZZY_RATIO:
        return True
    if abs(len(tok) - len(word)) <= 2 and (tok in word or word in tok):
        return True
    # Sliding window: busca tok en ventanas del mismo tamaño dentro de palabras largas
    n = len(tok)
    if n >= 4 and len(word) > n:
        for i in range(len(word) - n + 1):
            if difflib.SequenceMatcher(None, tok, word[i:i + n]).ratio() >= FUZZY_RATIO:
                return True
    return False


def _match_token(raw, token):
    """Decide si el texto OCR `raw` contiene el apellido `token`.

    Función pura (sin imagen ni red) para poder testearla. Devuelve
    (texto_para_log, encontrado).
    """
    text = raw.upper()  # for log display
    text_cmp = _normalize(raw)  # accentless for comparison

    ocr_words = [w.strip(".,;:!?-_|/\\\"'()[]{}¡¿") for w in re.split(r'[\s\-]+', text_cmp)]
    ocr_words = [w for w in ocr_words if w]

    # Split token into sub-tokens on spaces AND hyphens (OCR often separates
    # compound surnames like SANCHEZ-INFANTE into two words).
    sub_tokens = [t for t in re.split(r'[\s\-]+', _normalize(token)) if len(t) >= 3]
    if not sub_tokens:
        return text, False

    # Regla apellido/nombre:
    # 1 token  → obligatorio
    # 2 tokens → 1º obligatorio (apellido), 2º opcional (nombre de pila)
    # 3 tokens → 1º+2º obligatorios (apellido compuesto), 3º opcional (nombre)
    # 4+       → 1º+2º obligatorios, resto ignorado
    n = len(sub_tokens)
    if n >= 3:
        required = sub_tokens[:2]
    elif n == 2:
        required = sub_tokens[:1]
    else:
        required = sub_tokens
    failures = sum(1 for tok in required
                   if not any(_token_matches_word(tok, w) for w in ocr_words))
    return text, failures == 0


_TIMER_IN_TEXT_RE = re.compile(r'\b\d{1,2}:\d{2}\b')


def ocr_banner(path, token, fmt):
    banner_crop = fmt["banner_crop"]
    banner_color = fmt["banner_color"]
    min_px = fmt["banner_min_px"]

    if banner_color == "yellow":
        mask = yellow_mask(path)
        if mask.sum() < min_px:
            return "", False
        bin_arr = np.zeros((*mask.shape, 3), dtype=np.uint8)
        bin_arr[mask] = 255
    else:  # blue: fondo azul, texto blanco
        mask = blue_mask(path, banner_crop)
        if mask.sum() < min_px:
            return "", False
        img = Image.open(path).convert("RGB")
        w, h = img.size
        x0, y0, x1, y1 = banner_crop
        arr = np.array(img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1))))
        # Extraer píxeles blancos (los dígitos/letras) del banner azul.
        # Umbral 175 (no 200) para recuperar píxeles intermedios y espacios entre letras.
        white = (arr[:, :, 0] > 175) & (arr[:, :, 1] > 175) & (arr[:, :, 2] > 175)
        # Filtrar: solo filas que tengan píxeles azules significativos.
        # El crop puede incluir el fondo de la sala (beige/blanco) por encima del overlay;
        # esas filas tienen 0 píxeles azules y generan ruido que confunde al OCR.
        row_has_blue = mask.sum(axis=1) > 20
        white[~row_has_blue] = False
        bin_arr = np.zeros((*white.shape, 3), dtype=np.uint8)
        bin_arr[white] = 255

    pil = Image.fromarray(bin_arr).resize(
        (bin_arr.shape[1] * OCR_SCALE, bin_arr.shape[0] * OCR_SCALE), Image.NEAREST)
    _scan_rw.ocr_enter()
    try:
        raw = pytesseract.image_to_string(
            pil, config="--oem 3 --psm 6 -l spa").replace("\n", " ").strip()
    finally:
        _scan_rw.ocr_exit()
    text, found = _match_token(raw, token)
    # Clasificaciones y tablas de resultados muestran el nombre sin timer en directo.
    # Si require_timer_in_banner, rechazar hits donde no hay señal de ticker en vivo.
    if found and fmt.get("require_timer_in_banner"):
        # Aceptar si: (a) hay timer MM:SS, (b) hay "RANK" (DL usa formato distinto),
        # o (c) hay patrón IPF de ticker en vivo "OP-<peso>KG" — cubre el caso donde el
        # timer existe en el frame pero el OCR lo garblifica (lee "1"" en vez de "00:58").
        # Las tablas de clasificación nunca contienen "OP-<N>KG" en el OCR.
        has_timer      = bool(_TIMER_IN_TEXT_RE.search(raw))
        has_rank       = bool(re.search(r'\bRANK\b', raw.upper()))
        has_ipf_ticker = bool(re.search(r'OP-\d', raw.upper()))
        if not has_timer and not has_rank and not has_ipf_ticker:
            return text, False
    return text, found


def _read_timer_crop(img, w, h, x0, y0, x1, y1, timer_color="red"):
    """Intenta leer el timer en la región especificada. Devuelve segundos o None."""
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    arr = np.array(crop)

    if timer_color == "red":
        bg_mask = (arr[:, :, 0] > 120) & (arr[:, :, 1] < 80) & (arr[:, :, 2] < 80)
        min_bg_px = 200
    else:  # blue
        bg_mask = (arr[:, :, 2] > 80) & (arr[:, :, 0] < 80) & (arr[:, :, 1] < 100)
        min_bg_px = 50

    if bg_mask.sum() > min_bg_px:
        rows = np.where(bg_mask.any(axis=1))[0]
        cols = np.where(bg_mask.any(axis=0))[0]
        pad = 4
        r0 = max(0, rows[0] - pad)
        r1 = min(arr.shape[0] - 1, rows[-1] + pad)
        c0 = max(0, cols[0] - pad)
        c1 = min(arr.shape[1] - 1, cols[-1] + pad)
        crop_arr = arr[r0:r1 + 1, c0:c1 + 1]
        psm = 7
    else:
        crop_arr = arr
        psm = 6

    if timer_color == "blue":
        # Dígitos blancos sobre fondo azul: extraer píxeles blancos para el OCR
        white = (crop_arr[:, :, 0] > 180) & (crop_arr[:, :, 1] > 180) & (crop_arr[:, :, 2] > 180)
        bin_img = np.zeros(crop_arr.shape[:2], dtype=np.uint8)
        bin_img[white] = 255
        crop_pil = Image.fromarray(bin_img)
    else:
        crop_pil = Image.fromarray(crop_arr)

    crop4 = crop_pil.resize((crop_pil.width * TIMER_SCALE, crop_pil.height * TIMER_SCALE), Image.NEAREST)
    text = pytesseract.image_to_string(
        crop4, config=f"--oem 3 --psm {psm} -l spa -c tessedit_char_whitelist=0123456789:").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def read_timer(path, fmt):
    """Lee el timer del frame usando las regiones y color de fondo del formato activo."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    for crop_box in fmt["timer_crops"]:
        t = _read_timer_crop(img, w, h, *crop_box, timer_color=fmt["timer_color"])
        if t is not None:
            return t
    return None


def detect_comp_start(url, work_dir, fmt, max_probe_s=360):
    """Lee el timer pre-competición en frames tempranos para calcular comp_start.

    Si la federación no tiene timer pre-competición visible, devuelve 0 directamente
    y el scan de sentadilla arrancará desde el inicio del vídeo.
    """
    if not fmt["has_precomp_timer"]:
        err("  [comp_start] federación sin timer pre-competición → comp_start=0s")
        return 0
    err("  [comp_start] buscando timer pre-competición...")
    for probe in range(30, max_probe_s + 1, 30):
        out = work_dir / f"pre_{probe:05d}.jpg"
        if not extract_frame(url, probe, out):
            continue
        t = read_timer(out, fmt)
        err(f"  [comp_start] @{probe}s ({_hms(probe)}) → timer={t!r}")
        if t is not None and 30 < t < 7200:
            comp_start = probe + t
            err(f"  [comp_start] → {comp_start}s ({_hms(comp_start)})")
            return comp_start
    err("  [comp_start] timer no legible — usando 0s como fallback")
    return 0


def _scan_one(url, work_dir, secs, token, prefix, fmt):
    """Extrae un frame y le pasa el OCR. Devuelve tiempos de ffmpeg y OCR por separado.

    Pensado para ejecutarse en un ThreadPool: ffmpeg y tesseract son subprocesos
    que liberan el GIL, así que varios frames avanzan en paralelo.
    """
    out = work_dir / f"{prefix}_{secs:06d}.jpg"
    t0 = time.perf_counter()
    ok = extract_frame(url, secs, out)
    t1 = time.perf_counter()
    if not ok:
        return secs, False, "", False, int((t1 - t0) * 1000), 0
    text, found = ocr_banner(out, token, fmt)
    t2 = time.perf_counter()
    return secs, True, text, found, int((t1 - t0) * 1000), int((t2 - t1) * 1000)


def scan_movement(url, work_dir, start_s, max_window_s, token, label, prefix, fmt):
    """
    Scan denso de un bloque de movimiento. Para cuando el último de EARLY_STOP_N grupos
    lleva GROUP_GAP_S sin nuevas detecciones (banner de repetición cerrado).
    Devuelve lista de grupos [[t1, t2, ...], [t1, t2, ...], [t1, t2, ...]].

    Los frames se extraen+OCRean en lotes de SCAN_BATCH con SCAN_WORKERS hilos; el
    early-stop se evalúa procesando cada lote en orden (a lo sumo se desperdicia un
    lote tras el corte). El cuello es ffmpeg (seek remoto), que paraleliza bien.
    """
    err(f"  [{label}] scan desde {start_s // 3600}h{(start_s % 3600) // 60:02d}m "
        f"(max {max_window_s // 60} min, step {SCAN_STEP_S}s, {SCAN_WORKERS} hilos)")
    t_phase = time.perf_counter()
    hits = []
    groups = []
    end_s = start_s + max_window_s
    all_secs = list(range(start_s, end_s + 1, SCAN_STEP_S))
    n_frames = tot_ff = tot_ocr = 0
    i = 0
    stop = False
    consecutive_errors = 0

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for b in range(0, len(all_secs), SCAN_BATCH):
            if stop:
                break
            batch = all_secs[b:b + SCAN_BATCH]
            # ex.map preserva el orden de envío → resultados en orden de batch.
            results = ex.map(lambda s: _scan_one(url, work_dir, s, token, prefix, fmt), batch)
            for secs, ok, text, found, ff_ms, ocr_ms in results:
                i += 1
                n_frames += 1
                tot_ff += ff_ms
                tot_ocr += ocr_ms
                ts = f"{secs // 3600}h{(secs % 3600) // 60:02d}m{secs % 60:02d}s"
                if not ok:
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        err(f"  [{label}] bail-out: {consecutive_errors} errores consecutivos (URL expirada?)")
                        stop = True; break
                    err(f"  [{label} {i:3d}] {ts}  ERROR"); continue
                consecutive_errors = 0
                excerpt = (text[:50] + "…") if len(text) > 50 else text
                mark = "✓ HIT" if found else "·"
                err(f"  [{label} {i:3d}] {ts}  {mark:<7} ff{ff_ms:4d}ms ocr{ocr_ms:4d}ms  {excerpt!r}")

                if found:
                    hits.append(secs)
                    groups = []
                    cur = [hits[0]]
                    for s in hits[1:]:
                        if s - cur[-1] <= GROUP_GAP_S:
                            cur.append(s)
                        else:
                            groups.append(cur)
                            cur = [s]
                    groups.append(cur)

                # Stop once EARLY_STOP_N groups are identified AND the last group has closed
                # (GROUP_GAP_S seconds without a new detection = replay banner ended).
                if len(groups) >= EARLY_STOP_N and hits and (secs - hits[-1]) >= GROUP_GAP_S:
                    err(f"  [{label}] early-stop: {EARLY_STOP_N} grupos cerrados en frame {i}")
                    stop = True
                    break

    _record(n_frames, tot_ff, tot_ocr)
    dt = time.perf_counter() - t_phase
    err(f"  [{label}] scan: {n_frames} frames en {dt:.0f}s wall "
        f"(ff {tot_ff / 1000:.0f}s + ocr {tot_ocr / 1000:.0f}s)")
    return groups


def detect_break_timer(url, work_dir, search_from_s, label, prefix, fmt, video_end_s=None):
    """
    Escanea cada TIMER_STEP_S desde search_from_s buscando el timer de descanso
    entre movimientos (valor > BREAK_TIMER_MIN). Devuelve el timestamp de inicio
    del siguiente movimiento o None si no se encuentra.
    """
    err(f"  [{label}] buscando timer de descanso desde "
        f"{search_from_s // 3600}h{(search_from_s % 3600) // 60:02d}m...")
    max_scan = search_from_s + 5400  # buscar hasta 90 min después
    if video_end_s is not None:
        max_scan = min(max_scan, video_end_s)
    consecutive_errors = 0
    for secs in range(search_from_s, max_scan + 1, TIMER_STEP_S):
        ts = f"{secs // 3600}h{(secs % 3600) // 60:02d}m{secs % 60:02d}s"
        out = work_dir / f"{prefix}_{secs:06d}.jpg"
        if not out.exists() and not extract_frame(url, secs, out):
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                err(f"  [{label}] bail-out: {consecutive_errors} errores consecutivos (URL expirada?)")
                break
            err(f"  [{label}] {ts}  ERROR"); continue
        consecutive_errors = 0
        t = read_timer(out, fmt)
        err(f"  [{label}] {ts}  timer={t!r}")
        if t is not None and t > BREAK_TIMER_MIN:
            next_start = secs + t
            err(f"  [{label}] timer descanso = {t}s ({_hms(t)}) → next_start = {next_start}s ({_hms(next_start)})")
            return next_start
    err(f"  [{label}] timer no encontrado")
    return None


def refine_group_bounds(url, work_dir, groups, token, label, prefix, fmt, video_end_s=None):
    """
    Scan denso (REFINE_STEP_S) alrededor de min(g) y max(g) de cada grupo.
    Reduce la incertidumbre ±SCAN_STEP_S/2 del scan principal a ±REFINE_STEP_S/2.
    """
    refined = []
    n_frames = tot_ff = tot_ocr = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for gi, group in enumerate(groups):
            g_min, g_max = min(group), max(group)
            new_min, new_max = g_min, g_max

            # Buscar inicio más temprano: escanear REFINE_BEFORE_S segundos antes de g_min.
            # Frames independientes → en paralelo; new_min = el hit más temprano.
            before = list(range(max(0, g_min - REFINE_BEFORE_S), g_min, REFINE_STEP_S))
            pre = f"{prefix}_rb{gi}"
            for secs, ok, _t, found, ff_ms, ocr_ms in ex.map(
                    lambda s, p=pre: _scan_one(url, work_dir, s, token, p, fmt), before):
                n_frames += 1; tot_ff += ff_ms; tot_ocr += ocr_ms
                if ok and found:
                    err(f"  [{label}] refine intento {gi+1} inicio ✓ {secs}s ({_hms(secs)}) (era {g_min}s / {_hms(g_min)})")
                    new_min = min(new_min, secs)

            # Buscar fin más tardío: escanear hasta REFINE_AFTER_S después de g_max.
            # ex.map preserva el orden → extender mientras haya hits consecutivos y
            # parar en el primer miss (banner terminado), igual que la versión serie.
            after_end = g_max + REFINE_AFTER_S + 1
            if video_end_s is not None:
                after_end = min(after_end, video_end_s + 1)
            after = list(range(g_max + REFINE_STEP_S, after_end, REFINE_STEP_S))
            pst = f"{prefix}_re{gi}"
            for secs, ok, _t, found, ff_ms, ocr_ms in ex.map(
                    lambda s, p=pst: _scan_one(url, work_dir, s, token, p, fmt), after):
                n_frames += 1; tot_ff += ff_ms; tot_ocr += ocr_ms
                if ok and found:
                    err(f"  [{label}] refine intento {gi+1} fin ✓ {secs}s ({_hms(secs)}) (era {g_max}s / {_hms(g_max)})")
                    new_max = max(new_max, secs)
                else:
                    break  # primer miss: el banner ha terminado

            extra = set()
            if new_min < g_min:
                extra.add(new_min)
            if new_max > g_max:
                extra.add(new_max)
            refined_group = sorted(set(group) | extra)
            refined.append(refined_group)

            if new_min != g_min or new_max != g_max:
                err(f"  [{label}] refine intento {gi+1}: [{g_min}s/{_hms(g_min)}, {g_max}s/{_hms(g_max)}] → [{new_min}s/{_hms(new_min)}, {new_max}s/{_hms(new_max)}]")
            else:
                err(f"  [{label}] refine intento {gi+1}: sin cambios")

    _record(n_frames, tot_ff, tot_ocr)
    err(f"  [{label}] refine: {n_frames} frames (ff {tot_ff / 1000:.0f}s + ocr {tot_ocr / 1000:.0f}s)")
    return refined


def groups_to_timestamps(groups):
    """Devuelve el primer frame de cada grupo (inicio del banner)."""
    return [min(g) for g in groups]


def groups_to_ends(groups):
    """Devuelve el último frame detectado de cada grupo (fin de la repetición)."""
    return [max(g) for g in groups]


def trim_isolated_starts(groups, label):
    """
    Descarta el primer hit de un grupo si está aislado del siguiente
    (gap > ISOLATED_HIT_GAP_S). Ocurre cuando el overlay del levantador
    aparece durante la repetición del intento anterior: el nombre ya está
    en pantalla pero la cámara aún no apunta al levantador.
    """
    trimmed = []
    for gi, group in enumerate(groups):
        sgroup = sorted(group)
        if len(sgroup) >= 2 and (sgroup[1] - sgroup[0]) > ISOLATED_HIT_GAP_S:
            err(f"  [{label}] intento {gi+1}: primer hit aislado "
                f"({sgroup[0]}s, siguiente en {sgroup[1]}s, gap={sgroup[1]-sgroup[0]}s) "
                f"→ descartando, nuevo inicio: {sgroup[1]}s")
            sgroup = sgroup[1:]
        trimmed.append(sgroup)
    return trimmed


def main():
    parser = argparse.ArgumentParser(
        description="Detecta timestamps de un levantador en vídeo de powerlifting.")
    parser.add_argument("url", help="URL de YouTube del vídeo de competición")
    parser.add_argument("apellido", help="Apellido(s) del levantador (p.ej. OSUNA o 'CAMPANO DIAZ')")
    parser.add_argument("--federation", default="AEP", choices=list(FORMATS.keys()),
                        help="Federación: determina posición y color del banner y timer (default: AEP)")
    parser.add_argument("--work-dir", default="/tmp/find_lifter",
                        help="Directorio temporal para frames (default: /tmp/find_lifter)")
    parser.add_argument("--duration", type=float, default=None,
                        help="Duración del vídeo en segundos (evita buscar frames inexistentes)")
    args = parser.parse_args()

    fmt = FORMATS[args.federation.upper()]
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    token = _normalize(args.apellido)
    video_end_s = int(args.duration) if args.duration else None

    t_start = time.perf_counter()
    err(f"find_lifter.py — federación: {args.federation.upper()}  URL: {args.url}  token: {token}"
        + (f"  duración: {_hms(video_end_s)}" if video_end_s else ""))
    err("Leyenda líneas de scan OCR:")
    err("  [MOV nnn] pos  marca  ffXms ocrYms  texto")
    err("  MOV   = SQ/BN/DL (sentadilla / banca / peso muerto)")
    err("  nnn   = nº de frame analizado en el scan actual")
    err("  pos   = posición en el vídeo (h:m:s)")
    err("  marca = ✓ HIT: banner del levantador detectado  ·: no detectado")
    err("  ffX   = tiempo de extracción del frame con ffmpeg (seek al stream)")
    err("  ocrY  = tiempo de OCR (Tesseract) sobre la máscara del banner")
    err("  texto = primeros 50 caracteres leídos por OCR")

    # URL directa del stream
    err("\nObteniendo URL stream...")
    try:
        r = subprocess.run(
            ["yt-dlp", "--get-url", "-f", "bestvideo[height<=720][ext=mp4]/bestvideo[height<=720]",
             args.url], capture_output=True, text=True, timeout=30)
        url = r.stdout.strip().splitlines()[0]
    except subprocess.TimeoutExpired:
        err("ERROR: yt-dlp tardó más de 30s — URL no disponible")
        sys.exit(1)
    err("OK\n")

    result = {"squat": None, "bench": None, "deadlift": None,
              "comp_start": None, "elapsed_s": None}

    # ── 1. Inicio de competición ─────────────────────────────────────────────
    err("=== Fase 1: inicio de competición ===")
    comp_start = detect_comp_start(url, work_dir, fmt)
    result["comp_start"] = comp_start

    # ── 2. Sentadilla ────────────────────────────────────────────────────────
    err(""); err("=== Fase 2: sentadilla ===")
    squat_groups = scan_movement(
        url, work_dir,
        start_s=comp_start,
        max_window_s=min(90 * 60, video_end_s - comp_start) if video_end_s else 90 * 60,
        token=token,
        label="SQ",
        prefix="sq",
        fmt=fmt,
    )
    squat_groups = refine_group_bounds(url, work_dir, squat_groups, token, "SQ", "sq",
                                       fmt=fmt, video_end_s=video_end_s)
    squat_groups = trim_isolated_starts(squat_groups, "SQ")
    squat_ts = groups_to_timestamps(squat_groups)
    result["squat"] = squat_ts
    result["squat_ends"] = groups_to_ends(squat_groups)
    err(f"  → sentadilla: {squat_ts}")

    # Determinar si el levantador es G1 o G2 según cuándo ocurre su primera sentadilla.
    # G1 lifta en la primera mitad de cada ronda; G2 lifta después de que G1 termine.
    # Un G2 tiene sq_offset >> 30 min; un G1 tiene sq_offset de pocos minutos.
    # La decisión es binaria: G1 → sin salto en banca/DL; G2 → saltar la parte de G1.
    G2_THRESHOLD_S     = 1800  # 30 min: sq_offset mayor que esto indica G2
    GROUP_OFFSET_MARGIN_S    = 300  # margen de 5 min para banca (similar al de sentadilla)
    DL_OFFSET_MARGIN_S       = 600  # margen de 10 min para DL (rondas más rápidas que banca)
    sq_offset = (squat_ts[0] - comp_start) if squat_ts else 0
    is_g2 = sq_offset > G2_THRESHOLD_S
    err(f"  [grupo] sq_offset={sq_offset}s ({_hms(sq_offset)}) → {'G2' if is_g2 else 'G1'}")

    # ── 3. Inicio de banca (timer de descanso) ────────────────────────────────
    err(""); err("=== Fase 3: buscando inicio de banca ===")
    if len(squat_ts) >= 2:
        avg_gap_sq = (squat_ts[1] - squat_ts[0] + squat_ts[-1] - squat_ts[-2]) / 2
        search_from = int(comp_start + avg_gap_sq * 6)
        err(f"  [timer] avg_gap_sq={avg_gap_sq:.0f}s ({_hms(avg_gap_sq)}) → buscando desde {search_from}s ({_hms(search_from)})")
    else:
        search_from = (max(squat_ts) + 300) if squat_ts else (comp_start + 3600)
    bench_start = detect_break_timer(url, work_dir, search_from, "SQ→BN", "brk_sq",
                                      fmt=fmt, video_end_s=video_end_s)
    if bench_start is None:
        # Fallback: estimar desde duración del grupo de sentadilla
        if len(squat_ts) >= 2:
            group_dur = max(squat_ts) - comp_start
            bench_start = max(squat_ts) + group_dur + 600
            err(f"  Fallback bench_start estimado: {bench_start}s ({_hms(bench_start)})")
        else:
            err("  ERROR: no se puede estimar bench_start"); sys.exit(1)

    # ── 4. Banca ─────────────────────────────────────────────────────────────
    err(""); err("=== Fase 4: banca ===")
    if is_g2:
        bench_scan_start = max(bench_start, bench_start + sq_offset - GROUP_OFFSET_MARGIN_S)
        err(f"  [G2] sq_offset={sq_offset}s ({_hms(sq_offset)}) → saltando {bench_scan_start - bench_start}s ({_hms(bench_scan_start - bench_start)}) del bloque de banca")
    else:
        bench_scan_start = bench_start
        err(f"  [G1] escaneando desde el inicio del bloque de banca")
    bn_window = 90 * 60 - (bench_scan_start - bench_start)
    bench_groups = scan_movement(
        url, work_dir,
        start_s=bench_scan_start,
        max_window_s=min(bn_window, video_end_s - bench_scan_start) if video_end_s else bn_window,
        token=token,
        label="BN",
        prefix="bn",
        fmt=fmt,
    )
    bench_groups = refine_group_bounds(url, work_dir, bench_groups, token, "BN", "bn",
                                       fmt=fmt, video_end_s=video_end_s)
    bench_groups = trim_isolated_starts(bench_groups, "BN")
    bench_ts = groups_to_timestamps(bench_groups)
    result["bench"] = bench_ts
    result["bench_ends"] = groups_to_ends(bench_groups)
    err(f"  → banca: {bench_ts}")

    # ── 5. Inicio de DL (timer de descanso) ───────────────────────────────────
    err(""); err("=== Fase 5: buscando inicio de peso muerto ===")
    if len(bench_ts) >= 2:
        avg_gap_bn = (bench_ts[1] - bench_ts[0] + bench_ts[-1] - bench_ts[-2]) / 2
        search_from_dl = int(bench_start + avg_gap_bn * 6)
        err(f"  [timer] avg_gap_bn={avg_gap_bn:.0f}s ({_hms(avg_gap_bn)}) → buscando desde {search_from_dl}s ({_hms(search_from_dl)})")
    else:
        search_from_dl = (max(bench_ts) + 300) if bench_ts else (bench_start + 3600)
    dl_start = detect_break_timer(url, work_dir, search_from_dl, "BN→DL", "brk_bn",
                                   fmt=fmt, video_end_s=video_end_s)
    if dl_start is None:
        if len(bench_ts) >= 2:
            group_dur = max(bench_ts) - bench_start
            dl_start = max(bench_ts) + group_dur + 600
            err(f"  Fallback dl_start estimado: {dl_start}s ({_hms(dl_start)})")
        else:
            err("  ERROR: no se puede estimar dl_start"); sys.exit(1)

    # ── 6. Peso muerto ───────────────────────────────────────────────────────
    err(""); err("=== Fase 6: peso muerto ===")
    # Usar el offset de banca si está disponible (más reciente que squat)
    bn_offset = (bench_ts[0] - bench_start) if bench_ts else sq_offset
    is_g2_bn  = bn_offset > G2_THRESHOLD_S
    if is_g2_bn:
        dl_scan_start = max(dl_start, dl_start + bn_offset - DL_OFFSET_MARGIN_S)
        err(f"  [attempt offset] bn_offset={bn_offset}s ({_hms(bn_offset)}) → saltando {dl_scan_start - dl_start}s ({_hms(dl_scan_start - dl_start)}) del bloque de DL")
    else:
        dl_scan_start = dl_start
        err(f"  [G1] escaneando desde el inicio del bloque de DL")
    dl_window = 60 * 60 - (dl_scan_start - dl_start)
    dl_groups = scan_movement(
        url, work_dir,
        start_s=dl_scan_start,
        max_window_s=min(dl_window, video_end_s - dl_scan_start) if video_end_s else dl_window,
        token=token,
        label="DL",
        prefix="dl",
        fmt=fmt,
    )
    dl_groups = refine_group_bounds(url, work_dir, dl_groups, token, "DL", "dl",
                                    fmt=fmt, video_end_s=video_end_s)
    dl_groups = trim_isolated_starts(dl_groups, "DL")
    dl_ts = groups_to_timestamps(dl_groups)
    result["deadlift"] = dl_ts
    result["deadlift_ends"] = groups_to_ends(dl_groups)
    err(f"  → peso muerto: {dl_ts}")

    result["elapsed_s"] = round(time.perf_counter() - t_start, 1)
    err("")
    err(f"Resumen: {_STATS['frames']} frames · ffmpeg {_STATS['ff_ms'] / 1000:.0f}s "
        f"· OCR {_STATS['ocr_ms'] / 1000:.0f}s (suma de hilos, no wall-clock)")
    err(f"Terminado en {result['elapsed_s']}s ({_hms(result['elapsed_s'])}) wall-clock "
        f"con {SCAN_WORKERS} hilos")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
