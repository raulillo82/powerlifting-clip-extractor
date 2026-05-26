import hashlib
import hmac
import os
from pathlib import Path

import requests

_REPO = "raulillo82/powerlifting-clip-extractor"
_PAT_PATH = Path(os.environ.get("GITHUB_PAT_PATH", "/app/github_pat.key"))
_SECRET_PATH = Path(os.environ.get("GITHUB_WEBHOOK_SECRET_PATH", "/app/webhook_secret.key"))


def _read_key(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def create_issue(title: str, body: str) -> dict:
    """Create a GitHub issue. Returns {number, html_url} or raises RuntimeError."""
    pat = _read_key(_PAT_PATH)
    if not pat:
        raise RuntimeError("GitHub PAT not configured")
    r = requests.post(
        f"https://api.github.com/repos/{_REPO}/issues",
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "labels": ["user-feedback"]},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return {"number": data["number"], "html_url": data["html_url"]}


def verify_signature(body: bytes, sig_header: str) -> bool:
    """Return True if the webhook body matches the stored secret (HMAC-SHA256)."""
    secret = _read_key(_SECRET_PATH)
    if not secret or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)
