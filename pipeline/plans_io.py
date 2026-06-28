"""Shared MATSim population XML writing helpers."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Iterable, TypeAlias

from lxml import etree


Activity: TypeAlias = tuple[str, float, float, float | None]
PersonPlan: TypeAlias = tuple[str, Iterable[Activity]]


def stochastic_count(expected: float, rng) -> int:
    """Round an expected count to an integer, preserving the expectation.

    Deterministic ``round()`` per row zeroes out disaggregate demand (most CMAP
    roster rows have ``trips==1``, so ``round(1*0.1)==0`` drops ~everything).
    Stochastic rounding takes the floor plus the fractional part as a Bernoulli
    probability, so the population total matches expected*fraction in aggregate.
    Works with both ``random.Random`` and ``numpy.random.Generator`` (both
    expose ``.random()`` returning a float in [0, 1)).
    """
    base = int(expected)
    frac = expected - base
    if frac > 0.0 and rng.random() < frac:
        base += 1
    return base


def format_time(seconds: float) -> str:
    seconds_int = max(0, int(round(seconds)))
    hours = seconds_int // 3600
    minutes = (seconds_int % 3600) // 60
    secs = seconds_int % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def write_population(persons: Iterable[PersonPlan], path: str | Path) -> Path:
    """Write a gzipped MATSim population_v6 XML file.

    Each person is `(person_id, activities)`, and car legs are implied between
    consecutive activities.
    """
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(output, "wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write(b'<!DOCTYPE population SYSTEM "http://www.matsim.org/files/dtd/population_v6.dtd">\n')
        with etree.xmlfile(handle, encoding="UTF-8") as xf:
            with xf.element("population"):
                for person_id, activities_iter in persons:
                    activities = list(activities_iter)
                    with xf.element("person", id=str(person_id)):
                        with xf.element("plan", selected="yes"):
                            for index, (act_type, x, y, end_time) in enumerate(activities):
                                attributes = {
                                    "type": str(act_type),
                                    "x": f"{float(x):.3f}",
                                    "y": f"{float(y):.3f}",
                                }
                                if end_time is not None:
                                    attributes["end_time"] = format_time(float(end_time))
                                xf.write(etree.Element("activity", **attributes))
                                if index < len(activities) - 1:
                                    xf.write(etree.Element("leg", mode="car"))
    return output
