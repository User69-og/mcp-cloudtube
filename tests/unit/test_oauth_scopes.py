"""Scope checking at the end of the OAuth exchange.

These exist because of a real failure during first setup. Google grants
`openid` alongside `userinfo.email` whether or not it was asked for, and
oauthlib rejects any token response whose scope set is not an exact match for
the request -- raising a bare `Warning` from inside the library, which
surfaced as an unhelpful INTERNAL error on an authorisation that had actually
succeeded.

The fix is to treat the granted set as needing to *cover* the required set
rather than equal it. That is the correct test anyway: extra scopes are
harmless, missing ones are not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from mcp_cloudtube.auth.google_oauth import _assert_scopes_granted
from mcp_cloudtube.config import GOOGLE_IMPLICIT_SCOPES, GOOGLE_SCOPES
from mcp_cloudtube.errors import ErrorCode, ToolError

DRIVE = "https://www.googleapis.com/auth/drive.readonly"
UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
YT_READ = "https://www.googleapis.com/auth/youtube.readonly"
EMAIL = "https://www.googleapis.com/auth/userinfo.email"


@dataclass
class _Credentials:
    """Minimal stand-in; only `scopes` is read."""

    scopes: list[str] = field(default_factory=list)


class TestGrantedScopes:
    def test_exact_grant_is_accepted(self) -> None:
        _assert_scopes_granted(_Credentials(list(GOOGLE_SCOPES)))

    def test_google_adding_openid_is_accepted(self) -> None:
        """The regression. Google returns openid unasked; that is fine."""
        _assert_scopes_granted(_Credentials([DRIVE, UPLOAD, YT_READ, EMAIL, "openid"]))

    def test_a_grant_without_openid_is_still_accepted(self) -> None:
        """openid is tolerated, never required -- we do not depend on it."""
        _assert_scopes_granted(_Credentials([DRIVE, UPLOAD, YT_READ, EMAIL]))

    def test_scope_order_does_not_matter(self) -> None:
        """Google returns them in its own order, which differs from ours."""
        _assert_scopes_granted(_Credentials([EMAIL, "openid", YT_READ, DRIVE, UPLOAD]))

    def test_unknown_extra_scopes_are_tolerated(self) -> None:
        _assert_scopes_granted(
            _Credentials([DRIVE, UPLOAD, YT_READ, EMAIL, "openid", "profile"])
        )

    @pytest.mark.parametrize(
        ("missing", "label"),
        [(DRIVE, "drive.readonly"), (UPLOAD, "youtube.upload"), (YT_READ, "youtube.readonly")],
    )
    def test_a_missing_required_scope_is_rejected(self, missing: str, label: str) -> None:
        granted = [s for s in (DRIVE, UPLOAD, YT_READ, EMAIL) if s != missing]
        with pytest.raises(ToolError) as caught:
            _assert_scopes_granted(_Credentials(granted))

        assert caught.value.code == ErrorCode.AUTH_SCOPE_INSUFFICIENT
        assert label in caught.value.message
        assert "leave every permission ticked" in caught.value.remediation

    def test_an_empty_grant_is_rejected(self) -> None:
        with pytest.raises(ToolError) as caught:
            _assert_scopes_granted(_Credentials([]))
        assert caught.value.code == ErrorCode.AUTH_SCOPE_INSUFFICIENT

    def test_the_error_names_every_missing_scope(self) -> None:
        with pytest.raises(ToolError) as caught:
            _assert_scopes_granted(_Credentials([DRIVE]))
        assert set(caught.value.details["missing_scopes"]) == {UPLOAD, YT_READ, EMAIL}


class TestConfiguration:
    def test_openid_is_requested_explicitly(self) -> None:
        """Requesting it keeps the requested and granted sets equal."""
        assert "openid" in GOOGLE_SCOPES

    def test_openid_is_marked_implicit_so_it_is_never_required(self) -> None:
        assert "openid" in GOOGLE_IMPLICIT_SCOPES

    def test_oauthlib_scope_check_is_relaxed(self) -> None:
        """Importing the module must set this, or Google's extra scope raises.

        Relaxing oauthlib's check is only safe because _assert_scopes_granted
        replaces it with a stricter, more informative one.
        """
        import mcp_cloudtube.auth.google_oauth  # noqa: F401

        assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"
