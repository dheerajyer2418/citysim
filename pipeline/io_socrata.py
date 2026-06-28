"""Socrata access helpers for Chicago open-data datasets."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any


def fetch_paged(
    domain: str,
    dataset_id: str,
    *,
    where: str | None = None,
    select: str | None = None,
    page_size: int = 50_000,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages from a Socrata dataset using SODAPY_APP_TOKEN when present.

    TODO: instantiate sodapy.Socrata, pass optional query clauses, and page until empty.
    """
    app_token = os.getenv("SODAPY_APP_TOKEN")
    _ = (domain, dataset_id, where, select, page_size, app_token)
    raise NotImplementedError("TODO: fetch paged Socrata records")
