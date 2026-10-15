# Codex Media MCP

A local MCP server that gives regular Codex video and audio analysis through
OpenRouter. The default model is `google/gemini-3.5-flash-lite`. No Codex fork is needed.

Useful for reviewing rendered videos, investigating recorded bugs, describing
music, checking narration, and asking focused questions about a particular clip.
The server also works with other MCP clients that support Streamable HTTP.

`inspect_media(path, checks?, time_range?, thresholds?)` reads duration, codecs,
stream timing, audio channels/sample rate, dimensions and frame rate locally.
Optional checks measure silence, black/frozen frames, loudness and audio peaks.
It uploads nothing and does not need a key. Omit checks for quick metadata only.

`analyze_media(path, question?, time_range?)` returns detailed chronological
observations, speech, sounds, readable text and uncertainty. Without a question,
it summarizes all supplied media. With a question, it focuses on that question.
Its MCP instructions tell Codex to use the analysis as media evidence, retain
uncertainty, and request focused follow-up analysis when useful.

```json
{
  "path": "/home/example/projects/demo/recording.mp4",
  "question": "What happens immediately before the interface freezes?",
  "time_range": {"start_seconds": 120, "end_seconds": 165}
}
```

The server **physically extracts only 02:00–02:45** before uploading. It preserves
audio in video clips, accurately seeks by re-encoding instead of copying from an
earlier keyframe, and supplies the original-file timestamp offset to the model.
Reported timestamps are approximate model observations, not measured event times.

## Local measurements with inspect_media

```json
{
  "path": "/home/example/projects/demo/render.mp4",
  "checks": ["silence", "black_frames", "loudness", "audio_peaks", "freeze_frames"],
  "time_range": {"start_seconds": 120, "end_seconds": 165},
  "thresholds": {
    "silence_dbfs": -40,
    "min_silence_seconds": 0.5,
    "peak_dbfs": -1,
    "min_freeze_seconds": 2
  }
}
```

All parameters except `path` are optional. Omit `time_range` to scan the whole
file. Measurements use the original decoded video/audio, with **no resolution or
FPS cap**, no uploaded clips, no OpenRouter requests and no API key requirement.

| Check | Result | Default threshold |
| --- | --- | --- |
| `silence` | Quiet intervals across all audio channels | -40 dBFS, at least 0.5 seconds |
| `black_frames` | Dark intervals meeting the configured pixel ratio | At least 98% black pixels, 0.1 pixel threshold, at least 0.5 seconds |
| `freeze_frames` | Intervals with little frame-to-frame change | -60 dB difference tolerance, at least 2 seconds |
| `audio_peaks` | Sample peak and merged threshold-crossing windows | -1 dBFS threshold, 0.1-second windows |
| `loudness` | EBU R128 integrated LUFS, loudness range and true peak | Measures the selected audio; no target normalization |

Use `thresholds` to override defaults. Additional fields are `min_black_seconds`,
`black_pixel_threshold`, `black_frame_ratio`, `peak_window_seconds` and
`freeze_noise_db`. Thresholds and intervals are returned with the measurements.

Interval timestamps refer to original-file playback, with time zero at the input's
playback start rather than an embedded recording timecode. Ranged scans retain
that timeline. An `ends_at_scan_boundary` flag means an event was still ongoing
at the scan/stream end and may continue outside the selected range. Video timing
has frame precision; EOF boundaries can be estimated when stream duration is
unknown (`boundary_end_estimated`). Audio peak intervals locate windows containing
high samples, rather than exact individual sample timestamps. Container timestamp
precision is respected when grouping adjacent windows.

Each requested measurement has a `status` of `complete`, `unavailable` or `error`.
Absent streams and ranges with no usable stream data are unavailable, not zero
measurements. A failed check is explicit and other completed results remain.
`inspection_complete` is false on scan errors. An overall scan timeout returns an
error and terminates FFmpeg. Metadata-only inspection can run while a scan or
OpenRouter analysis is in progress; heavy operations run one at a time.

Silence, dark scenes and static frames can be intentional. High sample peaks
alone **do not prove clipping**. Short/very quiet audio can have no gated LUFS
measurement; these values are null with an explicit availability flag. Audio and
video end-time differences in `stream_timing` are metadata measurements, not
lip-sync detection. The first primary audio/video streams are scanned.

Output retains at most 100 intervals per check by default. `total_intervals`,
`total_duration_seconds` and `intervals_truncated` report the full scan even when
the returned interval list is bounded. Select a narrower range to examine more
detail. Tune `MEDIA_INSPECT_MAX_INTERVALS` or `MEDIA_INSPECT_TIMEOUT_SECONDS`
(default 900 seconds) in the server environment when needed.

## Start with Docker

You need Linux (including WSL2), Docker with Compose, a Codex CLI installation,
and an OpenRouter API key. Only the server needs the key; inspection is free of
provider requests, and analysis uses your OpenRouter credit.

Clone the repository and run from its directory:

```bash
git clone https://github.com/riqvip/codex-media-mcp.git
cd codex-media-mcp
cp .env.example .env       # Only for a new setup; preserve an existing .env.
chmod 600 .env
```

Set `OPENROUTER_API_KEY` in `.env`. Change `OPENROUTER_MODEL` if desired; the
selected model/provider must support the media input modality. A separate
`OPENROUTER_AUDIO_MODEL` is optional. There is no automatic model fallback.
`OPENROUTER_PROVIDER` optionally pins an OpenRouter provider/endpoint slug and
disables provider fallback. Leave it empty to allow OpenRouter routing. Model
and provider names and the OpenRouter request ID are reported with each analysis
when the provider supplies them, making latency and billing easier to trace.

`MEDIA_REASONING_EFFORT` controls thinking on compatible OpenRouter models:
`default` leaves the provider policy unchanged; `none` disables thinking; `low`,
`minimal`, `medium`, `high`, `xhigh`, and `max` request an effort level. Support
varies by model/provider. Lower effort may reduce latency but can affect analysis
quality; compare the same clip before changing your default. See
[OpenRouter reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
Alternatively, set a positive `MEDIA_REASONING_MAX_TOKENS` to request a thinking
token budget on compatible models. Leave `MEDIA_REASONING_EFFORT=default` when
using a token budget; the two controls are mutually exclusive. If neither is set,
the server leaves the provider's thinking policy unchanged. Requested settings
are reported per segment; check actual usage to see whether the provider honored
them. Recreate the container after changing environment settings:
`docker compose up -d --force-recreate --wait`.

Analysis returns per-segment `timings.preprocess_seconds` (local FFmpeg encoding
and validation) and `timings.provider_seconds` (request preparation, upload,
provider wait, generation and response handling), plus top-level metadata and
total timings. Provider time is not a measure of model computation alone.
When returned by the provider, usage includes `reasoning_tokens` and derived
`visible_completion_tokens`; absent values remain absent, rather than implying
zero thinking. Reasoning text is not returned by this tool.
Provider-reported cached, video and audio input token counts are included when
available. Provider accounting may omit or inconsistently report media tokens;
these fields do not independently establish what the model perceived.

```bash
docker compose up -d --build
docker compose ps
```

The endpoint is `http://127.0.0.1:8766/mcp`. The host port is configurable with
`MEDIA_MCP_PORT`; the container always listens on 8765. The health endpoint is
`/health` and checks server availability, not the API key or provider.

The container reads the Linux filesystem through the read-only `/:/host` mount.
Pass **original absolute Linux paths**, not `/host/...` paths. Absolute and relative
symlinks are resolved in the host filesystem namespace. There is no directory
allowlist or AGENTS.md path policy in this server. Codex must follow the user's
filesystem instructions when choosing paths. Standard Linux permissions still
apply: the container runs as UID/GID 1000 by default. Set `MEDIA_UID`/`MEDIA_GID`
in `.env` if the WSL user has different IDs. Symlinks into unavailable mount
namespaces cannot make inaccessible files readable.

The root mount is intentionally broad; the tool is limited to processing regular,
self-contained media files. It does not accept URLs or playlists, execute media
instructions, or modify source files. The OpenRouter key stays in server
configuration. Temporary clips are removed after success, failure or cancellation.

## Connect Codex

The following registers this endpoint and sets a 960-second tool timeout and
larger output budgets for both tools while preserving unrelated configuration:

```bash
python3 scripts/register_codex.py
```

The helper requires Python 3.11+ and the `codex` command. It uses your current
`CODEX_HOME`, or `~/.codex` by default. To connect additional profiles, pass their
Codex home directories explicitly. For example, if you have a `codexpro` wrapper
using `~/.codex-pro`:

```bash
python3 scripts/register_codex.py --home ~/.codex --home ~/.codex-pro
```

For a different host port, pass `--port` with the same value as `MEDIA_MCP_PORT`.
Other MCP clients can connect directly to the HTTP endpoint.

Alternatively, register manually and add `tool_timeout_sec = 960` to the
`[mcp_servers.media]` table:

```bash
codex mcp add media --url http://127.0.0.1:8766/mcp
```

Start a **new Codex session** and run `/mcp` to check the connection. Then ask:

> Analyze this video: `/absolute/path/demo.mp4`. Focus on 120–165 seconds and
> explain the visible error.

The server exposes these tools to MCP clients; running this server does not
retroactively add tools to an already-running Codex session.

## WSL startup

Once this Compose project is created, `restart: unless-stopped` starts its
container when the Linux Docker daemon starts. If Docker is not already enabled:

```bash
sudo systemctl enable --now docker.service
```

WSL must run systemd for this service setup. Check with `ps -p 1 -o comm=`.
If it does not, enable systemd in `/etc/wsl.conf` following the linked Microsoft
instructions and restart the distribution. This starts the service when WSL
starts; it does not start WSL when Windows boots. Deliberately stopping the
container keeps it stopped until `docker compose up -d` starts it again.

## Limits and media quality

Defaults: 300-second segments, at most 12 segments per call, 32 MiB per uploaded
clip, 8 GiB source files, 8,000 output tokens per segment, 240 seconds per provider
request and 900 seconds per analysis call. Each segment is uploaded separately.
Responses preserve all segment analyses in chronological order rather than
compressing them into another short model summary. No cache retains source media.

Use `inspect_media` first and select ranges for long files. A range that exceeds
the segment limit fails before any upload. An oversized or incomplete extracted
clip is rejected before upload. A later provider/extraction failure returns earlier
segment results with `coverage_complete: false`; an overall call timeout returns
an error. `output_truncated` and `analysis_complete` report provider token limits.
Codex's own context/output budget can also limit very large results; narrower
follow-up ranges are preferable to repeatedly uploading a long file.

Video is normalized to H.264/AAC, capped at a 1920px longest edge and 2fps, CRF23.
Higher-resolution and higher-FPS inputs are accepted; these caps apply to the
temporary upload, leaving the original untouched. Reducing resolution/frame rate
reduces upload size; token savings depend on the selected model/provider.
`MEDIA_VIDEO_MAX_FPS` is configurable. The default 2 FPS cap reduces local
encoding and upload work while matching Qwen's documented default sampling rate;
it does not guarantee fewer model tokens than uploading a higher-FPS file. At low
FPS, very short clips can have their video duration rounded up by one frame period;
no additional source frames outside the requested range are uploaded, and audio
retains its requested duration. Local `inspect_media` measurements keep the source
frame rate.
Audio-only input is encoded as 128kbps MP3. The first audio stream is used.
These settings can lose fine text, rapid events, alternate audio tracks or spatial
audio detail; every result reports preprocessing. Raise the video settings or
select shorter ranges for precise visual investigation. Provider-side sampling
can miss details even with a detailed prompt. Clips use the normal file timeline;
unusual source timestamps and edit lists may require normalization first.

Encoded frame rate and model sampling rate are different controls. Raising
`MEDIA_VIDEO_MAX_FPS` preserves more frames in the upload; it does not force the
provider to inspect them. Fast, sub-second events and small HUD elements need
targeted verification; a confident model description is not a frame measurement.
The tools currently accept local file paths. YouTube URL support requires a
separate input path and compatible provider routing; a model switch alone does
not enable URLs in these tools.

After changing `.env`, recreate this project so the container receives changes:

```bash
docker compose up -d --force-recreate
```

## Development and verification

Use Python 3.11+ and `uv`. Local execution also needs `ffmpeg` and `ffprobe`:

```bash
uv sync --dev
uv run ruff check src tests scripts
uv run pytest -q
uv run python -m codex_media_mcp.server
```

For local execution load the key through the process environment. `MEDIA_HOST_FS_ROOT`
defaults to `/` outside Docker. `.env` is loaded by Compose, not automatically by
the Python module. To run with the same local `.env` configuration using `uv`:

```bash
uv run --env-file .env python -m codex_media_mcp.server
```

The Docker test stage runs the full suite with real FFmpeg and mocked OpenRouter:

```bash
docker build --target test -t codex-media-mcp-test .
```

Tests verify actual uploaded clip duration and frame colors, audio payloads,
segmentation, partial coverage, size limits, timeout cleanup, credential redaction,
symlink mapping, local measurement intervals and an official MCP SDK client
handshake. Measurement fixtures contain known silence, dark frames, static scenes,
peak bursts and mismatched stream lengths. Host tests skip the
FFmpeg integration cases when FFmpeg is absent.

Test the running Docker endpoint without making paid API calls:

```bash
uv run python scripts/smoke_test.py --path /absolute/path/demo.mp4
```

Exercise local checks through the deployed MCP endpoint, without API requests:

```bash
uv run python scripts/smoke_test.py --path /absolute/path/demo.mp4 \
  --checks silence black_frames loudness audio_peaks freeze_frames \
  --start 120 --end 165 --thresholds '{"min_silence_seconds": 0.25}'
```

To test the complete delivery path with a real paid model call:

```bash
uv run python scripts/smoke_test.py --path /absolute/path/demo.mp4 \
  --analyze --start 120 --end 165 --question 'What happens before the error?'
```

## References

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Official MCP Python SDK, pinned v1 maintenance line](https://py.sdk.modelcontextprotocol.io/v1/)
- [OpenRouter video input](https://openrouter.ai/docs/guides/overview/multimodal/videos)
- [OpenRouter audio input](https://openrouter.ai/docs/guides/overview/multimodal/audio)
- [Qwen3.8 Omni Flash](https://openrouter.ai/qwen/qwen3.8-omni-flash)
- [Gemini 3.5 Flash Lite](https://openrouter.ai/google/gemini-3.5-flash-lite)
- [FFmpeg measurement filters](https://ffmpeg.org/ffmpeg-filters.html)
- [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd)

## License

MIT. See [LICENSE](LICENSE).
