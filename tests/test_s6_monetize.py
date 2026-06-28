"""Offline tests for s6 pothole monetization math."""

from __future__ import annotations

import math

from pipeline.s6_monetize import compute_bca


def test_compute_bca_known_values() -> None:
    result = compute_bca(
        vht_base=200.0,
        vht_fixed=100.0,
        vmt_pothole_base=50.0,
        total_potholes=10,
        coeffs={
            "value_of_time_usd_per_hr": 1.5,
            "extra_damage_usd_per_vmt": 2.0,
            "repair_cost_usd_per_pothole": 100.0,
        },
        period_years=1,
        discount_rate=0.0,
        annual_days=2.0,
    )

    assert math.isclose(result["daily_benefits_usd"]["travel_time"], 150.0)
    assert math.isclose(result["daily_benefits_usd"]["vehicle_damage"], 100.0)
    assert math.isclose(result["annual_benefits_usd"]["total"], 500.0)
    assert math.isclose(result["cost_usd"], 1000.0)
    assert math.isclose(result["benefit_cost_ratio"], 0.5)
    assert math.isclose(result["net_benefit_usd"], -500.0)


def test_compute_bca_handles_zero_cost() -> None:
    result = compute_bca(
        vht_base=10.0,
        vht_fixed=5.0,
        vmt_pothole_base=10.0,
        total_potholes=0,
        coeffs={
            "value_of_time_usd_per_hr": 10.0,
            "extra_damage_usd_per_vmt": 1.0,
            "repair_cost_usd_per_pothole": 100.0,
        },
        period_years=1,
        discount_rate=0.0,
        annual_days=1.0,
    )

    assert result["cost_usd"] == 0.0
    assert result["benefit_cost_ratio"] is None
    assert math.isclose(result["net_benefit_usd"], 60.0)
