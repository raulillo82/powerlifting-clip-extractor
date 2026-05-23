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
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

from extract_lifts import parse_timestamp, run

app = Flask(__name__)

# In-memory cache — populated from disk on cache miss so server restarts are safe
jobs: dict[str, dict] = {}


# ── Persistence ────────────────────────────────────────────────────────────────

def _status_path(job_id: str) -> Path:
    return Path("lifts") / job_id[:8] / "status.json"


def _save_job(job_id: str, job: dict) -> None:
    path = _status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status":     job["status"],
        "log":        job["log"],
        "output_dir": job["output_dir"],
    }))


def _load_job(job_id: str) -> dict | None:
    path = _status_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ── Live log buffer ────────────────────────────────────────────────────────────

class _LiveLog(io.StringIO):
    """StringIO that updates job["log"] on every write and flushes to disk every 5 s."""
    def __init__(self, job_id: str, job: dict):
        super().__init__()
        self._job_id = job_id
        self._job = job
        self._last_flush = 0.0

    def write(self, s: str) -> int:
        result = super().write(s)
        self._job["log"] = self.getvalue()
        now = time.monotonic()
        if now - self._last_flush >= 5.0:
            _save_job(self._job_id, self._job)
            self._last_flush = now
        return result


# ── Worker ─────────────────────────────────────────────────────────────────────

def _worker(job_id: str, run_kwargs: dict) -> None:
    job = jobs[job_id]
    buf = _LiveLog(job_id, job)
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
        _save_job(job_id, job)  # final authoritative write


# ── Form parsing ───────────────────────────────────────────────────────────────

class FormError(ValueError):
    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


def _build_run_kwargs(form, files, output_dir: Path) -> dict:
    url = form.get("url", "").strip()
    if not url:
        raise FormError("YouTube URL is required.", field="url")

    if form.get("timestamps_mode") == "file":
        uploaded = files.get("timestamps_file")
        if not uploaded or uploaded.filename == "":
            raise FormError("Please select a timestamps file.", field="timestamps_file")
        content = uploaded.read().decode("utf-8")
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if len(lines) != 9:
            raise FormError(f"Expected 9 timestamps, got {len(lines)}.", field="timestamps_file")
        timestamps = [parse_timestamp(l) for l in lines]
    else:
        lines = [l.strip() for l in form.get("timestamps_text", "").splitlines() if l.strip()]
        if len(lines) != 9:
            raise FormError(f"Expected 9 timestamps, got {len(lines)}.", field="timestamps_text")
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


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def start_job():
    job_id = uuid.uuid4().hex
    output_dir = Path("lifts") / job_id[:8]
    try:
        run_kwargs = _build_run_kwargs(request.form, request.files, output_dir)
    except FormError as e:
        return render_template("index.html",
                               error=str(e),
                               error_field=e.field,
                               form_data=request.form), 400

    job = {"status": "running", "log": "", "output_dir": str(output_dir)}
    jobs[job_id] = job
    _save_job(job_id, job)  # persist immediately so the page survives a restart
    threading.Thread(target=_worker, args=(job_id, run_kwargs), daemon=True).start()
    return redirect(url_for("status", job_id=job_id))


@app.route("/status/<job_id>")
def status(job_id: str):
    # Accept the page even if the job isn't in memory — disk recovery happens in status_json
    return render_template("status.html", job_id=job_id)


@app.route("/status/<job_id>/json")
def status_json(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    jobs[job_id] = job  # restore to memory cache if it came from disk

    output_files: list[str] = []
    if job["status"] == "done":
        out_dir = Path(job["output_dir"])
        if out_dir.exists():
            output_files = sorted(
                str(f.relative_to(out_dir))
                for f in out_dir.rglob("*.mp4")
            )

    return jsonify({"status": job["status"], "log": job["log"], "output_files": output_files})


@app.route("/download/<job_id>/zip")
def download_zip(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job or job["status"] != "done":
        return "Not ready", 404
    out_dir = Path(job["output_dir"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out_dir.rglob("*.mp4")):
            zf.write(f, f.relative_to(out_dir))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="powerlifting_clips.zip")


@app.route("/download/<job_id>/<path:filename>")
def download_file(job_id: str, filename: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job:
        return "Job not found", 404
    file_path = (Path(job["output_dir"]) / filename).resolve()
    out_dir = Path(job["output_dir"]).resolve()
    if not file_path.is_relative_to(out_dir) or not file_path.is_file():
        return "File not found", 404
    return send_file(file_path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
