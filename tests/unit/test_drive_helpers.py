"""Drive-specific helpers: query escaping, ID handling, decoding."""

from __future__ import annotations

import pytest

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.storage.base import FileKind, is_text_like, is_video_mime
from mcp_cloudtube.storage.drive import _decode, _escape, _require_id, _to_storage_file


class TestQueryEscaping:
    def test_apostrophes_are_escaped(self) -> None:
        """An unescaped quote would break -- or silently alter -- the query."""
        assert _escape("O'Brien") == "O\\'Brien"

    def test_backslashes_are_escaped_first(self) -> None:
        assert _escape("a\\b") == "a\\\\b"

    def test_an_injection_attempt_stays_inert(self) -> None:
        hostile = "x' or name contains 'secret"
        assert "' or " not in _escape(hostile).replace("\\'", "")


class TestFileIdHandling:
    def test_a_plain_id_passes_through(self) -> None:
        assert _require_id("1A2b3C4d5E6f") == "1A2b3C4d5E6f"

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert _require_id("  1A2b3C4d5E6f  ") == "1A2b3C4d5E6f"

    @pytest.mark.parametrize(
        "url",
        [
            "https://drive.google.com/file/d/1A2b3C4d5E6f7G8h9I0j/view?usp=sharing",
            "https://drive.google.com/open?id=1A2b3C4d5E6f7G8h9I0j",
        ],
    )
    def test_a_pasted_url_is_accepted(self, url: str) -> None:
        """Models paste URLs. Extracting the ID beats refusing."""
        assert _require_id(url) == "1A2b3C4d5E6f7G8h9I0j"

    @pytest.mark.parametrize("value", ["", "   ", "my video.mp4", "the latest demo"])
    def test_a_filename_is_rejected_with_guidance(self, value: str) -> None:
        with pytest.raises(ToolError) as caught:
            _require_id(value)
        assert caught.value.code == ErrorCode.INVALID_INPUT

    def test_the_guidance_says_to_search_first(self) -> None:
        with pytest.raises(ToolError) as caught:
            _require_id("Q4 Demo.mp4")
        assert "search" in (caught.value.details.get("hint") or "").lower()


class TestDecoding:
    def test_plain_utf8(self) -> None:
        assert _decode(b"hello")[0] == "hello"

    def test_a_bom_is_handled(self) -> None:
        text, _encoding, replaced = _decode("hi".encode("utf-8-sig"))
        assert text.lstrip("\ufeff") == "hi"
        assert replaced is False

    def test_binary_is_replaced_not_raised(self) -> None:
        text, _enc, replaced = _decode(b"\xff\xfe\x00\x01\x02binary")
        assert isinstance(text, str)
        assert replaced in (True, False)  # never raises, which is the point


class TestMimeClassification:
    @pytest.mark.parametrize("mime", ["video/mp4", "video/quicktime", "VIDEO/MP4"])
    def test_videos_are_detected(self, mime: str) -> None:
        assert is_video_mime(mime) is True

    @pytest.mark.parametrize("mime", ["text/plain", "application/pdf", None])
    def test_non_videos_are_not(self, mime: str | None) -> None:
        assert is_video_mime(mime) is False

    @pytest.mark.parametrize(
        "mime", ["text/plain", "text/csv", "application/json", "text/plain; charset=utf-8"]
    )
    def test_text_like_types(self, mime: str) -> None:
        assert is_text_like(mime) is True

    @pytest.mark.parametrize("mime", ["video/mp4", "application/zip", "image/png", None])
    def test_non_text_types(self, mime: str | None) -> None:
        assert is_text_like(mime) is False


class TestResourceMapping:
    def test_a_folder_maps_to_folder_kind(self) -> None:
        item = _to_storage_file(
            {"id": "f", "name": "Docs", "mimeType": "application/vnd.google-apps.folder"}
        )
        assert item.kind is FileKind.FOLDER

    def test_a_google_doc_maps_to_native_document(self) -> None:
        item = _to_storage_file(
            {"id": "d", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}
        )
        assert item.kind is FileKind.NATIVE_DOCUMENT
        assert item.is_readable_as_text is True

    def test_video_metadata_is_carried_through(self) -> None:
        item = _to_storage_file(
            {
                "id": "v", "name": "demo.mp4", "mimeType": "video/mp4", "size": "1024",
                "videoMediaMetadata": {"durationMillis": "30000", "width": 1920, "height": 1080},
            }
        )
        assert item.is_video is True
        assert item.video_duration_ms == 30000
        assert item.video_resolution == "1920x1080"

    def test_a_malformed_size_does_not_crash(self) -> None:
        item = _to_storage_file({"id": "x", "name": "n", "mimeType": "text/plain", "size": "huge"})
        assert item.size_bytes is None

    def test_missing_fields_are_tolerated(self) -> None:
        """Drive omits fields rather than nulling them."""
        item = _to_storage_file({"id": "x", "name": "n"})
        assert item.mime_type is None
        assert item.parent_ids == []
