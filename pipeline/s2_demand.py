"""Stage s2: LODES commute demand synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pipeline.crosswalk import build_taz_link_crosswalk, load_links_from_gpkg, sample_activity_coord
from pipeline.download import download_to_raw
from pipeline.io_arcgis import fetch_arcgis_geojson
from pipeline.plans_io import stochastic_count, write_population


PLANS_OUTPUT = "plans.xml.gz"
RNG_SEED = 42


@dataclass(frozen=True)
class AgentPlan:
    person_id: str
    home_x: float
    home_y: float
    work_x: float
    work_y: float
    home_end_time: float
    work_end_time: float


def _format_time(seconds: float) -> str:
    from pipeline.plans_io import format_time

    return format_time(seconds)


def sample_departure_times(rng) -> tuple[float, float]:
    """Sample home and work activity end times in seconds after midnight."""
    home_end = float(rng.normal(7.5 * 3600, 1.0 * 3600))
    home_end = min(max(home_end, 5.0 * 3600), 10.0 * 3600)

    work_lower = max(home_end + 4.0 * 3600, 14.0 * 3600)
    work_end = float(rng.normal(17.0 * 3600, 1.25 * 3600))
    work_end = min(max(work_end, work_lower), 21.0 * 3600)
    return home_end, work_end


def buffered_boundary_envelope_4326(boundary_gdf, metric_crs: str, buffer_m: float) -> tuple[float, float, float, float]:
    """Return a WGS84 envelope for a metric-buffered boundary."""
    import geopandas as gpd

    boundary_metric = boundary_gdf.to_crs(metric_crs)
    buffered = boundary_metric.geometry.union_all().buffer(buffer_m)
    buffered_wgs84 = gpd.GeoDataFrame({"geometry": [buffered]}, crs=metric_crs).to_crs("EPSG:4326")
    bounds = buffered_wgs84.total_bounds
    return (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))


def block_representative_points(blocks_gdf, metric_crs: str):
    """Build projected block representative points using CENTLON/CENTLAT when present."""
    import geopandas as gpd
    from shapely.geometry import Point

    if {"CENTLON", "CENTLAT"}.issubset(blocks_gdf.columns):
        lon = blocks_gdf["CENTLON"].astype(float)
        lat = blocks_gdf["CENTLAT"].astype(float)
        points = gpd.GeoDataFrame(
            blocks_gdf.drop(columns="geometry"),
            geometry=[Point(x, y) for x, y in zip(lon, lat)],
            crs="EPSG:4326",
        )
        return points.to_crs(metric_crs)

    blocks_metric = blocks_gdf.to_crs(metric_crs)
    points = blocks_metric.copy()
    points.geometry = blocks_metric.geometry.representative_point()
    return points


def build_block_taz_map(blocks_gdf, taz_gdf, metric_crs: str) -> dict[str, str]:
    """Map Census block GEOIDs to TAZ ids by point-in-polygon spatial join."""
    block_points = block_representative_points(blocks_gdf, metric_crs)
    taz_metric = taz_gdf.to_crs(metric_crs)
    joined = block_points.sjoin(taz_metric[["taz_id", "geometry"]], how="inner", predicate="within")
    if "GEOID" not in joined.columns:
        raise ValueError("Blocks GeoDataFrame must include GEOID.")
    return {
        str(row["GEOID"]): str(row["taz_id"])
        for _, row in joined.dropna(subset=["GEOID", "taz_id"]).iterrows()
    }


def read_lodes_od(path: str | Path):
    import pandas as pd

    od = pd.read_csv(
        path,
        usecols=["w_geocode", "h_geocode", "S000"],
        dtype={"w_geocode": "string", "h_geocode": "string"},
    )
    od["S000"] = pd.to_numeric(od["S000"], errors="coerce").fillna(0).astype(int)
    return od


def aggregate_intra_area_lodes(od_df, block_taz: dict[str, str]):
    """Aggregate LODES jobs for OD rows with both ends inside mapped blocks."""
    import pandas as pd

    mapped_blocks = set(block_taz)
    intra = od_df[
        od_df["h_geocode"].astype(str).isin(mapped_blocks)
        & od_df["w_geocode"].astype(str).isin(mapped_blocks)
    ].copy()
    intra["home_taz"] = intra["h_geocode"].astype(str).map(block_taz)
    intra["work_taz"] = intra["w_geocode"].astype(str).map(block_taz)
    return (
        intra.groupby(["home_taz", "work_taz"], as_index=False)["S000"]
        .sum()
        .rename(columns={"S000": "jobs"})
        if not intra.empty
        else pd.DataFrame(columns=["home_taz", "work_taz", "jobs"])
    )


def generate_agent_plans(od_by_taz, sample_fraction: float, rng_seed: int = RNG_SEED) -> list[AgentPlan]:
    """Generate sampled MATSim home-work-home plans from aggregated TAZ OD jobs."""
    import numpy as np

    rng = np.random.default_rng(rng_seed)
    agents: list[AgentPlan] = []
    person_number = 0
    for _, row in od_by_taz.iterrows():
        jobs = int(row["jobs"])
        count = stochastic_count(jobs * sample_fraction, rng)
        for _ in range(count):
            home_x, home_y = sample_activity_coord(str(row["home_taz"]))
            work_x, work_y = sample_activity_coord(str(row["work_taz"]))
            home_end, work_end = sample_departure_times(rng)
            agents.append(
                AgentPlan(
                    person_id=f"lodes_{person_number:08d}",
                    home_x=home_x,
                    home_y=home_y,
                    work_x=work_x,
                    work_y=work_y,
                    home_end_time=home_end,
                    work_end_time=work_end,
                )
            )
            person_number += 1
    return agents


def write_plans_xml(agents: Iterable[AgentPlan], output_path: str | Path) -> Path:
    """Write gzipped MATSim population_v6 XML."""
    persons = (
        (
            agent.person_id,
            [
                ("home", agent.home_x, agent.home_y, agent.home_end_time),
                ("work", agent.work_x, agent.work_y, agent.work_end_time),
                ("home", agent.home_x, agent.home_y, None),
            ],
        )
        for agent in agents
    )
    return write_population(persons, output_path)


def run(cfg) -> None:
    """INPUT: LEHD LODES OD records, 2020 Census blocks, Logan Square TAZ, and S1 network links. OUTPUT: MATSim plans.xml.gz for scenarios/logan_square."""
    import geopandas as gpd

    plans_path = cfg.scenario_dir / PLANS_OUTPUT
    if plans_path.exists() and plans_path.stat().st_size > 0:
        print(f"s2 output already exists: {plans_path}")
        return

    boundary_path = cfg.boundary_path
    taz_path = cfg.taz_path
    links_path = cfg.network_links_path
    for path in (boundary_path, taz_path, links_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing required prior-stage artifact: {path}")

    boundary = gpd.read_file(boundary_path)
    buffer_m = float(cfg.sources["osm"].get("network_buffer_m", cfg.boundary.buffer_m))
    envelope = buffered_boundary_envelope_4326(boundary, cfg.crs, buffer_m)

    blocks = fetch_arcgis_geojson(
        cfg.sources["census"]["blocks_query_url"],
        geometry_envelope=envelope,
        out_fields="GEOID,CENTLON,CENTLAT,POP100,HU100",
    )
    blocks_metric = blocks.to_crs(cfg.crs)
    taz_gdf = gpd.read_file(taz_path).to_crs(cfg.crs)
    block_taz = build_block_taz_map(blocks, taz_gdf, cfg.crs)

    lodes_cfg = cfg.sources["lodes"]
    lodes_url = lodes_cfg["url_template"].format(year=lodes_cfg["year"])
    lodes_path = download_to_raw(lodes_url, cfg.data_raw / Path(lodes_url).name)
    od = read_lodes_od(lodes_path)

    # TODO: External trips with one end outside the area / cordon-gateway zones
    # are not modeled in this LODES v0. This undercounts traffic on
    # through-arterials and should be addressed with the CMAP all-purpose pass
    # plus boundary cordon zones.
    od_by_taz = aggregate_intra_area_lodes(od, block_taz)

    links = load_links_from_gpkg(links_path)
    build_taz_link_crosswalk(taz_gdf, links)
    agents = generate_agent_plans(od_by_taz, cfg.scenario.sample_fraction, rng_seed=RNG_SEED)
    write_plans_xml(agents, plans_path)

    total_scaled_jobs = int(round(float(od_by_taz["jobs"].sum()) * cfg.scenario.sample_fraction)) if not od_by_taz.empty else 0
    print(
        "s2 complete: "
        f"blocks_fetched={len(blocks_metric)}; "
        f"blocks_mapped_to_taz={len(block_taz)}; "
        f"intra_area_od_pairs={len(od_by_taz)}; "
        f"total_jobs_scaled={total_scaled_jobs}; "
        f"agents_written={len(agents)}"
    )

