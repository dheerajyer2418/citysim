"""Socrata access helpers for Chicago open-data datasets."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any


DEFAULT_PAGE_SIZE = 50_000


def fetch_paged(
    domain: str,
    dataset_id: str,
    *,
    where: str | None = None,
    select: str | None = None,
    page_size: int = 50_000,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages from a Socrata dataset using SOCRATA_APP_TOKEN when present.

    This record-oriented helper is retained for non-spatial datasets.
    """
    from sodapy import Socrata

    app_token = os.getenv("SOCRATA_APP_TOKEN")
    client = Socrata(domain, app_token)
    offset = 0
    try:
        while True:
            page = client.get(
                dataset_id,
                where=where,
                select=select,
                limit=page_size,
                offset=offset,
            )
            if not page:
                break
            yield page
            if len(page) < page_size:
                break
            offset += page_size
    finally:
        client.close()


def fetch_geojson(
    domain: str,
    dataset_id: str,
    where: str | None = None,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
):
    """Fetch a Socrata GeoJSON dataset into a GeoDataFrame.

    Uses the SODA ``.geojson`` endpoint with optional ``$where`` and paging.
    Network access happens only when callers invoke this function.
    """
    import geopandas as gpd
    import requests

    headers = {}
    app_token = os.getenv("SOCRATA_APP_TOKEN")
    if app_token:
        headers["X-App-Token"] = app_token

    endpoint = f"https://{domain}/resource/{dataset_id}.geojson"
    features: list[dict[str, Any]] = []
    offset = 0

    while True:
        params: dict[str, Any] = {"$limit": page_size, "$offset": offset}
        if where:
            params["$where"] = where
        response = requests.get(endpoint, params=params, headers=headers, timeout=60)
        response.raise_for_status()
        payload = response.json()
        page_features = payload.get("features", [])
        features.extend(page_features)
        if len(page_features) < page_size:
            break
        offset += page_size

    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
