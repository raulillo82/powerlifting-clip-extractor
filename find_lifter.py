#!/usr/bin/env python3
"""
find_lifter.py — Detección automática de timestamps de un levantador en vídeo AEP.

Uso:
    python3 find_lifter.py <youtube_url> <apellido> [--work-dir /tmp/find_lifter]

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

import sys, argparse, subprocess, time, difflib, re, json, tempfile
from pathlib import Path
from PIL import Image
import numpy as np, pytesseract

# ── Parámetros de detección ────────────────────────────────────────────────────

BANNER_CROP      = (0.00, 0.78, 0.45, 1.00)  # x0, y0, x1, y1 relativo al frame
TIMER_CROP       = (0.78, 0.88, 1.00, 1.00)  # reloj de intento / timer de descanso
YELLOW_H_RANGE   = (10, 33)                   # hue en rango HSV escalado 0-180
YELLOW_MIN_S     = 100
YELLOW_MIN_V     = 100
YELLOW_MIN_PX    = 150                        # píxeles mínimos para activar OCR
OCR_SCALE        = 2                          # escala de la imagen binarizada para tesseract
TIMER_SCALE      = 4                          # escala del crop del timer
FUZZY_RATIO      = 0.70
GROUP_GAP_S      = 90                         # segundos de gap para separar grupos
SCAN_STEP_S      = 10                         # step del scan denso
TIMER_STEP_S     = 60                         # step del scan del timer de descanso
BREAK_TIMER_MIN  = 120                        # segundos mínimos para considerar timer de descanso
EARLY_STOP_N     = 3                          # detener el scan al completar N grupos


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def extract_frame(url, secs, out):
    r = subprocess.run(
        ["ffmpeg", "-ss", str(secs), "-i", url, "-frames:v", "1",
         "-q:v", "3", "-vf", "scale=1280:-1", str(out), "-y"],
        capture_output=True, timeout=30)
    return out.exists() and out.stat().st_size > 0


def _hsv_hue(arr):
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
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


def ocr_banner(path, token):
    ym = yellow_mask(path)
    if ym.sum() < YELLOW_MIN_PX:
        return "", False
    bin_arr = np.zeros((*ym.shape, 3), dtype=np.uint8)
    bin_arr[ym] = 255
    pil = Image.fromarray(bin_arr).resize(
        (bin_arr.shape[1] * OCR_SCALE, bin_arr.shape[0] * OCR_SCALE), Image.NEAREST)
    text = pytesseract.image_to_string(
        pil, config="--oem 3 --psm 6 -l spa").upper().replace("\n", " ").strip()
    tok = token.upper()
    for word in text.split():
        word = word.strip(".,;:!?-_|/\\\"'()[]{}¡¿")
        if len(word) < max(4, len(tok) - 2):
            continue
        if difflib.SequenceMatcher(None, tok, word).ratio() >= FUZZY_RATIO:
            return text, True
        if abs(len(tok) - len(word)) <= 2 and (tok in word or word in tok):
            return text, True
    return text, False


def read_timer(path):
    """Lee el timer del cuadrante inf-der. Devuelve segundos o None."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    x0, y0, x1, y1 = TIMER_CROP
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    crop4 = crop.resize((crop.width * TIMER_SCALE, crop.height * TIMER_SCALE), Image.NEAREST)
    text = pytesseract.image_to_string(
        crop4, config="--oem 3 --psm 6 -l spa -c tessedit_char_whitelist=0123456789:").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def detect_comp_start(url, work_dir, max_probe_s=360):
    """Lee el timer pre-competición en frames tempranos para calcular comp_start."""
    err("  [comp_start] buscando timer pre-competición...")
    for probe in range(30, max_probe_s + 1, 30):
        out = work_dir / f"pre_{probe:05d}.jpg"
        if not extract_frame(url, probe, out):
            continue
        t = read_timer(out)
        err(f"  [comp_start] @{probe}s → timer={t!r}")
        if t is not None and 30 < t < 7200:
            comp_start = probe + t
            err(f"  [comp_start] → {comp_start}s ({comp_start // 60}m{comp_start % 60:02d}s)")
            return comp_start
    err("  [comp_start] timer no legible — usando 0s como fallback")
    return 0


def scan_movement(url, work_dir, start_s, max_window_s, token, label, prefix):
    """
    Scan denso de un bloque de movimiento. Para cuando detecta EARLY_STOP_N grupos.
    Devuelve lista de grupos [[t1, t2, ...], [t1, t2, ...], [t1, t2, ...]].
    """
    err(f"  [{label}] scan desde {start_s // 3600}h{(start_s % 3600) // 60:02d}m "
        f"(max {max_window_s // 60} min, step {SCAN_STEP_S}s)")
    hits = []
    groups = []
    end_s = start_s + max_window_s
    i = 0
    for secs in range(start_s, end_s + 1, SCAN_STEP_S):
        i += 1
        ts = f"{secs // 3600}h{(secs % 3600) // 60:02d}m{secs % 60:02d}s"
        out = work_dir / f"{prefix}_{secs:06d}.jpg"
        tf = time.perf_counter()
        if not extract_frame(url, secs, out):
            err(f"  [{label} {i:3d}] {ts}  ERROR"); continue
        text, found = ocr_banner(out, token)
        ms = int((time.perf_counter() - tf) * 1000)
        excerpt = (text[:50] + "…") if len(text) > 50 else text
        mark = "✓ HIT" if found else "·"
        err(f"  [{label} {i:3d}] {ts}  {mark:<7} {ms:4d}ms  {excerpt!r}")

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
            if len(groups) == EARLY_STOP_N:
                err(f"  [{label}] early-stop: {EARLY_STOP_N} grupos en frame {i}")
                break

    return groups


def detect_break_timer(url, work_dir, search_from_s, label, prefix):
    """
    Escanea cada TIMER_STEP_S desde search_from_s buscando el timer de descanso
    entre movimientos (valor > BREAK_TIMER_MIN). Devuelve el timestamp de inicio
    del siguiente movimiento o None si no se encuentra.
    """
    err(f"  [{label}] buscando timer de descanso desde "
        f"{search_from_s // 3600}h{(search_from_s % 3600) // 60:02d}m...")
    max_scan = search_from_s + 5400  # buscar hasta 90 min después
    for secs in range(search_from_s, max_scan + 1, TIMER_STEP_S):
        ts = f"{secs // 3600}h{(secs % 3600) // 60:02d}m{secs % 60:02d}s"
        out = work_dir / f"{prefix}_{secs:06d}.jpg"
        if not out.exists() and not extract_frame(url, secs, out):
            err(f"  [{label}] {ts}  ERROR"); continue
        t = read_timer(out)
        err(f"  [{label}] {ts}  timer={t!r}")
        if t is not None and t > BREAK_TIMER_MIN:
            next_start = secs + t
            err(f"  [{label}] timer descanso = {t}s → next_start = {next_start}s "
                f"({next_start // 3600}h{(next_start % 3600) // 60:02d}m{next_start % 60:02d}s)")
            return next_start
    err(f"  [{label}] timer no encontrado")
    return None


def groups_to_timestamps(groups):
    """Devuelve el primer frame de cada grupo (inicio del banner)."""
    return [min(g) for g in groups]


def groups_to_ends(groups):
    """Devuelve el último frame detectado de cada grupo (fin de la repetición)."""
    return [max(g) for g in groups]


def main():
    parser = argparse.ArgumentParser(description="Detecta timestamps de un levantador en vídeo AEP.")
    parser.add_argument("url", help="URL de YouTube del vídeo de competición")
    parser.add_argument("apellido", help="Primer apellido del levantador (p.ej. OSUNA)")
    parser.add_argument("--work-dir", default="/tmp/find_lifter",
                        help="Directorio temporal para frames (default: /tmp/find_lifter)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    token = args.apellido.upper()

    t_start = time.perf_counter()
    err(f"find_lifter.py — URL: {args.url}  token: {token}")

    # URL directa del stream
    err("\nObteniendo URL stream...")
    r = subprocess.run(
        ["yt-dlp", "--get-url", "-f", "bestvideo[height<=720][ext=mp4]/bestvideo[height<=720]",
         args.url], capture_output=True, text=True, timeout=30)
    url = r.stdout.strip().splitlines()[0]
    err("OK\n")

    result = {"squat": None, "bench": None, "deadlift": None,
              "comp_start": None, "elapsed_s": None}

    # ── 1. Inicio de competición ─────────────────────────────────────────────
    err("=== Fase 1: inicio de competición ===")
    comp_start = detect_comp_start(url, work_dir)
    result["comp_start"] = comp_start

    # ── 2. Sentadilla ────────────────────────────────────────────────────────
    err("\n=== Fase 2: sentadilla ===")
    squat_groups = scan_movement(
        url, work_dir,
        start_s=comp_start,
        max_window_s=90 * 60,
        token=token,
        label="SQ",
        prefix="sq",
    )
    squat_ts = groups_to_timestamps(squat_groups)
    result["squat"] = squat_ts
    result["squat_ends"] = groups_to_ends(squat_groups)
    err(f"  → sentadilla: {squat_ts}")

    # ── 3. Inicio de banca (timer de descanso) ────────────────────────────────
    err("\n=== Fase 3: buscando inicio de banca ===")
    search_from = (max(squat_ts) + 300) if squat_ts else (comp_start + 3600)
    bench_start = detect_break_timer(url, work_dir, search_from, "SQ→BN", "brk_sq")
    if bench_start is None:
        # Fallback: estimar desde duración del grupo de sentadilla
        if len(squat_ts) >= 2:
            group_dur = max(squat_ts) - comp_start
            bench_start = max(squat_ts) + group_dur + 600
            err(f"  Fallback bench_start estimado: {bench_start}s")
        else:
            err("  ERROR: no se puede estimar bench_start"); sys.exit(1)

    # ── 4. Banca ─────────────────────────────────────────────────────────────
    err("\n=== Fase 4: banca ===")
    bench_groups = scan_movement(
        url, work_dir,
        start_s=bench_start,
        max_window_s=90 * 60,
        token=token,
        label="BN",
        prefix="bn",
    )
    bench_ts = groups_to_timestamps(bench_groups)
    result["bench"] = bench_ts
    result["bench_ends"] = groups_to_ends(bench_groups)
    err(f"  → banca: {bench_ts}")

    # ── 5. Inicio de DL (timer de descanso) ───────────────────────────────────
    err("\n=== Fase 5: buscando inicio de peso muerto ===")
    search_from_dl = (max(bench_ts) + 300) if bench_ts else (bench_start + 3600)
    dl_start = detect_break_timer(url, work_dir, search_from_dl, "BN→DL", "brk_bn")
    if dl_start is None:
        if len(bench_ts) >= 2:
            group_dur = max(bench_ts) - bench_start
            dl_start = max(bench_ts) + group_dur + 600
            err(f"  Fallback dl_start estimado: {dl_start}s")
        else:
            err("  ERROR: no se puede estimar dl_start"); sys.exit(1)

    # ── 6. Peso muerto ───────────────────────────────────────────────────────
    err("\n=== Fase 6: peso muerto ===")
    dl_groups = scan_movement(
        url, work_dir,
        start_s=dl_start,
        max_window_s=60 * 60,
        token=token,
        label="DL",
        prefix="dl",
    )
    dl_ts = groups_to_timestamps(dl_groups)
    result["deadlift"] = dl_ts
    result["deadlift_ends"] = groups_to_ends(dl_groups)
    err(f"  → peso muerto: {dl_ts}")

    result["elapsed_s"] = round(time.perf_counter() - t_start, 1)
    err(f"\nTerminado en {result['elapsed_s']}s ({result['elapsed_s'] / 60:.1f} min)")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
