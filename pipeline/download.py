"""Cached HTTPS downloader stubs for data/raw."""

from __future__ import annotations

from pathlib import Path


def download_to_raw(url: str, destination: Path, *, overwrite: bool = False) -> Path:
    """Download an HTTPS file into data/raw with simple cache semantics.

    TODO: implement streaming requests download, checksum validation, and retry policy.
    """
    if destination.exists() and not overwrite:
        return destination
    raise NotImplementedError(f"TODO: download {url} to {destination}")
