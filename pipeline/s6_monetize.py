"""Stage s6: cost-benefit monetization."""

from __future__ import annotations


def run(cfg) -> None:
    """INPUT: Scenario deltas for delay, VMT, crashes, emissions, and intervention cost. OUTPUT: monetized benefits, costs, and benefit-cost ratio."""
    coeffs = cfg.fhwa_coefficients
    print(
        "TODO s6: monetize deltas using placeholder FHWA coefficients "
        f"(value of time=${coeffs.value_of_time_usd_per_hr}/hr) and compute BCR."
    )
