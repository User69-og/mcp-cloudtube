"""Validation of YouTube video metadata, performed before any bytes move.

Ordering matters more than the rules themselves. YouTube validates metadata
when the upload *finishes*, so a 101-character title on a 2 GB file is
discovered after the whole file has been staged from Drive and sent to
YouTube -- several minutes and 1600 quota units spent to learn something that
was knowable instantly. Every rule here is checked up front for that reason.

Limits are YouTube's documented ones. Where YouTube is merely strict and we
can be helpful instead, we are: a title that is too long produces a message
saying by how much, and tags are de-duplicated rather than rejected.
"""

from __future__ import annotations

import re
from typing import Any, Final

from ..errors import ErrorCode, ToolError, invalid_input

#: YouTube's documented field limits.
MAX_TITLE_CHARS: Final = 100
MAX_DESCRIPTION_CHARS: Final = 5000
MAX_TAGS_TOTAL_CHARS: Final = 500
MAX_TAG_CHARS: Final = 100

VALID_PRIVACY: Final[frozenset[str]] = frozenset({"public", "private", "unlisted"})

#: Characters YouTube rejects outright in titles and descriptions.
_FORBIDDEN = re.compile(r"[<>]")

#: Category IDs that exist on every YouTube channel. Not exhaustive -- the
#: full list is region-dependent -- but enough to validate the common ones and
#: to suggest something sensible when the model guesses.
COMMON_CATEGORIES: Final[dict[str, str]] = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
}

#: Sensible default for a product demo, which is what this server is most
#: likely to be publishing.
DEFAULT_CATEGORY_ID: Final = "28"


class ValidatedMetadata:
    """Normalised, YouTube-ready metadata."""

    def __init__(
        self,
        *,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str,
        category_id: str,
        made_for_kids: bool,
    ) -> None:
        self.title = title
        self.description = description
        self.tags = tags
        self.privacy_status = privacy_status
        self.category_id = category_id
        self.made_for_kids = made_for_kids

    def to_request_body(self) -> dict[str, Any]:
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
                "categoryId": self.category_id,
            },
            "status": {
                "privacyStatus": self.privacy_status,
                "selfDeclaredMadeForKids": self.made_for_kids,
                # Without this, YouTube applies the channel default, which on a
                # fresh channel can be "no embedding" and surprises people.
                "embeddable": True,
            },
        }


def validate(
    *,
    title: str,
    description: str | None = None,
    tags: list[str] | None = None,
    privacy_status: str = "private",
    category_id: str | None = None,
    made_for_kids: bool = False,
) -> ValidatedMetadata:
    """Validate and normalise. Raises `ToolError(INVALID_INPUT)` on bad input."""

    # -- title ----------------------------------------------------------------
    if not isinstance(title, str) or not title.strip():
        raise invalid_input(
            "A video title is required and cannot be empty.",
            hint="Ask the user what the video should be called.",
        )
    title = title.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise invalid_input(
            f"The title is {len(title)} characters; YouTube allows {MAX_TITLE_CHARS}. "
            f"It is {len(title) - MAX_TITLE_CHARS} too long.",
            hint="Shorten the title and call the tool again. Nothing was uploaded.",
            title_length=len(title),
            limit=MAX_TITLE_CHARS,
        )
    if _FORBIDDEN.search(title):
        raise invalid_input(
            "The title contains '<' or '>', which YouTube rejects.",
            hint="Remove the angle brackets and try again.",
        )

    # -- description -----------------------------------------------------------
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise invalid_input(
            f"The description is {len(description)} characters; YouTube allows "
            f"{MAX_DESCRIPTION_CHARS}.",
            hint="Shorten it and call the tool again. Nothing was uploaded.",
            description_length=len(description),
            limit=MAX_DESCRIPTION_CHARS,
        )
    if _FORBIDDEN.search(description):
        raise invalid_input(
            "The description contains '<' or '>', which YouTube rejects.",
            hint="Remove the angle brackets and try again.",
        )

    # -- tags --------------------------------------------------------------------
    cleaned_tags: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        if not isinstance(raw, str):
            raise invalid_input(
                f"Tags must be strings, but one was a {type(raw).__name__}.",
                hint='Pass tags as a list of strings, e.g. ["demo", "product"].',
            )
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_CHARS:
            raise invalid_input(
                f"The tag {tag[:40]!r}... is {len(tag)} characters; the limit per tag is "
                f"{MAX_TAG_CHARS}.",
            )
        key = tag.casefold()
        if key in seen:
            continue  # silently de-duplicate; not worth failing over
        seen.add(key)
        cleaned_tags.append(tag)

    # YouTube counts the aggregate length, including the quoting it applies to
    # tags containing spaces. Approximated the same way YouTube does.
    total = sum(len(t) + (2 if " " in t else 0) for t in cleaned_tags) + max(
        0, len(cleaned_tags) - 1
    )
    if total > MAX_TAGS_TOTAL_CHARS:
        raise invalid_input(
            f"The tags total about {total} characters; YouTube allows "
            f"{MAX_TAGS_TOTAL_CHARS} across all tags combined.",
            hint="Drop some tags and try again. Nothing was uploaded.",
            tags_total_chars=total,
            limit=MAX_TAGS_TOTAL_CHARS,
            tag_count=len(cleaned_tags),
        )

    # -- privacy ------------------------------------------------------------------
    privacy = (privacy_status or "private").strip().lower()
    if privacy not in VALID_PRIVACY:
        raise invalid_input(
            f"{privacy_status!r} is not a valid privacy status.",
            hint="Use exactly one of: public, private, unlisted.",
            allowed=sorted(VALID_PRIVACY),
        )

    # -- category -------------------------------------------------------------------
    category = (category_id or DEFAULT_CATEGORY_ID).strip()
    if not category.isdigit():
        raise invalid_input(
            f"{category_id!r} is not a valid YouTube category ID; they are numeric strings.",
            hint=(
                "Omit category_id to use the default, or pick one: "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in sorted(COMMON_CATEGORIES.items(), key=lambda p: int(p[0]))
                )
            ),
        )

    return ValidatedMetadata(
        title=title,
        description=description,
        tags=cleaned_tags,
        privacy_status=privacy,
        category_id=category,
        made_for_kids=bool(made_for_kids),
    )


def privacy_mismatch_error(
    requested: str, applied: str, video_id: str, video_url: str
) -> ToolError:
    """Built when YouTube silently applies a different privacy than requested.

    This is the expected outcome for any API project that has not passed
    YouTube's compliance audit: uploads are forced to private and locked. It is
    reported as an error rather than folded into a success, because a user told
    "uploaded as unlisted" when the video is actually private has been given
    materially wrong information about who can see it.
    """
    return ToolError(
        ErrorCode.PRIVACY_NOT_APPLIED,
        f"The video uploaded successfully, but YouTube set its privacy to {applied!r} "
        f"rather than the requested {requested!r}.",
        remediation=(
            f"The video is live at {video_url} and is {applied}. This happens when the Google "
            "Cloud project has not completed YouTube's API compliance audit: uploads from an "
            "unaudited project are locked to private regardless of what the request asks for. "
            f"Tell the user the video is {applied}, not {requested}, and that changing it "
            "requires either the audit or editing the video by hand in YouTube Studio."
        ),
        retryable=False,
        details={
            "video_id": video_id,
            "video_url": video_url,
            "requested_privacy": requested,
            "applied_privacy": applied,
        },
    )
