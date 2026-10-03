"""Generic MCP access-logging middleware.

Logs every inbound request/notification a server receives (tool calls,
resource reads, `initialize`, etc.), independent of which client connects -
the logging lives in the server process itself rather than in any one
client's own logging.

Each session (starting at an `initialize` request) gets its own log file,
named `<count_up_id>_<date_time>_<client_name>.jsonl` under the configured
log directory. Each line is a standalone JSON object (JSON Lines format).
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_client_name(name: str | None) -> str:
    if not name:
        return "unknown"
    sanitized = _UNSAFE_FILENAME_CHARS.sub("-", name).strip("-")
    return sanitized or "unknown"


def _client_name_from_params(params: dict[str, Any] | None) -> str:
    if not params:
        return "unknown"
    client_info = params.get("clientInfo") or {}
    return _sanitize_client_name(client_info.get("name"))


def _next_session_id(log_dir: Path) -> int:
    """The next count-up id: one past the highest id already used in log_dir."""
    ids = [
        int(match.group(1))
        for path in log_dir.glob("*.jsonl")
        if (match := re.match(r"(\d+)_", path.name))
    ]
    return max(ids, default=0) + 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _extract_usage(result: Any) -> dict[str, Any] | None:
    """Pull the "usage" object out of a tool's JSON result, if it has one.

    Tools that wrap a Messages/Token-Counting-shaped API response (deep-think's
    send_message, count_tokens, ask_an_expert, ...) return a dict containing a
    "usage" field; the MCP framework serializes that dict into the first text
    content block of the tool result, so this parses it back out.

    The API doesn't report total input tokens as a single field - it's the sum
    of three separate ones:
        total_input_tokens = cache_read_input_tokens + cache_creation_input_tokens + input_tokens
    That total is computed here and added to the returned usage dict so it
    lands in the log without every caller having to re-derive it.
    """
    content = getattr(result, "content", None)
    if not content:
        return None
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    usage = dict(usage)
    usage["total_input_tokens"] = (
        usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("input_tokens", 0)
    )
    return usage


class AccessLogMiddleware(ServerMiddleware[Any]):
    """Logs every inbound request/notification to a per-session JSON-lines file under `log_dir`."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # A uuid, not id(self): logging.getLogger caches by name process-wide,
        # and object ids get reused once garbage collected - a stale, still
        # -registered logger from an earlier instance would otherwise leak in.
        self._logger = logging.getLogger(f"access_logging.{uuid.uuid4().hex}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False  # never fall through to a stdio-adjacent root handler
        self._session_id = 0

    def _start_session(self, params: dict[str, Any] | None) -> None:
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()

        self._session_id = _next_session_id(self._log_dir)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        client_name = _client_name_from_params(params)
        filename = f"{self._session_id}_{timestamp}_{client_name}.jsonl"

        try:
            handler: logging.Handler = logging.FileHandler(self._log_dir / filename)
            handler.setFormatter(logging.Formatter("%(message)s"))
        except OSError:
            # A client may launch the server with a cwd/permissions where the
            # log file can't be created - that must never stop the server.
            handler = logging.NullHandler()
        self._logger.addHandler(handler)

    def _log(self, entry: dict[str, Any]) -> None:
        try:
            self._logger.info(json.dumps(entry, default=str))
        except Exception:
            pass

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method == "initialize" or not self._logger.handlers:
            # A new session starts at `initialize`; anything arriving without
            # one already open (e.g. a client that skips it) still gets a file.
            self._start_session(ctx.params)

        request_id = uuid.uuid4().hex[:8]
        tool_name = ctx.params.get("name") if ctx.method == "tools/call" and ctx.params else None

        self._log(
            {
                "ts": _now_iso(),
                "session_id": self._session_id,
                "request_id": request_id,
                "event": "request",
                "method": ctx.method,
                "tool_name": tool_name,
                "params": ctx.params,
            }
        )
        start = monotonic()
        try:
            result = await call_next(ctx)
        except Exception as e:
            self._log(
                {
                    "ts": _now_iso(),
                    "session_id": self._session_id,
                    "request_id": request_id,
                    "event": "response",
                    "method": ctx.method,
                    "tool_name": tool_name,
                    "status": "error",
                    "error": str(e),
                    "usage": None,
                    "duration_ms": round((monotonic() - start) * 1000, 3),
                }
            )
            raise
        self._log(
            {
                "ts": _now_iso(),
                "session_id": self._session_id,
                "request_id": request_id,
                "event": "response",
                "method": ctx.method,
                "tool_name": tool_name,
                "status": "ok",
                "error": None,
                "usage": _extract_usage(result),
                "duration_ms": round((monotonic() - start) * 1000, 3),
            }
        )
        return result
