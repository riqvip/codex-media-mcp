import asyncio
import json
import math
import os
import shutil
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from codex_media_mcp.measurements import InspectionThresholds, scan_metadata
from codex_media_mcp.media import MediaError, TimeRange
from codex_media_mcp.server import create_server
from codex_media_mcp.service import MediaService
from codex_media_mcp.settings import Settings


@pytest.fixture
def fixture_media(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real measurements run in the Docker test image")
    path = tmp_path / "events.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=if(between(t\\,1\\,2)\\,0\\,if(between(t\\,2.5\\,3)\\,"
            "0.99*sin(2*PI*440*t)\\,0.1*sin(2*PI*440*t))):s=48000:d=4",
            "-filter_complex",
            "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "4:a",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path


async def test_all_measurements_match_known_events(fixture_media):
    service = MediaService(Settings())  # Deliberately no OpenRouter key.
    result = await service.inspect_media(
        str(fixture_media),
        ["silence", "black_frames", "audio_peaks", "loudness", "freeze_frames"],
        thresholds=InspectionThresholds(min_freeze_seconds=0.5),
    )
    assert result["inspection_complete"]
    measurements = result["measurements"]
    black = measurements["black_frames"]["intervals"]
    assert len(black) == 1
    assert black[0]["start_seconds"] == pytest.approx(1, abs=0.01)
    assert black[0]["end_seconds"] == pytest.approx(2, abs=0.01)
    assert not black[0]["ends_at_scan_boundary"]
    silence = measurements["silence"]["intervals"]
    assert len(silence) == 1
    assert silence[0]["start_seconds"] == pytest.approx(1, abs=0.01)
    assert silence[0]["end_seconds"] == pytest.approx(2, abs=0.01)
    peaks = measurements["audio_peaks"]
    assert peaks["max_sample_peak_dbfs"] == pytest.approx(20 * math.log10(0.99), abs=0.01)
    assert peaks["samples_scanned_per_channel"] == 192000
    assert len(peaks["intervals"]) == 1
    assert peaks["intervals"][0]["start_seconds"] == pytest.approx(2.5, abs=0.01)
    assert peaks["intervals"][0]["end_seconds"] == pytest.approx(3, abs=0.01)
    freezes = measurements["freeze_frames"]["intervals"]
    assert freezes[0]["start_seconds"] == 0
    assert freezes[0]["end_seconds"] == pytest.approx(1, abs=0.01)
    assert freezes[-1]["start_seconds"] == pytest.approx(3, abs=0.01)
    assert freezes[-1]["end_seconds"] == pytest.approx(4, abs=0.01)
    assert freezes[-1]["ends_at_scan_boundary"]
    assert measurements["black_frames"]["frames_scanned"] == 40
    loudness = measurements["loudness"]
    assert loudness["measurement_available"]
    assert -30 < loudness["integrated_lufs"] < 0
    assert loudness["true_peak_dbtp"] == pytest.approx(20 * math.log10(0.99), abs=0.1)
    json.dumps(result, allow_nan=False)


async def test_range_offsets_and_trailing_intervals(fixture_media):
    result = await MediaService(Settings()).inspect_media(
        str(fixture_media),
        ["silence", "black_frames"],
        TimeRange(start_seconds=1.25, end_seconds=1.8),
        InspectionThresholds(min_silence_seconds=0.2, min_black_seconds=0.2),
    )
    for name in ["silence", "black_frames"]:
        intervals = result["measurements"][name]["intervals"]
        assert len(intervals) == 1
        assert 1.25 <= intervals[0]["start_seconds"] <= 1.3
        assert intervals[0]["end_seconds"] == pytest.approx(1.8, abs=0.001)
        assert intervals[0]["ends_at_scan_boundary"]
    excluded = await MediaService(Settings()).inspect_media(
        str(fixture_media),
        ["silence", "black_frames", "audio_peaks"],
        TimeRange(start_seconds=3.1, end_seconds=3.9),
    )
    assert all(not item["intervals"] for item in excluded["measurements"].values())


async def test_thresholds_and_bounded_interval_output(fixture_media):
    result = await MediaService(Settings(inspect_max_intervals=1)).inspect_media(
        str(fixture_media),
        ["audio_peaks", "black_frames", "freeze_frames"],
        thresholds=InspectionThresholds(
            peak_dbfs=-0.01, min_black_seconds=2, min_freeze_seconds=0.5
        ),
    )
    assert result["measurements"]["audio_peaks"]["intervals"] == []
    assert result["measurements"]["black_frames"]["intervals"] == []
    freezes = result["measurements"]["freeze_frames"]
    assert len(freezes["intervals"]) == 1
    assert freezes["total_intervals"] >= 3
    assert freezes["intervals_truncated"]


async def test_metadata_only_never_scans(fixture_media, monkeypatch):
    async def fail(*_args):
        raise AssertionError("Metadata-only inspection must not run measurement scans")

    monkeypatch.setattr("codex_media_mcp.service.measure", fail)
    result = await MediaService(Settings()).inspect_media(str(fixture_media))
    assert "measurements" not in result
    assert result["streams"][0]["codec"] == "ffv1"
    assert result["streams"][1]["sample_rate"] == 48000
    assert result["streams"][1]["channels"] == 1


async def test_audio_only_silence_and_missing_video(fixture_media, tmp_path):
    audio = tmp_path / "silence.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=1",
            str(audio),
        ],
        check=True,
    )
    result = await MediaService(Settings()).inspect_media(
        str(audio),
        ["silence", "black_frames", "loudness", "audio_peaks"],
    )
    assert result["measurements"]["black_frames"]["status"] == "unavailable"
    silence = result["measurements"]["silence"]["intervals"]
    assert len(silence) == 1 and silence[0]["end_seconds"] == pytest.approx(1)
    assert result["measurements"]["audio_peaks"]["max_sample_peak_dbfs"] is None
    assert result["measurements"]["loudness"]["integrated_lufs"] is None
    json.dumps(result, allow_nan=False)


async def test_stereo_silence_requires_all_channels_quiet(fixture_media, tmp_path):
    audio = tmp_path / "one-audible-channel.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.1*sin(2*PI*440*t)|0:s=48000:d=1",
            str(audio),
        ],
        check=True,
    )
    result = await MediaService(Settings()).inspect_media(str(audio), ["silence"])
    assert result["measurements"]["silence"]["intervals"] == []


async def test_stream_end_difference(fixture_media, tmp_path):
    video = tmp_path / "mismatched-streams.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:r=10:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    result = await MediaService(Settings()).inspect_media(str(video))
    assert result["stream_timing"]["audio_minus_video_end_seconds"] == pytest.approx(-1, abs=0.1)


async def test_video_eof_does_not_extend_black_interval_to_audio_end(fixture_media, tmp_path):
    video = tmp_path / "short-video-long-audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    service = MediaService(Settings())
    result = await service.inspect_media(str(video), ["black_frames"])
    interval = result["measurements"]["black_frames"]["intervals"][0]
    assert interval["end_seconds"] == pytest.approx(1, abs=0.01)
    no_video = await service.inspect_media(
        str(video),
        ["black_frames"],
        TimeRange(start_seconds=2, end_seconds=3),
    )
    assert no_video["measurements"]["black_frames"]["status"] == "unavailable"
    assert no_video["measurements"]["black_frames"]["intervals"] == []


async def test_scan_timeout_cleanup_and_busy(fixture_media, monkeypatch):
    async def slow(*_args):
        await asyncio.sleep(10)

    monkeypatch.setattr("codex_media_mcp.service.measure", slow)
    service = MediaService(Settings(inspect_timeout=0.01))
    with pytest.raises(MediaError, match="timed out"):
        await service.inspect_media(str(fixture_media), ["silence"])
    assert not service._lock.locked()
    async with service._lock:
        with pytest.raises(MediaError, match="Another media"):
            await service.inspect_media(str(fixture_media), ["silence"])
        assert "measurements" not in await service.inspect_media(str(fixture_media))


async def test_scan_failure_is_explicit_and_retains_other_checks(fixture_media):
    result = await MediaService(Settings(ffmpeg="/missing-ffmpeg")).inspect_media(
        str(fixture_media),
        ["silence", "loudness"],
    )
    assert not result["inspection_complete"]
    assert all(value["status"] == "error" for value in result["measurements"].values())


async def test_measurements_through_official_mcp_client(fixture_media):
    app = create_server(Settings()).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=client) as (
                r,
                w,
                _,
            ):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "inspect_media",
                        {
                            "path": str(fixture_media),
                            "checks": ["silence", "black_frames"],
                            "time_range": {"start_seconds": 1.25, "end_seconds": 1.8},
                            "thresholds": {"min_silence_seconds": 0.2, "min_black_seconds": 0.2},
                        },
                    )
                    assert not result.isError
                    assert result.structuredContent["inspection_complete"]
                    assert result.structuredContent["measurements"]["silence"]["intervals"]
                    invalid = await session.call_tool(
                        "inspect_media",
                        {
                            "path": str(fixture_media),
                            "checks": ["not_a_check"],
                        },
                    )
                    assert invalid.isError


@pytest.mark.parametrize(
    "overrides",
    [
        {"silence_dbfs": 1},
        {"peak_window_seconds": 0},
        {"black_frame_ratio": 2},
        {"freeze_noise_db": float("nan")},
        {"min_black_seconds": float("inf")},
        {"surprise": 1},
    ],
)
def test_invalid_thresholds(overrides):
    with pytest.raises(ValidationError):
        InspectionThresholds(**overrides)


async def test_cancelling_metadata_scan_kills_subprocess(tmp_path):
    pid_file = tmp_path / "scanner.pid"
    script = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "print('frame:0 pts:0 pts_time:0',flush=True); time.sleep(30)"
    )
    task = asyncio.create_task(scan_metadata([sys.executable, "-c", script], lambda *_args: None))
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
