from __future__ import annotations

import math
import os
import tempfile

import pandas as pd
import pytest

from tools.resource_tools import (
    load_resource_availability,
    compute_demand,
    allocate_resources,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_csv(rows: list[dict]) -> str:
    df = pd.DataFrame(rows)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    df.to_csv(tmp.name, index=False)
    tmp.close()
    return tmp.name


def _make_reconciled(rows: list[dict]) -> pd.DataFrame:
    base = {
        "corridor_id": "C1_I95_NJ_BOS",
        "planning_day": "Day0",
        "is_valid": 1,
        "sla_tier": 1,
        "temp_control": "Room Temp (20-25C)",
    }
    return pd.DataFrame([{**base, **r} for r in rows])


# ---------------------------------------------------------------------------
# load_resource_availability
# ---------------------------------------------------------------------------

def test_load_resource_availability_parses_csv():
    path = _make_csv([
        {"day": "Day0", "resource_type": "driver",               "available_count": 6, "notes": ""},
        {"day": "Day0", "resource_type": "truck_standard",        "available_count": 4, "notes": ""},
        {"day": "Day0", "resource_type": "truck_temp_controlled", "available_count": 2, "notes": ""},
        {"day": "Day1", "resource_type": "driver",               "available_count": 6, "notes": ""},
        {"day": "Day1", "resource_type": "truck_standard",        "available_count": 4, "notes": ""},
        {"day": "Day1", "resource_type": "truck_temp_controlled", "available_count": 2, "notes": ""},
    ])
    try:
        avail = load_resource_availability(path)
        assert avail["Day0"]["driver"] == 6
        assert avail["Day0"]["truck_standard"] == 4
        assert avail["Day0"]["truck_temp_controlled"] == 2
        assert avail["Day1"]["truck_temp_controlled"] == 2
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# compute_demand
# ---------------------------------------------------------------------------

def test_compute_demand_trucks_match_capacity_model():
    # 10 room-temp Tier1 units in Day0 C1 -> ceil(10 * 1.10 / 10) = 2 trucks_std
    df = _make_reconciled([{"sla_tier": 1} for _ in range(10)])
    demand = compute_demand(df, planning_days=["Day0"], corridors=["C1_I95_NJ_BOS"])
    d = demand["C1_I95_NJ_BOS"]["Day0"]
    assert d["total_units"] == 10
    assert d["trucks_std_needed"] == 2          # ceil(10 * 1.10 / 10)
    assert d["trucks_temp_needed"] == 0
    assert d["drivers_needed"] == 2


def test_compute_demand_cold_chain_uses_temp_trucks():
    df = _make_reconciled([
        {"temp_control": "Cold (2-8C)", "sla_tier": 1},
        {"temp_control": "Cold (2-8C)", "sla_tier": 1},
    ])
    demand = compute_demand(df, planning_days=["Day0"], corridors=["C1_I95_NJ_BOS"])
    d = demand["C1_I95_NJ_BOS"]["Day0"]
    assert d["cold_chain_units"] == 2
    assert d["trucks_temp_needed"] == 1         # ceil(2 * 1.10 / 10) = 1
    assert d["trucks_std_needed"] == 0


def test_compute_demand_excludes_invalid_rows():
    rows = [
        {"sla_tier": 1, "is_valid": 1},
        {"sla_tier": 1, "is_valid": 0},   # excluded
        {"sla_tier": 1, "is_valid": 0},   # excluded
    ]
    df = _make_reconciled(rows)
    demand = compute_demand(df, planning_days=["Day0"], corridors=["C1_I95_NJ_BOS"])
    assert demand["C1_I95_NJ_BOS"]["Day0"]["total_units"] == 1


# ---------------------------------------------------------------------------
# allocate_resources
# ---------------------------------------------------------------------------

def _simple_avail(std: int, temp: int, drivers: int) -> dict:
    return {
        "Day0": {"truck_standard": std, "truck_temp_controlled": temp, "driver": drivers}
    }


def _simple_demand(tier1: int, tier2: int, cold: int) -> dict:
    """Build a minimal demand dict for C1 on Day0."""
    cold_tier1 = min(cold, tier1)
    cold_tier2 = cold - cold_tier1
    room_temp  = (tier1 + tier2) - cold
    return {
        "C1_I95_NJ_BOS": {
            "Day0": {
                "total_units":       tier1 + tier2,
                "cold_chain_units":  cold,
                "room_temp_units":   room_temp,
                "tier1_units":       tier1,
                "tier2_units":       tier2,
                "tier1_cold_units":  cold_tier1,
                "tier1_room_units":  tier1 - cold_tier1,
                "tier2_cold_units":  cold_tier2,
                "tier2_room_units":  tier2 - cold_tier2,
                "trucks_std_needed":  math.ceil(room_temp  * 1.10 / 10),
                "trucks_temp_needed": math.ceil(cold * 1.10 / 10),
                "drivers_needed":     math.ceil(room_temp * 1.10 / 10) + math.ceil(cold * 1.10 / 10),
            }
        }
    }


def test_no_shortage_zero_penalty():
    demand = _simple_demand(tier1=5, tier2=5, cold=0)
    avail  = _simple_avail(std=10, temp=10, drivers=20)
    result = allocate_resources(demand, avail)
    assert result["total_penalty"] == 0
    assert result["total_unmet_units"] == 0


def test_temp_truck_shortage_incurs_cold_chain_penalty():
    # 4 cold-chain units but 0 temp trucks -> all unmet
    demand = _simple_demand(tier1=4, tier2=0, cold=4)
    avail  = _simple_avail(std=10, temp=0, drivers=20)
    result = allocate_resources(demand, avail)
    # All 4 units are Tier1-cold -> 180 pts each
    assert result["total_penalty"] == 4 * (100 + 80)
    assert result["total_unmet_units"] == 4


def test_tier1_penalised_more_than_tier2():
    # 2 Tier1-room unmet vs 2 Tier2-room unmet: Tier1 should yield higher penalty
    demand_t1 = _simple_demand(tier1=2, tier2=0, cold=0)
    demand_t2 = _simple_demand(tier1=0, tier2=2, cold=0)
    avail_none = _simple_avail(std=0, temp=0, drivers=0)
    r1 = allocate_resources(demand_t1, avail_none)
    r2 = allocate_resources(demand_t2, avail_none)
    assert r1["total_penalty"] > r2["total_penalty"]


def test_allocation_bottleneck_identified():
    # Only 1 temp truck available vs 2 needed -> bottleneck = trucks_temp
    demand = _simple_demand(tier1=0, tier2=10, cold=10)
    avail  = _simple_avail(std=10, temp=1, drivers=20)
    result = allocate_resources(demand, avail)
    assert result["summary_by_day"]["Day0"]["bottleneck"] == "trucks_temp"
