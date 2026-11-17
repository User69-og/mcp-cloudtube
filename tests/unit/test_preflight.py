"""The dependency preflight.

It exists for one failure: the Claude Desktop extension defaults its
interpreter to whatever `python` is on PATH, which is usually not the one
holding this project's dependencies. The process then dies during import and
the app reports only "couldn't start".

The constraint that shapes this module is that it must import nothing
third-party, since third-party imports are precisely what fails.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from mcp_cloudtube._preflight import REQUIRED, check_dependencies, missing_dependencies


class TestImportHygiene:
    def test_it_imports_only_the_standard_library(self) -> None:
        """A third-party import here would defeat the entire purpose."""
        source = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "mcp_cloudtube" / "_preflight.py"
        ).read_text(encoding="utf-8")

        stdlib = {"importlib", "importlib.util", "sys", "__future__"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in {m.split(".")[0] for m in stdlib}, (
                        f"_preflight imports {alias.name}, which may not be installed"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] in {m.split(".")[0] for m in stdlib}, (
                    f"_preflight imports from {node.module}, which may not be installed"
                )

    def test_it_runs_before_the_package_is_importable(self) -> None:
        """It must not pull in the rest of mcp_cloudtube, which imports mcp."""
        source = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "mcp_cloudtube" / "_preflight.py"
        ).read_text(encoding="utf-8")
        assert "from ." not in source
        assert "from mcp_cloudtube" not in source


class TestDetection:
    def test_nothing_is_missing_in_a_working_environment(self) -> None:
        assert missing_dependencies() == []

    def test_check_passes_silently_when_satisfied(self) -> None:
        check_dependencies()  # must not raise or print

    def test_every_runtime_dependency_is_covered(self) -> None:
        """Keeps REQUIRED in step with pyproject, so a new dep is not missed."""
        pyproject = (
            pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
        ).read_text(encoding="utf-8")
        block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
        declared = {
            line.split('"')[1].split(">")[0].split("=")[0].strip()
            for line in block.splitlines()
            if '"' in line
        }
        assert declared == set(REQUIRED.values()), (
            "pyproject dependencies and _preflight.REQUIRED have drifted apart"
        )


class TestReporting:
    def test_a_missing_dependency_exits_with_guidance(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            "mcp_cloudtube._preflight.missing_dependencies", lambda: ["mcp", "structlog"]
        )

        with pytest.raises(SystemExit) as caught:
            check_dependencies()

        assert caught.value.code == 3
        err = capsys.readouterr().err
        assert "mcp, structlog" in err
        assert "Python interpreter" in err
        assert ".venv" in err

    def test_the_message_names_the_offending_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without this the user cannot tell which Python was even used."""
        import sys

        monkeypatch.setattr(
            "mcp_cloudtube._preflight.missing_dependencies", lambda: ["mcp"]
        )
        with pytest.raises(SystemExit):
            check_dependencies()
        assert sys.executable in capsys.readouterr().err

    def test_guidance_goes_to_stderr_not_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stdout is the JSON-RPC channel; writing there corrupts the transport."""
        monkeypatch.setattr(
            "mcp_cloudtube._preflight.missing_dependencies", lambda: ["mcp"]
        )
        with pytest.raises(SystemExit):
            check_dependencies()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err
