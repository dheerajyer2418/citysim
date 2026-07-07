from __future__ import annotations
import bisect, collections, csv, json, math, pathlib
from datetime import datetime, timezone, timedelta
from typing import Any
import requests, yaml
from shapely.geometry import Point, mapping
from shapely.ops import transform as shapely_transform
from pyproj import Transformer
from pipeline.s5_interventions import (
    _read_network_links, snap_potholes_to_links,
    _fetch_pothole_records, _valid_potholes, _project_potholes)
from pipeline.s4_calibrate import _buffered_boundary_bounds_4326


def normalize(values: list, method: str = 'percentile') -> list:
    n = len(values)
    if n == 0:
        return []
    if method == 'percentile':
        if n == 1 or all(v == values[0] for v in values):
            return [0.0] * n
        # Map each value to the fraction of values strictly less than it, so tied
        # values (e.g. the many zero-crash/zero-pothole links) collapse to the same
        # percentile instead of being spread across the rank range.
        ordered = sorted(values)
        return [bisect.bisect_left(ordered, v) / float(n - 1) for v in values]
    if method == 'minmax':
        min_v = min(values)
        max_v = max(values)
        if max_v == min_v:
            return [0.0] * n
        return [float(v - min_v) / float(max_v - min_v) for v in values]
    raise ValueError(f"Unknown normalization method: {method}")


def combine_scores(components: dict, weights: dict) -> list:
    active_keys = [key for key in components if key in weights]
    if not active_keys:
        first = next(iter(components.values()), [])
        return [0.0] * len(first)
    weight_total = sum(float(weights[key]) for key in active_keys)
    if weight_total == 0:
        return [0.0] * len(components[active_keys[0]])
    scores = []
    for i in range(len(components[active_keys[0]])):
        weighted_sum = sum(float(weights[key]) * float(components[key][i]) for key in active_keys)
        scores.append(100.0 * weighted_sum / weight_total)
    return scores


def _weighted_snap(points_weights: list, links: list, max_distance_m: float) -> dict:
    """Snap each (point, weight) to its nearest link within max_distance_m and sum weights per link.

    Uses an STRtree spatial index (mirroring ``snap_potholes_to_links``) so large
    point sets do not degrade to an O(points x links) scan.
    """
    from operator import index as operator_index

    from shapely.strtree import STRtree

    result = collections.defaultdict(float)
    geometries = [link.geometry for link in links if link.geometry is not None and not link.geometry.is_empty]
    indexed_links = [link for link in links if link.geometry is not None and not link.geometry.is_empty]
    if not geometries:
        return dict(result)

    tree = STRtree(geometries)
    geometry_id_to_link = {id(geometry): link for geometry, link in zip(geometries, indexed_links)}
    for point, weight in points_weights:
        best_link = None
        best_distance = math.inf
        for item in tree.query(point.buffer(max_distance_m)):
            try:
                link_index = operator_index(item)
            except TypeError:
                link_index = None
            link = indexed_links[link_index] if link_index is not None else geometry_id_to_link[id(item)]
            distance = float(link.geometry.distance(point))
            if distance <= max_distance_m and distance < best_distance:
                best_link = link
                best_distance = distance
        if best_link is not None:
            result[str(best_link.link_id)] += float(weight)
    return dict(result)


def _fetch_and_cache(url: str, params: dict, cache_path, page_size: int = 50000) -> list:
    """Fetch a Socrata JSON dataset with offset paging, caching the full result on disk."""
    cache_path = pathlib.Path(cache_path)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return json.loads(cache_path.read_text(encoding='utf-8'))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list = []
    offset = 0
    while True:
        page_params = dict(params)
        page_params['$limit'] = page_size
        page_params['$offset'] = offset
        r = requests.get(url, params=page_params, timeout=60)
        r.raise_for_status()
        page = r.json()
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    cache_path.write_text(json.dumps(rows), encoding='utf-8')
    return rows


def _volume_field(record: dict) -> float | None:
    keys = ['vehiclecount', 'total_passing_vehicle_volume', 'vehicle_volume', 'volume', 'count']
    keys.extend(key for key in record if 'volume' in str(key).lower())
    for key in keys:
        val = record.get(key)
        if val is None or val == '':
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _record_lat_lon(record: dict) -> tuple[float, float] | None:
    pairs = [('latitude', 'longitude'), ('midpointlat', 'midpointlon')]
    for lat_key, lon_key in pairs:
        try:
            return float(record[lat_key]), float(record[lon_key])
        except (KeyError, ValueError, TypeError):
            continue
    return None


def run(cfg) -> None:
    with open(cfg.project_root / 'params.yaml', encoding='utf-8') as f:
        raw = yaml.safe_load(f)
    ni = raw.get('needs_index', {})
    if not ni.get('enabled', True):
        print('[s7] needs_index disabled')
        return
    snap_dist = float(ni.get('snap_distance_m', 40))
    norm_method = ni.get('normalization', 'percentile')
    top_n = int(ni.get('top_n', 25))
    crash_cfg = ni.get('crash', {})
    lookback_years = int(crash_cfg.get('lookback_years', 5))
    sev_weights = crash_cfg.get('severity_weights',
        {'fatal': 5.0, 'incapacitating': 4.0, 'non_incapacitating': 2.0, 'other': 1.0})
    score_weights = ni.get('weights',
        {'safety': 0.45, 'pavement': 0.25, 'congestion': 0.30})

    links = _read_network_links(cfg.network_links_path)
    links = [l for l in links if not l.is_connector]
    link_ids = [l.link_id for l in links]

    bounds = _buffered_boundary_bounds_4326(
        cfg.boundary_path,
        cfg.crs,
        cfg.sources['osm']['network_buffer_m'])
    min_lon, min_lat, max_lon, max_lat = bounds

    cfg.data_raw.mkdir(parents=True, exist_ok=True)
    cache_path = cfg.data_raw / f'{cfg.area_slug}_crashes_raw.json'
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=365 * lookback_years)
    cutoff_str = cutoff_dt.strftime('%Y-%m-%dT%H:%M:%S')
    where_str = (
        f"latitude >= {min_lat} AND latitude <= {max_lat} AND "
        f"longitude >= {min_lon} AND longitude <= {max_lon} AND "
        f"crash_date >= '{cutoff_str}'"
    )
    records = _fetch_and_cache('https://data.cityofchicago.org/resource/85ca-t3if.json',
        {'$where': where_str, '$select': 'latitude,longitude,most_severe_injury,crash_date'}, cache_path)
    transformer_in = Transformer.from_crs('EPSG:4326', cfg.crs, always_xy=True)
    crash_points_weights = []
    for rec in records:
        lat_lon = _record_lat_lon(rec)
        if lat_lon is None:
            continue
        lat, lon = lat_lon
        sev = rec.get('most_severe_injury', '').upper().strip()
        if 'FATAL' in sev:
            w = sev_weights.get('fatal', 5.0)
        elif 'INCAPACITATING' in sev:
            w = sev_weights.get('incapacitating', 4.0)
        elif 'NONINCAPACITATING' in sev or 'REPORTED' in sev:
            w = sev_weights.get('non_incapacitating', 2.0)
        else:
            w = sev_weights.get('other', 1.0)
        x, y = transformer_in.transform(lon, lat)
        crash_points_weights.append((Point(x, y), w))
    crashes_per_link = _weighted_snap(crash_points_weights, links, snap_dist)

    pothole_csv = cfg.data_interim / 'pothole_links.csv'
    if pothole_csv.exists():
        with open(pothole_csv, newline='', encoding='utf-8') as f:
            potholes_per_link = {row['link_id']: int(row['n_potholes'])
                                 for row in csv.DictReader(f) if row.get('n_potholes')}
    else:
        pot_cfg = cfg.interventions.get('pothole', cfg.interventions.get('potholes', {}))
        raw_pots = _fetch_pothole_records(cfg, bounds)
        try:
            valid_pts = _valid_potholes(raw_pots, pot_cfg)
        except TypeError:
            active_statuses = set(pot_cfg.get('active_statuses', []))
            valid_pts = _valid_potholes(
                raw_pots,
                recent_days=pot_cfg.get('recent_days'),
                active_statuses=active_statuses,
            )
        projected_pts = _project_potholes(valid_pts, cfg.crs)
        snap_counter = snap_potholes_to_links(projected_pts, links, snap_dist)
        potholes_per_link = dict(snap_counter)

    cache_path = cfg.data_raw / f'{cfg.area_slug}_adt_raw.json'
    # gc7y-n4xa uses midpointlat/midpointlon (not latitude/longitude) and is a
    # daily time series (many rows per segment), so aggregate to one mean
    # vehiclecount per segment before snapping.
    adt_where = (f"midpointlat >= {min_lat} AND midpointlat <= {max_lat}"
                 f" AND midpointlon >= {min_lon} AND midpointlon <= {max_lon}")
    adt_records = _fetch_and_cache('https://data.cityofchicago.org/resource/gc7y-n4xa.json',
        {'$where': adt_where, '$select': 'segmentid,midpointlat,midpointlon,vehiclecount'}, cache_path)
    seg_volumes: dict = collections.defaultdict(list)
    seg_coord: dict = {}
    for rec in adt_records:
        lat_lon = _record_lat_lon(rec)
        vol = _volume_field(rec)
        if lat_lon is None or vol is None:
            continue
        seg = str(rec.get('segmentid') or lat_lon)
        seg_volumes[seg].append(vol)
        seg_coord[seg] = lat_lon
    adt_points_weights = []
    for seg, vols in seg_volumes.items():
        lat, lon = seg_coord[seg]
        x, y = transformer_in.transform(lon, lat)
        adt_points_weights.append((Point(x, y), sum(vols) / len(vols)))
    adt_per_link = _weighted_snap(adt_points_weights, links, snap_dist)

    safety_raw = [crashes_per_link.get(lid, 0.0) for lid in link_ids]
    pavement_raw = [float(potholes_per_link.get(lid, 0)) for lid in link_ids]
    congestion_raw = [adt_per_link.get(lid, 0.0) for lid in link_ids]

    safety_norm = normalize(safety_raw, norm_method)
    pavement_norm = normalize(pavement_raw, norm_method)
    congestion_norm = normalize(congestion_raw, norm_method)

    need_scores = combine_scores(
        {'safety': safety_norm, 'pavement': pavement_norm, 'congestion': congestion_norm},
        score_weights)

    cfg.data_processed.mkdir(parents=True, exist_ok=True)

    with open(cfg.data_processed / 'needs_index.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'link_id', 'crashes_raw', 'potholes_raw', 'adt_raw',
            'safety_norm', 'pavement_norm', 'congestion_norm', 'need_score',
        ])
        for i, link_id in enumerate(link_ids):
            writer.writerow([
                link_id, safety_raw[i], pavement_raw[i], congestion_raw[i],
                safety_norm[i], pavement_norm[i], congestion_norm[i], need_scores[i],
            ])

    transformer_out = Transformer.from_crs(cfg.crs, 'EPSG:4326', always_xy=True)
    features = []
    for i, link in enumerate(links):
        reprojected = shapely_transform(transformer_out.transform, link.geometry)
        features.append({
            'type': 'Feature',
            'geometry': mapping(reprojected),
            'properties': {
                'link_id': link.link_id,
                'need_score': need_scores[i],
                'safety_norm': safety_norm[i],
                'pavement_norm': pavement_norm[i],
                'congestion_norm': congestion_norm[i],
            },
        })
    geojson_dict = {'type': 'FeatureCollection', 'features': features}
    (cfg.data_processed / 'needs_index.geojson').write_text(json.dumps(geojson_dict), encoding='utf-8')

    layer_coverage = {
        'safety': sum(1 for v in safety_raw if v > 0) / len(link_ids) if link_ids else 0.0,
        'pavement': sum(1 for v in pavement_raw if v > 0) / len(link_ids) if link_ids else 0.0,
        'congestion': sum(1 for v in congestion_raw if v > 0) / len(link_ids) if link_ids else 0.0,
    }
    top_segments = []
    for i in sorted(range(len(link_ids)), key=lambda idx: need_scores[idx], reverse=True)[:top_n]:
        top_segments.append({
            'link_id': link_ids[i],
            'need_score': need_scores[i],
            'components': {
                'safety': safety_norm[i],
                'pavement': pavement_norm[i],
                'congestion': congestion_norm[i],
            },
        })
    summary = {
        'weights': score_weights,
        'normalization': norm_method,
        'snap_distance_m': snap_dist,
        'layer_coverage': layer_coverage,
        'top_segments': top_segments,
        'sources': [
            {
                'name': 'Traffic Crashes - Crashes',
                'dataset_id': '85ca-t3if',
                'url': 'https://data.cityofchicago.org/resource/85ca-t3if.json',
            },
            {
                'name': '311 Service Requests - Pot Holes Reported',
                'dataset_id': '7as2-ds3y',
                'url': 'https://data.cityofchicago.org/resource/7as2-ds3y.json',
            },
            {
                'name': 'Average Daily Traffic Counts',
                'dataset_id': 'gc7y-n4xa',
                'url': 'https://data.cityofchicago.org/resource/gc7y-n4xa.json',
            },
        ],
    }
    (cfg.data_processed / 'needs_index_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')

    sorted_scores = sorted(need_scores)
    mid = len(sorted_scores) // 2
    median_score = (
        0.0 if not sorted_scores else
        (sorted_scores[mid] if len(sorted_scores) % 2 else (sorted_scores[mid - 1] + sorted_scores[mid]) / 2.0)
    )
    max_score = max(need_scores) if need_scores else 0.0
    print(
        f"[s7] links={len(link_ids)} crashes_snapped={len(crashes_per_link)} "
        f"potholes={sum(1 for v in pavement_raw if v > 0)} adt_links={len(adt_per_link)} "
        f"max_score={max_score:.1f} median={median_score:.1f}"
    )

