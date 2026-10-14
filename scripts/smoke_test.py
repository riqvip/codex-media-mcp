"""Exercise the deployed MCP endpoint; --analyze makes real paid OpenRouter calls."""

import argparse
import asyncio
import json
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check(args) -> None:
    async with streamable_http_client(args.url) as (read, write, _):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=960)
        ) as session:
            await session.initialize()
            print("Available tools:", ", ".join(t.name for t in (await session.list_tools()).tools))
            for path in args.path:
                result = await session.call_tool("inspect_media", {"path": path})
                if result.isError:
                    raise RuntimeError(result.content[0].text)
                data = result.structuredContent or json.loads(result.content[0].text)
                print("Inspected:", json.dumps(data, indent=2))
                if args.analyze:
                    arguments = {"path": path}
                    if args.question:
                        arguments["question"] = args.question
                    if args.start is not None or args.end is not None:
                        if args.start is None or args.end is None:
                            raise ValueError("Provide both --start and --end")
                        arguments["time_range"] = {
                            "start_seconds": args.start,
                            "end_seconds": args.end,
                        }
                    result = await session.call_tool("analyze_media", arguments)
                    if result.isError:
                        raise RuntimeError(result.content[0].text)
                    data = result.structuredContent or json.loads(result.content[0].text)
                    print("Analysis:", json.dumps(data, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--question")
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    asyncio.run(check(parser.parse_args()))


if __name__ == "__main__":
    main()
