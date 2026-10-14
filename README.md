# Codex Media MCP

A local MCP server that gives regular Codex video and audio analysis through
OpenRouter. The default model is `qwen/qwen3.8-omni-flash`. No Codex fork is needed.

Useful for reviewing rendered videos, investigating recorded bugs, describing
music, checking narration, and asking focused questions about a particular clip.
The server also works with other MCP clients that support Streamable HTTP.

`inspect_media(path)` reads duration, streams, dimensions and frame rate locally.
It uploads nothing and does not need a key.

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

The following registers this endpoint and sets a 960-second tool timeout and a
larger analysis output budget while preserving unrelated configuration:

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

Video is normalized to H.264/AAC, capped at a 1920px longest edge and 30fps, CRF23.
Higher-resolution and higher-FPS inputs are accepted; these caps apply to the
temporary upload, leaving the original untouched. Reducing resolution/frame rate
reduces upload size; token savings depend on the selected model/provider.
Audio-only input is encoded as 128kbps MP3. The first audio stream is used.
These settings can lose fine text, rapid events, alternate audio tracks or spatial
audio detail; every result reports preprocessing. Raise the video settings or
select shorter ranges for precise visual investigation. Provider-side sampling
can miss details even with a detailed prompt. Clips use the normal file timeline;
unusual source timestamps and edit lists may require normalization first.

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
symlink mapping and an official MCP SDK client handshake. Host tests skip the
FFmpeg integration cases when FFmpeg is absent.

Test the running Docker endpoint without making paid API calls:

```bash
uv run python scripts/smoke_test.py --path /absolute/path/demo.mp4
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
- [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd)

## License

MIT. See [LICENSE](LICENSE).
