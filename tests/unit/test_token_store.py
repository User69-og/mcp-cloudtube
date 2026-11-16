"""Credential persistence: atomicity, corruption, and never crashing."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_cloudtube.auth.token_store import DEFAULT_ACCOUNT, TokenStore
from mcp_cloudtube.errors import ErrorCode, ToolError

SAMPLE = {
    "token": "test-access-value",
    "refresh_token": "test-refresh-value",
    "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
}


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "token.json")


class TestRoundTrip:
    def test_absent_before_saving(self, store: TokenStore) -> None:
        assert store.exists() is False
        assert store.load() is None

    def test_save_then_load(self, store: TokenStore) -> None:
        store.save(SAMPLE)
        assert store.load() == SAMPLE
        assert store.accounts() == [DEFAULT_ACCOUNT]

    def test_delete(self, store: TokenStore) -> None:
        store.save(SAMPLE)
        assert store.delete() is True
        assert store.load() is None

    def test_deleting_nothing_is_not_an_error(self, store: TokenStore) -> None:
        assert store.delete() is False

    def test_accounts_are_kept_separate(self, store: TokenStore) -> None:
        store.save(SAMPLE, "alice")
        store.save({**SAMPLE, "token": "other"}, "bob")
        assert store.accounts() == ["alice", "bob"]
        assert store.load("bob")["token"] == "other"  # type: ignore[index]


class TestResilience:
    def test_a_corrupt_file_asks_for_re_authorisation(self, store: TokenStore) -> None:
        """A truncated write must be recoverable, not fatal on every call."""
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text('{"default": {"tok', encoding="utf-8")

        with pytest.raises(ToolError) as caught:
            store.load()

        assert caught.value.code == ErrorCode.AUTH_REQUIRED
        assert "connect_account" in caught.value.remediation

    def test_an_empty_file_reads_as_no_accounts(self, store: TokenStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("", encoding="utf-8")
        assert store.load() is None

    def test_a_legacy_single_account_file_is_still_readable(self, store: TokenStore) -> None:
        """An upgrade must not silently sign the user out."""
        import json

        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(SAMPLE), encoding="utf-8")
        assert store.load() == SAMPLE

    def test_writing_leaves_no_temp_files_behind(self, store: TokenStore) -> None:
        store.save(SAMPLE)
        leftovers = [p for p in store.path.parent.iterdir() if p.name.startswith(".token-")]
        assert leftovers == []

    def test_a_save_replaces_rather_than_appends(self, store: TokenStore) -> None:
        store.save(SAMPLE)
        store.save({**SAMPLE, "token": "second"})
        assert store.load()["token"] == "second"  # type: ignore[index]
