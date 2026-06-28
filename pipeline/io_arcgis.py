"""ArcGIS REST GeoJSON fetch helpers."""

from __future__ import annotations

import json
from typing import Any


def _envelope_param(geometry_envelope: tuple[float, float, float, float] | None) -> str | None:
    if geometry_envelope is None:
        return None
    xmin, ymin, xmax, ymax = geometry_envelope
    return json.dumps(
        {
            "xmin": xmin,
            "ymin": ymin,
            "xmax": xmax,
            "ymax": ymax,
            "spatialReference": {"wkid": 4326},
        }
    )


def _exceeded_transfer_limit(payload: dict[str, Any]) -> bool:
    if bool(payload.get("exceededTransferLimit")):
        return True
    properties = payload.get("properties")
    return bool(isinstance(properties, dict) and properties.get("exceededTransferLimit"))


def fetch_arcgis_geojson(
    query_url: str,
    *,
    geometry_envelope: tuple[float, float, float, float] | None = None,
    where: str = "1=1",
    out_fields: str = "*",
):
    """Fetch a paged ArcGIS REST query endpoint as a GeoDataFrame.

    The helper requests GeoJSON in EPSG:4326 and keeps paging with
    ``resultOffset`` until the server stops returning a capped page.
    """
    import geopandas as gpd
    import requests

    page_size = 2000
    offset = 0
    features: list[dict[str, Any]] = []
    envelope = _envelope_param(geometry_envelope)

    while True:
        params: dict[str, Any] = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        if envelope is not None:
            params.update(
                {
                    "geometry": envelope,
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                }
            )

        response = requests.get(query_url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        page_features = payload.get("features", [])
        features.extend(page_features)

        if not page_features:
            break
        if len(page_features) < page_size and not _exceeded_transfer_limit(payload):
            break
        offset += len(page_features)

    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
