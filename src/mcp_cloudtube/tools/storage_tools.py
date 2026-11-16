"""Tools for browsing, searching and reading the connected storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from ..context import ServerContext
from ..errors import invalid_input
from ..storage.base import FileKind, SearchQuery
from ._wrap import tool_handler


def _parse_when(value: str | None, *, field: str) -> datetime | None:
    """Accept an ISO date or a relative phrase like '7d'.

    Models produce both. Accepting the relative form avoids a round trip where
    the model has to ask what today's date is before it can search for recent
    files.
    """
    if not value:
        return None
    text = value.strip()

    if text.lower().endswith(("d", "h", "w")) and text[:-1].strip().isdigit():
        amount = int(text[:-1])
        unit = text[-1].lower()
        delta = {
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return datetime.now(UTC) - delta

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise invalid_input(
            f"{field} must be an ISO 8601 date (2026-01-31 or 2026-01-31T12:00:00Z) or a "
            f"relative age such as '7d', '24h' or '2w'. Got {value!r}.",
            field=field,
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def register(server: Any, ctx: ServerContext) -> None:
    provider = ctx.storage.display_name

    @server.tool(
        name="list_files",
        title=f"List {provider} folder contents",
        description=(
            f"List the files and folders directly inside one {provider} folder.\n\n"
            "Omit `folder_id` to list the root of the user's drive. To go deeper, call this "
            "again with the `id` of a folder from the previous result.\n\n"
            "Use this when the user wants to browse or asks what is in a folder. If they are "
            "looking for a *particular* file, prefer `search_files` -- it searches the whole "
            "drive at once instead of walking the tree folder by folder.\n\n"
            "Results are paginated. When `next_page_token` is present there are more items; "
            "pass it back as `page_token` to continue."
        ),
        annotations={"readOnlyHint": True},
    )
    @tool_handler("list_files")
    async def list_files(
        folder_id: Annotated[
            str | None,
            Field(description="Folder to list. Omit for the drive root."),
        ] = None,
        page_size: Annotated[
            int, Field(description="How many items to return. Clamped to 1-100.")
        ] = 25,
        page_token: Annotated[
            str | None,
            Field(description="From a previous result's next_page_token, to get the next page."),
        ] = None,
        include_trashed: Annotated[
            bool, Field(description="Include items in the trash. Normally false.")
        ] = False,
        order_by: Annotated[
            str,
            Field(
                description=(
                    "One of: modified_desc, modified_asc, created_desc, created_asc, "
                    "name_asc, name_desc, size_desc."
                )
            ),
        ] = "modified_desc",
    ) -> dict[str, Any]:
        listing = await ctx.storage.list_files(
            folder_id=folder_id,
            page_size=page_size,
            page_token=page_token,
            include_trashed=include_trashed,
            order_by=order_by,
        )
        payload = listing.model_dump(mode="json", exclude_none=True)
        if not listing.files:
            payload["message"] = (
                "That folder is empty."
                if folder_id
                else "The root of the drive appears to be empty."
            )
        return payload

    @server.tool(
        name="search_files",
        title=f"Search {provider}",
        description=(
            f"Find files anywhere in the user's {provider} by name, content, type or date.\n\n"
            "This is the right tool for requests like 'find the latest product demo video' or "
            "'the budget spreadsheet from last month'. Combine filters: `name_contains='demo'` "
            "with `videos_only=true` and `order_by='modified_desc'` answers 'the latest demo "
            "video' in one call.\n\n"
            "Notes that save a wasted call:\n"
            "- `name_contains` matches substrings of the filename, not the content. To search "
            "inside documents use `full_text`.\n"
            "- `videos_only=true` is the reliable way to restrict to videos; do not try to "
            "guess video MIME types.\n"
            "- `modified_after` accepts relative ages like '7d' or '2w', so you do not need to "
            "know today's date.\n"
            "- At least one filter is required; an unfiltered search would return the whole "
            "drive."
        ),
        annotations={"readOnlyHint": True},
    )
    @tool_handler("search_files")
    async def search_files(
        name_contains: Annotated[
            str | None, Field(description="Substring of the file name, case-insensitive.")
        ] = None,
        full_text: Annotated[
            str | None,
            Field(description="Text to look for inside documents, not just in names."),
        ] = None,
        videos_only: Annotated[
            bool, Field(description="Restrict to video files. Use this for upload requests.")
        ] = False,
        mime_types: Annotated[
            list[str] | None,
            Field(description="Exact MIME types to match, e.g. ['application/pdf']."),
        ] = None,
        folder_id: Annotated[
            str | None, Field(description="Restrict the search to one folder.")
        ] = None,
        modified_after: Annotated[
            str | None,
            Field(description="ISO date, or a relative age such as '7d', '24h', '2w'."),
        ] = None,
        modified_before: Annotated[
            str | None, Field(description="ISO date, or a relative age.")
        ] = None,
        kind: Annotated[
            str | None, Field(description="Restrict to 'file' or 'folder'.")
        ] = None,
        order_by: Annotated[
            str,
            Field(description="modified_desc (default), modified_asc, created_desc, name_asc, ..."),
        ] = "modified_desc",
        page_size: Annotated[
            int, Field(description="How many items to return. Clamped to 1-100.")
        ] = 25,
        page_token: Annotated[str | None, Field(description="From a previous result.")] = None,
    ) -> dict[str, Any]:
        parsed_kind = None
        if kind:
            try:
                parsed_kind = FileKind(kind.lower())
            except ValueError as exc:
                raise invalid_input(
                    f"{kind!r} is not a valid kind.",
                    allowed=["file", "folder"],
                ) from exc

        query = SearchQuery(
            name_contains=name_contains,
            full_text=full_text,
            videos_only=videos_only,
            mime_types=mime_types or [],
            parent_id=folder_id,
            modified_after=_parse_when(modified_after, field="modified_after"),
            modified_before=_parse_when(modified_before, field="modified_before"),
            kind=parsed_kind,
            order_by=order_by,
            page_size=page_size,
            page_token=page_token,
        )

        listing = await ctx.storage.search_files(query)
        payload = listing.model_dump(mode="json", exclude_none=True)

        if not listing.files:
            # An empty result is the single most common point where a model
            # starts guessing. Say what to try instead.
            payload["message"] = (
                "Nothing matched that search. Before telling the user the file does not "
                "exist, consider: a shorter or different `name_contains`; dropping "
                "`videos_only`; widening the date range; or `list_files` to browse. Do not "
                "invent a file ID."
            )
        return payload

    @server.tool(
        name="get_file_metadata",
        title="Describe a file",
        description=(
            "Return full details about one file or folder: name, type, size, timestamps, "
            "owner, link, and for videos the duration and resolution.\n\n"
            "Use this to confirm a file is what the user meant before acting on it -- "
            "especially before uploading a video, where picking the wrong file is expensive to "
            "undo. It is also the right way to describe a file that `read_file` cannot read, "
            "such as a video, an archive or an image.\n\n"
            "`file_id` comes from `list_files` or `search_files`. Never guess one; if you only "
            "have a name, search for it first."
        ),
        annotations={"readOnlyHint": True},
    )
    @tool_handler("get_file_metadata")
    async def get_file_metadata(
        file_id: Annotated[str, Field(description="The file's ID, from a list or search result.")],
    ) -> dict[str, Any]:
        item = await ctx.storage.get_metadata(file_id)
        payload = item.model_dump(mode="json", exclude_none=True)
        payload["summary"] = item.summary()
        if item.is_video:
            payload["can_upload_to_youtube"] = True
        elif not item.is_readable_as_text:
            payload["note"] = (
                "This file's content cannot be read as text. Describe it from these details "
                "rather than calling read_file."
            )
        return payload

    @server.tool(
        name="read_file",
        title="Read a file's content",
        description=(
            "Return the readable text of a file, so you can summarise it or answer questions "
            "about it.\n\n"
            "Handles plain text, code, CSV, JSON and similar formats directly; exports Google "
            "Docs, Sheets and Slides to text; and extracts the text layer from PDFs.\n\n"
            "It cannot read videos, images, archives or other binaries -- those return "
            "UNSUPPORTED_MEDIA_TYPE, and you should use `get_file_metadata` to describe them "
            "instead. You never need to read a video before uploading it.\n\n"
            "Large files are truncated. When the result says `truncated: true`, say so before "
            "summarising, rather than implying you read the whole thing."
        ),
        annotations={"readOnlyHint": True},
    )
    @tool_handler("read_file")
    async def read_file(
        file_id: Annotated[str, Field(description="The file's ID, from a list or search result.")],
        max_bytes: Annotated[
            int | None,
            Field(
                description=(
                    "Cap on how much to read. Defaults to the server's limit (1 MB). Lower it "
                    "when you only need the beginning of a large file."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        content = await ctx.storage.read_file(file_id, max_bytes=max_bytes)
        return content.model_dump(mode="json", exclude_none=True)
