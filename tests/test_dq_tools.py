from __future__ import annotations

import pandas as pd

from tools.dq_tools import reconcile_shipments, assign_tier


def _row(**overrides):
    base = {
        "shipment_date": "2026-03-06",
        "planning_day": "Day0",
        "is_planning_window": 1,
        "corridor_id": "C1_I95_NJ_BOS",
        "item_id": 10050,
        "item_name": "Heparin Sodium",
        "unique_item_id": "HEP-1",
        "dispatch_location": "Boston-MGH",
    }
    base.update(overrides)
    return base


def test_exact_match_is_valid():
    df = pd.DataFrame([_row(item_id=10050, item_name="Heparin Sodium", unique_item_id="HEP-1")])
    res = reconcile_shipments(df)
    row = res.reconciled.iloc[0]
    assert row["reason_code"] == "exact_match"
    assert row["canonical_item_id"] == "HEP-SOD"
    assert bool(row["is_valid"]) is True


def test_alias_match_resolves_when_item_id_agrees():
    # "Heparin Na" is an A.2 alias for HEP-SOD (item_id 10050).
    df = pd.DataFrame([_row(item_id=10050, item_name="Heparin Na", unique_item_id="HEP-2")])
    res = reconcile_shipments(df)
    row = res.reconciled.iloc[0]
    assert row["reason_code"] == "alias_match"
    assert row["canonical_item_id"] == "HEP-SOD"
    assert bool(row["is_valid"]) is True


def test_legacy_id_map():
    # legacy item_id 1070 maps to ALB-INH per A.3. Use a name that does NOT
    # match the master/alias tables so the legacy branch (D5) is exercised
    # rather than the higher-precedence name-alias branch (D4).
    df = pd.DataFrame([_row(item_id=1070, item_name="Salbutamol", unique_item_id="ALB-1")])
    res = reconcile_shipments(df)
    row = res.reconciled.iloc[0]
    assert row["reason_code"] == "legacy_id_map"
    assert row["canonical_item_id"] == "ALB-INH"
    assert bool(row["is_valid"]) is True


def test_name_alias_takes_precedence_over_legacy_id():
    # When both a name-alias (D4) and a legacy id (D5) could match, D4 wins
    # per playbook precedence — both resolve to the same canonical here.
    df = pd.DataFrame([_row(item_id=1070, item_name="Albuterol Inhaler", unique_item_id="ALB-1")])
    res = reconcile_shipments(df)
    row = res.reconciled.iloc[0]
    assert row["reason_code"] == "alias_match"
    assert row["canonical_item_id"] == "ALB-INH"


def test_dq01_missing_uid_excluded():
    df = pd.DataFrame([_row(unique_item_id=None)])
    res = reconcile_shipments(df)
    row = res.reconciled.iloc[0]
    assert row["reason_code"] == "excluded_dq01_missing_uid"
    assert bool(row["is_valid"]) is False
    assert res.report["dq01_missing_uid"] == 1


def test_dq02_invalid_item_id_excluded():
    df = pd.DataFrame([_row(item_id=88888, item_name="Mystery Drug", unique_item_id="MYS-1")])
    res = reconcile_shipments(df)
    row = res.reconciled.iloc[0]
    assert row["reason_code"] == "excluded_dq02_invalid_id"
    assert bool(row["is_valid"]) is False


def test_dq04_duplicate_uid_flagged():
    df = pd.DataFrame([
        _row(unique_item_id="DUP-1"),
        _row(unique_item_id="DUP-1"),
    ])
    res = reconcile_shipments(df)
    assert res.report["dq04_duplicate_uid"] == 1
    # first occurrence stays valid, the duplicate is flagged
    assert (res.reconciled["reason_code"] == "flagged_dq04_duplicate_uid").sum() == 1


def test_sla_tier_backfilled():
    df = pd.DataFrame([
        _row(item_id=10021, item_name="Remdesivir 100mg", unique_item_id="RMD-1"),  # Antiviral -> Tier 1
        _row(item_id=10070, item_name="Albuterol Inhaler", unique_item_id="ALB-2"),  # Bronchodilator -> Tier 2
    ])
    res = reconcile_shipments(df)
    tiers = dict(zip(res.reconciled["unique_item_id"], res.reconciled["sla_tier"]))
    assert tiers["RMD-1"] == 1
    assert tiers["ALB-2"] == 2


def test_assign_tier_defaults_to_two():
    assert assign_tier("Antiviral") == 1
    assert assign_tier("Opioid Analgesic") == 2
    assert assign_tier("Totally Unknown Type") == 2


def test_missing_required_columns_raises():
    import pytest
    df = pd.DataFrame([{"item_id": 10050}])
    with pytest.raises(ValueError):
        reconcile_shipments(df)
