#!/usr/bin/env python3
"""
Flask web interface for the Powerlifting Clip Extractor.

Run with:
    python3 app.py
Then open http://localhost:5000 in your browser.
From another machine on the same network: http://mordor:5000
"""

import contextlib
import io
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

from extract_lifts import load_timestamps_file, parse_timestamp, run

app = Flask(__name__)

# In-memory job store — single-user tool, no persistence needed
jobs: dict[str, dict] = {}


def _build_run_kwargs(form: dict, output_dir: Path) -> dict:
    url = form["url"].strip()
    if not url:
        raise ValueError("YouTube URL is required.")

    if form.get("timestamps_mode") == "file":
        path = Path(form["timestamps_file"].strip())
        if not path.exists():
            raise FileNotFoundError(f"Timestamps file not found: {path}")
        timestamps = load_timestamps_file(path)
    else:
        lines = [l.strip() for l in form.get("timestamps_text", "").splitlines() if l.strip()]
        if len(lines) != 9:
            raise ValueError(f"Expected 9 timestamps, got {len(lines)}.")
        timestamps = [parse_timestamp(l) for l in lines]

    duration = int(form.get("duration") or 60)
    durations = {
        "squat":    int(form.get("duration_squat")    or 0) or duration,
        "bench":    int(form.get("duration_bench")    or 0) or duration,
        "deadlift": int(form.get("duration_deadlift") or 0) or duration,
    }

    preview_width = 0
    if "preview" in form:
        preview_width = int(form.get("preview_width") or 640)

    music_source = form.get("music", "").strip()
    music_start_raw = form.get("music_start", "").strip()
    music_start = float(parse_timestamp(music_start_raw)) if music_start_raw else 0.0

    return dict(
        url=url,
        timestamps=timestamps,
        durations=durations,
        squat_attempt=int(form.get("squat_attempt", 3)),
        bench_attempt=int(form.get("bench_attempt", 3)),
        deadlift_attempt=int(form.get("deadlift_attempt", 3)),
        output_dir=output_dir,
        skip_individual="skip_individual" in form,
        skip_combined="skip_combined" in form,
        preview_width=preview_width,
        no_replay="no_replay" in form,
        music_source=music_source,
        music_start=music_start,
        interactive=False,
    )


def _worker(job_id: str, run_kwargs: dict) -> None:
    job = jobs[job_id]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run(**run_kwargs)
        job["status"] = "done"
    except SystemExit as e:
        job["status"] = "error"
        buf.write(f"\nFailed: {e}\n")
    except Exception as e:
        job["status"] = "error"
        buf.write(f"\nUnexpected error: {e}\n")
    finally:
        job["log"] = buf.getvalue()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def start_job():
    job_id = uuid.uuid4().hex
    output_dir = Path(request.form.get("output_dir") or "lifts")
    try:
        run_kwargs = _build_run_kwargs(request.form, output_dir)
    except (ValueError, FileNotFoundError) as e:
        return render_template("index.html", error=str(e)), 400

    jobs[job_id] = {"status": "running", "log": "", "output_dir": str(output_dir)}
    threading.Thread(target=_worker, args=(job_id, run_kwargs), daemon=True).start()
    return redirect(url_for("status", job_id=job_id))


@app.route("/status/<job_id>")
def status(job_id: str):
    if job_id not in jobs:
        return "Job not found", 404
    return render_template("status.html", job_id=job_id)


@app.route("/status/<job_id>/json")
def status_json(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    output_files: list[str] = []
    if job["status"] == "done":
        out_dir = Path(job["output_dir"])
        if out_dir.exists():
            output_files = sorted(
                str(f.relative_to(out_dir))
                for f in out_dir.rglob("*.mp4")
            )

    return jsonify({"status": job["status"], "log": job["log"], "output_files": output_files})


@app.route("/download/<job_id>/<path:filename>")
def download_file(job_id: str, filename: str):
    job = jobs.get(job_id)
    if not job:
        return "Job not found", 404
    file_path = (Path(job["output_dir"]) / filename).resolve()
    out_dir = Path(job["output_dir"]).resolve()
    if not file_path.is_relative_to(out_dir) or not file_path.is_file():
        return "File not found", 404
    return send_file(file_path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
