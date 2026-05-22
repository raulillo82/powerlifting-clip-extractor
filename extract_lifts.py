#!/usr/bin/env python3
"""
Extract powerlifting lifts from a YouTube competition video and create
an Instagram-compatible combined video with 3 lifts stacked vertically.

Modes:
  - Interactive : run without arguments, prompts for every input with defaults
  - Parameter   : pass URL as first argument; timestamps via --times FILE
                  or inline with --timestamps t1 t2 ... t9

Requirements: yt-dlp, ffmpeg
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

MOVEMENTS = [
    "squat", "squat", "squat",
    "bench", "bench", "bench",
    "deadlift", "deadlift", "deadlift",
]
DEFAULT_DURATION = 60
DEFAULT_OUTPUT_DIR = "lifts"
DEFAULT_TIMES_FILE = "times.txt"


# ── Timestamp helpers ──────────────────────────────────────────────────────────

def parse_timestamp(ts: str) -> int:
    """Parse mixed-format timestamp string to total seconds.

    Handles: HH:MM:SS, H:MM:SS, MM:SS, XhMM:SS, Xh:MM:SS
    Examples: '0:21:27', '1h23:30', '2h33:4'
    """
    ts = ts.strip()
    ts = re.sub(r'^(\d+)h:?(\d+):(\d+)$', r'\1:\2:\3', ts)  # normalise XhMM:SS
    parts = ts.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    raise ValueError(f"Cannot parse timestamp: '{ts}'")


def seconds_to_hms(total: int) -> str:
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_timestamps_file(path: Path) -> list[int]:
    """Read exactly 9 timestamps from a file (one per line, blank lines skipped)."""
    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    if len(lines) != 9:
        sys.exit(f"Error: expected 9 timestamps in '{path}', found {len(lines)}")
    return [parse_timestamp(l) for l in lines]


# ── Interactive prompt helpers ─────────────────────────────────────────────────

def prompt(question: str, default: str = "") -> str:
    """Show a prompt with an optional default; pressing Enter returns the default."""
    hint = f" [{default}]" if default else ""
    while True:
        answer = input(f"{question}{hint}: ").strip()
        if answer:
            return answer
        if default:
            return default
        print("  (this field is required, please enter a value)")


def prompt_int(question: str, default: int, min_val: int = 1, max_val: int = 9999) -> int:
    while True:
        raw = prompt(question, str(default))
        try:
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            print(f"  (enter a number between {min_val} and {max_val})")
        except ValueError:
            print("  (enter a valid integer)")


def prompt_bool(question: str, default: bool = True) -> bool:
    default_str = "yes" if default else "no"
    raw = prompt(f"{question} (yes/no)", default_str).lower()
    return raw in ("y", "yes", "s", "si", "sí", "1", "true")


def prompt_choice(question: str, choices: list[str], default: str) -> str:
    opts = "/".join(choices)
    while True:
        raw = prompt(f"{question} ({opts})", default)
        if raw in choices:
            return raw
        print(f"  (choose one of: {opts})")


def prompt_timestamps_manual() -> list[int]:
    """Prompt user to enter each of the 9 lift timestamps individually."""
    labels = [
        "Squat 1", "Squat 2", "Squat 3",
        "Bench 1", "Bench 2", "Bench 3",
        "Deadlift 1", "Deadlift 2", "Deadlift 3",
    ]
    print("  Enter timestamps in H:MM:SS or XhMM:SS format:")
    timestamps = []
    for label in labels:
        while True:
            raw = input(f"    {label}: ").strip()
            try:
                timestamps.append(parse_timestamp(raw))
                break
            except ValueError as e:
                print(f"    Error: {e} — try again")
    return timestamps


# ── Core download / assembly logic ─────────────────────────────────────────────

def get_clip_duration(path: Path) -> float:
    """Return the real duration of a video file in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def download_clip(url: str, start: int, duration: int, output: Path, label: str) -> None:
    """Download a single timed section from YouTube using yt-dlp."""
    end = start + duration
    section = f"*{seconds_to_hms(start)}-{seconds_to_hms(end)}"
    print(f"\n  [{label}]  {seconds_to_hms(start)} → {seconds_to_hms(end)}")

    tmp = output.with_suffix(".tmp.mp4")
    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "--force-keyframes-at-cuts",           # accurate cut at exact timestamps (slow but precise)
        "-f", "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]",  # H.264 + AAC → Instagram compatible
        "--merge-output-format", "mp4",
        "-N", "4",                             # parallel fragment downloads
        "-o", str(tmp),
        "--no-playlist",
        url,
    ]
    subprocess.run(cmd, check=True)

    # yt-dlp's --postprocessor-args doesn't reach the download-sections ffmpeg call,
    # so we apply faststart explicitly as a separate stream-copy pass (fast, lossless)
    faststart_cmd = [
        "ffmpeg", "-i", str(tmp),
        "-c", "copy", "-movflags", "+faststart",
        "-y", str(output),
    ]
    subprocess.run(faststart_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp.unlink()


def make_combined(clips: list[Path], output: Path, preview_width: int = 0) -> None:
    """Stack three clips vertically with ffmpeg vstack (no audio).

    Shorter clips are frozen on their last frame until the longest one ends.
    If preview_width > 0, each clip is scaled to that width before stacking.
    """
    real_durations = [get_clip_duration(c) for c in clips]
    max_dur = max(real_durations)

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]

    # Build per-stream filter: optional scale + tpad freeze to match longest clip
    labels = ["a", "b", "c"]
    parts = []
    for i, (dur, lbl) in enumerate(zip(real_durations, labels)):
        pad = max_dur - dur
        scale = f"scale={preview_width}:-2," if preview_width else ""
        parts.append(f"[{i}:v]{scale}tpad=stop_mode=clone:stop_duration={pad:.3f}[{lbl}]")

    filter_complex = ";".join(parts) + ";[a][b][c]vstack=inputs=3[v]"

    cmd = [
        "ffmpeg",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-an",                                 # no audio in combined video
        "-movflags", "+faststart",
        "-y",
        str(output),
    ]
    print(f"\n  [combined]  Creating {output.name} ...")
    subprocess.run(cmd, check=True)


def make_preview(source: Path, dest: Path, width: int) -> None:
    """Generate a low-resolution copy of a clip for local preview."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-i", str(source),
        "-vf", f"scale={width}:-2",
        "-c:v", "libx264", "-crf", "28", "-preset", "fast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-y", str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(
    url: str,
    timestamps: list[int],
    durations: dict[str, int],   # keys: "squat", "bench", "deadlift"
    squat_attempt: int,
    bench_attempt: int,
    deadlift_attempt: int,
    output_dir: Path,
    skip_individual: bool,
    skip_combined: bool,
    preview_width: int,   # 0 = no preview
    no_replay: bool = False,
) -> None:
    if no_replay:
        # Without a slow-motion replay, lifts are roughly half as long
        durations = {k: max(10, v // 2) for k, v in durations.items()}
    output_dir.mkdir(parents=True, exist_ok=True)

    clip_paths: list[Path] = []
    for i, movement in enumerate(MOVEMENTS, 1):
        attempt = ((i - 1) % 3) + 1
        clip_paths.append(output_dir / f"lift_{i:02d}_{movement}_attempt{attempt}.mp4")

    if not skip_individual:
        print(f"\nDownloading {len(timestamps)} clips into '{output_dir}/'...")
        for i, (ts, path) in enumerate(zip(timestamps, clip_paths), 1):
            movement = MOVEMENTS[i - 1]
            attempt = ((i - 1) % 3) + 1
            duration = durations[movement]
            download_clip(url, ts, duration, path, f"lift {i:02d} — {movement} attempt {attempt}")
            if preview_width:
                prev = output_dir / "preview" / path.name
                print(f"    → preview {prev.name}")
                make_preview(path, prev, preview_width)
    else:
        missing = [p for p in clip_paths if not p.exists()]
        if missing:
            sys.exit("Missing clips (run without --skip-individual first):\n" +
                     "\n".join(f"  {p}" for p in missing))
        if preview_width:
            print(f"\nGenerating previews from existing clips...")
            for path in clip_paths:
                prev = output_dir / "preview" / path.name
                print(f"  {path.name} → preview/{path.name}")
                make_preview(path, prev, preview_width)

    if skip_combined:
        print(f"\nDone. Clips saved to: {output_dir}/")
        return

    selected = [
        clip_paths[squat_attempt - 1],          # squat:    index 0–2
        clip_paths[3 + bench_attempt - 1],       # bench:    index 3–5
        clip_paths[6 + deadlift_attempt - 1],    # deadlift: index 6–8
    ]
    suffix = f"combined_s{squat_attempt}_b{bench_attempt}_d{deadlift_attempt}"
    combined_path = output_dir / f"{suffix}.mp4"
    make_combined(selected, combined_path)

    if preview_width:
        prev_combined = output_dir / "preview" / f"{suffix}.mp4"
        print(f"  → combined preview {prev_combined.name}")
        make_combined(selected, prev_combined, preview_width=preview_width)

    print(f"\n{'='*52}")
    print(f"  Individual clips : {output_dir}/")
    if preview_width:
        print(f"  Previews (low-res): {output_dir}/preview/")
    print(f"  Combined video   : {combined_path}")
    print(f"{'='*52}")


# ── Modes ──────────────────────────────────────────────────────────────────────

def interactive_mode() -> None:
    print("╔══════════════════════════════════════════════╗")
    print("║      Powerlifting Clip Extractor             ║")
    print("╚══════════════════════════════════════════════╝")
    print("(Press Enter to accept the value shown in [brackets])\n")

    url = prompt("YouTube URL")

    # Timestamps: from file or manual entry
    default_file = DEFAULT_TIMES_FILE if Path(DEFAULT_TIMES_FILE).exists() else ""
    if default_file:
        print(f"\nTimestamps: press Enter to load '{default_file}', or type another path / 'manual'.")
    else:
        print("\nTimestamps: enter a file path or 'manual' to type them one by one.")

    raw = input(f"Timestamps file{' [' + default_file + ']' if default_file else ''} (or 'manual'): ").strip()

    if raw.lower() == "manual":
        timestamps = prompt_timestamps_manual()
    elif raw:
        timestamps = load_timestamps_file(Path(raw))
        print(f"  Loaded {len(timestamps)} timestamps from '{raw}'")
    elif default_file:
        timestamps = load_timestamps_file(Path(default_file))
        print(f"  Loaded {len(timestamps)} timestamps from '{default_file}'")
    else:
        timestamps = prompt_timestamps_manual()

    print("\nClip duration in seconds (one per movement, Enter to keep same value):")
    dur_default = prompt_int("  Default for all", DEFAULT_DURATION, min_val=10, max_val=300)
    dur_squat    = prompt_int("  Squat",    dur_default, min_val=10, max_val=300)
    dur_bench    = prompt_int("  Bench",    dur_default, min_val=10, max_val=300)
    dur_deadlift = prompt_int("  Deadlift", dur_default, min_val=10, max_val=300)
    durations = {"squat": dur_squat, "bench": dur_bench, "deadlift": dur_deadlift}

    output_dir = Path(prompt("Output directory", DEFAULT_OUTPUT_DIR))

    print("\nWhich attempt to include in the combined video?")
    print("  (1 = first attempt, 2 = second, 3 = last — one per movement)")
    squat    = int(prompt_choice("  Squat",    ["1", "2", "3"], "3"))
    bench    = int(prompt_choice("  Bench",    ["1", "2", "3"], "3"))
    deadlift = int(prompt_choice("  Deadlift", ["1", "2", "3"], "3"))

    skip_ind  = not prompt_bool("\nDownload individual clips?", default=True)
    skip_comb = not prompt_bool("Create combined video?",      default=True)
    want_prev = prompt_bool("Generate low-res previews? (useful for slow devices)", default=False)
    prev_width = 640 if want_prev else 0

    print()
    run(url, timestamps, durations, squat, bench, deadlift, output_dir, skip_ind, skip_comb, prev_width)


def cli_mode(args: argparse.Namespace) -> None:
    if args.timestamps:
        if len(args.timestamps) != 9:
            sys.exit(f"Error: --timestamps requires exactly 9 values, got {len(args.timestamps)}")
        timestamps = [parse_timestamp(t) for t in args.timestamps]
    else:
        times_path = Path(args.times)
        if not times_path.exists():
            sys.exit(f"Error: timestamps file '{times_path}' not found. "
                     f"Use --times PATH or --timestamps t1..t9")
        timestamps = load_timestamps_file(times_path)

    durations = {
        "squat":    args.duration_squat    or args.duration,
        "bench":    args.duration_bench    or args.duration,
        "deadlift": args.duration_deadlift or args.duration,
    }
    run(
        url=args.url,
        timestamps=timestamps,
        durations=durations,
        squat_attempt=args.squat,
        bench_attempt=args.bench,
        deadlift_attempt=args.deadlift,
        output_dir=Path(args.output_dir),
        skip_individual=args.skip_individual,
        skip_combined=args.skip_combined,
        preview_width=args.preview_width,
        no_replay=args.no_replay,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_lifts.py",
        description="Extract powerlifting competition lifts from YouTube and create a combined Instagram video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Interactive mode — prompts for everything
  python extract_lifts.py

  # Parameter mode — timestamps from file (default: times.txt)
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao

  # Parameter mode — timestamps inline
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao \\
      --timestamps 0:21:27 0:29:55 0:38:15 1h23:30 1h32:21 1h41:30 2h26:15 2h33:4 2h41:35

  # Custom attempt selection for combined video
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao --squat 2 --bench 3 --deadlift 3

  # Only recreate the combined video from already-downloaded clips
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao --skip-individual
        """,
    )
    parser.add_argument("url", nargs="?",
                        help="YouTube URL (omit to run in interactive mode)")
    parser.add_argument("--times", metavar="FILE", default=DEFAULT_TIMES_FILE,
                        help=f"File with 9 timestamps, one per line (default: {DEFAULT_TIMES_FILE})")
    parser.add_argument("--timestamps", nargs=9, metavar="TS",
                        help="9 timestamps inline, e.g. 0:21:27 1h23:30 ... (overrides --times)")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, metavar="SECS",
                        help=f"Clip duration in seconds for all movements (default: {DEFAULT_DURATION})")
    parser.add_argument("--duration-squat", type=int, default=0, metavar="SECS",
                        help="Clip duration for squats (overrides --duration)")
    parser.add_argument("--duration-bench", type=int, default=0, metavar="SECS",
                        help="Clip duration for bench press (overrides --duration)")
    parser.add_argument("--duration-deadlift", type=int, default=0, metavar="SECS",
                        help="Clip duration for deadlifts (overrides --duration)")
    parser.add_argument("--squat",    type=int, default=3, choices=[1, 2, 3], metavar="{1,2,3}",
                        help="Squat attempt for combined video (default: 3)")
    parser.add_argument("--bench",    type=int, default=3, choices=[1, 2, 3], metavar="{1,2,3}",
                        help="Bench attempt for combined video (default: 3)")
    parser.add_argument("--deadlift", type=int, default=3, choices=[1, 2, 3], metavar="{1,2,3}",
                        help="Deadlift attempt for combined video (default: 3)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, metavar="DIR",
                        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}/)")
    parser.add_argument("--skip-individual", action="store_true",
                        help="Skip downloads, use existing clips in --output-dir")
    parser.add_argument("--skip-combined", action="store_true",
                        help="Download individual clips only, skip combined video")
    parser.add_argument("--preview", dest="preview_width", nargs="?", const=640,
                        type=int, default=0, metavar="WIDTH",
                        help="Generate low-res previews in <output-dir>/preview/ (default width: 640px)")
    parser.add_argument(
        "--no-replay", action="store_true", default=False,
        help=(
            "Use this ONLY if the competition video has no slow-motion replays after each lift. "
            "Most federation broadcasts (AEP, IPF…) include a replay, so the default is replay=on. "
            "When set, all clip durations are halved automatically. "
            "Recommended: leave this unset unless you are sure there are no replays."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.url:
        cli_mode(args)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
