"""An in-memory storage provider for tests.

Mirrors the `StorageProvider` protocol so the tool layer and the upload
coordinator can be exercised without Google Drive, a network, or credentials.
Failures are injected by setting `fail_next`, which lets a test assert on
behaviour ("the staged file is cleaned up when the download dies") rather than
on mocks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.storage.base import (
    FileContent,
    FileKind,
    FileListing,
    SearchQuery,
    StorageFile,
)


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@dataclass
class FakeStorage:
    """Storage with a small, realistic tree of files."""

    display_name: str = "Fake Drive"
    files: dict[str, StorageFile] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    #: Set to a ToolError to make the next matching call fail.
    fail_next: ToolError | None = None
    #: Fail only calls to this method name; None means the next call of any kind.
    fail_on: str | None = None
    #: Fail a staged download after writing this many bytes.
    truncate_download_at: int | None = None
    calls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.files:
            return
        self._add(
            "folder_demos", "Demo Videos", FileKind.FOLDER,
            mime="application/vnd.google-apps.folder",
        )
        self._add(
            "vid_q4", "Q4 Product Demo.mp4", FileKind.FILE, mime="video/mp4",
            size=5_242_880, modified=_ago(1), is_video=True, parents=["folder_demos"],
            content=b"\x00\x00\x00\x20ftypisom" + b"video-bytes" * 1000,
        )
        self._add(
            "vid_old", "Old Demo 2024.mp4", FileKind.FILE, mime="video/mp4",
            size=1_048_576, modified=_ago(400), is_video=True, parents=["folder_demos"],
            content=b"\x00\x00\x00\x20ftypisom" + b"old" * 500,
        )
        self._add(
            "doc_notes", "Release Notes.txt", FileKind.FILE, mime="text/plain",
            size=58, modified=_ago(2), readable=True,
            content=b"Q4 release: new dashboard, faster sync, dark mode.\n",
        )
        self._add(
            "sheet_budget", "Budget", FileKind.NATIVE_DOCUMENT,
            mime="application/vnd.google-apps.spreadsheet", modified=_ago(5), readable=True,
            content=b"item,cost\nhosting,120\n",
        )
        self._add(
            "zip_archive", "assets.zip", FileKind.FILE, mime="application/zip",
            size=9_000_000, modified=_ago(10),
        )

    def _add(
        self,
        file_id: str,
        name: str,
        kind: FileKind,
        *,
        mime: str | None = None,
        size: int | None = None,
        modified: datetime | None = None,
        is_video: bool = False,
        readable: bool = False,
        parents: list[str] | None = None,
        content: bytes | None = None,
    ) -> None:
        self.files[file_id] = StorageFile(
            id=file_id,
            name=name,
            kind=kind,
            mime_type=mime,
            size_bytes=size,
            created_at=_ago(500),
            modified_at=modified or _ago(1),
            parent_ids=parents or [],
            is_video=is_video,
            is_readable_as_text=readable,
            web_url=f"https://example.invalid/{file_id}",
        )
        if content is not None:
            self.contents[file_id] = content

    # -- failure injection ---------------------------------------------------

    def _maybe_fail(self, method: str) -> None:
        self.calls.append(method)
        if self.fail_next is None:
            return
        if self.fail_on is not None and self.fail_on != method:
            return
        error = self.fail_next
        self.fail_next = None
        self.fail_on = None
        raise error

    # -- protocol ------------------------------------------------------------

    async def list_files(
        self,
        *,
        folder_id: str | None = None,
        page_size: int = 25,
        page_token: str | None = None,
        include_trashed: bool = False,
        order_by: str = "modified_desc",
    ) -> FileListing:
        self._maybe_fail("list_files")
        if folder_id:
            items = [f for f in self.files.values() if folder_id in f.parent_ids]
        else:
            items = [f for f in self.files.values() if not f.parent_ids]
        items = sorted(items, key=lambda f: f.modified_at or _ago(9999), reverse=True)
        page = items[:page_size]
        return FileListing(files=page, total_returned=len(page))

    async def search_files(self, query: SearchQuery) -> FileListing:
        self._maybe_fail("search_files")
        items = list(self.files.values())

        if query.name_contains:
            needle = query.name_contains.casefold()
            items = [f for f in items if needle in f.name.casefold()]
        if query.videos_only:
            items = [f for f in items if f.is_video]
        if query.mime_types:
            items = [f for f in items if f.mime_type in query.mime_types]
        if query.parent_id:
            items = [f for f in items if query.parent_id in f.parent_ids]
        if query.modified_after:
            items = [
                f for f in items if f.modified_at and f.modified_at >= query.modified_after
            ]
        if query.kind is FileKind.FOLDER:
            items = [f for f in items if f.kind is FileKind.FOLDER]
        elif query.kind is FileKind.FILE:
            items = [f for f in items if f.kind is not FileKind.FOLDER]

        reverse = query.order_by.endswith("_desc")
        items = sorted(items, key=lambda f: f.modified_at or _ago(9999), reverse=reverse)
        page = items[: query.page_size]
        return FileListing(files=page, total_returned=len(page))

    async def get_metadata(self, file_id: str) -> StorageFile:
        self._maybe_fail("get_metadata")
        item = self.files.get(file_id)
        if item is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"No file with ID {file_id!r}.")
        return item

    async def read_file(self, file_id: str, *, max_bytes: int | None = None) -> FileContent:
        self._maybe_fail("read_file")
        item = await self.get_metadata(file_id)

        if item.kind is FileKind.FOLDER:
            raise ToolError(
                ErrorCode.INVALID_INPUT, f"{item.summary()} is a folder, not a file."
            )
        if not item.is_readable_as_text:
            raise ToolError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                f"{item.summary()} is not a text file.",
            )

        raw = self.contents.get(file_id, b"")
        limit = max_bytes or 1_048_576
        truncated = len(raw) > limit
        return FileContent(
            file_id=file_id,
            name=item.name,
            mime_type=item.mime_type,
            text=raw[:limit].decode("utf-8", errors="replace"),
            bytes_read=min(len(raw), limit),
            total_bytes=len(raw),
            truncated=truncated,
            extraction="decoded",
        )

    async def download_to_path(
        self,
        file_id: str,
        destination: Path,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> StorageFile:
        self._maybe_fail("download_to_path")
        item = await self.get_metadata(file_id)
        raw = self.contents.get(file_id, b"")

        if self.truncate_download_at is not None:
            destination.write_bytes(raw[: self.truncate_download_at])
            raise ToolError(
                ErrorCode.NETWORK_ERROR,
                "The connection dropped while downloading the file from storage.",
                retryable=True,
            )

        destination.write_bytes(raw)
        if progress is not None:
            progress(len(raw), len(raw))
        return item

    async def check_access(self) -> dict[str, Any]:
        self._maybe_fail("check_access")
        return {"provider": self.display_name, "account_email": "tester@example.invalid"}
