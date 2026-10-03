import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import pytest

from access_logging import AccessLogMiddleware


@dataclass
class FakeTextContent:
    text: str


@dataclass
class FakeCallToolResult:
    """Minimal stand-in for CallToolResult: the middleware only reads
    `.content[0].text`, a JSON string, same as the real MCP framework produces
    for a tool that returns a dict."""

    content: list[FakeTextContent]


@dataclass
class FakeContext:
    """Minimal stand-in for ServerRequestContext: the middleware only reads
    `.method` and `.params`."""

    method: str
    params: dict[str, Any] | None


def run(coro):
    return asyncio.run(coro)


def call_next_ok(result: Any = None):
    async def _call_next(ctx):
        return result

    return _call_next


def call_next_raises(exc: Exception):
    async def _call_next(ctx):
        raise exc

    return _call_next


def log_files(log_dir):
    return sorted(log_dir.glob("*.jsonl"))


def read_entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_creates_log_dir_if_missing(tmp_path):
    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()

    AccessLogMiddleware(log_dir)

    assert log_dir.is_dir()


def test_initialize_creates_session_file_named_with_id_date_and_client(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    ctx = FakeContext(
        method="initialize",
        params={"clientInfo": {"name": "mcp-inspector"}},
    )

    run(middleware(ctx, call_next_ok()))

    files = log_files(tmp_path)
    assert len(files) == 1
    assert re.match(r"^1_\d{8}-\d{6}_mcp-inspector\.jsonl$", files[0].name)


def test_missing_client_name_falls_back_to_unknown(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    ctx = FakeContext(method="initialize", params={})

    run(middleware(ctx, call_next_ok()))

    files = log_files(tmp_path)
    assert re.match(r"^1_\d{8}-\d{6}_unknown\.jsonl$", files[0].name)


def test_no_client_info_at_all_falls_back_to_unknown(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    ctx = FakeContext(method="initialize", params=None)

    run(middleware(ctx, call_next_ok()))

    files = log_files(tmp_path)
    assert re.match(r"^1_\d{8}-\d{6}_unknown\.jsonl$", files[0].name)


def test_sanitizes_unsafe_characters_in_client_name(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    ctx = FakeContext(
        method="initialize",
        params={"clientInfo": {"name": "My Client/v1 (beta)!"}},
    )

    run(middleware(ctx, call_next_ok()))

    files = log_files(tmp_path)
    assert len(files) == 1
    # Only safe filename characters remain, no path separators or spaces.
    assert re.match(r"^1_\d{8}-\d{6}_My-Client-v1-beta\.jsonl$", files[0].name)


def test_each_initialize_starts_a_new_session_file_with_incrementing_id(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)

    run(middleware(FakeContext("initialize", {"clientInfo": {"name": "alice"}}), call_next_ok()))
    run(middleware(FakeContext("initialize", {"clientInfo": {"name": "bob"}}), call_next_ok()))

    files = log_files(tmp_path)
    assert len(files) == 2
    assert files[0].name.startswith("1_")
    assert "alice" in files[0].name
    assert files[1].name.startswith("2_")
    assert "bob" in files[1].name


def test_session_id_continues_across_middleware_instances_sharing_a_dir(tmp_path):
    first = AccessLogMiddleware(tmp_path)
    run(first(FakeContext("initialize", {"clientInfo": {"name": "alice"}}), call_next_ok()))

    second = AccessLogMiddleware(tmp_path)
    run(second(FakeContext("initialize", {"clientInfo": {"name": "bob"}}), call_next_ok()))

    files = log_files(tmp_path)
    assert len(files) == 2
    assert files[1].name.startswith("2_")


def test_request_before_any_initialize_still_gets_a_session_file(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    ctx = FakeContext(method="tools/list", params={})

    run(middleware(ctx, call_next_ok()))

    files = log_files(tmp_path)
    assert len(files) == 1
    assert "unknown" in files[0].name


def test_log_line_format_for_successful_call(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    init_ctx = FakeContext("initialize", {"clientInfo": {"name": "alice"}})
    run(middleware(init_ctx, call_next_ok()))

    call_ctx = FakeContext("tools/call", {"name": "list_notes", "arguments": {}})
    run(middleware(call_ctx, call_next_ok("some result")))

    entries = read_entries(log_files(tmp_path)[0])
    request, response = entries[2], entries[3]

    ts_re = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
    assert re.match(ts_re, request["ts"])
    assert request["session_id"] == 1
    assert request["event"] == "request"
    assert request["method"] == "tools/call"
    assert request["tool_name"] == "list_notes"
    assert request["params"] == {"name": "list_notes", "arguments": {}}

    assert response["request_id"] == request["request_id"]
    assert response["event"] == "response"
    assert response["method"] == "tools/call"
    assert response["tool_name"] == "list_notes"
    assert response["status"] == "ok"
    assert response["error"] is None
    assert isinstance(response["duration_ms"], (int, float))


def test_failed_call_is_logged_as_error_and_exception_still_propagates(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    run(middleware(FakeContext("initialize", {"clientInfo": {"name": "alice"}}), call_next_ok()))

    ctx = FakeContext("tools/call", {"name": "read_note", "arguments": {"name": "x"}})

    with pytest.raises(ValueError, match="boom"):
        run(middleware(ctx, call_next_raises(ValueError("boom"))))

    entries = read_entries(log_files(tmp_path)[0])
    response = entries[3]
    assert response["status"] == "error"
    assert response["error"] == "boom"


def test_call_next_return_value_is_passed_through(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    run(middleware(FakeContext("initialize", {}), call_next_ok()))

    result = run(middleware(FakeContext("tools/call", {}), call_next_ok({"ok": True})))

    assert result == {"ok": True}


def test_response_includes_usage_and_computed_total_input_tokens(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    run(middleware(FakeContext("initialize", {"clientInfo": {"name": "alice"}}), call_next_ok()))

    tool_result = FakeCallToolResult(
        content=[
            FakeTextContent(
                text=json.dumps(
                    {
                        "usage": {
                            "input_tokens": 2095,
                            "output_tokens": 503,
                            "cache_creation_input_tokens": 2051,
                            "cache_read_input_tokens": 2051,
                        }
                    }
                )
            )
        ]
    )
    call_ctx = FakeContext("tools/call", {"name": "send_message", "arguments": {}})
    run(middleware(call_ctx, call_next_ok(tool_result)))

    entries = read_entries(log_files(tmp_path)[0])
    response = entries[3]

    assert response["usage"]["input_tokens"] == 2095
    assert response["usage"]["cache_creation_input_tokens"] == 2051
    assert response["usage"]["cache_read_input_tokens"] == 2051
    # total_input_tokens = cache_read_input_tokens + cache_creation_input_tokens + input_tokens
    assert response["usage"]["total_input_tokens"] == 2095 + 2051 + 2051


def test_response_usage_is_none_when_result_has_no_usage(tmp_path):
    middleware = AccessLogMiddleware(tmp_path)
    run(middleware(FakeContext("initialize", {"clientInfo": {"name": "alice"}}), call_next_ok()))

    call_ctx = FakeContext("tools/call", {"name": "list_notes", "arguments": {}})
    run(middleware(call_ctx, call_next_ok("some result")))

    entries = read_entries(log_files(tmp_path)[0])
    assert entries[3]["usage"] is None


def test_logging_failure_does_not_break_the_request(tmp_path, monkeypatch):
    middleware = AccessLogMiddleware(tmp_path)
    run(middleware(FakeContext("initialize", {}), call_next_ok()))

    def broken_info(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(middleware._logger, "info", broken_info)

    result = run(middleware(FakeContext("tools/call", {}), call_next_ok("fine")))

    assert result == "fine"
