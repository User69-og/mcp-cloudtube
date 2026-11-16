"""YouTube publishing: metadata validation, resumable upload, job tracking."""

from .client import YouTubeClient, YouTubeTransport
from .jobs import JobState, JobStore
from .metadata import validate
from .uploader import UploadCoordinator

__all__ = [
    "JobState",
    "JobStore",
    "UploadCoordinator",
    "YouTubeClient",
    "YouTubeTransport",
    "validate",
]
