"""Tests that talk to the real Google Drive and YouTube APIs.

Marked `integration` and excluded from the default run, because they need a
connected Google account. Run them with:

    pytest -m integration        (or: make test-live)

Why these exist
---------------
The rest of the suite runs against fakes, which is what makes it fast,
offline and able to simulate failures nobody can produce on demand -- a
dropped connection at 75%, an exhausted quota, an expired resume session.

What a fake cannot do is prove the real API still behaves the way the code
assumes. Field names change, scopes get reinterpreted, an SDK upgrade alters a
return shape. These tests cover the seam the fakes necessarily skip: the code
that actually calls Google.

They are read-only and deliberately cheap. Drive operations cost roughly one
quota unit each, against a quota measured in thousands per minute. Nothing
here uploads, and nothing writes -- the server holds `drive.readonly`, so it
could not write even if a test asked it to.

Tests skip themselves rather than fail when no account is connected, so a
reviewer who has not done the Google Cloud setup still gets a green suite.
"""

from __future__ import annotations

import pytest

from mcp_cloudtube.auth.google_oauth import GoogleAuthManager
from mcp_cloudtube.config import Settings, get_settings
from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.storage.base import FileKind, SearchQuery
from mcp_cloudtube.storage.drive import GoogleDriveProvider
from mcp_cloudtube.youtube.client import YouTubeClient

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

#: The folder docs/SETUP_GOOGLE_CLOUD.md asks you to create.
TEST_FOLDER = "MCP Test"


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """Real settings, pointing at the real state directory.

    Deliberately not the `settings` fixture from conftest, which isolates
    everything into a temp directory -- the whole point here is to use the
    credentials an actual user has connected.
    """
    settings = get_settings(refresh=True)
    try:
        settings.resolve_client_secret()
    except ToolError as exc:
        pytest.skip(f"no OAuth client secrets file: {exc.message}")
    return settings


@pytest.fixture(scope="session")
def live_auth(live_settings: Settings) -> GoogleAuthManager:
    auth = GoogleAuthManager(live_settings)
    status = auth.status()
    if not status.get("connected"):
        pytest.skip(
            "no Google account connected. Start the server and run "
            "connect_account from a chat client first."
        )
    try:
        auth.get_credentials()
    except ToolError as exc:
        pytest.skip(f"stored credentials are not usable: {exc.code}")
    return auth


@pytest.fixture
def drive(live_auth: GoogleAuthManager, live_settings: Settings) -> GoogleDriveProvider:
    return GoogleDriveProvider(live_auth, live_settings)


@pytest.fixture
def youtube(live_auth: GoogleAuthManager, live_settings: Settings) -> YouTubeClient:
    return YouTubeClient(live_auth, live_settings)


@pytest.fixture
async def test_folder_id(drive: GoogleDriveProvider) -> str:
    """Find the documented test folder, or skip the tests that need it."""
    listing = await drive.search_files(
        SearchQuery(name_contains=TEST_FOLDER, kind=FileKind.FOLDER, page_size=5)
    )
    folders = [f for f in listing.files if f.name == TEST_FOLDER]
    if not folders:
        pytest.skip(
            f"no '{TEST_FOLDER}' folder in this Drive. See "
            "docs/SETUP_GOOGLE_CLOUD.md step 6."
        )
    return folders[0].id


# ---------------------------------------------------------------------------
# Credentials actually work
# ---------------------------------------------------------------------------


class TestAccess:
    async def test_drive_is_reachable(self, drive: GoogleDriveProvider) -> None:
        """The cheapest possible proof that the stored token is usable."""
        info = await drive.check_access()
        assert info["provider"] == "Google Drive"
        assert "@" in (info["account_email"] or ""), "no account email returned"

    async def test_youtube_channel_exists(self, youtube: YouTubeClient) -> None:
        """An account with no channel cannot upload; better to know early."""
        channel = await youtube.check_access()
        assert channel["channel_id"]
        assert channel["channel_title"]


# ---------------------------------------------------------------------------
# Browsing and searching
# ---------------------------------------------------------------------------


class TestBrowsing:
    async def test_root_listing_returns_items(self, drive: GoogleDriveProvider) -> None:
        listing = await drive.list_files(folder_id=None, page_size=10)
        assert listing.total_returned == len(listing.files)
        for item in listing.files:
            assert item.id and item.name
            assert item.kind in set(FileKind)

    async def test_page_size_is_respected(self, drive: GoogleDriveProvider) -> None:
        listing = await drive.list_files(folder_id=None, page_size=2)
        assert len(listing.files) <= 2

    async def test_trashed_items_are_excluded_by_default(
        self, drive: GoogleDriveProvider
    ) -> None:
        listing = await drive.list_files(folder_id=None, page_size=25)
        assert all(not item.trashed for item in listing.files)

    async def test_search_by_name_finds_the_test_folder(
        self, drive: GoogleDriveProvider
    ) -> None:
        listing = await drive.search_files(
            SearchQuery(name_contains=TEST_FOLDER, kind=FileKind.FOLDER)
        )
        if not listing.files:
            pytest.skip(f"no '{TEST_FOLDER}' folder in this Drive")
        assert any(f.name == TEST_FOLDER for f in listing.files)

    async def test_videos_only_returns_only_videos(
        self, drive: GoogleDriveProvider
    ) -> None:
        """The filter the brief's own example prompt depends on."""
        listing = await drive.search_files(SearchQuery(videos_only=True, page_size=25))
        if not listing.files:
            pytest.skip("no videos in this Drive")
        for item in listing.files:
            assert item.is_video, f"{item.name} is not a video"
            assert (item.mime_type or "").startswith("video/")

    async def test_an_unmatchable_search_returns_empty_not_an_error(
        self, drive: GoogleDriveProvider
    ) -> None:
        listing = await drive.search_files(
            SearchQuery(name_contains="zzz-nothing-is-called-this-zzz")
        )
        assert listing.files == []

    async def test_an_apostrophe_does_not_break_the_query(
        self, drive: GoogleDriveProvider
    ) -> None:
        """Unescaped, this changes the meaning of Drive's q expression."""
        listing = await drive.search_files(SearchQuery(name_contains="O'Brien's file"))
        assert listing.files == []  # no error is the point, not the result


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    async def test_video_metadata_includes_duration_and_resolution(
        self, drive: GoogleDriveProvider
    ) -> None:
        listing = await drive.search_files(SearchQuery(videos_only=True, page_size=5))
        if not listing.files:
            pytest.skip("no videos in this Drive")

        item = await drive.get_metadata(listing.files[0].id)
        assert item.is_video
        assert item.size_bytes and item.size_bytes > 0
        assert item.video_duration_ms, "Drive reported no duration"
        assert item.video_resolution, "Drive reported no resolution"

    async def test_a_nonexistent_id_is_not_found(
        self, drive: GoogleDriveProvider
    ) -> None:
        """Shape-valid but nonexistent: exercises the real 404 path."""
        with pytest.raises(ToolError) as caught:
            await drive.get_metadata("1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert caught.value.code == ErrorCode.NOT_FOUND
        assert caught.value.retryable is False

    async def test_a_malformed_id_fails_before_any_network_call(
        self, drive: GoogleDriveProvider
    ) -> None:
        with pytest.raises(ToolError) as caught:
            await drive.get_metadata("obviously not an id")
        assert caught.value.code == ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------------------
# Reading content
# ---------------------------------------------------------------------------


class TestReading:
    async def test_the_test_folder_contains_the_documented_fixtures(
        self, drive: GoogleDriveProvider, test_folder_id: str
    ) -> None:
        listing = await drive.list_files(folder_id=test_folder_id, page_size=25)
        names = {f.name for f in listing.files}
        assert any(n.endswith(".mp4") for n in names), "no video in the test folder"

    async def test_plain_text_is_readable(
        self, drive: GoogleDriveProvider, test_folder_id: str
    ) -> None:
        listing = await drive.list_files(folder_id=test_folder_id, page_size=25)
        text_files = [f for f in listing.files if (f.mime_type or "") == "text/plain"]
        if not text_files:
            pytest.skip("no .txt file in the test folder")

        content = await drive.read_file(text_files[0].id)
        assert content.text.strip()
        assert content.extraction == "decoded"
        assert content.bytes_read > 0

    async def test_pdf_text_is_extracted(
        self, drive: GoogleDriveProvider, test_folder_id: str
    ) -> None:
        """The path that crashed on first real use; now covered live."""
        listing = await drive.list_files(folder_id=test_folder_id, page_size=25)
        pdfs = [f for f in listing.files if (f.mime_type or "") == "application/pdf"]
        if not pdfs:
            pytest.skip("no PDF in the test folder")

        content = await drive.read_file(pdfs[0].id)
        assert content.extraction == "pdf_text"
        assert content.notice, "a PDF read should always carry a notice"

    async def test_a_binary_is_refused_with_guidance(
        self, drive: GoogleDriveProvider, test_folder_id: str
    ) -> None:
        listing = await drive.list_files(folder_id=test_folder_id, page_size=25)
        binaries = [
            f
            for f in listing.files
            if (f.mime_type or "").endswith("zip") or f.name.endswith(".zip")
        ]
        if not binaries:
            pytest.skip("no archive in the test folder")

        with pytest.raises(ToolError) as caught:
            await drive.read_file(binaries[0].id)
        assert caught.value.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE
        assert "get_file_metadata" in caught.value.remediation

    async def test_reading_a_folder_is_refused(
        self, drive: GoogleDriveProvider, test_folder_id: str
    ) -> None:
        with pytest.raises(ToolError) as caught:
            await drive.read_file(test_folder_id)
        assert caught.value.code == ErrorCode.INVALID_INPUT
        assert "folder" in caught.value.message.lower()

    async def test_max_bytes_truncates(
        self, drive: GoogleDriveProvider, test_folder_id: str
    ) -> None:
        listing = await drive.list_files(folder_id=test_folder_id, page_size=25)
        text_files = [
            f
            for f in listing.files
            if (f.mime_type or "") == "text/plain" and (f.size_bytes or 0) > 1024
        ]
        if not text_files:
            pytest.skip("no text file large enough to truncate")

        content = await drive.read_file(text_files[0].id, max_bytes=1024)
        assert content.truncated
        assert content.notice and "truncated" in content.notice.lower()
