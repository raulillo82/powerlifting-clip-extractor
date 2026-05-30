#!/usr/bin/env python3
"""
pytest suite for find_lifter.py

Run with:
    pytest test_find_lifter.py -v

No network or media files needed — frame extraction + OCR (`_scan_one`) is mocked,
so these tests exercise the pure detection/grouping logic only.
"""

import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import find_lifter as fl


@pytest.fixture(autouse=True)
def _quiet_and_reset():
    """Silence the stderr logger and reset the global cost accumulator per test."""
    fl._STATS.update(frames=0, ff_ms=0, ocr_ms=0)
    with patch.object(fl, "err", lambda *a, **k: None):
        yield


def _fake_scan_one(hits):
    """Build a `_scan_one` replacement that reports HIT for any secs in `hits`."""
    def _impl(url, work_dir, secs, token, prefix):
        found = secs in hits
        return secs, True, ("NOMBRE" if found else "otro"), found, 100, 50
    return _impl


# ── _normalize ──────────────────────────────────────────────────────────────

class TestNormalize:
    def test_strips_accents_and_uppercases(self):
        assert fl._normalize("Osuna Sánchez-Infante") == "OSUNA SANCHEZ-INFANTE"

    def test_plain_text_unchanged(self):
        assert fl._normalize("mellado") == "MELLADO"


# ── _token_matches_word ───────────────────────────────────────────────────────

class TestTokenMatchesWord:
    def test_exact_match(self):
        assert fl._token_matches_word("MELLADO", "MELLADO")

    def test_fuzzy_ocr_error(self):
        # Una letra mal leída debe seguir encajando por ratio difuso.
        assert fl._token_matches_word("MELLADO", "MELLADD")

    def test_subset_match(self):
        # word contiene tok con diferencia de longitud <= 2
        assert fl._token_matches_word("ZAPATA", "ZAPATAS")

    def test_too_short_word_rejected(self):
        assert not fl._token_matches_word("MELLADO", "MEL")

    def test_unrelated_rejected(self):
        assert not fl._token_matches_word("MELLADO", "GUTIERREZ")


# ── _match_token (lógica N-1 de N extraída de ocr_banner) ─────────────────────

class TestMatchToken:
    def test_full_match(self):
        _, found = fl._match_token("JUAN MELLADO ZAPATA", "MELLADO ZAPATA JUAN MA")
        assert found

    def test_one_failure_tolerated_when_three_subtokens(self):
        # sub_tokens = MELLADO, ZAPATA, JUAN (MA<3 se descarta). Falta JUAN → 1 fallo, OK.
        _, found = fl._match_token("MELLADO ZAPATA", "MELLADO ZAPATA JUAN MA")
        assert found

    def test_two_failures_rejected_when_three_subtokens(self):
        _, found = fl._match_token("MELLADO", "MELLADO ZAPATA JUAN MA")
        assert not found

    def test_compound_surname_split_by_ocr(self):
        # OCR separa SANCHEZ-INFANTE en dos palabras; debe encajar igualmente.
        _, found = fl._match_token("OSUNA SANCHEZ INFANTE", "OSUNA SANCHEZ-INFANTE")
        assert found

    def test_short_token_requires_all(self):
        # 2 sub_tokens (<3) → max_failures=0, exige ambos.
        _, found = fl._match_token("OSUNA", "OSUNA PEREZ")
        assert not found

    def test_empty_token(self):
        # token sin sub_tokens >=3 → nunca encuentra.
        text, found = fl._match_token("CUALQUIER COSA", "AB")
        assert found is False
        assert text == "CUALQUIER COSA"

    def test_returns_uppercased_display_text(self):
        text, _ = fl._match_token("juan mellado", "MELLADO")
        assert text == "JUAN MELLADO"


# ── scan_movement (paralelizado) ──────────────────────────────────────────────

class TestScanMovement:
    def _run(self, hits, start_s=2000, window=2000):
        with patch.object(fl, "_scan_one", _fake_scan_one(set(hits))):
            return fl.scan_movement("u", Path("/tmp"), start_s=start_s,
                                    max_window_s=window, token="X",
                                    label="SQ", prefix="sq")

    def test_groups_split_by_gap(self):
        # Tres grupos separados por > GROUP_GAP_S (90s).
        hits = [2000, 2010, 2020, 2300, 2310, 2600, 2610, 2620]
        groups = self._run(hits)
        assert groups == [[2000, 2010, 2020], [2300, 2310], [2600, 2610, 2620]]

    def test_timestamps_and_ends(self):
        hits = [2000, 2010, 2020, 2300, 2310, 2600, 2610, 2620]
        groups = self._run(hits)
        assert fl.groups_to_timestamps(groups) == [2000, 2300, 2600]
        assert fl.groups_to_ends(groups) == [2020, 2310, 2620]

    def test_early_stop_does_not_scan_whole_window(self):
        # Tras cerrar 3 grupos (último hit 2620), debe parar ~GROUP_GAP_S después,
        # no recorrer hasta el final de la ventana (4000s).
        hits = [2000, 2010, 2020, 2300, 2310, 2600, 2610, 2620]
        scanned = []
        orig = _fake_scan_one(set(hits))

        def recording(url, wd, secs, token, prefix):
            scanned.append(secs)
            return orig(url, wd, secs, token, prefix)

        with patch.object(fl, "_scan_one", recording):
            fl.scan_movement("u", Path("/tmp"), start_s=2000, max_window_s=2000,
                             token="X", label="SQ", prefix="sq")
        assert max(scanned) < 2800  # cortó poco después de 2620 + 90s

    def test_batch_size_does_not_change_result(self):
        # La paralelización por lotes debe dar el mismo resultado sea cual sea el lote.
        hits = [2000, 2010, 2020, 2300, 2310, 2600, 2610, 2620]
        with patch.object(fl, "SCAN_BATCH", 1):
            g1 = self._run(hits)
        with patch.object(fl, "SCAN_BATCH", 12):
            g12 = self._run(hits)
        assert g1 == g12

    def test_no_hits_returns_empty(self):
        assert self._run([]) == []


# ── refine_group_bounds (paralelizado) ────────────────────────────────────────

class TestRefineGroupBounds:
    def test_extends_min_and_max(self):
        # Banner real empieza antes (2996) y termina después (3026) del grupo detectado.
        hits = {2996, 2998, 3000, 3010, 3020, 3022, 3024, 3026}
        with patch.object(fl, "_scan_one", _fake_scan_one(hits)):
            out = fl.refine_group_bounds("u", Path("/tmp"), [[3000, 3010, 3020]],
                                         "X", "SQ", "sq")
        assert min(out[0]) == 2996
        assert max(out[0]) == 3026

    def test_stops_at_first_miss_on_the_end(self):
        # Hueco en 3022 (miss) → no debe extender hasta 3026 aunque haya hit allí.
        hits = {3000, 3010, 3020, 3026}
        with patch.object(fl, "_scan_one", _fake_scan_one(hits)):
            out = fl.refine_group_bounds("u", Path("/tmp"), [[3000, 3010, 3020]],
                                         "X", "SQ", "sq")
        assert max(out[0]) == 3020  # no extendió

    def test_no_change_when_nothing_around(self):
        with patch.object(fl, "_scan_one", _fake_scan_one(set())):
            out = fl.refine_group_bounds("u", Path("/tmp"), [[3000, 3010, 3020]],
                                         "X", "SQ", "sq")
        assert out == [[3000, 3010, 3020]]


# ── trim_isolated_starts (fix del overlay pre-cambiado) ───────────────────────

class TestTrimIsolatedStarts:
    def test_drops_isolated_first_hit(self):
        # gap 2845-2820 = 25 > ISOLATED_HIT_GAP_S (15) → descarta 2820.
        out = fl.trim_isolated_starts([[2820, 2845, 2855]], "DL")
        assert out == [[2845, 2855]]

    def test_keeps_when_gap_within_threshold(self):
        # gap 2830-2820 = 10 <= 15 → conserva.
        out = fl.trim_isolated_starts([[2820, 2830, 2840]], "DL")
        assert out == [[2820, 2830, 2840]]

    def test_gap_exactly_threshold_is_kept(self):
        # gap == ISOLATED_HIT_GAP_S (15) no es "> umbral" → conserva.
        gap = fl.ISOLATED_HIT_GAP_S
        out = fl.trim_isolated_starts([[1000, 1000 + gap, 1100]], "DL")
        assert out[0][0] == 1000

    def test_single_element_group_untouched(self):
        out = fl.trim_isolated_starts([[5000]], "DL")
        assert out == [[5000]]

    def test_multiple_groups(self):
        out = fl.trim_isolated_starts([[100, 150, 160], [500, 510]], "DL")
        assert out == [[150, 160], [500, 510]]


# ── groups_to_timestamps / groups_to_ends ─────────────────────────────────────

class TestGroupsHelpers:
    def test_timestamps_take_min(self):
        assert fl.groups_to_timestamps([[20, 10, 30], [200, 210]]) == [10, 200]

    def test_ends_take_max(self):
        assert fl.groups_to_ends([[20, 10, 30], [200, 210]]) == [30, 210]


# ── extract_frame timeout handling ───────────────────────────────────────────

class TestExtractFrameTimeout:
    def test_returns_false_on_timeout(self, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            result = fl.extract_frame("url", 0, tmp_path / "out.jpg")
        assert result is False


# ── scan_movement bail-out on consecutive extraction errors ──────────────────

class TestScanMovementBailout:
    def test_stops_early_on_consecutive_errors(self):
        # _scan_one always returns ok=False (simulates expired stream URL).
        def _always_fail(url, work_dir, secs, token, prefix):
            return secs, False, "", False, 30000, 0

        scanned = []
        orig = _always_fail

        def recording(url, wd, secs, token, prefix):
            scanned.append(secs)
            return orig(url, wd, secs, token, prefix)

        with patch.object(fl, "_scan_one", recording):
            fl.scan_movement("u", Path("/tmp"), start_s=0, max_window_s=3000,
                             token="X", label="SQ", prefix="sq")

        # Should bail out well before scanning all 300+ frames.
        assert len(scanned) <= (fl.MAX_CONSECUTIVE_ERRORS + 1) * fl.SCAN_BATCH


# ── detect_break_timer bail-out on consecutive extraction errors ─────────────

class TestDetectBreakTimerBailout:
    def test_bails_out_and_returns_none(self):
        call_count = []

        def _always_fail(url, secs, out):
            call_count.append(secs)
            return False

        with patch.object(fl, "extract_frame", side_effect=_always_fail):
            result = fl.detect_break_timer("url", Path("/tmp"), 0, "LBL", "pfx")

        assert result is None
        # Must bail out after MAX_CONSECUTIVE_ERRORS consecutive failures.
        assert len(call_count) <= fl.MAX_CONSECUTIVE_ERRORS


# ── read_timer multi-crop fallback ───────────────────────────────────────────

class TestReadTimerMultiCrop:
    def _make_img(self):
        from PIL import Image
        return Image.new("RGB", (1280, 720), (0, 0, 0))

    def test_falls_back_to_second_crop(self):
        returns = iter([None, 300])
        with patch.object(fl, "_read_timer_crop", side_effect=returns):
            with patch("PIL.Image.open", return_value=self._make_img()):
                assert fl.read_timer("fake.jpg") == 300

    def test_returns_first_hit_without_trying_second(self):
        mock = MagicMock(return_value=120)
        with patch.object(fl, "_read_timer_crop", mock):
            with patch("PIL.Image.open", return_value=self._make_img()):
                result = fl.read_timer("fake.jpg")
        assert result == 120
        assert mock.call_count == 1  # stopped after first hit

    def test_all_fail_returns_none(self):
        with patch.object(fl, "_read_timer_crop", return_value=None):
            with patch("PIL.Image.open", return_value=self._make_img()):
                assert fl.read_timer("fake.jpg") is None
