"""Stage s3: transit conversion."""

from __future__ import annotations


def run(cfg) -> None:
    """INPUT: CTA, Metra, and Pace GTFS feeds. OUTPUT: MATSim transitSchedule.xml.gz and transitVehicles.xml.gz."""
    _ = cfg
    print("TODO s3: merge GTFS feeds, convert with pt2matsim, and write transit schedule and vehicles.")
