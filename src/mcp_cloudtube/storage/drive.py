"""Google Drive implementation of `StorageProvider`.

Everything Drive-specific lives here: query syntax, the `fields` masks, the
Google-native export table, shared-drive flags, and the partial-download
mechanics. Nothing above this module imports `googleapiclient`.

Three Drive quirks drive most of the code below:

*   **Google-native documents have no bytes.** A Google Doc is not a file you
    can download; `files.get_media` returns a 403 for it. It must be
    *exported* to a concrete format first. The export table below picks a
    text-bearing format per native type so `read_file` works on Docs, Sheets
    and Slides rather than failing on the most common things in a Drive.
*   **Query strings are injectable.** Drive's `q` parameter is a small
    expression language, and a filename containing an apostrophe will break a
    naively built query -- or worse, change its meaning. Every user-supplied
    value goes through `_escape`.
*   **Shared drives are opt-in.** Without `supportsAllDrives` and
    `includeItemsFromAllDrives`, files in a shared drive are silently
    invisible, which presents as "the model cannot find my file" with no
    error anywhere. Both flags are set on every call.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from ..auth.google_oauth import GoogleAuthManager
from ..config import Settings
from ..errors import ErrorCode, ToolError, invalid_input
from ..logging_setup import get_logger
from ..resilience import call_google
from .base import (
    FileContent,
    FileKind,
    FileListing,
    SearchQuery,
    StorageFile,
    is_text_like,
    is_video_mime,
)

logger = get_logger(__name__)

SERVICE = "google-drive"

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

#: Google-native types and the format we export them to for reading.
#: Chosen for text fidelity rather than visual fidelity: the consumer is a
#: language model answering questions, not a human looking at layout.
NATIVE_EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "text/plain", "Google Doc exported as plain text",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "text/csv", "Google Sheet exported as CSV (first sheet only)",
    ),
    "application/vnd.google-apps.presentation": (
        "text/plain", "Google Slides exported as plain text",
    ),
    "application/vnd.google-apps.script": (
        "application/vnd.google-apps.script+json", "Apps Script project as JSON",
    ),
    "application/vnd.google-apps.drawing": (
        "image/svg+xml", "Google Drawing exported as SVG",
    ),
}

#: Field mask. Drive bills nothing for extra fields but returns a much larger
#: payload; asking for exactly what the model needs keeps tool results small
#: enough to stay inside a context window on a big folder.
FILE_FIELDS = (
    "id,name,mimeType,size,createdTime,modifiedTime,parents,webViewLink,trashed,"
    "md5Checksum,owners(displayName,emailAddress),videoMediaMetadata(durationMillis,width,height),"
    "shortcutDetails(targetId,targetMimeType)"
)
LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"

_ORDER_BY = {
    "modified_desc": "modifiedTime desc",
    "modified_asc": "modifiedTime asc",
    "created_desc": "createdTime desc",
    "created_asc": "createdTime asc",
    "name_asc": "name_natural",
    "name_desc": "name_natural desc",
    "size_desc": "quotaBytesUsed desc",
}


def _escape(value: str) -> str:
    """Escape a literal for inclusion in a Drive `q` expression."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_storage_file(payload: dict[str, Any]) -> StorageFile:
    """Map one Drive resource onto the provider-agnostic model."""
    mime = payload.get("mimeType")
    native = mime in NATIVE_EXPORTS

    if mime == FOLDER_MIME:
        kind = FileKind.FOLDER
    elif mime == SHORTCUT_MIME:
        kind = FileKind.SHORTCUT
    elif native:
        kind = FileKind.NATIVE_DOCUMENT
    else:
        kind = FileKind.FILE

    size_raw = payload.get("size")
    try:
        size = int(size_raw) if size_raw is not None else None
    except (TypeError, ValueError):
        size = None

    owners = payload.get("owners") or []
    owner = None
    if owners and isinstance(owners[0], dict):
        owner = owners[0].get("displayName") or owners[0].get("emailAddress")

    video_meta = payload.get("videoMediaMetadata") or {}
    duration = video_meta.get("durationMillis")
    try:
        duration_ms = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    resolution = None
    if video_meta.get("width") and video_meta.get("height"):
        resolution = f"{video_meta['width']}x{video_meta['height']}"

    readable = native or is_text_like(mime) or mime == "application/pdf"

    return StorageFile(
        id=payload.get("id", ""),
        name=payload.get("name", "(unnamed)"),
        kind=kind,
        mime_type=mime,
        size_bytes=size,
        created_at=_parse_time(payload.get("createdTime")),
        modified_at=_parse_time(payload.get("modifiedTime")),
        parent_ids=list(payload.get("parents") or ()),
        web_url=payload.get("webViewLink"),
        is_video=is_video_mime(mime),
        is_readable_as_text=readable,
        trashed=bool(payload.get("trashed")),
        owner=owner,
        checksum=payload.get("md5Checksum"),
        video_duration_ms=duration_ms,
        video_resolution=resolution,
    )


class GoogleDriveProvider:
    """Reads a user's Google Drive. Never writes to it.

    The OAuth grant is `drive.readonly`, so this class *cannot* modify the
    user's Drive even if asked to. That is a deliberate blast-radius decision:
    the server's job is to read files and publish to YouTube, and a bug here
    should not be able to delete someone's documents.
    """

    display_name = "Google Drive"

    def __init__(self, auth: GoogleAuthManager, settings: Settings) -> None:
        self._auth = auth
        self._settings = settings

    # -- service construction ----------------------------------------------

    def _service(self) -> Any:
        """Build a Drive client with current credentials.

        Rebuilt per call rather than cached: `get_credentials` may refresh an
        expired token, and a cached client holds the old one. Discovery is
        served from the library's static cache, so this is cheap.
        """
        credentials: Credentials = self._auth.get_credentials()
        return build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    async def _call(
        self, fn: Callable[[], Any], *, operation: str, timeout: float | None = None
    ) -> Any:
        return await call_google(
            fn,
            operation=operation,
            service=SERVICE,
            settings=self._settings,
            deadline_seconds=timeout or self._settings.request_timeout_seconds * 3,
            attempt_timeout=timeout or self._settings.request_timeout_seconds,
        )

    # -- listing and searching ----------------------------------------------

    async def list_files(
        self,
        *,
        folder_id: str | None = None,
        page_size: int = 25,
        page_token: str | None = None,
        include_trashed: bool = False,
        order_by: str = "modified_desc",
    ) -> FileListing:
        clauses: list[str] = []
        parent = folder_id or "root"
        clauses.append(f"'{_escape(parent)}' in parents")
        if not include_trashed:
            clauses.append("trashed = false")

        return await self._list(
            query=" and ".join(clauses),
            page_size=page_size,
            page_token=page_token,
            order_by=order_by,
            operation=f"list the contents of folder '{parent}'",
        )

    async def search_files(self, query: SearchQuery) -> FileListing:
        clauses: list[str] = []

        if query.name_contains:
            clauses.append(f"name contains '{_escape(query.name_contains)}'")
        if query.full_text:
            clauses.append(f"fullText contains '{_escape(query.full_text)}'")
        if query.parent_id:
            clauses.append(f"'{_escape(query.parent_id)}' in parents")
        if not query.include_trashed:
            clauses.append("trashed = false")

        if query.videos_only:
            clauses.append("mimeType contains 'video/'")
        elif query.mime_types:
            mime_clause = " or ".join(f"mimeType = '{_escape(m)}'" for m in query.mime_types)
            clauses.append(f"({mime_clause})")

        if query.kind is FileKind.FOLDER:
            clauses.append(f"mimeType = '{FOLDER_MIME}'")
        elif query.kind is FileKind.FILE:
            clauses.append(f"mimeType != '{FOLDER_MIME}'")

        if query.modified_after:
            clauses.append(f"modifiedTime > '{query.modified_after.isoformat()}'")
        if query.modified_before:
            clauses.append(f"modifiedTime < '{query.modified_before.isoformat()}'")

        if not clauses:
            raise invalid_input(
                "A search needs at least one condition, otherwise it would return the "
                "entire Drive.",
                hint="Pass name_contains, full_text, videos_only, a mime type, or a date range.",
            )

        return await self._list(
            query=" and ".join(clauses),
            page_size=query.page_size,
            page_token=query.page_token,
            order_by=query.order_by,
            operation="search Drive",
        )

    async def _list(
        self,
        *,
        query: str,
        page_size: int,
        page_token: str | None,
        order_by: str,
        operation: str,
    ) -> FileListing:
        page_size = max(1, min(page_size, self._settings.max_list_page_size))
        order = _ORDER_BY.get(order_by)
        if order is None:
            raise invalid_input(
                f"Unknown order_by value {order_by!r}.",
                allowed=sorted(_ORDER_BY),
            )

        def execute() -> dict[str, Any]:
            return (
                self._service()
                .files()
                .list(
                    q=query,
                    pageSize=page_size,
                    pageToken=page_token,
                    orderBy=order,
                    fields=LIST_FIELDS,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    corpora="allDrives",
                )
                .execute()
            )

        logger.info("drive.list", query=query, page_size=page_size)
        payload = await self._call(execute, operation=operation)

        files = [_to_storage_file(item) for item in payload.get("files", [])]
        token = payload.get("nextPageToken")
        return FileListing(
            files=files,
            next_page_token=token,
            total_returned=len(files),
            truncated=bool(token),
        )

    # -- metadata -------------------------------------------------------------

    async def get_metadata(self, file_id: str) -> StorageFile:
        file_id = _require_id(file_id)

        def execute() -> dict[str, Any]:
            return (
                self._service()
                .files()
                .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )

        payload = await self._call(execute, operation=f"read metadata for file {file_id}")
        item = _to_storage_file(payload)

        # A shortcut points at something else; resolve it so the model does not
        # have to understand Drive shortcuts to follow one.
        if item.kind is FileKind.SHORTCUT:
            target = (payload.get("shortcutDetails") or {}).get("targetId")
            if target:
                logger.info("drive.shortcut_resolved", shortcut=file_id, target=target)
                resolved = await self.get_metadata(target)
                resolved.path_hint = f"via shortcut '{item.name}'"
                return resolved

        return item

    # -- reading ---------------------------------------------------------------

    async def read_file(self, file_id: str, *, max_bytes: int | None = None) -> FileContent:
        file_id = _require_id(file_id)
        # Clamp rather than reject: a model guessing 50 or 10**9 should get a
        # sensible read, not an error it has to recover from.
        limit = min(
            max(max_bytes or self._settings.max_read_bytes, 1024),
            self._settings.max_read_bytes,
        )
        meta = await self.get_metadata(file_id)

        if meta.kind is FileKind.FOLDER:
            raise invalid_input(
                f"{meta.summary()} is a folder, not a file, so it has no content to read.",
                hint="Use list_files with this ID as folder_id to see what is inside it.",
                file_id=file_id,
            )

        if meta.kind is FileKind.NATIVE_DOCUMENT:
            return await self._export_native(meta, limit)

        mime = (meta.mime_type or "").split(";", 1)[0].lower()

        if mime == "application/pdf":
            return await self._read_pdf(meta, limit)

        if not is_text_like(mime):
            raise ToolError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                f"{meta.summary()} is not a text file, so its bytes cannot be shown as text.",
                remediation=(
                    "Describe it from get_file_metadata instead. If this is a video you want "
                    "to publish, pass its ID straight to upload_video_to_youtube -- that tool "
                    "does not need to read the file as text."
                ),
                details={"file_id": file_id, "mime_type": meta.mime_type},
            )

        if meta.size_bytes and meta.size_bytes > limit:
            logger.info("drive.read_truncating", file_id=file_id, size=meta.size_bytes, limit=limit)

        raw = await self._download_bytes(file_id, limit, operation=f"read file '{meta.name}'")
        text, encoding, replaced = _decode(raw)

        notice = None
        if meta.size_bytes and meta.size_bytes > len(raw):
            notice = (
                f"Only the first {len(raw)} bytes of {meta.size_bytes} were read. Tell the user "
                "the content is truncated before summarising it."
            )
        if replaced:
            notice = (notice + " " if notice else "") + (
                "Some bytes were not valid text and were replaced; the file may be binary."
            )

        return FileContent(
            file_id=file_id,
            name=meta.name,
            mime_type=meta.mime_type,
            text=text,
            encoding=encoding,
            bytes_read=len(raw),
            total_bytes=meta.size_bytes,
            truncated=bool(meta.size_bytes and meta.size_bytes > len(raw)),
            extraction="decoded",
            notice=notice,
        )

    async def _export_native(self, meta: StorageFile, limit: int) -> FileContent:
        export_mime, description = NATIVE_EXPORTS[meta.mime_type or ""]

        def execute() -> bytes:
            return (
                self._service()
                .files()
                .export(fileId=meta.id, mimeType=export_mime)
                .execute()
            )

        raw = await self._call(execute, operation=f"export '{meta.name}'")
        if isinstance(raw, str):
            raw = raw.encode("utf-8")

        truncated = len(raw) > limit
        text, encoding, _ = _decode(raw[:limit])

        return FileContent(
            file_id=meta.id,
            name=meta.name,
            mime_type=meta.mime_type,
            text=text,
            encoding=encoding,
            bytes_read=min(len(raw), limit),
            total_bytes=len(raw),
            truncated=truncated,
            extraction="exported",
            notice=(
                f"{description}."
                + (" Content was truncated at the read limit." if truncated else "")
            ),
        )

    async def _read_pdf(self, meta: StorageFile, limit: int) -> FileContent:
        raw = await self._download_bytes(
            meta.id,
            # PDF text is a fraction of the file size; read generously then cap
            # the extracted text rather than the bytes.
            min(self._settings.max_read_bytes * 8, (meta.size_bytes or limit) + 1),
            operation=f"download PDF '{meta.name}'",
        )

        text, page_count = _pdf_to_text(raw, summary=meta.summary(), file_id=meta.id)

        truncated = len(text) > limit
        if truncated:
            text = text[:limit]

        notice = f"Text extracted from {page_count} PDF page(s)."
        if not text.strip():
            notice = (
                f"This PDF has {page_count} page(s) but no extractable text layer -- it is "
                "probably a scan. Tell the user it would need OCR; do not claim it is empty."
            )
        elif truncated:
            notice += " Truncated at the read limit."

        return FileContent(
            file_id=meta.id,
            name=meta.name,
            mime_type=meta.mime_type,
            text=text,
            bytes_read=len(raw),
            total_bytes=meta.size_bytes,
            truncated=truncated,
            extraction="pdf_text",
            notice=notice,
        )

    async def _download_bytes(self, file_id: str, limit: int, *, operation: str) -> bytes:
        """Download at most `limit` bytes, stopping early rather than buffering it all."""

        def execute() -> bytes:
            request = self._service().files().get_media(
                fileId=file_id, supportsAllDrives=True
            )
            buffer = io.BytesIO()
            chunk = min(limit, 1024 * 1024)
            downloader = MediaIoBaseDownload(buffer, request, chunksize=max(chunk, 262144))
            done = False
            while not done and buffer.tell() < limit:
                _status, done = downloader.next_chunk()
            return buffer.getvalue()[:limit]

        return await self._call(execute, operation=operation)

    # -- staging for upload ------------------------------------------------------

    async def download_to_path(
        self,
        file_id: str,
        destination: Path,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> StorageFile:
        """Stage the whole file on disk so a resumable upload can seek in it."""
        file_id = _require_id(file_id)
        meta = await self.get_metadata(file_id)

        if meta.kind is FileKind.FOLDER:
            raise invalid_input(
                f"{meta.summary()} is a folder and cannot be uploaded as a video.",
                file_id=file_id,
            )
        if meta.kind is FileKind.NATIVE_DOCUMENT:
            raise invalid_input(
                f"{meta.summary()} is a Google-native document, not a video file.",
                hint="Pick an actual video file such as an .mp4 or .mov.",
                file_id=file_id,
            )

        total = meta.size_bytes
        if total and total > self._settings.max_upload_bytes:
            raise ToolError(
                ErrorCode.FILE_TOO_LARGE,
                f"{meta.summary()} is larger than the configured upload limit of "
                f"{self._settings.max_upload_bytes} bytes.",
                remediation=(
                    "Raise CLOUDTUBE_MAX_UPLOAD_BYTES if this is intentional, or choose a "
                    "smaller file."
                ),
                details={"file_id": file_id, "size_bytes": total},
            )

        _assert_disk_space(destination, total)

        def execute() -> None:
            request = self._service().files().get_media(
                fileId=file_id, supportsAllDrives=True
            )
            with destination.open("wb") as handle:
                downloader = MediaIoBaseDownload(
                    handle, request, chunksize=self._settings.upload_chunk_bytes
                )
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if progress is not None and status is not None:
                        progress(int(status.resumable_progress), total)

        logger.info("drive.staging", file_id=file_id, size=total, destination=str(destination))
        await self._call(
            execute,
            operation=f"download '{meta.name}' from Drive",
            timeout=self._settings.upload_deadline_seconds,
        )

        staged = destination.stat().st_size
        if total is not None and staged != total:
            destination.unlink(missing_ok=True)
            raise ToolError(
                ErrorCode.UPLOAD_FAILED,
                f"The download of {meta.summary()} finished with {staged} bytes but Drive "
                f"reported {total}. The staged copy was incomplete and has been discarded.",
                remediation="Try again; the file was not uploaded, so nothing is half-published.",
                retryable=True,
                details={"file_id": file_id, "expected": total, "received": staged},
            )

        return meta

    # -- health ---------------------------------------------------------------------

    async def check_access(self) -> dict[str, Any]:
        def execute() -> dict[str, Any]:
            return (
                self._service()
                .about()
                .get(fields="user(displayName,emailAddress),storageQuota(limit,usage)")
                .execute()
            )

        payload = await self._call(
            execute, operation="verify Drive access", timeout=15.0
        )
        user = payload.get("user") or {}
        quota = payload.get("storageQuota") or {}
        return {
            "provider": self.display_name,
            "account_email": user.get("emailAddress"),
            "account_name": user.get("displayName"),
            "storage_used_bytes": _int_or_none(quota.get("usage")),
            "storage_limit_bytes": _int_or_none(quota.get("limit")),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{5,}$")


def _pdf_to_text(raw: bytes, *, summary: str, file_id: str) -> tuple[str, int]:
    """Extract text from PDF bytes. Returns (text, page_count).

    Pure and synchronous so it can be tested against real PDF bytes without a
    provider, a network or credentials -- which is how the bug this replaced
    went unnoticed: every PDF path ran only against live Drive.

    The exception list is deliberately explicit. `PyPdfError` is pypdf's base
    class (there is no `PdfError`, which is what the earlier version wrongly
    imported), and `DependencyError` sits outside that hierarchy -- pypdf
    raises it when an encrypted file needs a crypto backend that is not
    installed, which is exactly a case we must not crash on.
    """
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ToolError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                f"{summary} is a password-protected PDF.",
                remediation=(
                    "The server has no password for it. Describe it from its metadata, or "
                    "ask the user for an unprotected copy."
                ),
                details={"file_id": file_id},
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except ToolError:
        raise
    except (PyPdfError, DependencyError, ValueError, OSError, RecursionError) as exc:
        raise ToolError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"The PDF {summary} could not be parsed: {exc}",
            remediation=(
                "The file may be corrupt or only partially downloaded. Describe it from "
                "its metadata instead."
            ),
            details={"file_id": file_id},
            cause=exc,
        ) from exc

    return "\n\n".join(p for p in pages if p.strip()), len(pages)


def _require_id(file_id: str) -> str:
    """Validate a file ID before spending a network call on it.

    Models sometimes pass a filename, a full URL, or a sentence where an ID
    belongs. Catching that here turns a confusing Drive 404 into a message
    that tells the model exactly what it did wrong.
    """
    if not isinstance(file_id, str) or not file_id.strip():
        raise invalid_input("A file ID is required but none was given.")

    value = file_id.strip()

    # Be forgiving about a pasted Drive URL; extract the ID rather than refuse.
    if "drive.google.com" in value or value.startswith("http"):
        match = re.search(r"/d/([A-Za-z0-9_\-]{10,})|[?&]id=([A-Za-z0-9_\-]{10,})", value)
        if match:
            return match.group(1) or match.group(2)
        raise invalid_input(
            "That looks like a Google Drive URL but no file ID could be read from it.",
            hint="Use search_files or list_files to get the file's ID.",
            received=value[:120],
        )

    if not _ID_RE.match(value):
        raise invalid_input(
            f"{value!r} is not a Drive file ID.",
            hint=(
                "File IDs are opaque strings returned by list_files and search_files. If you "
                "have a file name, search for it first rather than guessing the ID."
            ),
        )
    return value


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """Decode bytes to text, reporting which encoding worked."""
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding), encoding, False
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8", True


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _assert_disk_space(destination: Path, needed: int | None) -> None:
    """Refuse to start a multi-gigabyte download that cannot possibly finish.

    Running out of disk halfway through staging produces a truncated file and
    a confusing upload failure much later. Checking first costs nothing.
    """
    if not needed:
        return
    try:
        usage = os.statvfs(destination.parent) if hasattr(os, "statvfs") else None
        free = usage.f_bavail * usage.f_frsize if usage else _windows_free_bytes(destination.parent)
    except OSError:
        return
    if free is None:
        return

    # Ask for a 10% margin: the temp file is not the only thing writing here.
    if free < needed * 1.1:
        raise ToolError(
            ErrorCode.CONFIG_ERROR,
            f"Not enough free disk space to stage the video: {needed} bytes are needed at "
            f"{destination.parent} but only {free} are free.",
            remediation=(
                "Free up space, or set CLOUDTUBE_STATE_DIR to a drive with more room. The "
                "server stages the file on disk so that a dropped upload can resume instead "
                "of restarting."
            ),
            details={"needed_bytes": needed, "free_bytes": free},
        )


def _windows_free_bytes(path: Path) -> int | None:
    try:
        import ctypes

        free = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(str(path)), ctypes.pointer(free), None, None
        )
        return int(free.value) if ok else None
    except Exception:
        return None
