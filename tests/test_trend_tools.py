from __future__ import annotations

import pandas as pd

from tools.dq_tools import reconcile_shipments
from tools.trend_tools import (
    required_trucks,
    compute_corridor_kpis,
    compute_pop_trend,
    compute_deep_dive_tables,
)


def _make_reconciled():
    rows = []
    # History block (two corridors, two weeks) + planning window Day0/Day1
    for day in range(1, 8):
        rows.append({
            "shipment_date": f"2026-02-{20 + day:02d}", "planning_day": "History",
            "is_planning_window": 0, "corridor_id": "C1_I95_NJ_BOS",
            "item_id": 10021, "item_name": "Remdesivir 100mg",
            "unique_item_id": f"RMD-H{day}", "dispatch_location": "Boston-MGH",
        })
    rows += [
        {"shipment_date": "2026-03-06", "planning_day": "Day0", "is_planning_window": 1,
         "corridor_id": "C1_I95_NJ_BOS", "item_id": 10021, "item_name": "Remdesivir 100mg",
         "unique_item_id": "RMD-P1", "dispatch_location": "Boston-MGH"},
        {"shipment_date": "2026-03-06", "planning_day": "Day0", "is_planning_window": 1,
         "corridor_id": "C1_I95_NJ_BOS", "item_id": 10070, "item_name": "Albuterol Inhaler",
         "unique_item_id": "ALB-P1", "dispatch_location": "Boston-MGH"},
        {"shipment_date": "2026-03-07", "planning_day": "Day1", "is_planning_window": 1,
         "corridor_id": "C2_NJ_PHL", "item_id": 10022, "item_name": "Insulin Lispro",
         "unique_item_id": "INS-P1", "dispatch_location": "Philadelphia-UPenn"},
    ]
    return reconcile_shipments(pd.DataFrame(rows)).reconciled


def test_required_trucks_capacity_model():
    assert required_trucks(0) == 0
    assert required_trucks(10) == 2      # ceil(10 * 1.10 / 10) = 2
    assert required_trucks(9) == 1       # ceil(9 * 1.10 / 10) = ceil(0.99) = 1
    assert required_trucks(19) == 3      # ceil(19 * 1.10 / 10) = ceil(2.09) = 3


def test_corridor_kpis_planning_window():
    rec = _make_reconciled()
    kpis = compute_corridor_kpis(rec, "C1_I95_NJ_BOS")
    pw = kpis["planning_window"]
    assert pw["valid_units"] == 2          # RMD-P1 + ALB-P1
    assert pw["tier1_units"] == 1          # Remdesivir (Antiviral)
    assert pw["tier2_units"] == 1          # Albuterol (Bronchodilator)
    assert pw["cold_chain_units"] == 1     # Remdesivir is Cold (2-8C)


def test_pop_trend_available():
    rec = _make_reconciled()
    pop = compute_pop_trend(rec)
    assert pop["available"] is True
    assert "C1_I95_NJ_BOS" in pop["by_corridor"]
    delta = pop["by_corridor"]["C1_I95_NJ_BOS"]["delta"]
    assert "valid_units" in delta


def test_deep_dive_tables_shapes():
    rec = _make_reconciled()
    deep = compute_deep_dive_tables(rec)
    assert "item_spikes" in deep
    assert "exclusion_breakdown" in deep
    assert isinstance(deep["daily_valid_units"], list)
