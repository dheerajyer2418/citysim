"""Stage s4: calibration and validation."""

from __future__ import annotations


def geh(observed: float, simulated: float) -> float:
    """Compute GEH statistic for traffic count comparison."""
    if observed + simulated == 0:
        return 0.0
    return ((2.0 * (simulated - observed) ** 2) / (observed + simulated)) ** 0.5


def run(cfg) -> None:
    """INPUT: ADT count observations, MATSim link volumes, and CMAP timau validation counts. OUTPUT: counts.xml, Cadyts calibration inputs, and GEH validation summary."""
    _ = cfg
    print("TODO s4: build counts.xml, configure Cadyts, exclude connectors, and compute GEH vs CMAP timau.")
