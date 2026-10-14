import asyncio
import json
import math
import os
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .settings import Settings


class MediaError(Exception):
    """A safe, actionable error that can be shown to the calling model."""


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start_seconds: float = Field(
        ge=0, description="Start timestamp in the original file, in seconds."
    )
    end_seconds: float = Field(gt=0, description="End timestamp in the original file, in seconds.")

    @model_validator(mode="after")
    def check_order(self) -> "TimeRange":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


def host_path(path: str, root: Path) -> Path:
    """Resolve a host path, anchoring absolute symlinks inside the host mount.

    This only maps namespaces; it is not an access allowlist. In Docker, an
    absolute symlink /a -> /b must resolve to /host/b, not the container's /b.
    """
    if not path.startswith("/") or "\x00" in path:
        raise MediaError("Use an absolute Linux filesystem path, not a URL or relative path.")
    pending = deque(Path(path).parts[1:])
    resolved: list[str] = []
    links = 0
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise MediaError("The media path contains a symlink loop.")
            target = os.readlink(candidate)
            target_parts = Path(target).parts
            if target.startswith("/"):
                resolved.clear()
                target_parts = target_parts[1:]
            pending.extendleft(reversed(target_parts))
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


async def run_process(args: list[str], timeout: float) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise MediaError(
            f"{args[0]} is not installed. Use the Docker image or install FFmpeg."
        ) from exc
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        # FFmpeg can echo file contents, URLs or metadata in stderr. Do not forward it.
        raise MediaError("FFmpeg could not process this media. Check its format and readability.")
    return stdout


@dataclass(frozen=True)
class MediaInfo:
    path: str
    local_path: Path
    duration_seconds: float
    size_bytes: int
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    fps: float | None
    streams: tuple[dict, ...] = ()
    format_name: str | None = None
    format_start_seconds: float = 0

    @property
    def kind(self) -> str:
        return "video" if self.has_video else "audio"

    def public(self) -> dict:
        return {
            "path": self.path,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "format_name": self.format_name,
            "streams": list(self.streams),
            "stream_timing": stream_timing(self),
        }


async def inspect(path: str, settings: Settings) -> MediaInfo:
    local = host_path(path, settings.host_fs_root)
    try:
        if not local.is_file():
            raise MediaError("The path must refer to a readable, regular media file.")
        size = local.stat().st_size
        if size > settings.max_source_bytes:
            raise MediaError(
                "Source exceeds MEDIA_MAX_SOURCE_GB. Raise the limit or use a smaller file."
            )
        with local.open("rb"):
            pass
        output = await run_process(
            [
                settings.ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration,format_name,start_time:"
                "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
                "duration,start_time,sample_rate,channels,channel_layout,pix_fmt,time_base:"
                "stream_disposition=attached_pic",
                "-of",
                "json",
                str(local),
            ],
            30,
        )
    except OSError as exc:
        raise MediaError("The media file is not readable by the server's Linux user.") from exc
    except TimeoutError as exc:
        raise MediaError("Media inspection timed out.") from exc
    try:
        data = json.loads(output)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        # Reject playlist/concat demuxers that can pull unrelated files into an upload.
        formats = set(data["format"].get("format_name", "").split(","))
        if formats & {"hls", "dash", "concat", "sdp", "image2", "image2pipe"}:
            raise MediaError("Use a self-contained audio/video file rather than a playlist.")
        streams = data.get("streams", [])
        video = next(
            (
                s
                for s in streams
                if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")
            ),
            None,
        )
        audio = any(s.get("codec_type") == "audio" for s in streams)
        if not video and not audio:
            raise MediaError("No audio or video stream was found.")
        fps = None
        if video:
            numerator, denominator = video.get("avg_frame_rate", "0/0").split("/")
            if float(denominator):
                fps = float(numerator) / float(denominator)
        return MediaInfo(
            path,
            local,
            duration,
            size,
            bool(video),
            audio,
            video.get("width") if video else None,
            video.get("height") if video else None,
            fps,
            tuple(stream_metadata(s) for s in streams),
            data["format"].get("format_name"),
            finite_number(data["format"].get("start_time")) or 0,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaError(
            "The file has no valid finite duration or readable media metadata."
        ) from exc


def selected_range(info: MediaInfo, requested: TimeRange | None) -> tuple[float, float]:
    start = requested.start_seconds if requested else 0.0
    end = requested.end_seconds if requested else info.duration_seconds
    if start >= info.duration_seconds or end > info.duration_seconds + 0.05:
        raise MediaError(
            f"Range is outside the file's {info.duration_seconds:.3f}-second duration."
        )
    end = min(end, info.duration_seconds)
    return start, end


def segments(
    info: MediaInfo, requested: TimeRange | None, settings: Settings
) -> list[tuple[float, float]]:
    start, end = selected_range(info, requested)
    count = math.ceil((end - start) / settings.segment_seconds)
    if count > settings.max_segments:
        raise MediaError(
            f"This range needs {count} segments; the limit is {settings.max_segments}. "
            "Use time_range to analyze shorter sections, or raise MEDIA_MAX_SEGMENTS."
        )
    return [
        (start + i * settings.segment_seconds, min(end, start + (i + 1) * settings.segment_seconds))
        for i in range(count)
    ]


def finite_number(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def stream_metadata(stream: dict) -> dict:
    result = {
        "index": stream.get("index"),
        "kind": stream.get("codec_type"),
        "codec": stream.get("codec_name"),
        "start_seconds": finite_number(stream.get("start_time")),
        "duration_seconds": finite_number(stream.get("duration")),
        "attached_picture": bool(stream.get("disposition", {}).get("attached_pic")),
    }
    for key in ("width", "height", "channels", "channel_layout", "pix_fmt"):
        if key in stream:
            result[key] = stream[key]
    if "sample_rate" in stream:
        result["sample_rate"] = int(stream["sample_rate"])
    try:
        result["time_base_seconds"] = float(Fraction(stream.get("time_base", "0"))) or None
    except (ValueError, TypeError, ZeroDivisionError):
        result["time_base_seconds"] = None
    return result


def primary_stream(info: MediaInfo, kind: str) -> dict:
    return next((s for s in info.streams if s["kind"] == kind and not s["attached_picture"]), {})


def stream_timing(info: MediaInfo) -> dict:
    video = primary_stream(info, "video")
    audio = primary_stream(info, "audio")

    def end(stream):
        if stream.get("start_seconds") is None or stream.get("duration_seconds") is None:
            return None
        return stream["start_seconds"] - info.format_start_seconds + stream["duration_seconds"]

    video_end, audio_end = end(video), end(audio)
    return {
        "video_end_seconds": video_end,
        "audio_end_seconds": audio_end,
        "audio_minus_video_end_seconds": (
            round(audio_end - video_end, 6)
            if video_end is not None and audio_end is not None
            else None
        ),
        "note": "First audio/video stream end times from metadata, relative to playback start. "
        "Unknown timing is null. An end-time difference does not measure lip sync.",
    }


async def extract_clip(
    info: MediaInfo, start: float, end: float, destination: Path, settings: Settings
) -> None:
    args = [
        settings.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-ss",
        str(start),
        "-i",
        str(info.local_path),
        "-t",
        str(end - start),
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]
    if info.has_video:
        # Encoding after accurate input seeking excludes keyframe preroll from the upload.
        edge = settings.video_max_edge
        scale = (
            f"scale=w='trunc(iw*min(1,{edge}/max(iw,ih))/2)*2':"
            f"h='trunc(ih*min(1,{edge}/max(iw,ih))/2)*2'"
        )
        args += [
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-vf",
            scale,
            "-fpsmax",
            str(settings.video_max_fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(settings.video_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
        ]
    else:
        # MP3 works broadly across audio-input providers; preserve stereo when present.
        args += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"]
    # Stop an oversized encode before it consumes unbounded temporary disk space.
    args += ["-fs", str(settings.max_upload_bytes + 1024 * 1024), str(destination)]
    try:
        await run_process(args, settings.ffmpeg_timeout)
    except TimeoutError as exc:
        raise MediaError("Clip extraction timed out. Select a shorter time_range.") from exc
    if destination.stat().st_size > settings.max_upload_bytes:
        raise MediaError(
            "Encoded clip exceeds MEDIA_MAX_UPLOAD_MB. Select a shorter time_range or "
            "reduce MEDIA_SEGMENT_SECONDS; no oversized clip was uploaded."
        )
    # Never silently upload an incomplete clip (for example after FFmpeg's size stop).
    clip_info = await inspect(
        str(destination),
        Settings(
            host_fs_root=Path("/"),
            ffprobe=settings.ffprobe,
            max_source_bytes=settings.max_upload_bytes,
        ),
    )
    if abs(clip_info.duration_seconds - (end - start)) > 0.25:
        raise MediaError(
            "The extracted clip did not cover the requested range. No clip was uploaded."
        )
