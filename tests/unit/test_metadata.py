"""Metadata validation: everything that must fail before bytes move."""

from __future__ import annotations

import pytest

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.youtube.metadata import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    privacy_mismatch_error,
    validate,
)


class TestTitle:
    def test_whitespace_is_trimmed(self) -> None:
        assert validate(title="  Q4 Demo  ").title == "Q4 Demo"

    @pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
    def test_empty_titles_are_rejected(self, bad: str) -> None:
        with pytest.raises(ToolError) as caught:
            validate(title=bad)
        assert caught.value.code == ErrorCode.INVALID_INPUT

    def test_at_the_limit_is_allowed(self) -> None:
        assert len(validate(title="x" * MAX_TITLE_CHARS).title) == MAX_TITLE_CHARS

    def test_one_over_is_rejected_and_says_by_how_much(self) -> None:
        with pytest.raises(ToolError) as caught:
            validate(title="x" * (MAX_TITLE_CHARS + 7))
        assert "7 too long" in caught.value.message

    def test_angle_brackets_are_rejected(self) -> None:
        with pytest.raises(ToolError):
            validate(title="Demo <script>")


class TestDescription:
    def test_none_becomes_empty(self) -> None:
        assert validate(title="t", description=None).description == ""

    def test_over_the_limit_is_rejected(self) -> None:
        with pytest.raises(ToolError) as caught:
            validate(title="t", description="x" * (MAX_DESCRIPTION_CHARS + 1))
        assert caught.value.code == ErrorCode.INVALID_INPUT


class TestTags:
    def test_duplicates_are_dropped_not_rejected(self) -> None:
        """De-duplicating is kinder than failing, and changes nothing semantically."""
        assert validate(title="t", tags=["demo", "Demo", "DEMO"]).tags == ["demo"]

    def test_blank_tags_are_dropped(self) -> None:
        assert validate(title="t", tags=["a", "  ", ""]).tags == ["a"]

    def test_aggregate_length_is_enforced(self) -> None:
        with pytest.raises(ToolError) as caught:
            validate(title="t", tags=[f"tag-number-{i:03d}-padding" * 2 for i in range(40)])
        assert "across all tags combined" in caught.value.message

    def test_non_string_tag_is_a_clear_error(self) -> None:
        with pytest.raises(ToolError) as caught:
            validate(title="t", tags=["ok", 42])  # type: ignore[list-item]
        assert "must be strings" in caught.value.message


class TestPrivacy:
    @pytest.mark.parametrize("value", ["public", "private", "unlisted"])
    def test_valid_values(self, value: str) -> None:
        assert validate(title="t", privacy_status=value).privacy_status == value

    def test_case_is_normalised(self) -> None:
        assert validate(title="t", privacy_status="UNLISTED").privacy_status == "unlisted"

    @pytest.mark.parametrize("value", ["secret", "hidden", "friends", "PUBLIC-ish"])
    def test_invalid_values_list_the_allowed_ones(self, value: str) -> None:
        with pytest.raises(ToolError) as caught:
            validate(title="t", privacy_status=value)
        assert caught.value.details["allowed"] == ["private", "public", "unlisted"]

    def test_an_empty_value_falls_back_to_the_safe_default(self) -> None:
        """A model that sends "" should get private, not an error.

        Refusing would be pedantic, and guessing "public" would be dangerous.
        Falling back to the most restrictive option is the only safe reading.
        """
        assert validate(title="t", privacy_status="").privacy_status == "private"

    def test_the_default_is_the_safe_one(self) -> None:
        assert validate(title="t").privacy_status == "private"


class TestRequestBody:
    def test_body_has_the_shape_youtube_expects(self) -> None:
        body = validate(
            title="Q4", description="d", tags=["x"], privacy_status="unlisted"
        ).to_request_body()
        assert body["snippet"]["title"] == "Q4"
        assert body["status"]["privacyStatus"] == "unlisted"
        assert body["status"]["selfDeclaredMadeForKids"] is False


class TestPrivacyMismatch:
    def test_it_is_not_retryable_and_keeps_the_link(self) -> None:
        error = privacy_mismatch_error(
            "unlisted", "private", "abc", "https://youtu.be/abc"
        )
        assert error.code == ErrorCode.PRIVACY_NOT_APPLIED
        assert error.retryable is False
        assert "https://youtu.be/abc" in error.remediation
        assert "audit" in error.remediation
