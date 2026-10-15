import asyncio
import base64
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from codex_media_mcp.media import MediaError, TimeRange, host_path, inspect, run_process, segments
from codex_media_mcp.provider import OpenRouter
from codex_media_mcp.service import MediaService
from codex_media_mcp.settings import Settings


@pytest.fixture
def settings():
    return Settings(api_key="test-key", segment_seconds=2, max_segments=10)


@pytest.fixture
def media(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real FFmpeg integration tests run in the Docker test image")
    video = tmp_path / "red-then-blue.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    audio = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=3",
            str(audio),
        ],
        check=True,
    )
    return video, audio


def response(text="Observed blue video", finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "model": "google/gemini-3.5-flash-lite",
            "choices": [{"finish_reason": finish_reason, "message": {"content": text}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost": 0.001,
            },
        },
    )


def test_host_mapping_absolute_and_relative_symlinks(tmp_path):
    (tmp_path / "data").mkdir()
    target = tmp_path / "data" / "clip.mp4"
    target.touch()
    (tmp_path / "absolute.mp4").symlink_to("/data/clip.mp4")
    (tmp_path / "relative.mp4").symlink_to("data/clip.mp4")
    assert host_path("/absolute.mp4", tmp_path) == target
    assert host_path("/relative.mp4", tmp_path) == target
    assert host_path("/data/../data/clip.mp4", tmp_path) == target


def test_host_mapping_has_no_media_directory_allowlist(tmp_path):
    for path in ["/etc/sample.mp4", "/mnt/storage/sample.wav", "/home/example/demo.mp4"]:
        assert host_path(path, tmp_path) == tmp_path / path.lstrip("/")


def test_host_mapping_loop_and_relative_input(tmp_path):
    (tmp_path / "loop").symlink_to("/loop")
    with pytest.raises(MediaError, match="symlink loop"):
        host_path("/loop", tmp_path)
    with pytest.raises(MediaError, match="absolute"):
        host_path("https://example.com/video.mp4", tmp_path)


@pytest.mark.parametrize(
    "start,end", [(2, 1), (1, 1), (-1, 3), (0, float("inf")), (float("nan"), 3)]
)
def test_invalid_ranges(start, end):
    with pytest.raises(ValidationError):
        TimeRange(start_seconds=start, end_seconds=end)


async def test_inspection_and_range_bounds(media, settings):
    info = await inspect(str(media[0]), settings)
    assert info.has_audio and info.has_video
    assert info.width == 64
    assert info.duration_seconds == pytest.approx(4, abs=0.05)
    assert segments(info, None, settings) == [(0, 2), (2, 4)]
    with pytest.raises(MediaError, match="outside"):
        segments(info, TimeRange(start_seconds=3, end_seconds=5), settings)
    with pytest.raises(MediaError, match="limit"):
        segments(info, None, replace(settings, max_segments=1))


async def test_upload_contains_only_requested_range(media, settings, tmp_path):
    uploaded = []

    async def receive(request):
        body = json.loads(request.content)
        assert body["model"] == settings.model
        assert request.headers["Authorization"] == "Bearer test-key"
        parts = body["messages"][1]["content"]
        assert "Question: What color is visible?" in parts[1]["text"]
        assert "Add 2.250 seconds" in parts[1]["text"]
        encoded = parts[0]["video_url"]["url"].split(",", 1)[1]
        clip = tmp_path / "uploaded.mp4"
        clip.write_bytes(base64.b64decode(encoded))
        info = await inspect(str(clip), settings)
        assert info.duration_seconds == pytest.approx(1, abs=0.1)
        assert info.fps <= settings.video_max_fps
        assert info.has_audio
        # The original starts red. EVERY uploaded frame must be blue, proving
        # the request did not merely ask about a range while sending the full file.
        pixels = await run_process(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(clip),
                "-vf",
                "scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            10,
        )
        assert pixels and len(pixels) % 3 == 0
        assert all(pixels[i] < 30 and pixels[i + 2] > 200 for i in range(0, len(pixels), 3))
        uploaded.append(clip.stat().st_size)
        return response()

    service = MediaService(settings, OpenRouter(settings, httpx.MockTransport(receive)))
    result = await service.analyze_media(
        str(media[0]), "What color is visible?", TimeRange(start_seconds=2.25, end_seconds=3.25)
    )
    assert result["coverage_complete"] and result["analysis_complete"]
    assert result["requested_range"] == {"start_seconds": 2.25, "end_seconds": 3.25}
    assert len(uploaded) == 1
    assert result["segments"][0]["uploaded_bytes"] == uploaded[0]
    timings = result["segments"][0]["timings"]
    assert timings["preprocess_seconds"] > 0
    assert timings["provider_seconds"] >= 0
    assert result["timings"]["total_seconds"] >= sum(timings.values()) - 0.01


async def test_two_fps_upload_preserves_short_range_and_audio(media, settings, tmp_path):
    from codex_media_mcp.media import extract_clip, primary_stream

    source = await inspect(str(media[0]), settings)
    assert source.fps == 10
    clip = tmp_path / "short-two-fps.mp4"
    await extract_clip(source, 2.25, 2.35, clip, settings)
    info = await inspect(str(clip), settings)
    assert info.fps <= 2
    assert info.has_audio
    assert primary_stream(info, "audio")["duration_seconds"] == pytest.approx(0.1, abs=0.03)
    # Only a frame from the requested blue portion is encoded, despite the
    # single-frame MP4 timeline being longer than this 100ms source range.
    pixels = await run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(clip),
            "-vf",
            "scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        10,
    )
    assert pixels and all(pixels[i] < 30 and pixels[i + 2] > 200 for i in range(0, len(pixels), 3))


async def test_two_fps_sampling_preserves_scene_timing_and_tail(media, settings, tmp_path):
    from codex_media_mcp.media import extract_clip

    source = await inspect(str(media[0]), settings)
    clip = tmp_path / "scene-timing.mp4"
    await extract_clip(source, 0, 4, clip, settings)
    pixels = await run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(clip),
            "-vf",
            "scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        10,
    )
    # Eight half-second frames: red until source second 2, then blue through EOF.
    assert len(pixels) == 8 * 3
    for index in range(8):
        red, _, blue = pixels[index * 3 : index * 3 + 3]
        if index < 4:
            assert red > 200 and blue < 30
        else:
            assert red < 30 and blue > 200


async def test_default_full_coverage_and_partial_failure(media, settings):
    calls = []

    def receive(request):
        calls.append(json.loads(request.content))
        return response(f"Segment {len(calls)}")

    service = MediaService(settings, OpenRouter(settings, httpx.MockTransport(receive)))
    result = await service.analyze_media(str(media[0]))
    assert result["coverage_complete"]
    assert [(s["source_start_seconds"], s["source_end_seconds"]) for s in result["segments"]] == [
        (0, 2),
        (2, 4),
    ]
    assert all(
        "comprehensive summary" in call["messages"][1]["content"][1]["text"] for call in calls
    )

    count = 0

    def fail_later(_request):
        nonlocal count
        count += 1
        return response() if count == 1 else httpx.Response(429, text="private provider body")

    service = MediaService(settings, OpenRouter(settings, httpx.MockTransport(fail_later)))
    partial = await service.analyze_media(str(media[0]))
    assert not partial["coverage_complete"] and not partial["analysis_complete"]
    assert len(partial["segments"]) == 1
    assert "429" in partial["error"]
    assert "private provider body" not in json.dumps(partial)
    assert partial["timings"]["total_seconds"] > 0


async def test_audio_upload_and_truncation(media, settings):
    settings = replace(settings, audio_model="example/audio", segment_seconds=10)

    def receive(request):
        body = json.loads(request.content)
        assert body["model"] == "example/audio"
        audio = body["messages"][1]["content"][0]
        assert audio["type"] == "input_audio"
        assert audio["input_audio"]["format"] == "mp3"
        assert len(base64.b64decode(audio["input_audio"]["data"])) > 1000
        return response(finish_reason="length")

    service = MediaService(settings, OpenRouter(settings, httpx.MockTransport(receive)))
    result = await service.analyze_media(
        str(media[1]), time_range=TimeRange(start_seconds=0.5, end_seconds=1.5)
    )
    assert result["coverage_complete"] and not result["analysis_complete"]
    assert result["segments"][0]["output_truncated"]


async def test_size_limit_never_uploads(media, settings):
    settings = replace(settings, max_upload_bytes=100)
    calls = []

    def receive(request):
        calls.append(request)
        return response()

    service = MediaService(settings, OpenRouter(settings, httpx.MockTransport(receive)))
    with pytest.raises(MediaError, match="exceeds"):
        await service.analyze_media(str(media[0]))
    assert not calls


async def test_missing_key_before_upload(settings):
    service = MediaService(replace(settings, api_key=""))
    with pytest.raises(MediaError, match="OPENROUTER_API_KEY"):
        await service.analyze_media("/missing/video.mp4")


async def test_call_timeout_cleans_temporary_clips(media, settings, monkeypatch):
    class SlowProvider:
        async def analyze(self, *_args):
            await asyncio.sleep(10)

    settings = replace(settings, call_timeout=0.5)
    service = MediaService(settings, SlowProvider())
    # No temporary clips should survive cancellation, whichever stage times out.
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setattr(tempfile, "tempdir", root)
        with pytest.raises(MediaError, match="call timeout"):
            await service.analyze_media(str(media[0]))
        assert not list(Path(root).iterdir())


async def test_provider_errors_are_redacted(settings, tmp_path):
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"fake audio")
    from codex_media_mcp.media import MediaInfo

    info = MediaInfo(str(clip), clip, 1, 10, False, True, None, None, None)
    for status in [401, 402, 429, 500]:
        client = OpenRouter(
            settings,
            httpx.MockTransport(
                lambda _request, status=status: httpx.Response(
                    status, text="test-key private media"
                )
            ),
        )
        with pytest.raises(MediaError) as exc:
            await client.analyze(info, clip, 0, 1, None)
        assert "test-key" not in str(exc.value)
        assert "private media" not in str(exc.value)


@pytest.mark.parametrize(
    "effort,budget",
    [
        ("default", None),
        ("low", None),
        ("none", None),
        ("default", 1024),
    ],
)
async def test_provider_reasoning_controls_and_numeric_usage(settings, tmp_path, effort, budget):
    from codex_media_mcp.media import MediaInfo

    settings = replace(
        settings, reasoning_effort=effort, reasoning_max_tokens=budget, provider="example/provider"
    )
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"test audio")
    info = MediaInfo(str(clip), clip, 1, 10, False, True, None, None, None)

    def receive(request):
        payload = json.loads(request.content)
        assert payload["provider"] == {"only": ["example/provider"], "allow_fallbacks": False}
        if budget is not None:
            assert payload["reasoning"] == {"max_tokens": budget}
        elif effort == "default":
            assert "reasoning" not in payload
        else:
            assert payload["reasoning"] == {"effort": effort}
        body = json.loads(response().content)
        body["usage"]["completion_tokens_details"] = {
            "reasoning_tokens": 7,
            "private_provider_field": "do not forward",
        }
        body["usage"]["prompt_tokens_details"] = {
            "cached_tokens": 10,
            "video_tokens": 50,
            "audio_tokens": 12,
            "private_provider_field": "do not forward",
        }
        body["id"] = "gen-test"
        body["provider"] = "Example"
        body["choices"][0]["message"]["reasoning"] = "do not forward thinking text"
        return httpx.Response(200, json=body)

    result = await OpenRouter(settings, httpx.MockTransport(receive)).analyze(
        info,
        clip,
        0,
        1,
        None,
    )
    assert result["reasoning_effort"] == effort
    assert result["reasoning_max_tokens"] == budget
    assert result["request_id"] == "gen-test" and result["provider"] == "Example"
    assert result["usage"]["cached_input_tokens"] == 10
    assert result["usage"]["video_input_tokens"] == 50
    assert result["usage"]["audio_input_tokens"] == 12
    assert result["usage"]["reasoning_tokens"] == 7
    assert result["usage"]["visible_completion_tokens"] == 13
    assert "do not forward" not in json.dumps(result)


def test_reasoning_setting_validation(monkeypatch):
    monkeypatch.delenv("MEDIA_REASONING_MAX_TOKENS", raising=False)
    monkeypatch.setenv("MEDIA_REASONING_EFFORT", " LOW ")
    assert Settings.from_env().reasoning_effort == "low"
    monkeypatch.setenv("MEDIA_REASONING_EFFORT", "unsupported")
    with pytest.raises(ValueError, match="MEDIA_REASONING_EFFORT"):
        Settings.from_env()


def test_reasoning_budget_validation(monkeypatch):
    monkeypatch.setenv("MEDIA_REASONING_EFFORT", "default")
    monkeypatch.setenv("MEDIA_REASONING_MAX_TOKENS", "1024")
    assert Settings.from_env().reasoning_max_tokens == 1024
    monkeypatch.setenv("MEDIA_REASONING_EFFORT", "low")
    with pytest.raises(ValueError, match="effort|EFFORT"):
        Settings.from_env()
    monkeypatch.setenv("MEDIA_REASONING_EFFORT", "default")
    monkeypatch.setenv("MEDIA_REASONING_MAX_TOKENS", "0")
    with pytest.raises(ValueError, match="positive"):
        Settings.from_env()
