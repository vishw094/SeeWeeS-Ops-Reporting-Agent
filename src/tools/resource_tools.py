"""Resource demand computation and penalty-minimising allocation.

Implements Dispatch Playbook §13 (Resource Constraints and Allocation Policy):
  - §13.1: Resource pools — driver, truck_standard, truck_temp_controlled
  - §13.2: Penalty model — Tier1=100, Tier2=40, cold-chain=+80, non-SLA-delay=10
  - Allocation objective: minimise total penalty; tie-break on fewer Tier1 units impacted
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

import pandas as pd


# ---------------------------------------------------------------------------
# Resource loading
# ---------------------------------------------------------------------------

def load_resource_availability(csv_path: str) -> Dict[str, Dict[str, int]]:
    """Load Resource_availability_48h.csv → {day_label: {resource_type: count}}."""
    df = pd.read_csv(csv_path)
    result: Dict[str, Dict[str, int]] = {}
    for _, row in df.iterrows():
        day      = str(row["day"]).strip()
        res_type = str(row["resource_type"]).strip()
        count    = int(row["available_count"])
        result.setdefault(day, {})[res_type] = count
    return result


# ---------------------------------------------------------------------------
# Demand computation
# ---------------------------------------------------------------------------

_COLD_CHAIN_TEMP_VALUES = {"Cold (2-8C)", "Strict Cold Chain (-20C)"}


def _is_cold_chain(temp_control: str | None) -> bool:
    return str(temp_control or "") in _COLD_CHAIN_TEMP_VALUES


def compute_demand(
    reconciled_df: pd.DataFrame,
    planning_days: List[str] | None = None,
    corridors: List[str] | None = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Compute truck and driver demand per corridor per planning day.

    Uses only DQ-valid rows. Mirrors the capacity model from §7.1-7.2:
      trucks_std  = ceil(room_temp_units  * 1.10 / 10)
      trucks_temp = ceil(cold_chain_units * 1.10 / 10)
      drivers     = trucks_std + trucks_temp

    Return shape: {corridor_id: {planning_day: {trucks_std_needed, trucks_temp_needed,
                                                 drivers_needed, tier1_units, tier2_units,
                                                 cold_chain_units, room_temp_units, total_units}}}
    """
    valid = reconciled_df[reconciled_df["is_valid"].astype(bool)].copy()

    if planning_days is None:
        planning_days = sorted(valid["planning_day"].unique().tolist())
    if corridors is None:
        corridors = sorted(valid["corridor_id"].unique().tolist())

    demand: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for corridor in corridors:
        demand[corridor] = {}
        c_df = valid[valid["corridor_id"] == corridor]

        for day in planning_days:
            d_df = c_df[c_df["planning_day"] == day]

            cold_mask   = d_df["temp_control"].apply(_is_cold_chain)
            cold_chain  = int(cold_mask.sum())
            room_temp   = int(len(d_df)) - cold_chain

            tier1_df    = d_df[d_df["sla_tier"] == 1]
            tier2_df    = d_df[d_df["sla_tier"] == 2]
            tier1_cold  = int(tier1_df["temp_control"].apply(_is_cold_chain).sum())
            tier2_cold  = int(tier2_df["temp_control"].apply(_is_cold_chain).sum())

            trucks_std  = math.ceil(room_temp  * 1.10 / 10)
            trucks_temp = math.ceil(cold_chain * 1.10 / 10)

            demand[corridor][day] = {
                "total_units":       int(len(d_df)),
                "cold_chain_units":  cold_chain,
                "room_temp_units":   room_temp,
                "tier1_units":       int(len(tier1_df)),
                "tier2_units":       int(len(tier2_df)),
                "tier1_cold_units":  tier1_cold,
                "tier1_room_units":  int(len(tier1_df)) - tier1_cold,
                "tier2_cold_units":  tier2_cold,
                "tier2_room_units":  int(len(tier2_df)) - tier2_cold,
                "trucks_std_needed":  trucks_std,
                "trucks_temp_needed": trucks_temp,
                "drivers_needed":     trucks_std + trucks_temp,
            }

    return demand


# ---------------------------------------------------------------------------
# Penalty-minimising allocation (§13.2)
# ---------------------------------------------------------------------------

_SLA_PENALTY   = {1: 100, 2: 40}
_COLD_EXTRA    = 80
_DELAY_PENALTY = 10     # non-SLA-delay: dispatched within SLA window but not on requested day

# Ordered highest → lowest penalty: Tier1-cold (180), Tier1-room (100), Tier2-cold (120), Tier2-room (40)
_SLOT_PRIORITY: List[Dict[str, Any]] = [
    {"tier": 1, "cold": True,  "key": "tier1_cold_units", "penalty": _SLA_PENALTY[1] + _COLD_EXTRA},
    {"tier": 1, "cold": False, "key": "tier1_room_units", "penalty": _SLA_PENALTY[1]},
    {"tier": 2, "cold": True,  "key": "tier2_cold_units", "penalty": _SLA_PENALTY[2] + _COLD_EXTRA},
    {"tier": 2, "cold": False, "key": "tier2_room_units", "penalty": _SLA_PENALTY[2]},
]


def allocate_resources(
    demand: Dict[str, Dict[str, Dict[str, Any]]],
    availability: Dict[str, Dict[str, int]],
) -> Dict[str, Any]:
    """Greedy penalty-minimising allocation across corridors and days.

    Strategy: for each day, compute how much of each truck/driver pool is
    available vs needed.  Derive a ``constraining_ratio`` (the bottleneck
    resource) and scale down *all* corridors uniformly.  Within each corridor,
    unmet units are attributed to the highest-penalty slot first (§13.2
    tie-break: fewer Tier 1 units impacted).

    Returns a nested dict with per-corridor-per-day plans and aggregate totals.
    """
    days      = sorted(availability.keys())
    corridors = sorted(demand.keys())

    allocation: Dict[str, Any] = {
        "by_corridor_day":  {},
        "total_penalty":    0,
        "total_unmet_units": 0,
        "summary_by_day":   {},
        "penalty_model": {
            "tier1_sla_violation":   _SLA_PENALTY[1],
            "tier2_sla_violation":   _SLA_PENALTY[2],
            "cold_chain_extra":      _COLD_EXTRA,
            "non_sla_delay":         _DELAY_PENALTY,
        },
    }

    for day in days:
        avail     = dict(availability.get(day, {}))
        pool_std  = avail.get("truck_standard",       0)
        pool_temp = avail.get("truck_temp_controlled", 0)
        pool_drv  = avail.get("driver",               0)

        # Aggregate demand across all corridors for this day
        total_std_needed  = sum(demand[c].get(day, {}).get("trucks_std_needed",  0) for c in corridors)
        total_temp_needed = sum(demand[c].get(day, {}).get("trucks_temp_needed", 0) for c in corridors)
        total_drv_needed  = sum(demand[c].get(day, {}).get("drivers_needed",     0) for c in corridors)

        # Capacity ratios (1.0 = fully covered; <1.0 = rationed)
        ratio_std  = (pool_std  / total_std_needed)  if total_std_needed  > 0 else 1.0
        ratio_temp = (pool_temp / total_temp_needed) if total_temp_needed > 0 else 1.0
        ratio_drv  = (pool_drv  / total_drv_needed)  if total_drv_needed  > 0 else 1.0
        constraining_ratio = min(1.0, ratio_std, ratio_temp, ratio_drv)

        day_penalty = 0
        day_unmet   = 0
        day_summary: Dict[str, Any] = {
            "available_trucks_std":  pool_std,
            "available_trucks_temp": pool_temp,
            "available_drivers":     pool_drv,
            "total_std_needed":      total_std_needed,
            "total_temp_needed":     total_temp_needed,
            "total_drv_needed":      total_drv_needed,
            "constraining_ratio":    round(constraining_ratio, 3),
            "bottleneck": (
                "trucks_std"  if ratio_std  <= ratio_temp and ratio_std  <= ratio_drv else
                "trucks_temp" if ratio_temp <= ratio_drv else "drivers"
            ) if constraining_ratio < 1.0 else "none",
            "corridor_plans": {},
        }

        for corridor in corridors:
            d           = demand[corridor].get(day, {})
            total_units = d.get("total_units", 0)

            allocated_units = math.floor(total_units * constraining_ratio)
            unmet_units     = total_units - allocated_units

            corridor_penalty   = 0
            penalty_breakdown: List[Dict[str, Any]] = []

            unmet_remaining = unmet_units
            for slot in _SLOT_PRIORITY:
                slot_demand  = d.get(slot["key"], 0)
                # How many in this slot are unmet?  Proportional to the overall shortage.
                slot_unmet   = min(unmet_remaining, max(0, slot_demand - math.floor(slot_demand * constraining_ratio)))
                slot_penalty = slot_unmet * slot["penalty"]
                corridor_penalty += slot_penalty
                unmet_remaining  -= slot_unmet

                if slot_demand > 0:
                    penalty_breakdown.append({
                        "tier":            slot["tier"],
                        "cold_chain":      slot["cold"],
                        "demand":          slot_demand,
                        "dispatched":      slot_demand - slot_unmet,
                        "unmet":           slot_unmet,
                        "penalty_per_unit": slot["penalty"],
                        "penalty":         slot_penalty,
                    })

            # Any leftover unmet (rounding residual) gets non-SLA-delay penalty
            if unmet_remaining > 0:
                corridor_penalty += unmet_remaining * _DELAY_PENALTY
                penalty_breakdown.append({
                    "tier": None, "cold_chain": False,
                    "demand": unmet_remaining, "dispatched": 0,
                    "unmet": unmet_remaining,
                    "penalty_per_unit": _DELAY_PENALTY,
                    "penalty": unmet_remaining * _DELAY_PENALTY,
                })

            day_penalty += corridor_penalty
            day_unmet   += unmet_units

            plan: Dict[str, Any] = {
                "total_units":        total_units,
                "allocated_units":    allocated_units,
                "unmet_units":        unmet_units,
                "trucks_std":         math.floor(d.get("trucks_std_needed",  0) * constraining_ratio),
                "trucks_temp":        math.floor(d.get("trucks_temp_needed", 0) * constraining_ratio),
                "drivers":            math.floor(d.get("drivers_needed",     0) * constraining_ratio),
                "penalty":            corridor_penalty,
                "constraining_ratio": round(constraining_ratio, 3),
                "penalty_breakdown":  penalty_breakdown,
            }
            day_summary["corridor_plans"][corridor] = plan
            allocation["by_corridor_day"].setdefault(corridor, {})[day] = plan

        day_summary["day_total_penalty"] = day_penalty
        day_summary["day_unmet_units"]   = day_unmet
        allocation["summary_by_day"][day] = day_summary
        allocation["total_penalty"]      += day_penalty
        allocation["total_unmet_units"]  += day_unmet

    return allocation
