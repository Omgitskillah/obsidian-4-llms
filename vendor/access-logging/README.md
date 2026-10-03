# access-logging

Generic MCP `ServerMiddleware` that logs every inbound request/notification
(tool calls, resource reads, `initialize`, etc.), independent of which client
connects (Inspector, Claude Desktop, or anything else) - the logging lives in
the server process itself rather than in any one client.

Each session (starting at `initialize`) gets its own log file, named:

```
<count_up_id>_<date_time>_<client_name>.jsonl
```

`client_name` comes from the client's self-reported `clientInfo.name` at
`initialize`, or `unknown` if none was given. `count_up_id` is the next
integer after the highest id already present in the log directory.

Each line is a standalone JSON object (JSON Lines format): one for the
request, one for its response, correlated by `request_id`:

```json
{"ts": "2026-09-13T15:42:55.448Z", "session_id": 1, "request_id": "a1b2c3d4", "event": "request", "method": "initialize", "tool_name": null, "params": {...}}
{"ts": "2026-09-13T15:42:55.451Z", "session_id": 1, "request_id": "a1b2c3d4", "event": "response", "method": "initialize", "tool_name": null, "status": "ok", "error": null, "duration_ms": 3.2}
```

For `tools/call` requests, `tool_name` is pulled out of `params` to the top
level for easy filtering/grouping.

## Usage

```python
from pathlib import Path

from access_logging import AccessLogMiddleware
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("my-server", middleware=[AccessLogMiddleware(Path("logs"))])
```
