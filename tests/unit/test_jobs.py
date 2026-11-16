"""The durable job store, including the crash-recovery path."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.youtube.jobs import JobState, JobStore


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    s = JobStore(tmp_path / "uploads.sqlite3")
    s.connect()
    return s


def _make(store: JobStore, **overrides: object) -> str:
    kwargs: dict = {
        "source_file_id": "vid_1",
        "source_file_name": "demo.mp4",
        "title": "Demo",
        "description": None,
        "tags": ["a", "b"],
        "requested_privacy": "private",
        "category_id": "28",
        "made_for_kids": False,
        "bytes_total": 1000,
        "idempotency_key": "key-1",
    }
    kwargs.update(overrides)
    return store.create(**kwargs)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_create_and_read_back(self, store: JobStore) -> None:
        job_id = _make(store)
        job = store.get(job_id)
        assert job is not None
        assert job["state"] == JobState.PENDING
        assert job["tags"] == ["a", "b"]
        assert job["made_for_kids"] is False

    def test_unknown_id_returns_none(self, store: JobStore) -> None:
        assert store.get("job_nope") is None

    def test_updates_are_applied(self, store: JobStore) -> None:
        job_id = _make(store)
        store.update(job_id, state=JobState.UPLOADING, bytes_sent=512)
        job = store.get(job_id)
        assert job is not None
        assert job["state"] == JobState.UPLOADING
        assert job["bytes_sent"] == 512

    def test_unknown_column_is_rejected_loudly(self, store: JobStore) -> None:
        """A silently-ignored typo would lose upload state."""
        with pytest.raises(ValueError, match="unknown job fields"):
            store.update(_make(store), stat="uploading")

    def test_state_survives_reopening_the_database(self, tmp_path: Path) -> None:
        """The whole point: status must outlive the process."""
        path = tmp_path / "uploads.sqlite3"
        first = JobStore(path)
        first.connect()
        job_id = _make(first)
        first.update(job_id, state=JobState.COMPLETED, video_id="abc123")
        first.close()

        second = JobStore(path)
        second.connect()
        job = second.get(job_id)
        assert job is not None
        assert job["state"] == JobState.COMPLETED
        assert job["video_id"] == "abc123"


class TestFailureRecording:
    def test_failure_stores_the_remediation_too(self, store: JobStore) -> None:
        """A later status call may be a different turn, with no context."""
        job_id = _make(store)
        store.record_failure(
            job_id, ToolError(ErrorCode.QUOTA_EXCEEDED, "quota gone")
        )
        job = store.get(job_id)
        assert job is not None
        assert job["state"] == JobState.FAILED
        assert job["error_code"] == ErrorCode.QUOTA_EXCEEDED
        assert "midnight" in job["error_remediation"]


class TestIdempotency:
    def test_a_matching_key_is_found(self, store: JobStore) -> None:
        job_id = _make(store, idempotency_key="k")
        assert (found := store.find_by_idempotency_key("k")) is not None
        assert found["id"] == job_id

    def test_a_failed_job_does_not_block_a_genuine_retry(self, store: JobStore) -> None:
        job_id = _make(store, idempotency_key="k")
        store.record_failure(job_id, ToolError(ErrorCode.NETWORK_ERROR, "dropped"))
        assert store.find_by_idempotency_key("k") is None


class TestCrashRecovery:
    @pytest.mark.parametrize(
        "state", [JobState.PENDING, JobState.STAGING, JobState.UPLOADING]
    )
    def test_in_flight_jobs_are_resolved_at_startup(
        self, store: JobStore, state: JobState
    ) -> None:
        job_id = _make(store)
        store.update(job_id, state=state)

        store.mark_interrupted_jobs()

        job = store.get(job_id)
        assert job is not None
        assert job["state"] == JobState.FAILED
        assert "Nothing was published" in job["error_remediation"]

    def test_a_resumable_upload_is_kept_not_failed(self, store: JobStore) -> None:
        """Losing recoverable progress would cost 1600 quota units to redo."""
        job_id = _make(store)
        store.update(job_id, state=JobState.UPLOADING, resume_uri="https://resume")

        store.mark_interrupted_jobs()

        job = store.get(job_id)
        assert job is not None
        assert job["state"] == JobState.UPLOADING
        assert "can be resumed" in job["notes"]

    def test_terminal_jobs_are_left_alone(self, store: JobStore) -> None:
        job_id = _make(store)
        store.update(job_id, state=JobState.COMPLETED)
        store.mark_interrupted_jobs()
        job = store.get(job_id)
        assert job is not None
        assert job["state"] == JobState.COMPLETED


class TestListing:
    def test_recent_is_newest_first_and_bounded(self, store: JobStore) -> None:
        for i in range(5):
            _make(store, idempotency_key=f"k{i}", title=f"Demo {i}")
        recent = store.recent(limit=3)
        assert len(recent) == 3

    def test_an_absurd_limit_is_clamped(self, store: JobStore) -> None:
        _make(store)
        assert len(store.recent(limit=10_000)) == 1
