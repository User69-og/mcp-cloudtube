"""End-to-end exercise of the tool surface, with both externals faked.

Marked integration because it drives the real `MCPServer`, the real tool
wrappers and the real coordinator; only Google Drive and YouTube are
substituted. It needs no credentials and no network, so it runs on every
change.

The central property under test: **a tool never raises.** Whatever goes wrong
underneath, the caller gets a payload with `ok: false`, a code and a
remediation. An exception escaping here would reach the MCP layer as a generic
protocol error with nothing for the model to act on.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import Context

from mcp_cloudtube.context import ServerContext
from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.server import create_server, describe_tools
from mcp_cloudtube.youtube.fake import FakeYouTube, quota_exhausted

from ..support.fake_storage import FakeStorage

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "connect_account",
    "auth_status",
    "disconnect_account",
    "list_files",
    "search_files",
    "get_file_metadata",
    "read_file",
    "upload_video_to_youtube",
    "get_upload_status",
    "cancel_upload",
}


@pytest.fixture
def wired(settings, storage: FakeStorage, youtube: FakeYouTube):
    """A real MCPServer wired to fake externals."""
    settings.ensure_ready()
    server, ctx = create_server(settings, youtube=youtube)
    ctx.storage = storage  # type: ignore[assignment]
    ctx.uploads._storage = storage
    return server, ctx, storage, youtube


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool the way a client would.

    Goes through the server's own tool manager rather than calling the
    undecorated function, so registration, schema coercion and the error
    wrapper are all exercised. The Context carries no request because these
    tools do not use one; a client supplies a real one at runtime.
    """
    context = Context(request_context=None, mcp_server=server)
    return await server._tool_manager.call_tool(name, arguments, context)


class TestToolSurface:
    def test_every_required_tool_is_present(self, wired) -> None:
        server, *_ = wired
        names = {tool["name"] for tool in describe_tools(server)}
        assert names >= EXPECTED_TOOLS

    def test_every_tool_has_a_substantial_description(self, wired) -> None:
        """The description is the whole of what the model knows about a tool."""
        server, *_ = wired
        for tool in server._tool_manager.list_tools():
            assert tool.description, f"{tool.name} has no description"
            assert len(tool.description) > 80, f"{tool.name}'s description is too thin"


class TestBrowsingAndReading:
    async def test_list_files_returns_the_root(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "list_files")
        assert result["ok"] is True
        assert any(f["name"] == "Demo Videos" for f in result["files"])

    async def test_search_finds_the_latest_video(self, wired) -> None:
        """The brief's example prompt, as the model would resolve it."""
        server, *_ = wired
        result = await call(
            server, "search_files", name_contains="demo", videos_only=True,
            order_by="modified_desc",
        )
        assert result["ok"] is True
        assert result["files"][0]["name"] == "Q4 Product Demo.mp4"

    async def test_empty_search_tells_the_model_what_to_do_next(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "search_files", name_contains="nothing-matches-this")
        assert result["ok"] is True
        assert result["files"] == []
        assert "Do not invent a file ID" in result["message"]

    async def test_read_file_returns_text(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "read_file", file_id="doc_notes")
        assert result["ok"] is True
        assert "dark mode" in result["text"]

    async def test_reading_a_binary_is_a_clean_refusal(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "read_file", file_id="zip_archive")
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.UNSUPPORTED_MEDIA_TYPE
        assert "get_file_metadata" in result["error"]["remediation"]

    async def test_unknown_file_is_not_found_not_a_crash(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "get_file_metadata", file_id="does_not_exist")
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.NOT_FOUND

    async def test_a_bad_date_filter_explains_the_accepted_formats(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "search_files", name_contains="x", modified_after="last week")
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.INVALID_INPUT
        assert "7d" in result["error"]["message"]

    async def test_relative_dates_are_accepted(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "search_files", videos_only=True, modified_after="7d")
        assert result["ok"] is True
        names = [f["name"] for f in result["files"]]
        assert "Q4 Product Demo.mp4" in names
        assert "Old Demo 2024.mp4" not in names, "the 400-day-old video should be filtered out"


class TestFailuresNeverEscape:
    """Whatever breaks underneath, the tool returns a payload."""

    async def test_storage_outage_becomes_a_payload(self, wired) -> None:
        server, _ctx, storage, _yt = wired
        storage.fail_next = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "Drive is down")

        result = await call(server, "list_files")
        assert result["ok"] is False
        assert result["error"]["retryable"] is True

    async def test_an_unexpected_exception_becomes_internal_not_a_raise(
        self, wired, monkeypatch
    ) -> None:
        server, _ctx, storage, _yt = wired

        async def explode(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("something nobody predicted")

        monkeypatch.setattr(storage, "list_files", explode)

        result = await call(server, "list_files")  # must not raise
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.INTERNAL
        assert "RuntimeError" in result["error"]["message"]

    async def test_auth_failure_points_at_connect_account(self, wired) -> None:
        server, _ctx, storage, _yt = wired
        storage.fail_next = ToolError(ErrorCode.AUTH_EXPIRED, "token gone")

        result = await call(server, "search_files", name_contains="x")
        assert result["ok"] is False
        assert "connect_account" in result["error"]["remediation"]

    async def test_a_secret_in_an_upstream_message_is_redacted(self, wired) -> None:
        """Google sometimes echoes request parameters back in error text."""
        server, _ctx, storage, _yt = wired
        storage.fail_next = ToolError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "failed for token ya29.a0AfH6SMBexampleexampleexample",  # cloudtube:allow-secret-pattern
        )
        result = await call(server, "list_files")
        assert "ya29" not in str(result)


class TestUploadThroughTheToolLayer:
    async def test_the_brief_scenario_end_to_end(self, wired) -> None:
        """'Find the latest product demo video and upload it as unlisted.'"""
        server, ctx, _storage, youtube = wired

        found = await call(
            server, "search_files", name_contains="demo", videos_only=True,
            order_by="modified_desc",
        )
        file_id = found["files"][0]["id"]

        started = await call(
            server, "upload_video_to_youtube", file_id=file_id, title="Q4 Demo",
            privacy_status="unlisted", tags=["demo"],
        )
        assert started["ok"] is True
        job_id = started["job_id"]

        await _wait_for_terminal(ctx, job_id)

        status = await call(server, "get_upload_status", job_id=job_id)
        assert status["ok"] is True
        assert status["state"] == "completed"
        assert status["video_url"].startswith("https://www.youtube.com/watch?v=")
        assert youtube.uploads[0]["title"] == "Q4 Demo"
        assert youtube.uploads[0]["requested_privacy"] == "unlisted"

    async def test_uploading_a_document_is_refused_with_guidance(self, wired) -> None:
        server, *_ = wired
        result = await call(
            server, "upload_video_to_youtube", file_id="doc_notes", title="Not a video"
        )
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.INVALID_INPUT
        assert "videos_only=true" in result["error"]["remediation"]

    async def test_an_overlong_title_is_rejected_at_the_boundary(self, wired) -> None:
        server, _ctx, _storage, youtube = wired
        result = await call(
            server, "upload_video_to_youtube", file_id="vid_q4", title="x" * 150
        )
        assert result["ok"] is False
        assert youtube.uploads == []

    async def test_quota_failure_surfaces_with_its_remediation(self, wired) -> None:
        from mcp_cloudtube.youtube.fake import FailurePlan

        server, ctx, _storage, youtube = wired
        youtube.plan = FailurePlan(fail_at_fraction=0.5, failure=quota_exhausted())

        started = await call(
            server, "upload_video_to_youtube", file_id="vid_q4", title="Q4 Demo"
        )
        await _wait_for_terminal(ctx, started["job_id"])

        status = await call(server, "get_upload_status", job_id=started["job_id"])
        assert status["state"] == "failed"
        assert status["problem"]["code"] == ErrorCode.QUOTA_EXCEEDED
        assert "do not retry an upload" in status["problem"]["remediation"]

    async def test_status_without_a_job_id_lists_recent_uploads(self, wired) -> None:
        server, ctx, *_ = wired
        started = await call(
            server, "upload_video_to_youtube", file_id="vid_q4", title="Q4 Demo"
        )
        await _wait_for_terminal(ctx, started["job_id"])

        listing = await call(server, "get_upload_status")
        assert listing["ok"] is True
        assert listing["count"] == 1

    async def test_unknown_job_id_is_a_payload_not_a_crash(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "get_upload_status", job_id="job_nope")
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.UPLOAD_NOT_FOUND


class TestAuthTools:
    async def test_auth_status_reports_disconnected_without_raising(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "auth_status")
        assert result["ok"] is True
        assert result["connected"] is False

    async def test_disconnect_requires_confirmation(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "disconnect_account")
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.INVALID_INPUT
        assert "confirm=true" in result["error"]["message"]

    async def test_disconnecting_when_not_connected_is_not_an_error(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "disconnect_account", confirm=True)
        assert result["ok"] is True
        assert result["was_connected"] is False


async def _wait_for_terminal(ctx: ServerContext, job_id: str, timeout: float = 10.0) -> None:
    import asyncio

    from mcp_cloudtube.youtube.jobs import JobState

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = ctx.jobs.get(job_id)
        if job and JobState(job["state"]).terminal:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


class TestOneErrorContract:
    """Bad arguments must come back in the same shape as every other failure.

    Schema-level constraints (pydantic `max_length`, `ge`, `le`) are
    deliberately not used for these fields. The framework raises on a schema
    violation *before* the handler runs, producing a protocol-level error with
    no code, no `retryable` flag and no remediation -- so a model that oversteps
    a limit gets something categorically different from every other failure.
    Limits are documented in the tool description and enforced in code instead.
    """

    async def test_overlong_title_returns_a_structured_payload(self, wired) -> None:
        server, _ctx, _storage, youtube = wired
        result = await call(
            server, "upload_video_to_youtube", file_id="vid_q4", title="x" * 107
        )
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.INVALID_INPUT
        assert "7 too long" in result["error"]["message"]
        assert result["error"]["retryable"] is False
        assert youtube.uploads == []

    async def test_an_absurd_page_size_is_clamped_not_rejected(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "list_files", page_size=100_000)
        assert result["ok"] is True

    async def test_a_tiny_max_bytes_is_clamped_not_rejected(self, wired) -> None:
        server, *_ = wired
        result = await call(server, "read_file", file_id="doc_notes", max_bytes=1)
        assert result["ok"] is True
