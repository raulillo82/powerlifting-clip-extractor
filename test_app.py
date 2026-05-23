"""Flask route tests using the built-in test client."""
import io
import json
import time
from unittest.mock import patch

import pytest

import app as flask_app

# ── Helpers ────────────────────────────────────────────────────────────────────

VALID_TIMESTAMPS = "\n".join([
    "0:21:27", "0:29:55", "0:38:15",
    "1h23:30", "1h32:21", "1h41:30",
    "2h26:15", "2h33:4",  "2h41:35",
])

VALID_FORM = {
    "url":             "https://www.youtube.com/watch?v=test",
    "timestamps_mode": "paste",
    "timestamps_text": VALID_TIMESTAMPS,
    "duration":        "60",
    "squat_attempt":   "3",
    "bench_attempt":   "3",
    "deadlift_attempt":"3",
}


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_jobs():
    flask_app.jobs.clear()
    yield
    flask_app.jobs.clear()


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


# ── Index ──────────────────────────────────────────────────────────────────────

class TestIndex:
    def test_returns_200(self, client):
        assert client.get("/").status_code == 200

    def test_contains_form(self, client):
        assert b"<form" in client.get("/").data


# ── /run ──────────────────────────────────────────────────────────────────────

class TestRun:
    def _post(self, client, overrides=None):
        data = {**VALID_FORM, **(overrides or {})}
        with patch("app._save_job"), patch("app.threading.Thread"):
            return client.post("/run", data=data, follow_redirects=False)

    def test_missing_url_returns_400(self, client):
        assert self._post(client, {"url": ""}).status_code == 400

    def test_missing_url_highlights_field(self, client):
        r = self._post(client, {"url": ""})
        assert b'id="url"' in r.data
        assert b"is-invalid" in r.data

    def test_missing_url_repopulates_timestamps(self, client):
        r = self._post(client, {"url": ""})
        assert VALID_TIMESTAMPS[:10].encode() in r.data

    def test_too_few_timestamps_returns_400(self, client):
        assert self._post(client, {"timestamps_text": "0:21:27\n0:29:55"}).status_code == 400

    def test_too_few_timestamps_highlights_field(self, client):
        r = self._post(client, {"timestamps_text": "0:21:27\n0:29:55"})
        assert b"is-invalid" in r.data

    def test_valid_form_redirects_to_status(self, client):
        r = self._post(client)
        assert r.status_code == 302
        assert "/status/" in r.headers["Location"]

    def test_valid_form_creates_running_job(self, client):
        self._post(client)
        assert len(flask_app.jobs) == 1
        assert next(iter(flask_app.jobs.values()))["status"] == "running"

    def test_file_mode_missing_file_returns_400(self, client):
        assert self._post(client, {"timestamps_mode": "file"}).status_code == 400

    def test_file_mode_wrong_count_returns_400(self, client):
        data = {**VALID_FORM, "timestamps_mode": "file",
                "timestamps_file": (io.BytesIO(b"0:21:27\n0:29:55"), "times.txt")}
        with patch("app._save_job"), patch("app.threading.Thread"):
            r = client.post("/run", data=data, follow_redirects=False)
        assert r.status_code == 400

    def test_file_mode_valid_file_redirects(self, client):
        data = {**VALID_FORM, "timestamps_mode": "file",
                "timestamps_file": (io.BytesIO(VALID_TIMESTAMPS.encode()), "times.txt")}
        with patch("app._save_job"), patch("app.threading.Thread"):
            r = client.post("/run", data=data, follow_redirects=False)
        assert r.status_code == 302


# ── /status/<id> ──────────────────────────────────────────────────────────────

class TestStatusPage:
    def test_any_job_id_returns_200(self, client):
        assert client.get("/status/anyjobid").status_code == 200


# ── /status/<id>/json ─────────────────────────────────────────────────────────

class TestStatusJson:
    def test_known_job_returns_status(self, client):
        flask_app.jobs["abc"] = {"status": "running", "log": "", "expires_at": None}
        r = client.get("/status/abc/json")
        assert r.status_code == 200
        assert json.loads(r.data)["status"] == "running"

    def test_unknown_job_returns_404(self, client):
        with patch("app._load_job", return_value=None):
            r = client.get("/status/doesnotexist/json")
        assert r.status_code == 404

    def test_disk_fallback_on_cache_miss(self, client):
        on_disk = {"status": "done", "log": "ok",
                   "output_dir": "lifts/abc12345", "expires_at": None}
        with patch("app._load_job", return_value=on_disk):
            r = client.get("/status/abc12345/json")
        assert r.status_code == 200
        assert json.loads(r.data)["status"] == "done"


# ── /download/<id>/zip ────────────────────────────────────────────────────────

class TestDownloadZip:
    def test_unknown_job_returns_404(self, client):
        with patch("app._load_job", return_value=None):
            assert client.get("/download/nosuchjob/zip").status_code == 404

    def test_running_job_returns_404(self, client):
        flask_app.jobs["runjob"] = {"status": "running", "log": "",
                                    "output_dir": "x", "expires_at": None}
        assert client.get("/download/runjob/zip").status_code == 404

    def test_expired_job_returns_410(self, client):
        flask_app.jobs["expjob"] = {"status": "done", "log": "",
                                    "output_dir": "lifts/expjob",
                                    "expires_at": time.time() - 1}
        assert client.get("/download/expjob/zip").status_code == 410
