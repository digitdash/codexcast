"""The optional MCP SDK layer; collection never runs in the model's request loop."""
import json
import os
from pathlib import Path
from typing import Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .collector import CollectorClient, CollectorConfig, log_event
from .lifecycle import desktop_owner


def configured_client():
    path = os.environ.get('CODEX_USAGE_CONFIG')
    if not path:
        raise ValueError('Use the installed plugin launcher with its local runtime configuration')
    config = CollectorConfig(**json.loads(Path(path).read_text()))
    return CollectorClient(config, owner=desktop_owner())


def create_server(client):
    server = FastMCP('Codex Usage', instructions='Account quota and local response statistics. Stats never polls. '
                     'Start once if stopped. Session IDs must be actual Codex thread IDs. '
                     'Current/global resets preserve history and account allowance.')
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=write)
    def usage_start() -> dict[str, Any]:
        """Start or attach to the one collector for this desktop client."""
        return client.start()

    @server.tool(annotations=read)
    def usage_stats(session_id: str) -> dict[str, Any]:
        """Read saved account statistics with the requesting Codex thread's baseline."""
        return client.stats(session_id)

    @server.tool(annotations=write)
    def usage_reset_current(session_id: str) -> dict[str, Any]:
        """Restart averages for this Codex thread; preserve history and account quota."""
        return client.reset(session_id)

    @server.tool(annotations=write)
    def usage_reset_all() -> dict[str, Any]:
        """Restart averages across every plugin session; preserve history and account quota."""
        return client.reset(None)

    @server.tool(annotations=write)
    def usage_stop() -> dict[str, Any]:
        """Stop the collector shared by all plugin sessions."""
        return client.stop()

    return server


def main():
    client = configured_client()
    log_event(client.config, 'mcp_start')
    # Starting/listing this server must never start the collector (including
    # when Codex itself loads plugins inside a telemetry-only app-server).
    try:
        create_server(client).run(transport='stdio')
    finally:
        log_event(client.config, 'mcp_stop')


if __name__ == '__main__':
    main()
