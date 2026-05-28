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

import sys, argparse, subprocess, time, difflib, re, json, unicodedata
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
REFINE_BEFORE_S  = 12                         # segundos antes de min(g) para refinar inicio
REFINE_AFTER_S   = 20                         # segundos después de max(g) para refinar fin
REFINE_STEP_S    = 2                          # step del scan de refinamiento


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def _normalize(text: str) -> str:
    """Quita tildes y convierte a mayúsculas para comparación robusta."""
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").upper()


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


def _token_matches_word(tok, word):
    """True si tok encaja con word mediante ratio difuso o subconjunto."""
    if len(word) < max(4, len(tok) - 2):
        return False
    if difflib.SequenceMatcher(None, tok, word).ratio() >= FUZZY_RATIO:
        return True
    if abs(len(tok) - len(word)) <= 2 and (tok in word or word in tok):
        return True
    return False


def ocr_banner(path, token):
    ym = yellow_mask(path)
    if ym.sum() < YELLOW_MIN_PX:
        return "", False
    bin_arr = np.zeros((*ym.shape, 3), dtype=np.uint8)
    bin_arr[ym] = 255
    pil = Image.fromarray(bin_arr).resize(
        (bin_arr.shape[1] * OCR_SCALE, bin_arr.shape[0] * OCR_SCALE), Image.NEAREST)
    raw = pytesseract.image_to_string(
        pil, config="--oem 3 --psm 6 -l spa").replace("\n", " ").strip()
    text = raw.upper()  # for log display
    text_cmp = _normalize(raw)  # accentless for comparison

    ocr_words = [w.strip(".,;:!?-_|/\\\"'()[]{}¡¿") for w in re.split(r'[\s\-]+', text_cmp)]
    ocr_words = [w for w in ocr_words if w]

    # Split token into sub-tokens on spaces AND hyphens (OCR often separates
    # compound surnames like SANCHEZ-INFANTE into two words).
    sub_tokens = [t for t in re.split(r'[\s\-]+', _normalize(token)) if len(t) >= 3]
    if not sub_tokens:
        return text, False

    # All sub-tokens must match at least one OCR word (AND logic)
    for tok in sub_tokens:
        if not any(_token_matches_word(tok, w) for w in ocr_words):
            return text, False
    return text, True


def read_timer(path):
    """Lee el timer del cuadrante inf-der. Devuelve segundos o None."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    x0, y0, x1, y1 = TIMER_CROP
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))

    # Aislar caja roja del timer (números blancos sobre fondo rojo AEP)
    arr = np.array(crop)
    red_mask = (arr[:, :, 0] > 120) & (arr[:, :, 1] < 80) & (arr[:, :, 2] < 80)
    if red_mask.sum() > 200:
        rows = np.where(red_mask.any(axis=1))[0]
        cols = np.where(red_mask.any(axis=0))[0]
        pad = 4
        r0 = max(0, rows[0] - pad)
        r1 = min(arr.shape[0] - 1, rows[-1] + pad)
        c0 = max(0, cols[0] - pad)
        c1 = min(arr.shape[1] - 1, cols[-1] + pad)
        crop = Image.fromarray(arr[r0:r1 + 1, c0:c1 + 1])
        psm = 7
    else:
        psm = 6

    crop4 = crop.resize((crop.width * TIMER_SCALE, crop.height * TIMER_SCALE), Image.NEAREST)
    text = pytesseract.image_to_string(
        crop4, config=f"--oem 3 --psm {psm} -l spa -c tessedit_char_whitelist=0123456789:").strip()
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
    Scan denso de un bloque de movimiento. Para cuando el último de EARLY_STOP_N grupos
    lleva GROUP_GAP_S sin nuevas detecciones (banner de repetición cerrado).
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

        # Stop once EARLY_STOP_N groups are identified AND the last group has closed
        # (GROUP_GAP_S seconds without a new detection = replay banner ended).
        if len(groups) >= EARLY_STOP_N and hits and (secs - hits[-1]) >= GROUP_GAP_S:
            err(f"  [{label}] early-stop: {EARLY_STOP_N} grupos cerrados en frame {i}")
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


def refine_group_bounds(url, work_dir, groups, token, label, prefix):
    """
    Scan denso (REFINE_STEP_S) alrededor de min(g) y max(g) de cada grupo.
    Reduce la incertidumbre ±SCAN_STEP_S/2 del scan principal a ±REFINE_STEP_S/2.
    """
    refined = []
    for gi, group in enumerate(groups):
        g_min, g_max = min(group), max(group)
        new_min, new_max = g_min, g_max

        # Buscar inicio más temprano: escanear REFINE_BEFORE_S segundos antes de g_min
        for secs in range(max(0, g_min - REFINE_BEFORE_S), g_min, REFINE_STEP_S):
            out = work_dir / f"{prefix}_rb{gi}_{secs:06d}.jpg"
            if not extract_frame(url, secs, out):
                continue
            _, found = ocr_banner(out, token)
            if found:
                err(f"  [{label}] refine g{gi+1} inicio ✓ {secs}s (era {g_min}s)")
                new_min = min(new_min, secs)

        # Buscar fin más tardío: escanear hasta REFINE_AFTER_S segundos después de g_max
        for secs in range(g_max + REFINE_STEP_S, g_max + REFINE_AFTER_S + 1, REFINE_STEP_S):
            out = work_dir / f"{prefix}_re{gi}_{secs:06d}.jpg"
            if not extract_frame(url, secs, out):
                continue
            _, found = ocr_banner(out, token)
            if found:
                err(f"  [{label}] refine g{gi+1} fin ✓ {secs}s (era {g_max}s)")
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
            err(f"  [{label}] refine g{gi+1}: [{g_min},{g_max}] → [{new_min},{new_max}]")
        else:
            err(f"  [{label}] refine g{gi+1}: sin cambios")
    return refined


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
    token = _normalize(args.apellido)

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
    squat_groups = refine_group_bounds(url, work_dir, squat_groups, token, "SQ", "sq")
    squat_ts = groups_to_timestamps(squat_groups)
    result["squat"] = squat_ts
    result["squat_ends"] = groups_to_ends(squat_groups)
    err(f"  → sentadilla: {squat_ts}")

    # Determinar si el levantador es G1 o G2 según cuándo ocurre su primera sentadilla.
    # G1 lifta en la primera mitad de cada ronda; G2 lifta después de que G1 termine.
    # Un G2 tiene sq_offset >> 30 min; un G1 tiene sq_offset de pocos minutos.
    # La decisión es binaria: G1 → sin salto en banca/DL; G2 → saltar la parte de G1.
    G2_THRESHOLD_S     = 1800  # 30 min: sq_offset mayor que esto indica G2
    GROUP_OFFSET_MARGIN_S = 300  # margen de 5 min para variaciones de orden dentro del grupo
    sq_offset = (squat_ts[0] - comp_start) if squat_ts else 0
    is_g2 = sq_offset > G2_THRESHOLD_S
    err(f"  [grupo] sq_offset={sq_offset}s → {'G2' if is_g2 else 'G1'}")

    # ── 3. Inicio de banca (timer de descanso) ────────────────────────────────
    err("\n=== Fase 3: buscando inicio de banca ===")
    if len(squat_ts) >= 2:
        avg_gap_sq = (squat_ts[1] - squat_ts[0] + squat_ts[-1] - squat_ts[-2]) / 2
        search_from = int(comp_start + avg_gap_sq * 6)
        err(f"  [timer] avg_gap_sq={avg_gap_sq:.0f}s → buscando desde {search_from}s ({search_from//3600}h{(search_from%3600)//60:02d}m)")
    else:
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
    if is_g2:
        bench_scan_start = max(bench_start, bench_start + sq_offset - GROUP_OFFSET_MARGIN_S)
        err(f"  [G2] sq_offset={sq_offset}s → saltando {bench_scan_start - bench_start}s del bloque de banca")
    else:
        bench_scan_start = bench_start
        err(f"  [G1] escaneando desde el inicio del bloque de banca")
    bench_groups = scan_movement(
        url, work_dir,
        start_s=bench_scan_start,
        max_window_s=90 * 60 - (bench_scan_start - bench_start),
        token=token,
        label="BN",
        prefix="bn",
    )
    bench_groups = refine_group_bounds(url, work_dir, bench_groups, token, "BN", "bn")
    bench_ts = groups_to_timestamps(bench_groups)
    result["bench"] = bench_ts
    result["bench_ends"] = groups_to_ends(bench_groups)
    err(f"  → banca: {bench_ts}")

    # ── 5. Inicio de DL (timer de descanso) ───────────────────────────────────
    err("\n=== Fase 5: buscando inicio de peso muerto ===")
    if len(bench_ts) >= 2:
        avg_gap_bn = (bench_ts[1] - bench_ts[0] + bench_ts[-1] - bench_ts[-2]) / 2
        search_from_dl = int(bench_start + avg_gap_bn * 6)
        err(f"  [timer] avg_gap_bn={avg_gap_bn:.0f}s → buscando desde {search_from_dl}s ({search_from_dl//3600}h{(search_from_dl%3600)//60:02d}m)")
    else:
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
    # Usar el offset de banca si está disponible (más reciente que squat)
    bn_offset = (bench_ts[0] - bench_start) if bench_ts else sq_offset
    is_g2_bn  = bn_offset > G2_THRESHOLD_S
    if is_g2_bn:
        dl_scan_start = max(dl_start, dl_start + bn_offset - GROUP_OFFSET_MARGIN_S)
        err(f"  [G2] bn_offset={bn_offset}s → saltando {dl_scan_start - dl_start}s del bloque de DL")
    else:
        dl_scan_start = dl_start
        err(f"  [G1] escaneando desde el inicio del bloque de DL")
    dl_groups = scan_movement(
        url, work_dir,
        start_s=dl_scan_start,
        max_window_s=60 * 60 - (dl_scan_start - dl_start),
        token=token,
        label="DL",
        prefix="dl",
    )
    dl_groups = refine_group_bounds(url, work_dir, dl_groups, token, "DL", "dl")
    dl_ts = groups_to_timestamps(dl_groups)
    result["deadlift"] = dl_ts
    result["deadlift_ends"] = groups_to_ends(dl_groups)
    err(f"  → peso muerto: {dl_ts}")

    result["elapsed_s"] = round(time.perf_counter() - t_start, 1)
    err(f"\nTerminado en {result['elapsed_s']}s ({result['elapsed_s'] / 60:.1f} min)")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
