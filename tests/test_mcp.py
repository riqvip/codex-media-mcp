import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from codex_media_mcp.server import create_server
from codex_media_mcp.settings import Settings


async def test_real_mcp_handshake_catalog_and_error():
    app = create_server(Settings()).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            health = await client.get("http://127.0.0.1:8765/health")
            assert health.json() == {"status": "ok"}
            async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert "physically trims" in initialized.instructions
                    listed = await session.list_tools()
                    tools = {tool.name: tool for tool in listed.tools}
                    assert set(tools) == {"inspect_media", "analyze_media"}
                    assert all(tool.outputSchema["type"] == "object" for tool in tools.values())
                    schema = tools["analyze_media"].inputSchema
                    assert schema["required"] == ["path"]
                    assert set(schema["properties"]) == {"path", "question", "time_range"}
                    result = await session.call_tool("analyze_media", {"path": "/demo.mp4"})
                    assert result.isError
                    assert "OPENROUTER_API_KEY" in result.content[0].text
                    invalid = await session.call_tool(
                        "analyze_media",
                        {
                            "path": "/demo.mp4",
                            "time_range": {"start_seconds": 4, "end_seconds": 2},
                        },
                    )
                    assert invalid.isError


async def test_http_origin_validation():
    app = create_server(Settings()).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            reply = await client.post(
                "http://127.0.0.1:8765/mcp",
                headers={
                    "Origin": "https://unrelated.example",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert reply.status_code == 403
