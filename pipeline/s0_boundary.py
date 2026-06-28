"""Stage s0: boundary and TAZ preprocessing."""

from __future__ import annotations

from pathlib import Path

from pipeline.crosswalk import detect_taz_id_column
from pipeline.download import download_and_extract_zip, download_to_raw
from pipeline.io_socrata import fetch_geojson


BOUNDARY_OUTPUT = "logan_square_boundary.gpkg"
TAZ_OUTPUT = "logan_square_taz.gpkg"


def _find_shapefile(extract_dir: Path) -> Path:
    shapefiles = sorted(extract_dir.rglob("*.shp"))
    if not shapefiles:
        raise FileNotFoundError(f"No shapefile found under {extract_dir}")

    preferred = [
        path
        for path in shapefiles
        if "zone" in path.stem.lower() or "taz" in path.stem.lower()
    ]
    return preferred[0] if preferred else shapefiles[0]


def _existing_outputs(boundary_path: Path, taz_path: Path) -> bool:
    return (
        boundary_path.exists()
        and boundary_path.stat().st_size > 0
        and taz_path.exists()
        and taz_path.stat().st_size > 0
    )


def run(cfg) -> None:
    """INPUT: Chicago community-area boundary and CMAP c24q4 TAZ polygons. OUTPUT: EPSG:26971 Logan Square boundary and clipped TAZ polygons in data/interim."""
    import geopandas as gpd

    cfg.data_interim.mkdir(parents=True, exist_ok=True)
    boundary_path = cfg.data_interim / BOUNDARY_OUTPUT
    taz_path = cfg.data_interim / TAZ_OUTPUT

    if _existing_outputs(boundary_path, taz_path):
        boundary_gdf = gpd.read_file(boundary_path)
        taz_gdf = gpd.read_file(taz_path)
        area_sq_km = float(boundary_gdf.geometry.area.sum()) / 1_000_000
        print(
            "s0 outputs already exist: "
            f"{boundary_path.name}, {taz_path.name}; "
            f"boundary_area_sq_km={area_sq_km:.3f}; taz_selected={len(taz_gdf)}"
        )
        return

    socrata_sources = cfg.sources["socrata"]
    community_source = socrata_sources["community_areas"]
    community_id = str(cfg.boundary.community_area_id)
    boundary_gdf = fetch_geojson(
        socrata_sources["domain"],
        community_source["dataset_id"],
        where=f"area_numbe = '{community_id}'",
    )

    if "area_numbe" in boundary_gdf.columns:
        boundary_gdf = boundary_gdf[boundary_gdf["area_numbe"].astype(str) == community_id]
    if boundary_gdf.empty:
        raise ValueError(f"No community area found for area_numbe == {community_id}")

    boundary_gdf = boundary_gdf.to_crs(cfg.crs)
    boundary_geom = boundary_gdf.geometry.union_all() if hasattr(boundary_gdf.geometry, "union_all") else boundary_gdf.unary_union
    boundary_out = gpd.GeoDataFrame(
        [
            {
                "community_area_id": cfg.boundary.community_area_id,
                "name": cfg.boundary.name,
                "geometry": boundary_geom,
            }
        ],
        crs=cfg.crs,
    )

    taz_cfg = cfg.sources["cmap"]["taz_polygons"]
    taz_url = taz_cfg["url"]
    if str(taz_cfg.get("format", "")).lower() == "geojson":
        taz_file = download_to_raw(taz_url, cfg.data_raw / "cmap_taz_zones17.geojson")
        taz_gdf = gpd.read_file(taz_file).to_crs(cfg.crs)
    else:
        extract_dir = download_and_extract_zip(taz_url, cfg.data_raw)
        taz_shapefile = _find_shapefile(extract_dir)
        taz_gdf = gpd.read_file(taz_shapefile).to_crs(cfg.crs)
    taz_id_column = detect_taz_id_column(taz_gdf)

    buffered_boundary = boundary_geom.buffer(cfg.boundary.buffer_m)
    selected_taz = taz_gdf[taz_gdf.geometry.intersects(buffered_boundary)].copy()
    selected_taz["taz_id"] = selected_taz[taz_id_column].astype(str)
    selected_taz["source_taz_id_field"] = taz_id_column

    boundary_out.to_file(boundary_path, driver="GPKG")
    selected_taz.to_file(taz_path, driver="GPKG")

    area_sq_km = float(boundary_out.geometry.area.sum()) / 1_000_000
    print(
        "s0 complete: "
        f"boundary_area_sq_km={area_sq_km:.3f}; "
        f"taz_selected={len(selected_taz)}; "
        f"taz_id_column={taz_id_column}; "
        f"outputs={boundary_path.name},{taz_path.name}"
    )
