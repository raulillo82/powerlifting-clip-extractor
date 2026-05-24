#!/usr/bin/env python3
"""
pytest suite for extract_lifts.py

Run with:
    pytest test_extract_lifts.py -v

No network or media files needed — external calls (yt-dlp, ffmpeg) are mocked.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from extract_lifts import (
    parse_timestamp,
    seconds_to_hms,
    load_timestamps_file,
    resolve_music,
    build_parser,
    run,
    get_clip_duration,
    add_music,
    add_mixed_audio,
)


# ── parse_timestamp ────────────────────────────────────────────────────────────

class TestParseTimestamp:
    def test_hh_mm_ss(self):
        assert parse_timestamp("0:21:27") == 21 * 60 + 27

    def test_h_mm_ss_with_hour(self):
        assert parse_timestamp("1:23:45") == 3600 + 23 * 60 + 45

    def test_xh_mm_ss(self):
        assert parse_timestamp("1h23:30") == 3600 + 23 * 60 + 30

    def test_xh_colon_mm_ss(self):
        assert parse_timestamp("2h:33:4") == 2 * 3600 + 33 * 60 + 4

    def test_mm_ss(self):
        assert parse_timestamp("1:30") == 90

    def test_strips_whitespace(self):
        assert parse_timestamp("  0:21:27  ") == 21 * 60 + 27

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_timestamp("not-a-time")

    def test_real_sample_file_timestamps(self):
        # Reproduce the actual times.txt used in the project to guard against regressions
        inputs = [
            "0:21:27", "0:29:55", "0:38:15",
            "1h23:30", "1h32:21", "1h41:30",
            "2h26:15", "2h33:4",  "2h41:35",
        ]
        expected = [
            0 * 3600 + 21 * 60 + 27,
            0 * 3600 + 29 * 60 + 55,
            0 * 3600 + 38 * 60 + 15,
            1 * 3600 + 23 * 60 + 30,
            1 * 3600 + 32 * 60 + 21,
            1 * 3600 + 41 * 60 + 30,
            2 * 3600 + 26 * 60 + 15,
            2 * 3600 + 33 * 60 +  4,
            2 * 3600 + 41 * 60 + 35,
        ]
        assert [parse_timestamp(t) for t in inputs] == expected


# ── seconds_to_hms ─────────────────────────────────────────────────────────────

class TestSecondsToHms:
    def test_zero(self):
        assert seconds_to_hms(0) == "00:00:00"

    def test_one_hour(self):
        assert seconds_to_hms(3600) == "01:00:00"

    def test_mixed(self):
        assert seconds_to_hms(1 * 3600 + 23 * 60 + 30) == "01:23:30"

    def test_roundtrip_with_parse_timestamp(self):
        for ts in ["0:21:27", "1h23:30", "2h33:4"]:
            secs = parse_timestamp(ts)
            # seconds_to_hms always produces HH:MM:SS — parse_timestamp handles that format
            assert parse_timestamp(seconds_to_hms(secs)) == secs


# ── load_timestamps_file ───────────────────────────────────────────────────────

class TestLoadTimestampsFile:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "times.txt"
        f.write_text(
            "0:21:27\n0:29:55\n0:38:15\n"
            "1h23:30\n1h32:21\n1h41:30\n"
            "2h26:15\n2h33:4\n2h41:35\n"
        )
        result = load_timestamps_file(f)
        assert len(result) == 9
        assert result[0] == 21 * 60 + 27
        assert result[3] == 3600 + 23 * 60 + 30

    def test_blank_lines_are_ignored(self, tmp_path):
        f = tmp_path / "times.txt"
        f.write_text(
            "\n0:21:27\n\n0:29:55\n0:38:15\n"
            "1h23:30\n1h32:21\n1h41:30\n"
            "2h26:15\n2h33:4\n2h41:35\n\n"
        )
        result = load_timestamps_file(f)
        assert len(result) == 9

    def test_wrong_count_calls_sys_exit(self, tmp_path):
        f = tmp_path / "bad.txt"
        f.write_text("0:21:27\n0:29:55\n")  # only 2 timestamps
        with pytest.raises(SystemExit):
            load_timestamps_file(f)


# ── resolve_music ──────────────────────────────────────────────────────────────

class TestResolveMusicUrl:
    def test_http_url_passthrough(self):
        url = "http://youtube.com/watch?v=abc"
        assert resolve_music(url) == url

    def test_https_url_passthrough(self):
        url = "https://www.youtube.com/watch?v=xyz"
        assert resolve_music(url) == url

    def test_search_query_non_interactive_picks_first(self):
        # patch("extract_lifts.search_youtube") replaces the real yt-dlp call
        # with a function that instantly returns our fake data
        fake_results = [
            {"title": "Song A", "channel": "Artist", "duration": "3:30", "url": "https://yt/A"},
            {"title": "Song B", "channel": "Artist", "duration": "4:00", "url": "https://yt/B"},
        ]
        with patch("extract_lifts.search_youtube", return_value=fake_results):
            result = resolve_music("daft punk harder better", interactive=False)
        assert result == "https://yt/A"

    def test_search_with_no_results_calls_sys_exit(self):
        with patch("extract_lifts.search_youtube", return_value=[]):
            with pytest.raises(SystemExit):
                resolve_music("this returns nothing", interactive=False)


# ── build_parser ───────────────────────────────────────────────────────────────

class TestBuildParser:
    def setup_method(self):
        self.parser = build_parser()

    def test_all_defaults(self):
        args = self.parser.parse_args(["https://yt.com/v"])
        assert args.duration == 60
        assert args.squat == 3
        assert args.bench == 3
        assert args.deadlift == 3
        assert args.output_dir == "lifts"
        assert args.skip_individual is False
        assert args.skip_combined is False
        assert args.no_replay is False
        assert args.music is None
        assert args.music_start == "0"

    def test_no_url_gives_none(self):
        args = self.parser.parse_args([])
        assert args.url is None

    def test_timestamps_inline(self):
        ts = ["0:21:27", "0:29:55", "0:38:15",
              "1h23:30", "1h32:21", "1h41:30",
              "2h26:15", "2h33:4", "2h41:35"]
        args = self.parser.parse_args(["https://yt.com/v", "--timestamps"] + ts)
        assert args.timestamps == ts

    def test_timestamps_requires_exactly_9(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["https://yt.com/v", "--timestamps", "0:00:01", "0:00:02"])

    def test_no_replay_flag(self):
        args = self.parser.parse_args(["https://yt.com/v", "--no-replay"])
        assert args.no_replay is True

    def test_per_movement_durations(self):
        args = self.parser.parse_args(
            ["https://yt.com/v", "--duration", "60",
             "--duration-squat", "70", "--duration-deadlift", "40"]
        )
        assert args.duration_squat == 70
        assert args.duration_bench == 0       # not set → cli_mode falls back to --duration
        assert args.duration_deadlift == 40

    def test_preview_default_width(self):
        args = self.parser.parse_args(["https://yt.com/v", "--preview"])
        assert args.preview_width == 640

    def test_preview_custom_width(self):
        args = self.parser.parse_args(["https://yt.com/v", "--preview", "480"])
        assert args.preview_width == 480


# ── run() — integration-level with all external I/O mocked ────────────────────

# Reusable test data matching the real times.txt
SAMPLE_TIMESTAMPS = [
    0 * 3600 + 21 * 60 + 27,
    0 * 3600 + 29 * 60 + 55,
    0 * 3600 + 38 * 60 + 15,
    1 * 3600 + 23 * 60 + 30,
    1 * 3600 + 32 * 60 + 21,
    1 * 3600 + 41 * 60 + 30,
    2 * 3600 + 26 * 60 + 15,
    2 * 3600 + 33 * 60 +  4,
    2 * 3600 + 41 * 60 + 35,
]
SAMPLE_DURATIONS = {"squat": 60, "bench": 60, "deadlift": 45}


class TestRun:
    def test_nine_clips_are_downloaded_with_correct_names(self, tmp_path):
        with patch("extract_lifts.download_clip") as mock_dl, \
             patch("extract_lifts.make_combined"):
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations=SAMPLE_DURATIONS,
                squat_attempt=3, bench_attempt=3, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=True,
                preview_width=0,
            )
        assert mock_dl.call_count == 9
        # call_args_list[i][0] is the tuple of positional args for call i
        # signature: download_clip(url, start, duration, output, label)
        first_output: Path = mock_dl.call_args_list[0][0][3]
        last_output: Path  = mock_dl.call_args_list[8][0][3]
        assert first_output.name == "lift_01_squat_attempt1.mp4"
        assert last_output.name  == "lift_09_deadlift_attempt3.mp4"

    def test_no_replay_halves_all_durations(self, tmp_path):
        captured: list[tuple[str, int]] = []

        def spy_download(url, start, duration, output, label):
            captured.append((label, duration))

        with patch("extract_lifts.download_clip", side_effect=spy_download):
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations={"squat": 60, "bench": 60, "deadlift": 45},
                squat_attempt=3, bench_attempt=3, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=True,
                preview_width=0,
                no_replay=True,
            )

        squat_dur    = next(d for lbl, d in captured if "squat"    in lbl)
        deadlift_dur = next(d for lbl, d in captured if "deadlift" in lbl)
        assert squat_dur == 30          # 60 // 2
        assert deadlift_dur == 22       # 45 // 2

    def test_no_replay_minimum_duration_is_10s(self, tmp_path):
        captured: list[tuple[str, int]] = []

        def spy_download(url, start, duration, output, label):
            captured.append((label, duration))

        with patch("extract_lifts.download_clip", side_effect=spy_download):
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations={"squat": 15, "bench": 15, "deadlift": 15},
                squat_attempt=3, bench_attempt=3, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=True,
                preview_width=0,
                no_replay=True,
            )

        for _lbl, dur in captured:
            assert dur >= 10            # max(10, v // 2) never goes below 10

    def test_skip_combined_skips_make_combined(self, tmp_path):
        with patch("extract_lifts.download_clip"), \
             patch("extract_lifts.make_combined") as mock_combined:
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations=SAMPLE_DURATIONS,
                squat_attempt=3, bench_attempt=3, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=True,
                preview_width=0,
            )
        mock_combined.assert_not_called()

    def test_combined_selects_correct_attempt(self, tmp_path):
        with patch("extract_lifts.download_clip"), \
             patch("extract_lifts.make_combined") as mock_combined:
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations=SAMPLE_DURATIONS,
                squat_attempt=2, bench_attempt=1, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=False,
                preview_width=0,
            )
        selected: list[Path] = mock_combined.call_args[0][0]
        assert "squat_attempt2"    in selected[0].name
        assert "bench_attempt1"    in selected[1].name
        assert "deadlift_attempt3" in selected[2].name

    def test_music_generates_for_instagram_and_with_music_files(self, tmp_path):
        with patch("extract_lifts.download_clip"), \
             patch("extract_lifts.make_combined") as mock_combined, \
             patch("extract_lifts.resolve_music", return_value="https://yt.com/music"), \
             patch("extract_lifts.download_audio"), \
             patch("extract_lifts.add_music") as mock_add_music:
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations=SAMPLE_DURATIONS,
                squat_attempt=3, bench_attempt=3, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=False,
                preview_width=0,
                music_source="daft punk",
            )

        combined_output: Path = mock_combined.call_args[0][1]
        assert "for-instagram" in combined_output.name

        music_output: Path = mock_add_music.call_args[0][2]  # add_music(video, audio, output, ...)
        assert "with-music" in music_output.name

    def test_no_music_uses_plain_combined_name(self, tmp_path):
        with patch("extract_lifts.download_clip"), \
             patch("extract_lifts.make_combined") as mock_combined:
            run(
                url="https://yt.com/v",
                timestamps=SAMPLE_TIMESTAMPS,
                durations=SAMPLE_DURATIONS,
                squat_attempt=3, bench_attempt=3, deadlift_attempt=3,
                output_dir=tmp_path,
                skip_individual=False,
                skip_combined=False,
                preview_width=0,
                music_source="",
            )

        combined_output: Path = mock_combined.call_args[0][1]
        assert "for-instagram" not in combined_output.name
        assert "with-music"    not in combined_output.name
        assert combined_output.name == "combined_s3_b3_d3.mp4"


# ── get_clip_duration ──────────────────────────────────────────────────────────

class TestGetClipDuration:
    def _make_result(self, stdout):
        m = MagicMock()
        m.stdout = stdout
        return m

    def test_returns_float_parsed_from_ffprobe(self, tmp_path):
        fake = tmp_path / "clip.mp4"
        fake.write_bytes(b"")
        with patch("subprocess.run", return_value=self._make_result("123.456\n")) as mock_run:
            result = get_clip_duration(fake)
        assert result == pytest.approx(123.456)

    def test_ffprobe_called_with_show_entries_duration(self, tmp_path):
        fake = tmp_path / "clip.mp4"
        fake.write_bytes(b"")
        with patch("subprocess.run", return_value=self._make_result("60.0\n")) as mock_run:
            get_clip_duration(fake)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffprobe"
        assert "-show_entries" in cmd
        assert "format=duration" in cmd


# ── add_music ──────────────────────────────────────────────────────────────────

class TestAddMusic:
    def _run(self, tmp_path, video_dur, song_dur, music_start):
        video = tmp_path / "v.mp4"
        audio = tmp_path / "a.m4a"
        output = tmp_path / "out.mp4"
        video.write_bytes(b"")
        audio.write_bytes(b"")

        captured_cmd = []
        def fake_run(cmd, **kw):
            captured_cmd.extend(cmd)

        with patch("extract_lifts.get_clip_duration", side_effect=[video_dur, song_dur]), \
             patch("subprocess.run", side_effect=fake_run):
            add_music(video, audio, output, music_start=music_start)

        return " ".join(captured_cmd)

    def test_start_clamped_when_too_close_to_end(self, tmp_path):
        # video=60s, song=100s, music_start=90 → max_start=40 → clamped
        cmd = self._run(tmp_path, video_dur=60.0, song_dur=100.0, music_start=90.0)
        assert "atrim=start=40.000" in cmd

    def test_start_unchanged_when_within_range(self, tmp_path):
        # video=60s, song=100s, music_start=10 → stays 10
        cmd = self._run(tmp_path, video_dur=60.0, song_dur=100.0, music_start=10.0)
        assert "atrim=start=10.000" in cmd

    def test_ffmpeg_maps_video_copy_and_aac(self, tmp_path):
        cmd = self._run(tmp_path, video_dur=30.0, song_dur=200.0, music_start=0.0)
        assert "-map" in cmd
        assert "0:v" in cmd
        assert "-c:v" in cmd and "copy" in cmd
        assert "-c:a" in cmd and "aac" in cmd
        assert "+faststart" in cmd


# ── add_mixed_audio ────────────────────────────────────────────────────────────

class TestAddMixedAudio:
    def _run(self, tmp_path, video_dur, song_dur, music_start):
        clip = tmp_path / "clip.mp4"
        audio = tmp_path / "a.m4a"
        output = tmp_path / "out.mp4"
        clip.write_bytes(b"")
        audio.write_bytes(b"")

        captured_cmd = []
        def fake_run(cmd, **kw):
            captured_cmd.extend(cmd)

        with patch("extract_lifts.get_clip_duration", side_effect=[video_dur, song_dur]), \
             patch("subprocess.run", side_effect=fake_run):
            add_mixed_audio(clip, audio, output, music_start=music_start)

        return " ".join(captured_cmd)

    def test_start_clamped_when_too_close_to_end(self, tmp_path):
        cmd = self._run(tmp_path, video_dur=60.0, song_dur=100.0, music_start=90.0)
        assert "atrim=start=40.000" in cmd

    def test_filter_complex_contains_amix(self, tmp_path):
        cmd = self._run(tmp_path, video_dur=30.0, song_dur=200.0, music_start=0.0)
        assert "amix=inputs=2" in cmd
