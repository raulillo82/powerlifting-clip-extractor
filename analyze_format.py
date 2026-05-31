#!/usr/bin/env python3
"""
Broadcast format analyzer for powerlifting competition videos.

Probes a sample of frames from a YouTube competition stream and identifies
where the countdown timer and athlete banner overlays appear, plus their
background colors.  Results are written as JSON (raw) and appended to
formats/competition_formats.md (human-readable table).

Usage:
    python3 analyze_format.py <youtube_url> <federation> "<competition>" \
        [--timestamps t1 t2 ...] \
        [--work-dir /tmp/fmt_analysis] \
        [--out-dir formats/]

If --timestamps is omitted the script samples every 5 minutes for the
first 40 minutes of the video (timestamps 300 600 ... 2400).
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
import pytesseract


# ── Candidate overlay regions (normalized x0, y0, x1, y1) ──────────────────

PROBE_REGIONS: list[tuple[str, float, float, float, float]] = [
    # Timer candidates — 4 corners
    ("timer_bottom_right", 0.78, 0.88, 1.00, 1.00),
    ("timer_top_right",    0.78, 0.00, 1.00, 0.12),
    ("timer_bottom_left",  0.00, 0.88, 0.22, 1.00),
    ("timer_top_left",     0.00, 0.00, 0.22, 0.12),
    # Banner candidates — horizontal strips and common positions
    ("banner_bottom_left", 0.00, 0.78, 0.45, 1.00),   # AEP standard
    ("banner_bottom_right",0.55, 0.78, 1.00, 1.00),
    ("banner_top_left",    0.00, 0.00, 0.45, 0.12),
    ("banner_top_right",   0.55, 0.00, 1.00, 0.12),
    ("banner_center_bot",  0.20, 0.82, 0.80, 1.00),
    ("banner_full_bottom", 0.00, 0.82, 1.00, 1.00),
    ("banner_full_top",    0.00, 0.00, 1.00, 0.18),
]

TIMER_RE = re.compile(r"(\d{1,2}):(\d{2})")
TIMER_SCALE = 4
BANNER_SCALE = 2
TIMER_MIN_PX = 200   # minimum colored pixels to attempt timer read

# Background color masks (R, G, B conditions as lambda on arrays)
BG_MASKS: dict[str, object] = {
    "red":    lambda R, G, B: (R > 120) & (G < 80)  & (B < 80),
    "blue":   lambda R, G, B: (B > 120) & (R < 80)  & (G < 80),
    "green":  lambda R, G, B: (G > 120) & (R < 80)  & (B < 80),
    "yellow": lambda R, G, B: (R > 150) & (G > 150) & (B < 100),
    "white":  lambda R, G, B: (R > 200) & (G > 200) & (B > 200),
    "dark":   lambda R, G, B: (R < 60)  & (G < 60)  & (B < 60),
}


# ── Frame extraction (standalone, no find_lifter dependency) ─────────────────

def _hms(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_stream_url(youtube_url: str) -> str:
    r = subprocess.run(
        ["yt-dlp", "--get-url", "-f",
         "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/bestvideo+bestaudio/best",
         "--no-playlist", youtube_url],
        capture_output=True, text=True, timeout=60,
    )
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(f"yt-dlp returned no URL for {youtube_url}")
    return lines[0]


def extract_frame(stream_url: str, secs: int, out: Path) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-ss", str(secs), "-i", stream_url,
         "-frames:v", "1", "-q:v", "3", "-vf", "scale=1280:-1",
         str(out), "-y"],
        capture_output=True, timeout=30,
    )
    return out.exists() and out.stat().st_size > 0


# ── Per-region analysis ──────────────────────────────────────────────────────

def _try_read_timer(img: Image.Image, w: int, h: int,
                    x0: float, y0: float, x1: float, y1: float) -> int | None:
    """Try every background color mask to find a MM:SS timer in this region."""
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    arr = np.array(crop)
    R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    for _name, mask_fn in BG_MASKS.items():
        mask = mask_fn(R, G, B)
        if mask.sum() < TIMER_MIN_PX:
            continue
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        pad = 4
        r0 = max(0, int(rows[0]) - pad)
        r1 = min(arr.shape[0] - 1, int(rows[-1]) + pad)
        c0 = max(0, int(cols[0]) - pad)
        c1 = min(arr.shape[1] - 1, int(cols[-1]) + pad)
        box = Image.fromarray(arr[r0:r1 + 1, c0:c1 + 1])
        scaled = box.resize((box.width * TIMER_SCALE, box.height * TIMER_SCALE),
                             Image.NEAREST)
        text = pytesseract.image_to_string(
            scaled,
            config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789:",
        ).strip()
        m = TIMER_RE.search(text)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
    return None


def analyze_region(img: Image.Image, w: int, h: int,
                   x0: float, y0: float, x1: float, y1: float) -> dict:
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    arr = np.array(crop)
    R, G, B = arr[:, :, 0].astype(float), arr[:, :, 1].astype(float), arr[:, :, 2].astype(float)

    result: dict = {
        "mean_rgb": [round(float(arr[:, :, c].mean()), 1) for c in range(3)],
        "std_rgb":  [round(float(arr[:, :, c].std()),  1) for c in range(3)],
        "color_variance": round(float(arr.std()), 1),
    }

    # Pixel counts per background color
    Ri, Gi, Bi = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    for name, mask_fn in BG_MASKS.items():
        result[f"{name}_px"] = int(mask_fn(Ri, Gi, Bi).sum())

    # Timer detection
    result["timer_value"] = _try_read_timer(img, w, h, x0, y0, x1, y1)

    # Text / banner detection
    scaled = crop.resize((crop.width * BANNER_SCALE, crop.height * BANNER_SCALE))
    ocr_text = pytesseract.image_to_string(
        scaled, config="--oem 3 --psm 6 -l spa",
    ).strip().replace("\n", " ")
    result["ocr_text"] = ocr_text[:120]
    result["text_len"]  = len(ocr_text)
    result["has_text"]  = len(ocr_text) >= 5

    return result


def analyze_frame(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    regions = {}
    for name, x0, y0, x1, y1 in PROBE_REGIONS:
        regions[name] = analyze_region(img, w, h, x0, y0, x1, y1)
    return regions


# ── Aggregation ──────────────────────────────────────────────────────────────

def _dominant_bg(region_results: list[dict]) -> str:
    """Return the name of the most common dominant background color."""
    counts: dict[str, int] = {name: 0 for name in BG_MASKS}
    for r in region_results:
        best = max(BG_MASKS, key=lambda n: r.get(f"{n}_px", 0))
        if r.get(f"{best}_px", 0) >= TIMER_MIN_PX:
            counts[best] += 1
    top = max(counts, key=lambda k: counts[k])
    return top if counts[top] > 0 else "mixed"


def aggregate(frames: list[dict]) -> dict:
    """Summarize per-region statistics across all sampled frames."""
    n = len(frames)
    if n == 0:
        return {}

    summary: dict[str, dict] = {}
    for name, *_ in PROBE_REGIONS:
        region_data = [f["regions"][name] for f in frames if name in f.get("regions", {})]
        if not region_data:
            continue

        timer_hits  = sum(1 for r in region_data if r.get("timer_value") is not None)
        text_hits   = sum(1 for r in region_data if r.get("has_text"))
        mean_var    = sum(r["color_variance"] for r in region_data) / len(region_data)

        summary[name] = {
            "timer_hit_rate": round(timer_hits / n, 2),
            "text_hit_rate":  round(text_hits  / n, 2),
            "mean_color_variance": round(mean_var, 1),
            "dominant_bg": _dominant_bg(region_data),
            "is_timer_candidate":  timer_hits / n > 0.30,
            "is_banner_candidate": text_hits  / n > 0.40 and mean_var < 60,
        }
    return summary


# ── Main ─────────────────────────────────────────────────────────────────────

def _print_summary(data: dict) -> None:
    agg = data.get("aggregation", {})
    print(f"\n{'─'*60}")
    print(f"  {data['federation']} — {data['competition']}")
    print(f"  {data['frames_analyzed']} frames analizados")
    print(f"{'─'*60}")

    timers  = [(n, v) for n, v in agg.items() if v.get("is_timer_candidate")]
    banners = [(n, v) for n, v in agg.items() if v.get("is_banner_candidate")]

    if timers:
        print("  TIMER posibles:")
        for name, v in timers:
            print(f"    {name:30s}  hit={v['timer_hit_rate']:.0%}  "
                  f"bg={v['dominant_bg']}")
    else:
        print("  TIMER: ningún candidato claro")

    if banners:
        print("  BANNER posibles:")
        for name, v in banners:
            print(f"    {name:30s}  text={v['text_hit_rate']:.0%}  "
                  f"var={v['mean_color_variance']:.0f}  bg={v['dominant_bg']}")
    else:
        print("  BANNER: ningún candidato claro")
    print()


def _append_to_table(data: dict, table_path: Path) -> None:
    agg = data.get("aggregation", {})

    def _fmt_candidates(key: str) -> str:
        hits = [(n, v) for n, v in agg.items() if v.get(key)]
        if not hits:
            return "—"
        return " / ".join(
            f"{n.replace('timer_','').replace('banner_','')} ({v['dominant_bg']})"
            for n, v in hits[:2]
        )

    timer_col  = _fmt_candidates("is_timer_candidate")
    banner_col = _fmt_candidates("is_banner_candidate")

    row = (
        f"| {data['federation']} "
        f"| {data['competition']} "
        f"| [{data['video_id']}](https://youtube.com/watch?v={data['video_id']}) "
        f"| {timer_col} "
        f"| {banner_col} "
        f"| {data['frames_analyzed']} frames |"
    )

    header = (
        "| Federación | Competición | Vídeo | Timer: región (fondo) "
        "| Banner: región (fondo) | Frames |"
    )
    sep = "|---|---|---|---|---|---|"

    if not table_path.exists():
        table_path.write_text(f"{header}\n{sep}\n{row}\n")
        return

    content = table_path.read_text()
    # Replace existing row for same competition if present
    new_rows = [
        line for line in content.splitlines()
        if data["competition"] not in line or line.startswith("|---") or line.startswith("| Fed")
    ]
    if row not in new_rows:
        new_rows.append(row)
    table_path.write_text("\n".join(new_rows) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url",         help="YouTube URL")
    parser.add_argument("federation",  help="e.g. AEP, IPF, EPF, USAPL")
    parser.add_argument("competition", help="Competition name (quoted if spaces)")
    parser.add_argument("--timestamps", nargs="+", type=int, default=None,
                        help="Seconds to sample (default: 300 600 … 2400)")
    parser.add_argument("--work-dir",  default="/tmp/fmt_analysis",
                        help="Temp directory for extracted frames")
    parser.add_argument("--out-dir",   default="formats",
                        help="Output directory for JSON + table")
    args = parser.parse_args()

    timestamps = args.timestamps or list(range(300, 2401, 300))
    work_dir   = Path(args.work_dir)
    out_dir    = Path(args.out_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Extract video ID for labeling
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", args.url)
    video_id = m.group(1) if m else "unknown"

    print(f"[{time.strftime('%H:%M:%S %Z')}] Obteniendo URL de stream...")
    try:
        stream_url = get_stream_url(args.url)
    except Exception as e:
        sys.exit(f"ERROR: {e}")
    print(f"[{time.strftime('%H:%M:%S %Z')}] OK — analizando {len(timestamps)} frames")

    frames: list[dict] = []
    for secs in timestamps:
        frame_path = work_dir / f"{video_id}_{secs:06d}.jpg"
        ts_label = _hms(secs)
        print(f"  @{ts_label} ", end="", flush=True)

        try:
            ok = extract_frame(stream_url, secs, frame_path)
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            continue
        if not ok:
            print("ERROR (frame vacío)")
            continue

        try:
            regions = analyze_frame(frame_path)
        except Exception as e:
            print(f"ERROR ({e})")
            continue

        timer_found  = any(r.get("timer_value") is not None for r in regions.values())
        banner_found = any(r.get("has_text") for r in regions.values())
        print(f"{'⏱ ' if timer_found else '  '}{'🏷 ' if banner_found else '  '}")
        frames.append({"secs": secs, "ts": ts_label, "regions": regions})

    agg = aggregate(frames)
    data = {
        "federation":      args.federation,
        "competition":     args.competition,
        "url":             args.url,
        "video_id":        video_id,
        "frames_analyzed": len(frames),
        "aggregation":     agg,
        "frames":          frames,
    }

    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", f"{args.federation}_{args.competition}")
    json_path  = out_dir / f"{safe_name}.json"
    table_path = out_dir / "competition_formats.md"

    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    _append_to_table(data, table_path)

    _print_summary(data)
    print(f"  JSON  → {json_path}")
    print(f"  Tabla → {table_path}")


if __name__ == "__main__":
    main()
