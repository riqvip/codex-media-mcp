import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    model: str = "google/gemini-3.5-flash-lite"
    audio_model: str = ""
    provider: str = ""
    reasoning_effort: str = "default"
    reasoning_max_tokens: int | None = None
    host_fs_root: Path = Path("/")
    segment_seconds: float = 300
    max_segments: int = 12
    max_upload_bytes: int = 32 * 1024 * 1024
    max_source_bytes: int = 8 * 1024**3
    max_output_tokens: int = 8000
    provider_timeout: float = 240
    call_timeout: float = 900
    ffmpeg_timeout: float = 180
    inspect_timeout: float = 900
    inspect_max_intervals: int = 100
    video_max_edge: int = 1920
    video_max_fps: int = 2
    video_crf: int = 23
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    @classmethod
    def from_env(cls) -> "Settings":
        from math import isfinite

        def number(name: str, default: float) -> float:
            value = float(os.getenv(name, str(default)))
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
            return value

        def integer(name: str, default: int) -> int:
            value = int(os.getenv(name, str(default)))
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return value

        crf = int(os.getenv("MEDIA_VIDEO_CRF", "23"))
        if not 0 <= crf <= 51:
            raise ValueError("MEDIA_VIDEO_CRF must be between 0 and 51")
        edge = integer("MEDIA_VIDEO_MAX_EDGE", 1920)
        if edge < 2:
            raise ValueError("MEDIA_VIDEO_MAX_EDGE must be at least 2")
        reasoning = os.getenv("MEDIA_REASONING_EFFORT", "default").strip().lower()
        if reasoning not in {"default", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(
                "MEDIA_REASONING_EFFORT must be default, none, minimal, low, "
                "medium, high, xhigh or max"
            )
        reasoning_budget = (
            integer("MEDIA_REASONING_MAX_TOKENS", 1)
            if os.getenv("MEDIA_REASONING_MAX_TOKENS", "").strip()
            else None
        )
        if reasoning_budget is not None and reasoning != "default":
            raise ValueError(
                "Use MEDIA_REASONING_EFFORT=default when setting MEDIA_REASONING_MAX_TOKENS"
            )
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            model=os.getenv("OPENROUTER_MODEL", cls.model).strip() or cls.model,
            audio_model=os.getenv("OPENROUTER_AUDIO_MODEL", "").strip(),
            provider=os.getenv("OPENROUTER_PROVIDER", "").strip(),
            reasoning_effort=reasoning,
            reasoning_max_tokens=reasoning_budget,
            host_fs_root=Path(os.getenv("MEDIA_HOST_FS_ROOT", "/")),
            segment_seconds=number("MEDIA_SEGMENT_SECONDS", 300),
            max_segments=integer("MEDIA_MAX_SEGMENTS", 12),
            max_upload_bytes=int(number("MEDIA_MAX_UPLOAD_MB", 32) * 1024**2),
            max_source_bytes=int(number("MEDIA_MAX_SOURCE_GB", 8) * 1024**3),
            max_output_tokens=integer("MEDIA_MAX_OUTPUT_TOKENS", 8000),
            provider_timeout=number("MEDIA_PROVIDER_TIMEOUT_SECONDS", 240),
            call_timeout=number("MEDIA_CALL_TIMEOUT_SECONDS", 900),
            ffmpeg_timeout=number("MEDIA_FFMPEG_TIMEOUT_SECONDS", 180),
            inspect_timeout=number("MEDIA_INSPECT_TIMEOUT_SECONDS", 900),
            inspect_max_intervals=integer("MEDIA_INSPECT_MAX_INTERVALS", 100),
            video_max_edge=edge,
            video_max_fps=integer("MEDIA_VIDEO_MAX_FPS", 2),
            video_crf=crf,
            ffmpeg=os.getenv("MEDIA_FFMPEG_BIN", "ffmpeg"),
            ffprobe=os.getenv("MEDIA_FFPROBE_BIN", "ffprobe"),
        )
