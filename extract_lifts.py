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

def download_clip(url: str, start: int, duration: int, output: Path, label: str) -> None:
    """Download a single timed section from YouTube using yt-dlp."""
    end = start + duration
    section = f"*{seconds_to_hms(start)}-{seconds_to_hms(end)}"
    print(f"\n  [{label}]  {seconds_to_hms(start)} → {seconds_to_hms(end)}")

    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "--force-keyframes-at-cuts",           # accurate cut at exact timestamps (slow but precise)
        "-f", "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]",  # H.264 + AAC → Instagram compatible
        "--merge-output-format", "mp4",
        "--postprocessor-args", "ffmpeg:-movflags +faststart",  # web-optimised MP4
        "-N", "4",                             # parallel fragment downloads
        "-o", str(output),
        "--no-playlist",
        url,
    ]
    subprocess.run(cmd, check=True)


def make_combined(clips: list[Path], output: Path) -> None:
    """Stack three clips vertically with ffmpeg vstack (no audio)."""
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]

    filter_complex = "[0:v][1:v][2:v]vstack=inputs=3[v]"
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


def run(
    url: str,
    timestamps: list[int],
    duration: int,
    squat_attempt: int,
    bench_attempt: int,
    deadlift_attempt: int,
    output_dir: Path,
    skip_individual: bool,
    skip_combined: bool,
) -> None:
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
            download_clip(url, ts, duration, path, f"lift {i:02d} — {movement} attempt {attempt}")
    else:
        missing = [p for p in clip_paths if not p.exists()]
        if missing:
            sys.exit("Missing clips (run without --skip-individual first):\n" +
                     "\n".join(f"  {p}" for p in missing))

    if skip_combined:
        print(f"\nDone. Clips saved to: {output_dir}/")
        return

    selected = [
        clip_paths[squat_attempt - 1],          # squat:    index 0–2
        clip_paths[3 + bench_attempt - 1],       # bench:    index 3–5
        clip_paths[6 + deadlift_attempt - 1],    # deadlift: index 6–8
    ]
    combined_path = output_dir / f"combined_s{squat_attempt}_b{bench_attempt}_d{deadlift_attempt}.mp4"
    make_combined(selected, combined_path)

    print(f"\n{'='*52}")
    print(f"  Individual clips : {output_dir}/")
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

    duration = prompt_int(
        "\nClip duration in seconds", DEFAULT_DURATION, min_val=10, max_val=300
    )
    output_dir = Path(prompt("Output directory", DEFAULT_OUTPUT_DIR))

    print("\nWhich attempt to include in the combined video?")
    print("  (1 = first attempt, 2 = second, 3 = last — one per movement)")
    squat    = int(prompt_choice("  Squat",    ["1", "2", "3"], "3"))
    bench    = int(prompt_choice("  Bench",    ["1", "2", "3"], "3"))
    deadlift = int(prompt_choice("  Deadlift", ["1", "2", "3"], "3"))

    skip_ind  = not prompt_bool("\nDownload individual clips?", default=True)
    skip_comb = not prompt_bool("Create combined video?",      default=True)

    print()
    run(url, timestamps, duration, squat, bench, deadlift, output_dir, skip_ind, skip_comb)


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

    run(
        url=args.url,
        timestamps=timestamps,
        duration=args.duration,
        squat_attempt=args.squat,
        bench_attempt=args.bench,
        deadlift_attempt=args.deadlift,
        output_dir=Path(args.output_dir),
        skip_individual=args.skip_individual,
        skip_combined=args.skip_combined,
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
                        help=f"Clip duration in seconds (default: {DEFAULT_DURATION})")
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
