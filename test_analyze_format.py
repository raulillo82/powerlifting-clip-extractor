#!/usr/bin/env python3
"""Tests for analyze_format.py — no network/disk access needed."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest
from PIL import Image

import analyze_format as af


# ── TestProbeColor ────────────────────────────────────────────────────────────

class TestProbeColor:
    def _solid_img(self, rgb: tuple[int, int, int], size=(100, 50)) -> Image.Image:
        arr = np.full((*size[::-1], 3), rgb, dtype=np.uint8)
        return Image.fromarray(arr, "RGB")

    def test_red_region_counted(self):
        img = self._solid_img((180, 30, 30))
        arr = np.array(img)
        R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        px = int(af.BG_MASKS["red"](R, G, B).sum())
        assert px > 200

    def test_yellow_region_counted(self):
        img = self._solid_img((200, 190, 50))
        arr = np.array(img)
        R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        px = int(af.BG_MASKS["yellow"](R, G, B).sum())
        assert px > 200

    def test_mixed_image_not_counted_as_single_color(self):
        # Random noise → no single color mask should fire high
        arr = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        for name, mask_fn in af.BG_MASKS.items():
            if name in ("white",):
                continue  # white mask may fire on random noise
            px = int(mask_fn(R, G, B).sum())
            assert px < 3000, f"{name}: {px} pixels in random image"


# ── TestTimerDetection ────────────────────────────────────────────────────────

class TestTimerDetection:
    def _make_timer_frame(self, text: str, bg_rgb=(200, 20, 20)) -> Image.Image:
        """Create a 1280×720 frame with a colored timer box in the bottom-right."""
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[634:720, 998:1280] = bg_rgb   # bottom-right crop region
        img = Image.fromarray(frame, "RGB")
        return img

    def test_returns_none_on_blank_frame(self):
        img = Image.fromarray(np.zeros((720, 1280, 3), dtype=np.uint8), "RGB")
        result = af._try_read_timer(img, 1280, 720, 0.78, 0.88, 1.00, 1.00)
        assert result is None

    def test_returns_none_when_no_colored_pixels(self):
        # Gray frame — no mask fires
        img = Image.fromarray(np.full((720, 1280, 3), 128, dtype=np.uint8), "RGB")
        result = af._try_read_timer(img, 1280, 720, 0.78, 0.88, 1.00, 1.00)
        assert result is None


# ── TestAggregation ───────────────────────────────────────────────────────────

class TestAggregation:
    def _make_frames(self, n: int, timer_region: str, timer_val: int | None,
                     text_regions: list[str]) -> list[dict]:
        frames = []
        for _ in range(n):
            regions = {}
            for name, *_ in af.PROBE_REGIONS:
                regions[name] = {
                    "timer_value": timer_val if name == timer_region else None,
                    "has_text": name in text_regions,
                    "color_variance": 20.0 if name in text_regions else 60.0,
                    "red_px": 500 if name == timer_region else 0,
                    "blue_px": 0, "green_px": 0, "yellow_px": 0,
                    "white_px": 0, "dark_px": 0,
                }
            frames.append({"regions": regions})
        return frames

    def test_timer_hit_rate_above_threshold_marked_as_candidate(self):
        # 4 out of 5 frames have timer in bottom_right → hit_rate = 0.8 > 0.3
        frames = self._make_frames(5, "timer_bottom_right", 300, [])
        frames[4]["regions"]["timer_bottom_right"]["timer_value"] = None  # 1 miss
        agg = af.aggregate(frames)
        assert agg["timer_bottom_right"]["timer_hit_rate"] == 0.8
        assert agg["timer_bottom_right"]["is_timer_candidate"] is True

    def test_timer_below_threshold_not_marked(self):
        # 1 out of 5 frames → hit_rate = 0.2 < 0.3
        frames = self._make_frames(5, "timer_bottom_right", 300, [])
        frames[1]["regions"]["timer_bottom_right"]["timer_value"] = None
        frames[2]["regions"]["timer_bottom_right"]["timer_value"] = None
        frames[3]["regions"]["timer_bottom_right"]["timer_value"] = None
        frames[4]["regions"]["timer_bottom_right"]["timer_value"] = None
        agg = af.aggregate(frames)
        assert agg["timer_bottom_right"]["is_timer_candidate"] is False

    def test_banner_candidate_requires_text_and_low_variance(self):
        frames = self._make_frames(5, "timer_bottom_right", None,
                                   ["banner_bottom_left"])
        agg = af.aggregate(frames)
        # text_hit_rate = 1.0, mean_variance = 20 < 60 → candidate
        assert agg["banner_bottom_left"]["is_banner_candidate"] is True
        # timer regions with no text → not banner candidates
        assert agg["timer_bottom_right"]["is_banner_candidate"] is False

    def test_banner_high_variance_not_candidate(self):
        # Even if text is found, high color variance (looks like video, not overlay)
        frames = self._make_frames(5, "timer_bottom_right", None,
                                   ["banner_bottom_left"])
        for f in frames:
            f["regions"]["banner_bottom_left"]["color_variance"] = 80.0
        agg = af.aggregate(frames)
        assert agg["banner_bottom_left"]["is_banner_candidate"] is False

    def test_empty_frames_returns_empty(self):
        assert af.aggregate([]) == {}


# ── TestAppendToTable ─────────────────────────────────────────────────────────

class TestAppendToTable:
    def _sample_data(self, fed="AEP", comp="Test 2026") -> dict:
        return {
            "federation": fed,
            "competition": comp,
            "video_id": "abc123",
            "url": "https://youtube.com/watch?v=abc123",
            "frames_analyzed": 8,
            "aggregation": {
                "timer_bottom_right": {
                    "is_timer_candidate": True, "dominant_bg": "red",
                    "timer_hit_rate": 0.75, "text_hit_rate": 0.0,
                    "mean_color_variance": 25.0, "is_banner_candidate": False,
                },
                "banner_bottom_left": {
                    "is_timer_candidate": False, "dominant_bg": "yellow",
                    "timer_hit_rate": 0.0, "text_hit_rate": 0.875,
                    "mean_color_variance": 22.0, "is_banner_candidate": True,
                },
            },
        }

    def test_creates_table_with_header_on_first_call(self, tmp_path):
        table = tmp_path / "formats.md"
        af._append_to_table(self._sample_data(), table)
        content = table.read_text()
        assert "| Federación" in content
        assert "AEP" in content
        assert "bottom_right (red)" in content

    def test_second_call_adds_row(self, tmp_path):
        table = tmp_path / "formats.md"
        af._append_to_table(self._sample_data("AEP", "Comp A"), table)
        af._append_to_table(self._sample_data("IPF", "Comp B"), table)
        rows = [l for l in table.read_text().splitlines() if l.startswith("| A") or l.startswith("| I")]
        assert len(rows) == 2
