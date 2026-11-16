"""Tools for connecting, inspecting and disconnecting the Google account."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..context import ServerContext
from ._wrap import tool_handler


def register(server: Any, ctx: ServerContext) -> None:
    @server.tool(
        name="connect_account",
        title="Connect a Google account",
        description=(
            "Start connecting the user's Google account, which grants this server read-only "
            "access to their Google Drive and permission to upload to their YouTube channel.\n\n"
            "Returns an authorisation URL. Show that URL to the user as a clickable link and "
            "ask them to open it and approve the permissions. The approval happens in their "
            "browser; this tool does not wait for it.\n\n"
            "After they say they have approved, call `auth_status` to confirm, or simply retry "
            "whatever they originally asked for.\n\n"
            "Call this when any other tool returns AUTH_REQUIRED or AUTH_EXPIRED. Do not call "
            "it speculatively at the start of a conversation; if an account is already "
            "connected, the other tools will just work."
        ),
    )
    @tool_handler("connect_account")
    async def connect_account() -> dict[str, Any]:
        url = ctx.auth.begin()
        return {
            "authorization_url": url,
            "expires_in_seconds": 600,
            "next_step": (
                "Give the user this link and ask them to open it and approve access. The link "
                "is valid for 10 minutes. Once they confirm, call auth_status or retry their "
                "original request."
            ),
            "what_is_granted": [
                "Read-only access to Google Drive (this server can never modify or delete files)",
                "Permission to upload videos to the connected YouTube channel",
                "The account's email address, so the server can say which account is connected",
            ],
            "note": (
                "Google will warn that the app is not verified. That is expected for a locally "
                "run server; the user should choose Advanced and continue, provided their "
                "account was added as a test user in the Cloud Console."
            ),
        }

    @server.tool(
        name="auth_status",
        title="Check which account is connected",
        description=(
            "Report whether a Google account is connected, which permissions were granted, and "
            "whether the stored credentials are still usable.\n\n"
            "Use this to answer 'am I connected?', to confirm after the user approves an "
            "authorisation, or to diagnose a permission error. An expired *access token* is "
            "normal and renews itself; only a failed refresh needs re-authorisation."
        ),
    )
    @tool_handler("auth_status")
    async def auth_status() -> dict[str, Any]:
        status = ctx.auth.status()

        if pending_error := ctx.auth.pending_error():
            status["last_attempt_failed"] = {
                "code": str(pending_error.code),
                "message": pending_error.message,
                "remediation": pending_error.remediation,
            }

        if status.get("connected"):
            # Prove the credentials actually work rather than just existing on
            # disk. "Connected" that fails on the next call is worse than a
            # clear "needs re-authorisation" now.
            try:
                status["drive"] = await ctx.storage.check_access()
            except Exception as exc:
                from ..errors import ToolError

                error = exc if isinstance(exc, ToolError) else None
                status["drive_check_failed"] = (
                    error.message if error else "Drive could not be reached."
                )
                if error:
                    status["remediation"] = error.remediation

            try:
                status["youtube"] = await ctx.youtube.check_access()
            except Exception as exc:
                from ..errors import ToolError

                error = exc if isinstance(exc, ToolError) else None
                status["youtube_check_failed"] = (
                    error.message if error else "YouTube could not be reached."
                )
                if error and "remediation" not in status:
                    status["remediation"] = error.remediation

        status["server"] = ctx.diagnostics()
        return status

    @server.tool(
        name="disconnect_account",
        title="Disconnect the Google account",
        description=(
            "Revoke this server's access to the user's Google account and delete the stored "
            "credentials from this machine.\n\n"
            "Only call this when the user explicitly asks to disconnect, sign out, or revoke "
            "access. It is not a troubleshooting step: if a call failed with an auth error, "
            "call `connect_account` instead, which re-authorises without destroying anything."
        ),
        annotations={"destructiveHint": True, "idempotentHint": True},
    )
    @tool_handler("disconnect_account")
    async def disconnect_account(
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Present so the account cannot be disconnected by a "
                    "misfired call; ask the user before setting it."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        if not confirm:
            from ..errors import invalid_input

            raise invalid_input(
                "Disconnecting requires confirm=true.",
                hint=(
                    "Ask the user to confirm they want to disconnect their Google account, "
                    "then call this again with confirm=true."
                ),
            )

        result = ctx.auth.disconnect()
        if not result["was_connected"]:
            return {
                **result,
                "message": "No account was connected, so there was nothing to disconnect.",
            }
        return {
            **result,
            "message": (
                "The Google account has been disconnected and the stored credentials deleted "
                "from this machine."
                + ("" if result["revoked_remotely"] else " Google could not be reached to "
                   "revoke the grant remotely.")
            ),
        }
