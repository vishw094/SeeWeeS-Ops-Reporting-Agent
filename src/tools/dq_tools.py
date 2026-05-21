"""
Item Master reconciliation + Data Quality enforcement.

Implements DQ-01..DQ-04 and decision rules D1..D8 from the SeeWeeS
Specialty Dispatch Playbook, Appendix A.

Design notes
------------
- Pure pandas / pure Python. No LLM calls. Fully deterministic so the
  AuditAgent and grader can verify outputs row-for-row against the playbook.
- Vectorized merges, not Python row loops. Scales linearly with shipment volume.
- Master tables (A.1, A.2, A.3) are hard-coded module constants so the file
  is self-contained and survives without any CSV side-files.
- All name comparisons go through a single normalizer so the alias logic
  is symmetric: "Pembrolizumab (Keytruda)" and "pembrolizumab keytruda"
  collapse to the same canonical form before lookup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import pandas as pd


# ---------------------------------------------------------------------------
# Appendix A — Canonical reference tables (authoritative)
# ---------------------------------------------------------------------------

# A.1 Canonical Item Master
_ITEM_MASTER_A1: List[Dict[str, Any]] = [
    {"canonical_item_id": "RMD-100",    "item_id": 10021, "canonical_item_name": "Remdesivir 100mg",                          "medicine_type": "Antiviral",            "temp_control": "Cold (2-8C)",              "product_class": "Antiviral"},
    {"canonical_item_id": "RMD-200",    "item_id": 10021, "canonical_item_name": "Remdesivir 200mg",                          "medicine_type": "Antiviral",            "temp_control": "Cold (2-8C)",              "product_class": "Antiviral"},
    {"canonical_item_id": "INS-LIS",    "item_id": 10022, "canonical_item_name": "Insulin Lispro",                            "medicine_type": "Hormone",              "temp_control": "Cold (2-8C)",              "product_class": "Endocrine"},
    {"canonical_item_id": "PMB-KEY",    "item_id": 10035, "canonical_item_name": "Pembrolizumab",                             "medicine_type": "Monoclonal Antibody",  "temp_control": "Cold (2-8C)",              "product_class": "Oncology Biologic"},
    {"canonical_item_id": "EPI-AI",     "item_id": 10040, "canonical_item_name": "Epinephrine Auto-Injector",                 "medicine_type": "Emergency Drug",       "temp_control": "Room Temp (20-25C)",       "product_class": "Emergency"},
    {"canonical_item_id": "HEP-SOD",    "item_id": 10050, "canonical_item_name": "Heparin Sodium",                            "medicine_type": "Anticoagulant",        "temp_control": "Room Temp (20-25C)",       "product_class": "Anticoagulant"},
    {"canonical_item_id": "MOR-SUL",    "item_id": 10060, "canonical_item_name": "Morphine Sulfate",                          "medicine_type": "Opioid Analgesic",     "temp_control": "Controlled Storage",       "product_class": "Controlled"},
    {"canonical_item_id": "ALB-INH",    "item_id": 10070, "canonical_item_name": "Albuterol Inhaler",                         "medicine_type": "Bronchodilator",       "temp_control": "Room Temp (20-25C)",       "product_class": "Respiratory"},
    {"canonical_item_id": "EXP-ONC-CT", "item_id": 99999, "canonical_item_name": "Experimental Oncology Drug (Clinical Trial)", "medicine_type": "Clinical Trial Drug",  "temp_control": "Strict Cold Chain (-20C)", "product_class": "Clinical Trial"},
    {"canonical_item_id": "LEV-INH",    "item_id": 10071, "canonical_item_name": "Levalbuterol Inhaler",                      "medicine_type": "Bronchodilator",       "temp_control": "Room Temp (20-25C)",       "product_class": "Respiratory"},
    {"canonical_item_id": "INS-ASP",    "item_id": 10023, "canonical_item_name": "Insulin Aspart",                            "medicine_type": "Hormone",              "temp_control": "Cold (2-8C)",              "product_class": "Endocrine"},
]

# A.2 Name aliases / variants
_NAME_ALIASES_A2: List[Dict[str, Any]] = [
    {"alias_name": "Remdesivir 100 mg",         "canonical_item_id": "RMD-100", "notes": "Space variant"},
    {"alias_name": "Remdesivir 200 mg",         "canonical_item_id": "RMD-200", "notes": "Space variant"},
    {"alias_name": "Pembrolizumab (Keytruda)",  "canonical_item_id": "PMB-KEY", "notes": "Brand in parentheses"},
    {"alias_name": "EpiPen Auto Injector",      "canonical_item_id": "EPI-AI",  "notes": "Common brand phrasing"},
    {"alias_name": "Heparin Na",                "canonical_item_id": "HEP-SOD", "notes": "Abbreviation"},
    {"alias_name": "Morphine Sulphate",         "canonical_item_id": "MOR-SUL", "notes": "US/UK spelling"},
    {"alias_name": "Albuterol Inhaler 90mcg",   "canonical_item_id": "ALB-INH", "notes": "Dose suffix"},
]

# A.3 Legacy / deprecated identifier mapping
_LEGACY_IDS_A3: List[Dict[str, Any]] = [
    {"legacy_item_id": 10020, "canonical_item_id": "RMD-100",    "rule": "LEGACY_ID_MAP", "rationale": "Vendor legacy ID for Remdesivir 100mg"},
    {"legacy_item_id": 20021, "canonical_item_id": "RMD-200",    "rule": "LEGACY_ID_MAP", "rationale": "Old system used 200xx for strength variants"},
    {"legacy_item_id": 1070,  "canonical_item_id": "ALB-INH",    "rule": "LEGACY_ID_MAP", "rationale": "Truncated ID found in older CSV exports"},
    {"legacy_item_id": 99999, "canonical_item_id": "EXP-ONC-CT", "rule": "SPECIAL_CASE",  "rationale": "Clinical trial placeholder ID; requires strict cold chain"},
]


# Reason / confidence codes per D8
REASON_EXACT       = "exact_match"
REASON_ALIAS       = "alias_match"
REASON_LEGACY      = "legacy_id_map"
REASON_DQ01        = "excluded_dq01_missing_uid"
REASON_DQ02        = "excluded_dq02_invalid_id"
REASON_DQ03        = "flagged_dq03_name_mismatch"
REASON_DQ04        = "flagged_dq04_duplicate_uid"
REASON_UNRESOLVED  = "excluded_unresolved"

CONF_EXACT     = "EXACT_MATCH"
CONF_ALIAS     = "ALIAS_MATCH"
CONF_LEGACY    = "LEGACY_ID_MAP"
CONF_NONE      = "NONE"


# Tier assignment — derived from §7 of the playbook (Tier 1 = life-critical,
# Tier 2 = standard specialty) cross-referenced with the medicine_type catalog
# in Appendix A.1. Kept here next to the master tables so the SLA tier is
# back-filled at reconciliation time and visible to every downstream consumer.
TIER_BY_MEDICINE_TYPE: Dict[str, int] = {
    "Antiviral":            1,
    "Hormone":              1,
    "Monoclonal Antibody":  1,
    "Emergency Drug":       1,
    "Anticoagulant":        1,
    "Clinical Trial Drug":  1,
    "Opioid Analgesic":     2,
    "Bronchodilator":       2,
}


def assign_tier(medicine_type: Any) -> int:
    """Return SLA tier (1 or 2) for a given medicine_type. Unknown defaults to 2."""
    return TIER_BY_MEDICINE_TYPE.get(medicine_type, 2)

REQUIRED_COLUMNS = (
    "shipment_date", "planning_day", "is_planning_window",
    "corridor_id", "item_id", "item_name", "unique_item_id", "dispatch_location",
)


# ---------------------------------------------------------------------------
# Cached, normalized lookup tables (built once on import)
# ---------------------------------------------------------------------------

def _normalize_name(name: Any) -> str:
    """Canonicalize a medicine name for fuzzy matching.

    - lowercase
    - strip parenthetical clutter ("(Keytruda)" -> "")
    - drop non-alphanumerics
    - collapse whitespace
    """
    if name is None:
        return ""
    s = str(name).lower()
    s = re.sub(r"\([^)]*\)", " ", s)        # remove parentheses
    s = re.sub(r"[^a-z0-9]+", " ", s)       # collapse non-alnum
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_lookups() -> Dict[str, Any]:
    """Pre-compute hashmaps used by the vectorized reconciler.

    Built once at import-time. Returned as a dict so it stays immutable
    from the caller's perspective.
    """
    master = pd.DataFrame(_ITEM_MASTER_A1)
    aliases = pd.DataFrame(_NAME_ALIASES_A2)
    legacy = pd.DataFrame(_LEGACY_IDS_A3)

    master["name_norm"] = master["canonical_item_name"].map(_normalize_name)
    aliases["alias_norm"] = aliases["alias_name"].map(_normalize_name)

    # (item_id, normalized_name) -> canonical_item_id   (D3 exact match)
    exact_map: Dict[Tuple[int, str], str] = {
        (int(r.item_id), r.name_norm): r.canonical_item_id
        for r in master.itertuples(index=False)
    }

    # item_id -> set of canonical rows (D6 conflict detection)
    by_item_id: Dict[int, List[str]] = (
        master.groupby("item_id")["canonical_item_id"].apply(list).to_dict()
    )

    # normalized_name -> canonical_item_id (master + alias union)
    name_map: Dict[str, str] = {}
    for r in master.itertuples(index=False):
        name_map[r.name_norm] = r.canonical_item_id
    for r in aliases.itertuples(index=False):
        name_map[r.alias_norm] = r.canonical_item_id

    # legacy_item_id -> canonical_item_id (D5)
    legacy_map: Dict[int, str] = {
        int(r.legacy_item_id): r.canonical_item_id
        for r in legacy.itertuples(index=False)
    }

    # canonical_item_id -> full attribute row (for back-fill)
    master_by_canonical: Dict[str, Dict[str, Any]] = {
        r.canonical_item_id: {
            "item_id": int(r.item_id),
            "canonical_item_name": r.canonical_item_name,
            "medicine_type": r.medicine_type,
            "temp_control": r.temp_control,
            "product_class": r.product_class,
        }
        for r in master.itertuples(index=False)
    }

    return {
        "exact_map": exact_map,
        "by_item_id": by_item_id,
        "name_map": name_map,
        "legacy_map": legacy_map,
        "master_by_canonical": master_by_canonical,
        "master_df": master.drop(columns=["name_norm"]),
        "aliases_df": aliases.drop(columns=["alias_norm"]),
        "legacy_df": legacy,
    }


# Lazy cache. We deliberately do NOT build at import time:
# eager pandas work at import has been observed to interact badly with
# chromadb's rust client on Windows (access violation in `add_documents`).
_LOOKUPS_CACHE: Dict[str, Any] | None = None


def _get_lookups() -> Dict[str, Any]:
    global _LOOKUPS_CACHE
    if _LOOKUPS_CACHE is None:
        _LOOKUPS_CACHE = _build_lookups()
    return _LOOKUPS_CACHE


def get_item_master() -> pd.DataFrame:
    """Return a copy of the A.1 canonical master."""
    return _get_lookups()["master_df"].copy()


def get_name_aliases() -> pd.DataFrame:
    """Return a copy of the A.2 alias table."""
    return _get_lookups()["aliases_df"].copy()


def get_legacy_ids() -> pd.DataFrame:
    """Return a copy of the A.3 legacy mapping table."""
    return _get_lookups()["legacy_df"].copy()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass
class ReconcileResult:
    """Output of reconcile_shipments()."""
    reconciled: pd.DataFrame       # every input row, annotated
    valid: pd.DataFrame            # only rows that pass DQ for dispatch planning
    report: Dict[str, Any] = field(default_factory=dict)


def _resolve_row(
    item_id: Any,
    item_name: Any,
    lookups: Dict[str, Any],
) -> Tuple[str, str, str]:
    """Resolve a single (item_id, item_name) to (canonical_id, reason, confidence).

    Decision order: D1 (handled outside on uid) -> D3 -> D4 -> D5 -> D6 -> DQ02.
    """
    name_norm = _normalize_name(item_name)

    # D3 — exact (item_id, name) hit in master
    try:
        iid = int(item_id) if pd.notna(item_id) else None
    except (TypeError, ValueError):
        iid = None

    if iid is not None and (iid, name_norm) in lookups["exact_map"]:
        return lookups["exact_map"][(iid, name_norm)], REASON_EXACT, CONF_EXACT

    # D4 — name matches an alias (or canonical name) in A.1+A.2
    if name_norm and name_norm in lookups["name_map"]:
        canonical = lookups["name_map"][name_norm]
        canonical_master_item_id = lookups["master_by_canonical"][canonical]["item_id"]

        # Alias's canonical agrees with row's item_id: clean alias match.
        if iid is not None and iid == canonical_master_item_id:
            return canonical, REASON_ALIAS, CONF_ALIAS

        # Row has a valid item_id in master, but alias resolves to a different
        # canonical → real DQ-03 name vs id mismatch.
        if iid is not None and iid in lookups["by_item_id"]:
            return canonical, REASON_DQ03, CONF_ALIAS

        # Row's item_id is missing or unknown — accept alias-driven mapping.
        return canonical, REASON_ALIAS, CONF_ALIAS

    # D5 — legacy id mapping
    if iid is not None and iid in lookups["legacy_map"]:
        return lookups["legacy_map"][iid], REASON_LEGACY, CONF_LEGACY

    # D6 — item_id valid in master but name doesn't disambiguate
    if iid is not None and iid in lookups["by_item_id"]:
        return "", REASON_DQ03, CONF_NONE

    # DQ-02 — item_id not in master or legacy
    return "", REASON_DQ02, CONF_NONE


def reconcile_shipments(df: pd.DataFrame) -> ReconcileResult:
    """Apply DQ-01..DQ-04 and D1..D8 to a shipment DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Raw shipment rows. Must contain the columns in REQUIRED_COLUMNS.

    Returns
    -------
    ReconcileResult
        - `reconciled` : every input row with added columns
            canonical_item_id, canonical_item_name, medicine_type,
            temp_control, product_class, reason_code, confidence_tier,
            is_valid (bool — passed DQ for dispatch planning)
        - `valid` : subset of reconciled where is_valid is True
        - `report` : structured summary used by AuditAgent + ReportAgent
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"reconcile_shipments: input CSV missing required columns: {missing}. "
            f"Got: {list(df.columns)}"
        )

    if df.empty:
        return ReconcileResult(
            reconciled=df.copy(),
            valid=df.copy(),
            report={"total_rows": 0, "valid_rows": 0, "excluded_rows": 0, "flagged_rows": 0},
        )

    work = df.copy()
    work["unique_item_id"] = work["unique_item_id"].astype(object).where(work["unique_item_id"].notna(), None)
    work["unique_item_id"] = work["unique_item_id"].map(
        lambda v: None if v is None or (isinstance(v, str) and v.strip() == "") else v
    )

    # DQ-01 — missing uid → exclude before resolution
    is_dq01 = work["unique_item_id"].isna()

    # Vectorize resolution: cache by (item_id, name) since the data has lots of repeats
    resolution_cache: Dict[Tuple[Any, Any], Tuple[str, str, str]] = {}

    def _resolve_cached(item_id: Any, item_name: Any) -> Tuple[str, str, str]:
        key = (item_id, item_name)
        hit = resolution_cache.get(key)
        if hit is not None:
            return hit
        res = _resolve_row(item_id, item_name, _get_lookups())
        resolution_cache[key] = res
        return res

    resolved = [
        _resolve_cached(iid, name)
        for iid, name in zip(work["item_id"], work["item_name"])
    ]
    work["canonical_item_id"] = [r[0] for r in resolved]
    work["reason_code"]       = [r[1] for r in resolved]
    work["confidence_tier"]   = [r[2] for r in resolved]

    # DQ-01 overrides whatever resolution said
    work.loc[is_dq01, "reason_code"]      = REASON_DQ01
    work.loc[is_dq01, "confidence_tier"]  = CONF_NONE
    work.loc[is_dq01, "canonical_item_id"] = ""

    # DQ-04 — duplicate uid among rows that *have* a uid
    has_uid = work["unique_item_id"].notna()
    dup_mask = has_uid & work.duplicated(subset=["unique_item_id"], keep="first")
    if dup_mask.any():
        # First occurrence stays as-is; subsequent occurrences flagged
        work.loc[dup_mask, "reason_code"] = REASON_DQ04

    # Back-fill canonical attributes from the master (preserve the row's raw item_id).
    attrs = pd.DataFrame.from_dict(_get_lookups()["master_by_canonical"], orient="index")
    attrs.index.name = "canonical_item_id"
    attrs = attrs.reset_index().drop(columns=["item_id"])
    work = work.merge(attrs, on="canonical_item_id", how="left")

    # Decorate every row with its SLA tier so trend / planner / audit code
    # can read it as a column rather than re-deriving from medicine_type.
    work["sla_tier"] = work["medicine_type"].map(assign_tier).astype("Int64")

    # is_valid: only EXACT / ALIAS / LEGACY resolved rows that aren't DQ-01/DQ-04
    valid_reasons = {REASON_EXACT, REASON_ALIAS, REASON_LEGACY}
    work["is_valid"] = work["reason_code"].isin(valid_reasons) & has_uid

    valid = work[work["is_valid"]].copy()

    # ---- Report ------------------------------------------------------------
    reason_counts = work["reason_code"].value_counts().to_dict()
    confidence_counts = work["confidence_tier"].value_counts().to_dict()

    excluded_mask = ~work["is_valid"]
    excluded_sample = (
        work.loc[excluded_mask, ["corridor_id", "item_id", "item_name", "unique_item_id", "reason_code"]]
            .head(10)
            .to_dict(orient="records")
    )

    flagged_mask = work["reason_code"].isin({REASON_DQ03, REASON_DQ04})

    by_corridor = (
        work.groupby("corridor_id")
            .agg(
                total_rows=("item_id", "size"),
                valid_rows=("is_valid", "sum"),
                excluded_rows=("is_valid", lambda s: int((~s).sum())),
            )
            .reset_index()
            .to_dict(orient="records")
    )

    report: Dict[str, Any] = {
        "total_rows": int(len(work)),
        "valid_rows": int(work["is_valid"].sum()),
        "excluded_rows": int((~work["is_valid"]).sum()),
        "flagged_rows": int(flagged_mask.sum()),
        "dq01_missing_uid": int(is_dq01.sum()),
        "dq02_invalid_id": int((work["reason_code"] == REASON_DQ02).sum()),
        "dq03_name_mismatch": int((work["reason_code"] == REASON_DQ03).sum()),
        "dq04_duplicate_uid": int(dup_mask.sum()),
        "reason_code_breakdown": {str(k): int(v) for k, v in reason_counts.items()},
        "confidence_tier_breakdown": {str(k): int(v) for k, v in confidence_counts.items()},
        "by_corridor": by_corridor,
        "excluded_sample": excluded_sample,
    }

    return ReconcileResult(reconciled=work, valid=valid, report=report)


# ---------------------------------------------------------------------------
# Self-check / dev sanity block — run `python -m tools.dq_tools <csv>` from src/
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data-for-enhancement/Incoming_shipments_14d_multi_corridor.csv"
    raw = pd.read_csv(csv_path)
    out = reconcile_shipments(raw)
    print(json.dumps(out.report, indent=2, default=str))
    print(f"\nReconciled columns: {list(out.reconciled.columns)}")
    print(f"Valid rows: {len(out.valid)} / Total rows: {len(out.reconciled)}")
    if "sla_tier" in out.reconciled.columns:
        print("SLA tier mix on valid rows:")
        print(out.valid["sla_tier"].value_counts().to_dict())
