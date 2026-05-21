"""
Corridor KPI computation + Period-over-Period (PoP) trend analysis.

Operates on the *reconciled* DataFrame returned by `dq_tools.reconcile_shipments`.
All metrics trace back to playbook sections:

- §7 (SLA tiers)                  -> Tier 1 / Tier 2 split
- §7.1 / §7.2 (capacity model)    -> required_trucks = ceil(volume * 1.10 / 10)
- §11 (DQ rules)                  -> exclusion_rate
- §13 (allocation / penalty)      -> downstream input for Feature 5

The PoP split divides the History window into early half (period A) and
late half (period B) by shipment_date midpoint. Day0 / Day1 planning rows
are summarized separately as the "planning_window" KPI block.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

import pandas as pd


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

TRUCK_CAPACITY = 10            # §7.1 — units per truck
PACKING_BUFFER = 1.10          # §7.1 — +10% inefficiency buffer

TEMP_CONTROLLED_VALUES = {"Cold (2-8C)", "Strict Cold Chain (-20C)"}


def required_trucks(volume: int) -> int:
    """Capacity model from §7.2: ceil(volume * 1.10 / 10)."""
    if volume <= 0:
        return 0
    return math.ceil(volume * PACKING_BUFFER / TRUCK_CAPACITY)


# ---------------------------------------------------------------------------
# KPI primitives
# ---------------------------------------------------------------------------

def _kpis_for_slice(slice_df: pd.DataFrame) -> Dict[str, Any]:
    """Compute the KPI block for an already-filtered slice of reconciled rows."""
    total_rows = int(len(slice_df))
    if total_rows == 0:
        return {
            "total_rows": 0, "valid_units": 0, "excluded_rows": 0,
            "exclusion_rate": 0.0,
            "cold_chain_units": 0, "room_temp_units": 0, "controlled_units": 0,
            "cold_chain_pct": 0.0,
            "tier1_units": 0, "tier2_units": 0,
            "tier1_pct": 0.0,
            "trucks_standard_required": 0, "trucks_temp_required": 0,
            "drivers_required": 0,
        }

    valid = slice_df[slice_df["is_valid"]]
    valid_units = int(len(valid))
    excluded = total_rows - valid_units

    temp_mask = valid["temp_control"].isin(TEMP_CONTROLLED_VALUES)
    cold_chain_units = int(temp_mask.sum())
    controlled_units = int((valid["temp_control"] == "Controlled Storage").sum())
    room_temp_units = valid_units - cold_chain_units - controlled_units

    # Prefer the back-filled `sla_tier` column (annotated by dq_tools.reconcile_shipments).
    # Fall back to re-deriving from medicine_type if a caller passes in raw rows.
    if "sla_tier" in valid.columns:
        tier_series = valid["sla_tier"]
    else:
        from tools.dq_tools import assign_tier  # local import avoids module-load order coupling
        tier_series = valid["medicine_type"].map(assign_tier)
    tier1_units = int((tier_series == 1).sum())
    tier2_units = valid_units - tier1_units

    # §7.2 capacity model: cold-chain → temp-controlled trucks; everything else → standard
    standard_volume = valid_units - cold_chain_units
    trucks_std = required_trucks(standard_volume)
    trucks_temp = required_trucks(cold_chain_units)

    return {
        "total_rows": total_rows,
        "valid_units": valid_units,
        "excluded_rows": excluded,
        "exclusion_rate": round(excluded / total_rows, 4) if total_rows else 0.0,
        "cold_chain_units": cold_chain_units,
        "room_temp_units": int(room_temp_units),
        "controlled_units": controlled_units,
        "cold_chain_pct": round(cold_chain_units / valid_units, 4) if valid_units else 0.0,
        "tier1_units": tier1_units,
        "tier2_units": tier2_units,
        "tier1_pct": round(tier1_units / valid_units, 4) if valid_units else 0.0,
        "trucks_standard_required": trucks_std,
        "trucks_temp_required": trucks_temp,
        "drivers_required": trucks_std + trucks_temp,
    }


# ---------------------------------------------------------------------------
# Per-corridor KPIs (planning window vs history)
# ---------------------------------------------------------------------------

def compute_corridor_kpis(reconciled: pd.DataFrame, corridor_id: str) -> Dict[str, Any]:
    """KPI breakdown for a single corridor.

    Returns separate blocks for `planning_window` (Day0+Day1), `history`,
    and `overall` (everything).
    """
    corr = reconciled[reconciled["corridor_id"] == corridor_id]
    planning = corr[corr["planning_day"].isin(["Day0", "Day1"])]
    history = corr[corr["planning_day"] == "History"]

    return {
        "corridor_id": corridor_id,
        "overall": _kpis_for_slice(corr),
        "planning_window": _kpis_for_slice(planning),
        "history": _kpis_for_slice(history),
    }


# ---------------------------------------------------------------------------
# Period-over-Period analysis
# ---------------------------------------------------------------------------

def _pct_change(new: float, old: float) -> float:
    """Safe percent change. Returns 0.0 when both old and new are 0."""
    if old == 0:
        return 0.0 if new == 0 else float("inf")
    return round((new - old) / old, 4)


def _delta_block(period_b: Dict[str, Any], period_a: Dict[str, Any]) -> Dict[str, Any]:
    """Compute absolute + pct delta for the comparable KPI keys."""
    keys = (
        "valid_units", "cold_chain_units", "room_temp_units",
        "tier1_units", "tier2_units",
        "trucks_standard_required", "trucks_temp_required",
        "drivers_required", "excluded_rows",
    )
    delta: Dict[str, Any] = {}
    for k in keys:
        a = period_a.get(k, 0)
        b = period_b.get(k, 0)
        delta[k] = {"absolute": b - a, "pct": _pct_change(b, a)}
    return delta


def compute_pop_trend(reconciled: pd.DataFrame) -> Dict[str, Any]:
    """Split History into early/late halves by shipment_date and compare.

    Period A = earlier 7 days of history.
    Period B = later 7 days of history.
    """
    history = reconciled[reconciled["planning_day"] == "History"].copy()
    if history.empty:
        return {"available": False, "reason": "no history rows"}

    history["shipment_date"] = pd.to_datetime(history["shipment_date"], errors="coerce")
    history = history.dropna(subset=["shipment_date"])
    if history.empty:
        return {"available": False, "reason": "no parseable shipment_date in history"}

    date_min = history["shipment_date"].min()
    date_max = history["shipment_date"].max()
    midpoint = date_min + (date_max - date_min) / 2

    period_a = history[history["shipment_date"] <= midpoint]
    period_b = history[history["shipment_date"] > midpoint]

    corridors: List[str] = sorted(history["corridor_id"].dropna().unique().tolist())
    by_corridor: Dict[str, Any] = {}
    for cid in corridors:
        a = _kpis_for_slice(period_a[period_a["corridor_id"] == cid])
        b = _kpis_for_slice(period_b[period_b["corridor_id"] == cid])
        by_corridor[cid] = {
            "period_a": a,
            "period_b": b,
            "delta": _delta_block(b, a),
        }

    return {
        "available": True,
        "period_a_window": {"start": str(date_min.date()), "end": str(midpoint.date())},
        "period_b_window": {"start": str((midpoint + pd.Timedelta(days=1)).date()), "end": str(date_max.date())},
        "by_corridor": by_corridor,
        "overall": {
            "period_a": _kpis_for_slice(period_a),
            "period_b": _kpis_for_slice(period_b),
            "delta": _delta_block(_kpis_for_slice(period_b), _kpis_for_slice(period_a)),
        },
    }


# ---------------------------------------------------------------------------
# Cross-corridor comparison (for the planning window)
# ---------------------------------------------------------------------------

def compare_corridors(reconciled: pd.DataFrame) -> Dict[str, Any]:
    """Side-by-side KPI comparison of the planning window per corridor."""
    corridors: List[str] = sorted(reconciled["corridor_id"].dropna().unique().tolist())
    out: Dict[str, Any] = {"corridors": corridors, "planning_window": {}, "history": {}, "overall": {}}
    for cid in corridors:
        block = compute_corridor_kpis(reconciled, cid)
        out["planning_window"][cid] = block["planning_window"]
        out["history"][cid] = block["history"]
        out["overall"][cid] = block["overall"]
    return out


# ---------------------------------------------------------------------------
# Top-level convenience: one call, full trend payload
# ---------------------------------------------------------------------------

def compute_trend_payload(reconciled: pd.DataFrame) -> Dict[str, Any]:
    """One-shot helper for the graph node. Bundles corridor + PoP analysis."""
    return {
        "comparison": compare_corridors(reconciled),
        "pop": compute_pop_trend(reconciled),
    }
