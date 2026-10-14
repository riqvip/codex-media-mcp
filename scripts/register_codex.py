"""Register this server without replacing unrelated Codex configuration."""

import argparse
import os
import re
import subprocess
import tomllib
from pathlib import Path


def register(home: Path, port: int) -> None:
    config = home / "config.toml"
    original = config.read_text() if config.exists() else ""
    existing = tomllib.loads(original).get("mcp_servers", {}).get("media")
    url = f"http://127.0.0.1:{port}/mcp"
    if existing and existing.get("url") != url:
        raise SystemExit(
            f"An unrelated 'media' MCP server already exists in {home}; choose another name."
        )
    if not existing:
        subprocess.run(
            ["codex", "mcp", "add", "media", "--url", url],
            env={**os.environ, "CODEX_HOME": str(home)},
            check=True,
        )
    content = config.read_text()
    # The Codex CLI writes this table; keep all other config text untouched.
    pattern = re.compile(r"(?m)^\[mcp_servers\.media\]\s*$")
    header = pattern.search(content)
    if not header:
        raise SystemExit(f"Could not locate the media table in {config}")
    next_header = re.search(r"(?m)^\[", content[header.end() :])
    end = header.end() + next_header.start() if next_header else len(content)
    block = content[header.end() : end]
    if re.search(r"(?m)^tool_timeout_sec\s*=", block):
        block = re.sub(r"(?m)^tool_timeout_sec\s*=.*$", "tool_timeout_sec = 960", block)
    else:
        block = block.rstrip() + "\ntool_timeout_sec = 960\n\n"
    modified = content[: header.end()] + block + content[end:]
    # A long whole-file analysis can be larger than Codex's ordinary tool budget.
    tool = tomllib.loads(modified)["mcp_servers"]["media"].get("tools", {}).get("analyze_media")
    if tool is None:
        modified = modified.rstrip() + (
            "\n\n[mcp_servers.media.tools.analyze_media]\noutput_token_limit = 40000\n"
        )
    tomllib.loads(modified)  # Verify TOML before writing.
    if modified != content:
        config.write_text(modified)
    print(f"Registered media MCP for {home.name}: {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--home", type=Path, action="append", help="Codex home; repeat for more profiles"
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    for home in args.home or [Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))]:
        register(home.expanduser(), args.port)


if __name__ == "__main__":
    main()
