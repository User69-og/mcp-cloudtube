"""Chooses the storage provider named in configuration.

One function, so that adding a provider touches this file and nothing else.
The error for an unknown name lists the valid ones, because an operator who
typos `CLOUDTUBE_STORAGE_PROVIDER` should not have to read the source to find
out what was expected.
"""

from __future__ import annotations

from ..auth.google_oauth import GoogleAuthManager
from ..config import Settings
from ..errors import config_error
from .base import StorageProvider

_PROVIDERS = {"gdrive"}


def build_provider(settings: Settings, auth: GoogleAuthManager) -> StorageProvider:
    name = settings.storage_provider

    if name == "gdrive":
        from .drive import GoogleDriveProvider

        return GoogleDriveProvider(auth, settings)

    raise config_error(
        f"Unknown storage provider {name!r}.",
        supported=sorted(_PROVIDERS),
    )
