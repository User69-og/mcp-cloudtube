"""Tools for publishing a stored video to YouTube and following the upload."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..context import ServerContext
from ._wrap import tool_handler


def register(server: Any, ctx: ServerContext) -> None:
    @server.tool(
        name="upload_video_to_youtube",
        title="Publish a stored video to YouTube",
        description=(
            "Upload a video file from the connected storage to the user's YouTube channel.\n\n"
            "This starts a background job and returns a `job_id` immediately -- it does not "
            "wait for the upload to finish. Follow it with `get_upload_status`. Report the "
            "job as started; do not claim the video is published until a status call says so.\n\n"
            "Before calling:\n"
            "- Get the `file_id` from `search_files` (use `videos_only=true`). Never guess it.\n"
            "- If more than one file could be what the user meant, ask which one. Publishing "
            "the wrong video to someone's channel is not something a later call can undo.\n"
            "- Confirm the title and privacy with the user if they did not state them.\n\n"
            "`privacy_status` defaults to 'private', which is the safe default: a video can "
            "always be made more public later, but something published publicly by mistake "
            "cannot be unseen.\n\n"
            "Known limitation: if the server's Google Cloud project has not passed YouTube's "
            "API compliance audit, YouTube forces every uploaded video to 'private' whatever "
            "this tool asks for. The upload still succeeds and the job completes, but carries "
            "a PRIVACY_NOT_APPLIED note. If you see that, tell the user the video is private "
            "and why -- do not tell them it is unlisted.\n\n"
            "Calling this twice with the same file, title and privacy returns the first job "
            "rather than publishing a second copy."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    @tool_handler("upload_video_to_youtube")
    async def upload_video_to_youtube(
        file_id: Annotated[
            str,
            Field(description="ID of the video file in storage, from search_files or list_files."),
        ],
        title: Annotated[
            str,
            Field(
                # The 100-character limit is stated in the description and
                # enforced in metadata.validate(), not declared as a schema
                # constraint. A schema violation is raised by the framework
                # before this handler runs, which would return a protocol-level
                # error with no code and no remediation -- breaking the
                # guarantee that every tool answers in the same shape. Our
                # validator also gives a better message ("it is 7 too long",
                # "nothing was uploaded").
                description="The video's title on YouTube. Required. Maximum 100 characters.",
            ),
        ],
        description: Annotated[
            str | None,
            Field(description="The video's description. Maximum 5000 characters."),
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Search tags, e.g. ['product demo', 'q4']. All tags together must stay "
                    "under about 500 characters."
                )
            ),
        ] = None,
        privacy_status: Annotated[
            str,
            Field(
                description=(
                    "'private' (default), 'unlisted' (anyone with the link) or 'public'. "
                    "Confirm with the user before choosing anything other than private."
                )
            ),
        ] = "private",
        category_id: Annotated[
            str | None,
            Field(
                description=(
                    "YouTube category ID. Omit to use 28 (Science & Technology). Common: "
                    "22 People & Blogs, 24 Entertainment, 27 Education, 28 Science & Technology."
                )
            ),
        ] = None,
        made_for_kids: Annotated[
            bool,
            Field(description="Declare the video as made for children. Usually false."),
        ] = False,
    ) -> dict[str, Any]:
        return await ctx.uploads.start(
            file_id=file_id,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            category_id=category_id,
            made_for_kids=made_for_kids,
        )

    @server.tool(
        name="get_upload_status",
        title="Check on an upload",
        description=(
            "Report how an upload is going, or how it ended.\n\n"
            "Pass a `job_id` for detail on one upload. Call with no arguments to list recent "
            "uploads -- useful when the user asks 'did my video upload?' and you do not have "
            "the job ID to hand.\n\n"
            "States mean: `staging` (copying out of storage), `uploading` (sending to "
            "YouTube), `processing` (YouTube has the bytes and is transcoding -- this is "
            "normal, not a stall), `completed`, `failed`, `cancelled`.\n\n"
            "When the result contains a `problem`, read its `remediation` and follow it rather "
            "than retrying blindly. A failed upload published nothing, so retrying is safe; a "
            "completed one with a `problem` means the video IS live and must not be uploaded "
            "again.\n\n"
            "Do not poll in a tight loop. For a large video, tell the user roughly how far it "
            "has got and check again when they ask."
        ),
        annotations={"readOnlyHint": True},
    )
    @tool_handler("get_upload_status")
    async def get_upload_status(
        job_id: Annotated[
            str | None,
            Field(description="The job to report on. Omit to list recent uploads."),
        ] = None,
        limit: Annotated[
            int,
            Field(description="How many recent jobs to list when job_id is omitted. Max 25."),
        ] = 5,
    ) -> dict[str, Any]:
        return await ctx.uploads.status(job_id, limit=limit)

    @server.tool(
        name="cancel_upload",
        title="Cancel an upload in progress",
        description=(
            "Stop an upload that has not finished.\n\n"
            "Only call this when the user asks to cancel. An upload that has already completed "
            "cannot be cancelled -- the video is published, and removing it means deleting it "
            "in YouTube Studio, which this server deliberately cannot do."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    @tool_handler("cancel_upload")
    async def cancel_upload(
        job_id: Annotated[str, Field(description="The job to cancel.")],
    ) -> dict[str, Any]:
        return await ctx.uploads.cancel(job_id)
