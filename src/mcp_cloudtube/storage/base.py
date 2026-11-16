"""Provider-agnostic storage model and interface.

The brief says to pick one provider and do it well, and treats a second
provider behind a common interface as a bonus. This file is that interface.
It exists even though only Google Drive ships, for two reasons:

*   The tool layer and the YouTube uploader are written against these types,
    so nothing above this line imports `googleapiclient`. Adding Dropbox means
    writing one new module, not editing six.
*   It forces a decision about what is genuinely common across providers.
    Drive's `fileId`, Dropbox's path and S3's key are all "an opaque handle a
    later call can use", so the model exposes `id` and nothing provider-shaped
    leaks into a tool schema that a model has to reason about.

Where providers genuinely differ, the model says so rather than pretending:
`native_export` marks Google-native documents, which have no byte stream to
download and must be exported to a concrete format instead.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class FileKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"
    #: A Google Doc/Sheet/Slide: real content, but no downloadable bytes.
    NATIVE_DOCUMENT = "native_document"
    SHORTCUT = "shortcut"


class StorageFile(BaseModel):
    """One file or folder, as described to the model.

    Field names are chosen to be self-explanatory in a tool result, because the
    model sees these keys and nothing else. `size_bytes` rather than `size`,
    `modified_at` rather than `modifiedTime`, and explicit `is_video` so the
    model does not have to know MIME strings to answer "find the latest demo
    video".
    """

    id: str = Field(description="Opaque handle; pass to other tools to act on this item.")
    name: str
    kind: FileKind
    mime_type: str | None = None
    size_bytes: int | None = Field(
        default=None,
        description="Null for folders and for Google-native documents, which have no fixed size.",
    )
    created_at: datetime | None = None
    modified_at: datetime | None = None
    parent_ids: list[str] = Field(default_factory=list)
    path_hint: str | None = Field(
        default=None,
        description="Human-readable location, when the provider can supply one cheaply.",
    )
    web_url: str | None = Field(default=None, description="Open in a browser.")
    is_video: bool = False
    is_readable_as_text: bool = Field(
        default=False,
        description="Whether read_file can return meaningful text for this item.",
    )
    trashed: bool = False
    owner: str | None = None
    checksum: str | None = None
    video_duration_ms: int | None = None
    video_resolution: str | None = None

    def summary(self) -> str:
        """One-line description, used in error messages and confirmations."""
        if self.kind is FileKind.FOLDER:
            return f"folder '{self.name}'"
        size = f", {_human_size(self.size_bytes)}" if self.size_bytes else ""
        return f"'{self.name}' ({self.mime_type or 'unknown type'}{size})"


class FileListing(BaseModel):
    """A page of results."""

    files: list[StorageFile]
    next_page_token: str | None = Field(
        default=None,
        description="Pass back as page_token to fetch the next page. Null means this is the last.",
    )
    total_returned: int = 0
    truncated: bool = Field(
        default=False,
        description="True when more results exist than were returned.",
    )


class FileContent(BaseModel):
    """Readable content extracted from a file."""

    file_id: str
    name: str
    mime_type: str | None
    text: str
    encoding: str = "utf-8"
    bytes_read: int = 0
    total_bytes: int | None = None
    truncated: bool = False
    extraction: str = Field(
        default="raw",
        description="How the text was obtained: raw, decoded, exported, or pdf_text.",
    )
    notice: str | None = Field(
        default=None,
        description="Anything the model should tell the user about how this was read.",
    )


class SearchQuery(BaseModel):
    """Normalised search parameters, shared across providers."""

    name_contains: str | None = None
    full_text: str | None = None
    mime_types: list[str] = Field(default_factory=list)
    kind: FileKind | None = None
    videos_only: bool = False
    parent_id: str | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    include_trashed: bool = False
    order_by: str = "modified_desc"
    page_size: int = 25
    page_token: str | None = None


@runtime_checkable
class StorageProvider(Protocol):
    """What a storage backend must do to be usable by this server.

    Implementations raise `ToolError` and nothing else; translating provider
    exceptions is the implementation's job, not the caller's.
    """

    #: Short identifier used in messages, e.g. "Google Drive".
    display_name: str

    async def list_files(
        self, *, folder_id: str | None, page_size: int, page_token: str | None,
        include_trashed: bool, order_by: str,
    ) -> FileListing: ...

    async def search_files(self, query: SearchQuery) -> FileListing: ...

    async def get_metadata(self, file_id: str) -> StorageFile: ...

    async def read_file(self, file_id: str, *, max_bytes: int) -> FileContent: ...

    async def download_to_path(
        self,
        file_id: str,
        destination: Path,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> StorageFile:
        """Stage a file's bytes on local disk, for upload to YouTube.

        Staging rather than streaming provider-to-YouTube is deliberate. A
        resumable upload that loses its connection has to re-send from the last
        acknowledged byte, which means the source must be seekable. A network
        stream is not, so a dropped connection would force the whole transfer
        to start over -- and at 1600 quota units per attempt, that is the
        expensive kind of failure. A staged temp file makes the retry cheap.
        """
        ...

    async def check_access(self) -> dict[str, Any]:
        """Cheap call proving credentials work; used by the health tool."""
        ...


#: MIME types whose bytes are meaningful as text without conversion.
TEXT_LIKE_PREFIXES: tuple[str, ...] = ("text/",)
TEXT_LIKE_EXACT: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
        "application/toml",
        "application/x-ndjson",
        "image/svg+xml",
    }
)


def is_text_like(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    base = mime_type.split(";", 1)[0].strip().lower()
    return base.startswith(TEXT_LIKE_PREFIXES) or base in TEXT_LIKE_EXACT


def is_video_mime(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    return mime_type.split(";", 1)[0].strip().lower().startswith("video/")


def _human_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


__all__ = [
    "FileContent",
    "FileKind",
    "FileListing",
    "SearchQuery",
    "StorageFile",
    "StorageProvider",
    "is_text_like",
    "is_video_mime",
]
