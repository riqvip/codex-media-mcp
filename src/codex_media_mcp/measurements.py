"""Streaming local FFmpeg measurements; no media upload or retained decoded media."""

import asyncio
import contextlib
import math
import re
from collections.abc import Callable
from functools import partial
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .media import MediaError, MediaInfo, finite_number, primary_stream, stream_timing
from .settings import Settings

InspectionCheck = Literal["silence", "black_frames", "loudness", "audio_peaks", "freeze_frames"]
CHECKS = {"silence", "black_frames", "loudness", "audio_peaks", "freeze_frames"}
METADATA_KEYS = {
    "lavfi.black_start",
    "lavfi.black_end",
    "lavfi.freezedetect.freeze_start",
    "lavfi.freezedetect.freeze_end",
    "lavfi.silence_start",
    "lavfi.silence_end",
    "lavfi.astats.Overall.Peak_level",
    "lavfi.astats.Overall.Number_of_samples",
    "lavfi.r128.I",
    "lavfi.r128.LRA",
    "lavfi.r128.true_peak",
}


class InspectionThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    silence_dbfs: float = Field(default=-40, ge=-120, le=0)
    min_silence_seconds: float = Field(default=0.5, ge=0.01)
    black_pixel_threshold: float = Field(default=0.1, ge=0, le=1)
    black_frame_ratio: float = Field(default=0.98, gt=0, le=1)
    min_black_seconds: float = Field(default=0.5, ge=0.01)
    peak_dbfs: float = Field(default=-1, ge=-120, le=0)
    peak_window_seconds: float = Field(default=0.1, ge=0.01, le=1)
    freeze_noise_db: float = Field(default=-60, ge=-120, le=0)
    min_freeze_seconds: float = Field(default=2, ge=0.01)


class Intervals:
    def __init__(self, start: float, end: float, minimum: float, limit: int):
        self.start, self.end, self.minimum, self.limit = start, end, minimum, limit
        self.open_start: float | None = None
        self.items: list[dict] = []
        self.total_count = 0
        self.total_seconds = 0.0

    def begin(self, value: float) -> None:
        if self.open_start is None:
            self.open_start = max(self.start, value)

    def finish(self, value: float, at_boundary: bool = False, **extra) -> None:
        if self.open_start is None:
            return
        begin, end = self.open_start, min(value, self.end)
        self.open_start = None
        if end - begin + 1e-6 < self.minimum:
            return
        self.total_count += 1
        self.total_seconds += end - begin
        if len(self.items) < self.limit:
            self.items.append(
                {
                    "start_seconds": round(begin, 6),
                    "end_seconds": round(end, 6),
                    "duration_seconds": round(end - begin, 6),
                    "ends_at_scan_boundary": at_boundary,
                    **extra,
                }
            )

    def result(self) -> dict:
        return {
            "status": "complete",
            "intervals": self.items,
            "total_intervals": self.total_count,
            "total_duration_seconds": round(self.total_seconds, 6),
            "intervals_truncated": self.total_count > len(self.items),
        }


async def scan_metadata(args: list[str], consume: Callable[[float, dict[str, str]], None]) -> None:
    """Stream numeric filter metadata. Drain stderr without returning file metadata/content."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise MediaError(
            "FFmpeg is not installed. Use the Docker image or install FFmpeg."
        ) from exc

    async def discard_errors():
        while await process.stderr.read(65536):
            pass

    drain = asyncio.create_task(discard_errors())
    timestamp = None
    values: dict[str, str] = {}
    try:
        while line := await process.stdout.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("frame:"):
                if timestamp is not None:
                    consume(timestamp, values)
                match = re.search(r"\bpts_time:(\S+)", text)
                timestamp = finite_number(match.group(1)) if match else None
                values = {}
            elif text.startswith("lavfi.") and "=" in text:
                key, value = text.split("=", 1)
                if key in METADATA_KEYS:
                    values[key] = value
        if timestamp is not None:
            consume(timestamp, values)
        await process.wait()
        await drain
        if process.returncode:
            raise MediaError(
                "FFmpeg could not finish the requested measurements. Check the media format."
            )
    except ValueError as exc:
        raise MediaError("FFmpeg returned malformed measurement metadata.") from exc
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        await drain


def command(info: MediaInfo, start: float, end: float, settings: Settings, kind: str) -> list[str]:
    return [
        settings.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-copyts",
        "-start_at_zero",
        "-protocol_whitelist",
        "file,pipe",
        "-ss",
        str(start),
        "-t",
        str(end - start),
        "-i",
        str(info.local_path),
        "-map",
        "0:V:0" if kind == "video" else "0:a:0",
    ]


async def scan_video(info, start, end, checks, thresholds, settings) -> dict:
    filters = [f"trim=start={start}:end={end}"]
    events = {}
    if "black_frames" in checks:
        filters.append(
            f"blackdetect=d={thresholds.min_black_seconds}:"
            f"pix_th={thresholds.black_pixel_threshold}:pic_th={thresholds.black_frame_ratio}"
        )
        events["black_frames"] = Intervals(
            start, end, thresholds.min_black_seconds, settings.inspect_max_intervals
        )
    if "freeze_frames" in checks:
        filters.append(
            f"freezedetect=n={thresholds.freeze_noise_db}dB:d={thresholds.min_freeze_seconds}"
        )
        events["freeze_frames"] = Intervals(
            start, end, thresholds.min_freeze_seconds, settings.inspect_max_intervals
        )
    filters += [
        "metadata=mode=add:key=lavfi.media_scan:value=1",
        "metadata=mode=print:file=/dev/stdout:direct=1",
    ]
    last_pts, previous_pts = None, None
    frames = 0

    def consume(pts, values):
        nonlocal last_pts, previous_pts, frames
        frames += 1
        previous_pts, last_pts = last_pts, pts
        for check, prefix in [
            ("black_frames", "lavfi.black_"),
            ("freeze_frames", "lavfi.freezedetect.freeze_"),
        ]:
            if check not in events:
                continue
            begin = finite_number(values.get(prefix + "start"))
            finish = finite_number(values.get(prefix + "end"))
            if begin is not None:
                events[check].begin(begin)
            if finish is not None:
                events[check].finish(finish)

    args = command(info, start, end, settings, "video") + [
        "-vf",
        ",".join(filters),
        "-an",
        "-fps_mode",
        "passthrough",
        "-f",
        "null",
        "-",
    ]
    await scan_metadata(args, consume)
    stream_end = stream_timing(info)["video_end_seconds"]
    last_frame_duration = (
        last_pts - previous_pts
        if last_pts is not None and previous_pts is not None
        else 1 / info.fps
        if info.fps and info.fps > 0
        else 0
    )
    decoded_end = (last_pts or start) + max(0, last_frame_duration)
    boundary = min(end, stream_end if stream_end is not None else decoded_end)
    for event in events.values():
        event.finish(boundary, at_boundary=True)
    return {
        check: {
            **event.result(),
            "status": "complete" if frames else "unavailable",
            "frames_scanned": frames,
            "boundary_end_estimated": stream_end is None,
            "note": "Source frames, original-file timestamps; qualifying static/dark scenes "
            "can be intentional. Boundary-ending intervals may continue outside the scan.",
        }
        for check, event in events.items()
    }


async def scan_audio(info, start, end, checks, thresholds, settings) -> dict:
    sample_rate = primary_stream(info, "audio").get("sample_rate")
    if not sample_rate:
        raise MediaError("The audio stream has no usable sample rate for local measurements.")
    samples_per_window = max(1, math.ceil(sample_rate * thresholds.peak_window_seconds))
    timestamp_precision = max(
        1 / sample_rate,
        primary_stream(info, "audio").get("time_base_seconds") or 0,
    )
    filters = [f"atrim=start={start}:end={end}", f"asetnsamples=n={samples_per_window}:p=0"]
    if "silence" in checks:
        filters.append(
            f"silencedetect=n={thresholds.silence_dbfs}dB:d={thresholds.min_silence_seconds}"
        )
    filters += [
        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall="
        + ("Peak_level+" if "audio_peaks" in checks else "")
        + "Number_of_samples",
        "ametadata=mode=print:file=/dev/stdout:direct=1",
    ]
    silence = Intervals(start, end, thresholds.min_silence_seconds, settings.inspect_max_intervals)
    peaks = Intervals(start, end, 0.000001, settings.inspect_max_intervals)
    audio_end = start
    peak_max = None
    interval_peak = None
    sample_count = 0
    peak_windows = 0

    def consume(pts, values):
        nonlocal audio_end, peak_max, interval_peak, sample_count, peak_windows
        if peaks.open_start is not None and pts > audio_end + timestamp_precision + 1e-6:
            peaks.finish(audio_end, max_sample_peak_dbfs=interval_peak)
            interval_peak = None
        count = finite_number(values.get("lavfi.astats.Overall.Number_of_samples")) or 0
        audio_end = min(end, pts + count / sample_rate)
        sample_count += int(count)
        begin = finite_number(values.get("lavfi.silence_start"))
        finish = finite_number(values.get("lavfi.silence_end"))
        if begin is not None:
            silence.begin(begin)
        if finish is not None:
            silence.finish(finish)
        peak = finite_number(values.get("lavfi.astats.Overall.Peak_level"))
        if peak is not None:
            peak_max = max(peak_max, peak) if peak_max is not None else peak
        if "audio_peaks" in checks:
            if peak is not None and peak >= thresholds.peak_dbfs:
                # Adjacent threshold-crossing windows form one interval.
                peaks.begin(pts)
                interval_peak = max(interval_peak, peak) if interval_peak is not None else peak
                peak_windows += 1
            elif peaks.open_start is not None:
                peaks.finish(pts, max_sample_peak_dbfs=interval_peak)
                interval_peak = None

    await scan_metadata(
        command(info, start, end, settings, "audio")
        + [
            "-af",
            ",".join(filters),
            "-vn",
            "-f",
            "null",
            "-",
        ],
        consume,
    )
    silence.finish(audio_end, at_boundary=True)
    peaks.finish(audio_end, at_boundary=True, max_sample_peak_dbfs=interval_peak)
    result = {}
    if "silence" in checks:
        result["silence"] = {
            **silence.result(),
            "status": "complete" if sample_count else "unavailable",
            "samples_scanned_per_channel": sample_count,
            "note": "All channels must be quiet. Boundary silence may extend outside the scan.",
        }
    if "audio_peaks" in checks:
        result["audio_peaks"] = {
            **peaks.result(),
            "status": "complete" if sample_count else "unavailable",
            "max_sample_peak_dbfs": peak_max,
            "sample_rate": sample_rate,
            "samples_scanned_per_channel": sample_count,
            "window_seconds": samples_per_window / sample_rate,
            "threshold_crossing_windows": peak_windows,
            "note": "Intervals locate windows containing samples at/above the threshold; "
            "they do not prove clipping. Null means no finite sample-peak measurement "
            "(for example digital silence).",
        }
    return result


async def scan_loudness(info, start, end, settings) -> dict:
    latest = {}
    frames = 0

    def consume(_pts, values):
        nonlocal frames
        frames += 1
        latest.update(values)

    await scan_metadata(
        command(info, start, end, settings, "audio")
        + [
            "-af",
            f"atrim=start={start}:end={end},ebur128=metadata=1:peak=true,"
            "ametadata=mode=print:file=/dev/stdout:direct=1",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        consume,
    )
    integrated = finite_number(latest.get("lavfi.r128.I"))
    amplitude = finite_number(latest.get("lavfi.r128.true_peak"))
    return {
        "status": "complete" if frames else "unavailable",
        "integrated_lufs": integrated if integrated is not None and integrated > -70 else None,
        "loudness_range_lu": finite_number(latest.get("lavfi.r128.LRA")) if frames else None,
        "true_peak_dbtp": 20 * math.log10(amplitude) if amplitude and amplitude > 0 else None,
        "measurement_available": integrated is not None and integrated > -70,
        "note": "EBU R128 on selected audio only; short/quiet ranges may have no gated loudness "
        "measurement. Null true peak can mean silence, report precision or too few samples. "
        "This is not a sample-peak or perceptual-quality judgment.",
    }


async def measure(info, start, end, checks, thresholds, settings) -> dict:
    results = {}
    video_checks = set(checks) & {"black_frames", "freeze_frames"}
    audio_checks = set(checks) & {"silence", "audio_peaks"}
    groups = []
    if video_checks and info.has_video:
        groups.append(
            (
                video_checks,
                partial(scan_video, info, start, end, video_checks, thresholds, settings),
            )
        )
    if audio_checks and info.has_audio:
        groups.append(
            (
                audio_checks,
                partial(scan_audio, info, start, end, audio_checks, thresholds, settings),
            )
        )
    if "loudness" in checks and info.has_audio:
        groups.append(({"loudness"}, partial(scan_loudness, info, start, end, settings)))
    for check in checks:
        absent = (check in {"black_frames", "freeze_frames"} and not info.has_video) or (
            check in {"silence", "audio_peaks", "loudness"} and not info.has_audio
        )
        if absent:
            results[check] = {"status": "unavailable", "reason": "Required stream is absent."}
    for group_checks, run in groups:
        try:
            group_result = await run()
            results.update(
                {"loudness": group_result} if group_checks == {"loudness"} else group_result
            )
        except MediaError as exc:
            results.update(
                {check: {"status": "error", "error": str(exc)} for check in group_checks}
            )
    return results
