"""Finding the OAuth client secrets file.

The Claude Desktop extension launches the server with an unpredictable
working directory, so a relative default path is no use to it. Rather than
force every installer to fill in a file picker, the server looks in several
conventional places. These tests pin that behaviour, including the two edge
cases that make a naive implementation wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_cloudtube.config import Settings
from mcp_cloudtube.errors import ErrorCode, ToolError

CONTENT = json.dumps({"installed": {"client_id": "test-only", "client_secret": "not-real"}})


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real developer machine out of these tests.

    Two of the fallback locations are relative to the current directory and
    the home directory. Without isolating both, a run from a checkout that
    happens to contain .secrets/client_secret.json resolves to the real file
    and the "nothing found" cases never fire -- which is exactly what happened
    the first time these were run.
    """
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(fake_home))
    monkeypatch.delenv("CLOUDTUBE_CLIENT_SECRET_FILE", raising=False)


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path / "state", **kwargs)  # type: ignore[arg-type]


class TestResolution:
    def test_the_configured_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit.json"
        explicit.write_text(CONTENT, encoding="utf-8")
        (tmp_path / "state").mkdir(parents=True)
        (tmp_path / "state" / "client_secret.json").write_text(CONTENT, encoding="utf-8")

        settings = _settings(tmp_path, client_secret_file=explicit)
        assert settings.resolve_client_secret() == explicit

    def test_falls_back_to_the_state_directory(self, tmp_path: Path) -> None:
        """What makes the extension's file picker optional."""
        state = tmp_path / "state"
        state.mkdir(parents=True)
        fallback = state / "client_secret.json"
        fallback.write_text(CONTENT, encoding="utf-8")

        settings = _settings(tmp_path, client_secret_file=tmp_path / "absent.json")
        assert settings.resolve_client_secret() == fallback

    def test_an_empty_setting_is_ignored(self, tmp_path: Path) -> None:
        """An unfilled extension field arrives as "", and Path("") is the cwd.

        A naive exists() check would resolve the credentials file to the
        current directory, which exists and is not a credentials file.
        """
        state = tmp_path / "state"
        state.mkdir(parents=True)
        (state / "client_secret.json").write_text(CONTENT, encoding="utf-8")

        settings = _settings(tmp_path, client_secret_file=Path(""))
        assert settings.resolve_client_secret() == state / "client_secret.json"

    def test_a_directory_is_never_accepted(self, tmp_path: Path) -> None:
        a_directory = tmp_path / "looks-like-a-file.json"
        a_directory.mkdir()
        settings = _settings(tmp_path, client_secret_file=a_directory)

        with pytest.raises(ToolError):
            settings.resolve_client_secret()

    def test_an_empty_file_is_skipped(self, tmp_path: Path) -> None:
        """A zero-byte download is a failed download, not a credential."""
        truncated = tmp_path / "truncated.json"
        truncated.write_text("", encoding="utf-8")
        state = tmp_path / "state"
        state.mkdir(parents=True)
        good = state / "client_secret.json"
        good.write_text(CONTENT, encoding="utf-8")

        settings = _settings(tmp_path, client_secret_file=truncated)
        assert settings.resolve_client_secret() == good


class TestWhenNothingIsFound:
    def test_the_error_names_every_location_tried(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, client_secret_file=tmp_path / "absent.json")

        with pytest.raises(ToolError) as caught:
            settings.resolve_client_secret()

        assert caught.value.code == ErrorCode.CONFIG_ERROR
        searched = caught.value.details["searched"]
        assert len(searched) >= 3
        for path in searched:
            assert path in caught.value.remediation

    def test_the_remediation_says_how_to_get_the_file(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, client_secret_file=tmp_path / "absent.json")
        with pytest.raises(ToolError) as caught:
            settings.resolve_client_secret()

        remediation = caught.value.remediation
        assert "Google Cloud Console" in remediation
        assert "SETUP_GOOGLE_CLOUD.md" in remediation

    def test_candidates_are_deduplicated(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        settings = _settings(tmp_path, client_secret_file=state / "client_secret.json")
        candidates = settings.candidate_client_secret_paths()
        assert len(candidates) == len(set(candidates))
