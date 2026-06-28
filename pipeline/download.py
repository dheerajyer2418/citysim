"""Cached HTTPS download and extraction helpers."""

from __future__ import annotations

from urllib.parse import urlparse
from zipfile import ZipFile
from pathlib import Path


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    if not name:
        raise ValueError(f"Cannot infer a filename from URL: {url}")
    return name


def download(url: str, dest_dir: str | Path) -> Path:
    """Download a URL into dest_dir, reusing a non-empty cached file.

    The caller is responsible for choosing a project-local destination such as
    ``cfg.data_raw``. This function performs network I/O only when the cached
    file is missing or empty.
    """
    import requests

    destination_dir = Path(dest_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / _filename_from_url(url)
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    tmp_destination = destination.with_suffix(destination.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with tmp_destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    tmp_destination.replace(destination)
    return destination


def download_and_extract_zip(url: str, dest_dir: str | Path) -> Path:
    """Download a ZIP and extract it under data/interim, returning that folder.

    If ``dest_dir`` is ``data/raw``, extraction goes to sibling
    ``data/interim/<zip-stem>``. Otherwise extraction goes to
    ``dest_dir/<zip-stem>``.
    """
    raw_dir = Path(dest_dir)
    zip_path = download(url, raw_dir)
    extract_root = raw_dir.parent / "interim" if raw_dir.name == "raw" else raw_dir
    extract_dir = extract_root / zip_path.stem
    marker = extract_dir / ".extract_complete"

    if marker.exists() and any(extract_dir.iterdir()):
        return extract_dir

    extract_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path) as zip_file:
        zip_file.extractall(extract_dir)
    marker.write_text(f"Extracted from {zip_path.name}\n", encoding="utf-8")
    return extract_dir


def download_to_raw(url: str, destination: Path, *, overwrite: bool = False) -> Path:
    """Backward-compatible helper for downloading to an explicit path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0 and not overwrite:
        return destination

    downloaded = download(url, destination.parent)
    if downloaded != destination:
        downloaded.replace(destination)
    return destination
