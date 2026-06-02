<div align="center">

# SeeWeeS Ops Reporting Agent

### Multi-Agent AI System for Time-Critical Medicine Dispatch

**LangGraph · LangChain · GPT-4.1-mini · ChromaDB · Open-Meteo**

![Tests](https://img.shields.io/badge/tests-29%20passing-brightgreen)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Framework](https://img.shields.io/badge/framework-LangGraph%200.2-purple)
![Status](https://img.shields.io/badge/features-1%20·%203%20·%205%20complete-success)

</div>

---

## Overview

SeeWeeS operates a specialty-medicine dispatch network shipping life-critical drugs from New Jersey distribution centers to hospitals across two corridors. This pipeline ingests a 14-day shipment CSV, reconciles every row against the Dispatch Playbook, scores weather risk across nine geographic waypoints, allocates trucks and drivers under a penalty model, drafts a 48-hour dispatch plan, and self-audits the plan before emailing a leadership-ready HTML report.

<table>
<tr>
<td width="33%" valign="top">

### Feature 1
**Self-Correction Audit Loop**

A two-layer audit (LLM rules + deterministic policy checks) reviews every dispatch plan. Failed plans loop back to the planner with violations injected. Three failures at risk-score 3/3 escalate to a human checkpoint.

</td>
<td width="33%" valign="top">

### Feature 3
**Deep-Dive Trend Analysis**

Item Master reconciliation via decision tree D1 to D8, four DQ rules with reason codes, per-corridor KPIs split into planning vs history windows, Period-over-Period trend deltas, and eight deterministic analytics tables.

</td>
<td width="33%" valign="top">

### Feature 5
**Multi-Region Resource Planning**

Weather risk computed per corridor across nine waypoints over a 48-hour horizon. Greedy penalty-minimising allocator across three resource pools (standard trucks, temp trucks, drivers) with nine priority slot types.

</td>
</tr>
</table>

---

## Table of Contents

| | |
|---|---|
| [1. System Architecture](#1-system-architecture) | [6. Running the Pipeline](#6-running-the-pipeline) |
| [2. Feature 1 — Self-Correction Audit Loop](#2-feature-1--self-correction-audit-loop) | [7. Running the Tests](#7-running-the-tests) |
| [3. Feature 3 — Trend Analysis & Reconciliation](#3-feature-3--trend-analysis--reconciliation) | [8. Environment Variables](#8-environment-variables) |
| [4. Feature 5 — Multi-Region Resource Planning](#4-feature-5--multi-region-resource-planning) | [9. Observed Results](#9-observed-results) |
| [5. How the Features Interact](#5-how-the-features-interact) | [10. File Reference](#10-file-reference) |

---

## 1. System Architecture

### Corridors

| Corridor | Route | Default SLA | Waypoints |
|---|---|---|---|
| `C1_I95_NJ_BOS` | Newark NJ → Boston MA (I-95) | Tier 1 (≤ 6h) | 5 |
| `C2_NJ_PHL` | Newark NJ → Philadelphia PA | Tier 2 (≤ 12h) | 4 |

### LangGraph Pipeline

```
                          ┌─────────────────┐
                          │   pdf_context   │   RAG over Dispatch Playbook
                          │  (ContextAgent) │   business_context + KPI defs
                          └────────┬────────┘
                                   │
                ┌──────────────────┴──────────────────┐   parallel fan-out
                │                                     │
        ┌───────▼──────────┐              ┌───────────▼──────────────┐
        │  dq_reconcile    │              │  weather_multi_corridor   │
        │   [Feature 3]    │              │       [Feature 5]         │
        │                  │              │                           │
        │ • DQ-01..04      │              │ • 9 waypoints / 2 corr.   │
        │ • Item Master    │              │ • Day-by-day risk 0..3    │
        │ • Alias / Legacy │              │ • Buffer % per corridor   │
        │ • PoP Trends     │              │ • Escalation flag         │
        │ • 8 Deep-Dive    │              │                           │
        │   Tables         │              │                           │
        └────────┬─────────┘              └───────────┬───────────────┘
                 │                                    │
                 └─────────────────┬──────────────────┘   fan-in
                                   │
                          ┌────────▼──────────┐
                          │ resource_allocator│   greedy penalty allocator
                          │    [Feature 5]    │   3 pools, 9 priority slots
                          └────────┬──────────┘
                                   │
                          ┌────────▼──────────┐
                          │      planner      │ ◄── retry with violations
                          │  (structured JSON)│
                          └────────┬──────────┘
                                   │
                          ┌────────▼──────────┐
                          │       audit       │   LLM rules + deterministic
                          │    [Feature 1]    │   policy checks
                          └────────┬──────────┘
                                   │
              passed ──────────────┼──────────────► report
                                   │
              retries < 3 ─────────► planner (violations injected)
                                   │
              retries = 3 ─────────►┌──────────────────┐
              risk = 3/3            │ human_checkpoint │
                                    └────────┬─────────┘
                                             │
                                    ┌────────▼─────────┐
                                    │      report      │   HTML + audit trail
                                    │   (ReportAgent)  │   + deep-dive HTML
                                    └────────┬─────────┘
                                             │
                                    ┌────────▼─────────┐
                                    │       email      │   SMTP w/ attachments
                                    └──────────────────┘
```

### Agent Roster

| Agent | Role | Implementation |
|---|---|---|
| `ContextAgent` | Extracts KPI definitions, SLAs, dispatch rules from playbook PDF | RAG + LLM |
| `TrendOpsAgent` | Narrates corridor KPIs and PoP deltas from reconciled data | LLM |
| `ResourcePlannerAgent` | Translates allocation output into 5-section human brief | LLM |
| `PlannerAgent` | Drafts 48h dispatch plan as strict structured JSON | LLM |
| `AuditAgent` | Verifies plan against 6 playbook rules, returns JSON verdict | LLM |
| `ReportAgent` | Compiles HTML leadership report with embedded audit trail | LLM |

---

## 2. Feature 1 — Self-Correction Audit Loop

> **The plan must be self-correcting and policy-verifiable, not just plausible.**

### Two-Layer Audit Design

**Layer 1 — `AuditAgent` (LLM, prompt-driven)**

The LLM auditor checks six qualitative rules from the Dispatch Playbook and returns structured JSON.

| # | Rule | Playbook Reference |
|---|---|---|
| 1 | `risk_score ≥ 3` → plan must include `+40% buffer` AND `escalation required` | §5.2 |
| 2 | Tier 1 (life-critical) dispatches must fit within the 6h transit window | §7 |
| 3 | Cold-chain items must be assigned only to temperature-controlled trucks | §8 |
| 4 | DQ-excluded rows must not appear in truck volume or dispatch counts | §11–12 |
| 5 | Plan must acknowledge driver/truck availability constraints | §13.1 |
| 6 | Tier 1 units prioritised over Tier 2 when resources are scarce | §13.2 |

Sample response:

```json
{"passed": true, "violations": [], "severity": "low"}
```

**Layer 2 — Deterministic hard checks (`apply_deterministic_audit_checks`)**

Code-level checks that verify the planner's structured JSON fields against fixed policy constants. These fire regardless of what the LLM concluded.

| Check | Policy | What is verified |
|---|---|---|
| Buffer % | §5.2 | `recommended_buffer_pct` must equal exactly `{0:0, 1:10, 2:25, 3:40}[risk_score]` |
| Escalation flag | §5.2 | `escalation_required` must be `true` when `risk_score == 3` |
| Rule citations | D8 | `cited_rules` must be non-empty |

> The plan passes only if **both** the LLM returned `passed=true` AND no deterministic violation fired.

### Structured-JSON Planner Output

The `PlannerAgent` returns a strict JSON object — no prose, no markdown fences:

```json
{
  "recommended_buffer_pct": 25,
  "escalation_required": false,
  "cited_rules": [
    "Tier 1 SLA = 6h (§7)",
    "risk_score 2 → +25% buffer (§5.2)"
  ],
  "dispatch_plan": "Dispatch C1 Tier-1 cold-chain units by 08:00...",
  "what_to_monitor": ["Precipitation at New Haven", "Temp truck availability Day1"],
  "contingency_triggers": ["Precip exceeds 20mm → activate Tier-1 air freight"],
  "expected_kpi_impacts": ["C1 cold-chain SLA compliance target: 98%"]
}
```

### Retry Routing

```
audit result → passed?                          ──► report
audit result → failed, retries < 3              ──► back to planner (violations injected)
audit result → failed, retries = 3, risk = 3/3  ──► human_checkpoint (stdin approval)
audit result → failed, retries = 3, risk < 3/3  ──► report (warnings logged)
```

### Human-in-the-Loop Escalation

If the audit loop exhausts three retries while weather risk is at `3/3`, the pipeline pauses at `node_human_checkpoint`:

```
============================================================
  !! HUMAN ESCALATION REQUIRED !!
  Weather risk score: 3/3 (max)
  Audit failed after 3 attempts.
  Outstanding violations:
    - recommended_buffer_pct=25 does not match the required 40% ...
============================================================
Manager: approve dispatch report anyway? (yes/no):
```

A `no` raises `RuntimeError` and halts the run. A `yes` lets the report proceed with all violations logged in the Audit Trail section.

### Key Files

| File | Role |
|---|---|
| `src/prompts.py` | `PLANNER_PROMPT` (structured-JSON contract), `AUDIT_PROMPT` (6 checks, JSON output) |
| `src/agents.py` | `run_planner_agent()`, `run_audit_agent()`, `_extract_json_block()` |
| `src/graph.py` | `node_planner`, `node_audit`, `node_human_checkpoint`, `route_audit()`, `apply_deterministic_audit_checks()` |
| `tests/test_audit_logic.py` | 6 unit tests — buffer mapping, pass/fail logic, escalation, cited-rules check |

---

## 3. Feature 3 — Trend Analysis & Reconciliation

> **The data feeding the planner must be clean, correctly labelled, and trend-aware before any LLM ever sees it.**

The legacy pipeline ran `IsolationForest` over whatever numeric columns happened to exist. Feature 3 replaces this entirely with a **playbook-faithful, fully deterministic** data quality and trend pipeline.

### Item Master Reconciliation (Appendix A)

Three reference tables are hard-coded as module constants — no external CSV dependency:

**A.1 — Canonical Item Master** (11 items)

```
canonical_item_id | item_id | canonical_item_name        | medicine_type       | temp_control
RMD-100           | 10021   | Remdesivir 100mg           | Antiviral           | Cold (2-8C)
INS-LIS           | 10022   | Insulin Lispro             | Hormone             | Cold (2-8C)
PMB-KEY           | 10035   | Pembrolizumab              | Monoclonal Antibody | Cold (2-8C)
EPI-AI            | 10040   | Epinephrine Auto-Injector  | Emergency Drug      | Room Temp
HEP-SOD           | 10050   | Heparin Sodium             | Anticoagulant       | Room Temp
MOR-SUL           | 10060   | Morphine Sulfate           | Opioid Analgesic    | Controlled Storage
ALB-INH           | 10070   | Albuterol Inhaler          | Bronchodilator      | Room Temp
EXP-ONC-CT        | 99999   | Experimental Oncology Drug | Clinical Trial Drug | Strict Cold (-20C)
... (+ 3 more)
```

**A.2 — Name Aliases** (7 variants)

```
"Heparin Na"                 → HEP-SOD   (abbreviation)
"Pembrolizumab (Keytruda)"   → PMB-KEY   (brand in parentheses)
"Morphine Sulphate"          → MOR-SUL   (US/UK spelling)
"Albuterol Inhaler 90mcg"    → ALB-INH   (dose suffix)
```

**A.3 — Legacy ID Mapping** (4 mappings)

```
10020  → RMD-100    (vendor legacy ID)
1070   → ALB-INH    (truncated ID from older CSV exports)
```

### Decision Tree D1 → D8

Each shipment row is resolved through a strict precedence chain:

```
Row arrives
  │
  ├─ unique_item_id is null/empty?              ──► DQ-01 (exclude)
  │
  ├─ (item_id, item_name) exact hit in A.1?     ──► EXACT_MATCH ✓
  │
  ├─ item_name matches A.2 alias?
  │    ├─ alias canonical's item_id agrees?     ──► ALIAS_MATCH ✓
  │    ├─ row's item_id valid but different?    ──► DQ-03 (flag, name/id conflict)
  │    └─ row's item_id unknown?                ──► ALIAS_MATCH ✓
  │
  ├─ item_id in A.3 legacy map?                 ──► LEGACY_ID_MAP ✓
  │
  ├─ item_id in master but name unclear?        ──► DQ-03 (flag)
  │
  └─ item_id not in master or legacy?           ──► DQ-02 (exclude)

Post-resolution:
  └─ duplicate unique_item_id detected?         ──► DQ-04 (flag, first kept)
```

Every resolved row gets: `canonical_item_id`, `canonical_item_name`, `medicine_type`, `temp_control`, `reason_code`, `confidence_tier`, `sla_tier`, `is_valid`.

### Data Quality Rules

| Rule | Condition | Action | Reason Code |
|---|---|---|---|
| **DQ-01** | `unique_item_id` null or empty | Exclude from planning | `excluded_dq01_missing_uid` |
| **DQ-02** | `item_id` not in master or legacy | Exclude | `excluded_dq02_invalid_id` |
| **DQ-03** | `item_name` conflicts with `item_id`'s canonical | Flag (keep for investigation) | `flagged_dq03_name_mismatch` |
| **DQ-04** | `unique_item_id` appears more than once | Flag duplicate occurrences | `flagged_dq04_duplicate_uid` |

> DQ-excluded rows are **never counted** in truck volume or dispatch totals. Every exclusion is logged with a reason code in the audit trail.

### SLA Tier Back-Fill

At reconciliation time, `sla_tier` is computed from `medicine_type` and stored as a first-class column on every valid row. Single source of truth feeds every downstream consumer.

```
Tier 1 (life-critical, ≤ 6h):   Antiviral · Hormone · Monoclonal Antibody
                                 Emergency Drug · Anticoagulant · Clinical Trial Drug

Tier 2 (standard, ≤ 12h):       Opioid Analgesic · Bronchodilator
                                 (unknown types default to Tier 2)
```

### Per-Corridor KPIs

For each corridor, KPIs are computed separately for:
- **Planning window** — Day0 and Day1 rows (48h dispatch horizon)
- **History** — all prior days (baseline)
- **Overall** — full series

| Metric | Formula / Reference |
|---|---|
| `valid_units` | Count of DQ-passing rows |
| `excluded_rows` / `exclusion_rate` | DQ-excluded count + ratio |
| `cold_chain_units` / `cold_chain_pct` | Items requiring Cold 2-8°C or Strict Cold Chain −20°C |
| `tier1_units` / `tier1_pct` | Life-critical Tier 1 share |
| `trucks_standard_required` | `ceil(room_temp_units × 1.10 / 10)` (§7.2) |
| `trucks_temp_required` | `ceil(cold_chain_units × 1.10 / 10)` (§7.2) |
| `drivers_required` | `trucks_std + trucks_temp` |

### Period-over-Period (PoP) Trend

The history window is split at its **date midpoint** into Period A and Period B. Per-corridor deltas surface whether valid volume, cold-chain load, and Tier-1 share rose, fell, or held flat.

```
period_a_window: 2026-02-20 → 2026-02-26
period_b_window: 2026-02-27 → 2026-03-05

C1_I95_NJ_BOS:
  valid_units:      A=45  B=52   Δ=+7    (+15.6%)
  cold_chain_units: A=28  B=31   Δ=+3    (+10.7%)
  tier1_pct:        A=0.73 B=0.75 Δ=+2.7%
```

> Planning-window rows are **excluded from the PoP baseline** — no data leakage between the "test" window and the historical baseline.

### Deep-Dive Appendix (8 Tables)

| Table | What it shows |
|---|---|
| Daily Valid Shipment Trend | Valid units per shipment_date + planning_day |
| Planning Window Corridor by Day | Valid units per corridor per Day0/Day1 |
| Top Item Spikes vs Baseline | Planning count vs historical daily avg; ≥ 1.5× flagged |
| Correction Breakdown | Alias-match and legacy-ID-map counts |
| Exclusion Breakdown by Reason | DQ-01..04 count by reason_code |
| Excluded Rows by Day | Per planning_day exclusion counts |
| Sample Corrected Rows | First 8 alias/legacy rows with before/after IDs |
| Sample Excluded Rows | First 8 excluded rows for investigation |

### Performance Optimizations

| Optimization | Impact |
|---|---|
| `_LOOKUPS_CACHE` — lookup tables built lazily, not at import time | Avoids Windows chromadb DLL crash; zero per-row overhead after first call |
| `resolution_cache` — `(item_id, item_name)` memoization | O(1) for repeated SKUs (high repetition typical) |
| Vectorized merge for attribute back-fill | Single `DataFrame.merge()` replaces per-row lookups |
| `business_context` cache keyed on source fingerprint | Warm rerun skips ContextAgent LLM call (~73× faster) |
| Deferred pandas import inside `node_dq_reconcile` | Eliminates access violation in chromadb rust client on Windows |

### Key Files

| File | Role |
|---|---|
| `src/tools/dq_tools.py` | Appendix A tables, `reconcile_shipments()`, DQ-01..04, D3→D8 decision tree |
| `src/tools/trend_tools.py` | `compute_corridor_kpis()`, `compute_pop_trend()`, `compute_deep_dive_tables()` |
| `src/tools/pdf_tools.py` | `PdfRag` with section-aware split; fingerprint cache |
| `src/tools/knowledge_tools.py` | RAG evaluation — Recall@k + grounded-answer accuracy |
| `src/agents.py` | `run_trend_agent()` — narrates corridor/PoP KPIs without inventing numbers |
| `src/prompts.py` | `TREND_OPS_PROMPT` — strict no-hallucination constraint |
| `tests/test_dq_tools.py` | 10 unit tests — exact/alias/legacy, DQ-01..04, SLA back-fill |
| `tests/test_trend_tools.py` | 4 unit tests — capacity model, corridor KPIs, PoP, deep-dive shapes |

---

## 4. Feature 5 — Multi-Region Resource Planning

> **A single weather sample at Newark cannot tell you whether a Boston-bound truck will hit a storm at New Haven. Resource planning that ignores per-leg weather and per-day shortfalls is operationally blind.**

### Multi-Corridor, Multi-Day Weather Risk

Replaces the legacy single-point weather sample with a corridor-aware scoring system that fetches Open-Meteo forecasts for every waypoint and aggregates risk across days.

**Waypoints (Playbook §3.2):**

```
C1_I95_NJ_BOS   (5 waypoints)        C2_NJ_PHL   (4 waypoints)
─────────────────────────────        ──────────────────────────
Newark, NJ                            Newark, NJ
New Haven, CT                         Edison, NJ
Hartford, CT                          Trenton, NJ
Worcester, MA                         Philadelphia, PA
Boston, MA
```

**Risk scoring (per waypoint per day):**

| Hazard | Threshold | Score Contribution |
|---|---|---|
| Precipitation | ≥ 15 mm | +1 |
| Wind gusts | ≥ 45 km/h | +1 |
| Minimum temperature | ≤ 0 °C | +1 |

**Aggregation:**

```
waypoint_score = sum of hazard flags (0..3, capped)
day_score      = max over all waypoints in corridor for that day
corridor_score = max over Day0 and Day1 (the 48h horizon)
```

Each corridor emits:

```json
{
  "corridor_id": "C1_I95_NJ_BOS",
  "risk_score_0_3": 2,
  "travel_buffer_pct": 25,
  "escalation_required": false,
  "waypoint_detail": [...]
}
```

A `worst_corridor_risk()` helper picks the maximum-risk corridor and returns it under `state["weather_risk"]` — keeping the deterministic audit layer backward-compatible without touching its logic.

### Penalty-Minimising Resource Allocation

`compute_demand()` translates reconciled shipments into per-day demand vectors. `allocate_resources()` then greedily fills the highest-penalty slots first under three pool constraints (standard trucks, temp-controlled trucks, drivers).

**Slot priority and penalties:**

| Slot | Penalty (per unmet unit) |
|---|---|
| Tier 1 — Cold chain | **180** |
| Tier 2 — Cold chain | **120** |
| Tier 1 — Room temp | **100** |
| Tier 2 — Room temp | **40** |
| Delay (slot served on Day1 instead of Day0) | **10** |

**Allocation output:**

```python
{
  "by_corridor_day": {
    "C1_I95_NJ_BOS": {
      "2026-05-28": {
        "tier1_cold_served": 18, "tier1_cold_unmet": 0,
        "tier1_room_served": 12, "tier1_room_unmet": 2,
        "tier2_cold_served": 4,  "tier2_cold_unmet": 0,
        "tier2_room_served": 5,  "tier2_room_unmet": 0,
        "penalty": 200,
        "bottleneck": "drivers"
      }
    }
  },
  "total_penalty": 380,
  "total_unmet_units": 4,
  "summary_by_day": {...},
  "penalty_model": {...}
}
```

Bottleneck detection returns one of `"trucks_std"`, `"trucks_temp"`, `"drivers"`, or `"none"` — whichever pool ran out first during the greedy fill.

### Parallel Fan-Out Execution

`node_dq_reconcile` and `node_weather_multi_corridor` write to **disjoint AppState keys** — letting LangGraph's deep-merge run them concurrently with no conflicts:

```python
g.add_edge("pdf_context", "dq_reconcile")
g.add_edge("pdf_context", "weather_multi_corridor")
g.add_edge("dq_reconcile", "resource_allocator")
g.add_edge("weather_multi_corridor", "resource_allocator")
g.add_edge("resource_allocator", "planner")
```

### `ResourcePlannerAgent` Brief

After allocation runs, the LLM produces a 5-section human brief:

1. **Resource Summary** — total demand vs availability
2. **By-Corridor Plan** — per-corridor Day0/Day1 assignments
3. **SLA Risk Flags** — Tier-1 cold-chain shortfalls
4. **Weather Interaction** — how risk inflates demand or constrains routing
5. **Recommended Actions** — concrete next steps

Prompt enforces a strict no-hallucination constraint: the agent may only narrate numbers that appear in the supplied demand/allocation/weather payloads.

### Key Files

| File | Role |
|---|---|
| `src/tools/weather_tools.py` | `_CORRIDORS`, `get_corridor_weather()`, `get_all_corridors_weather()`, `worst_corridor_risk()` |
| `src/tools/resource_tools.py` | `load_resource_availability()`, `compute_demand()`, `allocate_resources()`, `_SLOT_PRIORITY` |
| `src/agents.py` | `run_resource_planner_agent()` |
| `src/prompts.py` | `RESOURCE_PLANNER_PROMPT` — 5-section structure with anti-hallucination clause |
| `src/graph.py` | `node_weather_multi_corridor`, `node_resource_allocator`, parallel fan-out wiring |
| `tests/test_resource_tools.py` | 8 unit tests — CSV parse, capacity model, penalty math, bottleneck detection |

---

## 5. How the Features Interact

Each feature feeds the next. The chain is intentional: Feature 3 cleans the data, Feature 5 plans the resources, Feature 1 verifies the plan.

```
       ┌──────────────────────────────────────────────────────────────┐
       │                                                              │
       │   Feature 3                Feature 5               Feature 1 │
       │   ─────────                ─────────               ───────── │
       │                                                              │
       │   reconciled_df ─────┐                                       │
       │                      │                                       │
       │   corridor_kpis ─────┼──► resource_demand ──┐                │
       │                      │                      │                │
       │   weather_by_corr ───┘                      ├──► dispatch_plan ──► audit
       │                                             │         │      │
       │                              allocation ────┘         │      │
       │                                                       │      │
       │   ops_insights ─────────────────────────────────────► │      │
       │                                                       │      │
       │                              resource_insights ─────► │      │
       │                                                              │
       └──────────────────────────────────────────────────────────────┘
```

**Three concrete handshakes:**

| # | From | To | What flows |
|---|---|---|---|
| 1 | Feature 3 reconciler | Feature 5 demand | `is_valid` rows only; DQ-excluded rows never become truck volume |
| 2 | Feature 5 allocator | Feature 1 planner | `total_penalty`, bottleneck pool, per-corridor shortfalls fed to `PlannerAgent` |
| 3 | Feature 5 weather | Feature 1 audit | `risk_score_0_3` per corridor verified against §5.2 buffer/escalation policy |

> The deterministic audit (Feature 1) can verify exclusion enforcement with certainty because Feature 3 is the sole source of `is_valid` flags. No LLM guessing in the loop.

---

## 6. Running the Pipeline

### Prerequisites

```bash
pip install -r requirements.txt
```

### Configure

Create a `.env` at the project root (see [Environment Variables](#8-environment-variables) for the full list):

```env
OPENAI_API_KEY=sk-...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=sender@gmail.com
SMTP_PASSWORD=your-app-password
REPORT_EMAIL_TO=ops@example.com
```

### Run

From the project root:

```bash
python src/main.py
```

**The pipeline will:**

1. Index the Dispatch Playbook into ChromaDB (skipped on warm runs if source unchanged)
2. Reconcile the 14-day shipment CSV against Appendix A
3. Compute corridor KPIs, PoP trend, and deep-dive tables
4. Fetch weather for all 9 corridor waypoints (parallel with step 2)
5. Compute resource demand and run the penalty allocator
6. Draft a dispatch plan (PlannerAgent → structured JSON)
7. Audit the plan (up to 3 retry cycles, human checkpoint if risk = 3/3)
8. Generate the HTML report + deep-dive text appendix
9. Email the report (if `REPORT_EMAIL_TO` is set)

**Outputs:**

- `report_output.html` — full leadership report with embedded deep-dive appendix
- `report_appendix.txt` — plain-text version of the 8 analytics tables

### Typical Console Output

```
[RAGEval] Recall@5=1.0 grounded_accuracy=1.0
[ContextAgent] Cache hit — reusing business_context.
[DQReconcile] total=129 valid=124 excluded=5 flagged=0 (DQ-01=5, DQ-02=0, DQ-03=0, DQ-04=0)
[Timing] dq_reconcile took 7.04s
[Timing] weather_multi_corridor took 2.10s
[ResourceAllocator] total_penalty=380 unmet=4 bottleneck=drivers
[AuditAgent] Attempt 1/3: PASSED
[Timing] audit took 1.86s
[Timing] report took 22.40s
[Email] Sent to vishwvekariya094@gmail.com
```

---

## 7. Running the Tests

```bash
python -m pytest tests/ -v
```

> **Expected: 29 tests, 0 failures**

```
tests/test_audit_logic.py    :: 6 tests  · Feature 1 deterministic checks
tests/test_dq_tools.py       :: 10 tests · Feature 3 reconciliation + DQ rules
tests/test_resource_tools.py :: 8 tests  · Feature 5 penalty model + bottleneck
tests/test_trend_tools.py    :: 4 tests  · Feature 3 KPIs + PoP
tests/test_smoke.py          :: 1 test   · Import + build_graph smoke test
```

No `OPENAI_API_KEY` is required to run the tests — the lazy `_get_llm()` pattern ensures `ChatOpenAI` is never instantiated during the test session.

---

## 8. Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes | — | OpenAI API key (GPT-4.1-mini) |
| `WEATHER_TZ` | No | `America/New_York` | Timezone for waypoint forecasts |
| `SMTP_HOST` | No | — | SMTP host (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | No | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` | No | — | SMTP login address |
| `SMTP_PASSWORD` | No | — | SMTP app password (NOT regular password) |
| `REPORT_EMAIL_TO` | No | — | Recipient address; email step skipped if blank |
| `LANGCHAIN_TRACING_V2` | No | — | Set `true` to enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | No | — | LangSmith API key |
| `LANGCHAIN_PROJECT` | No | — | LangSmith project name |

> **Gmail users:** the password must be an **App Password** generated at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-Step Verification enabled). Plain Gmail passwords will be rejected.

---

## 9. Observed Results

### 14-Day Multi-Corridor CSV (129 rows)

```
Total rows          :  129
Valid rows          :  124   (96.1%)
Excluded (DQ-01)    :    5   (all missing unique_item_id)
DQ-02 / 03 / 04     :    0 / 0 / 0
exact_match         :  100
alias_match         :   24
Tier 1 valid units  :   95   (76.6%)
Tier 2 valid units  :   29
C1 valid / total    :   63 / 66
C2 valid / total    :   61 / 63
```

### Test Suite

```
============================================================
 platform win32 -- Python 3.11.X, pytest-8.X.X
============================================================
 tests/test_audit_logic.py ......                     [ 20%]
 tests/test_dq_tools.py ..........                    [ 55%]
 tests/test_resource_tools.py ........                [ 82%]
 tests/test_smoke.py .                                [ 86%]
 tests/test_trend_tools.py ....                       [100%]
============================================================
            29 passed in 40.88s
============================================================
```

### Performance (Cold vs Warm Run)

| Stage | Cold Run | Warm Run |
|---|---|---|
| `pdf_context` (RAG + ContextAgent) | ~30s | ~0.4s (cache hit) |
| `dq_reconcile` | 7.0s | 7.0s |
| `weather_multi_corridor` | 2.1s | 2.1s (parallel with dq) |
| `resource_allocator` | 1.5s | 1.5s |
| `planner` + `audit` (1 attempt) | 4.0s | 4.0s |
| `report` | 22.4s | 22.4s |

---

## 10. File Reference

```
src/
├── main.py                  Entry point — sets input paths, invokes graph
├── graph.py                 LangGraph StateGraph — all nodes, routing, AppState
├── agents.py                LLM agent wrappers (lazy init, JSON parsing)
├── prompts.py               ChatPromptTemplates for every agent
├── tracing.py               LangSmith probe — disables tracing on bad key
└── tools/
    ├── dq_tools.py          [F3] Item Master reconciliation, DQ-01..04
    ├── trend_tools.py       [F3] corridor KPIs, PoP trend, deep-dive tables
    ├── pdf_tools.py         [F3] RAG: fingerprint cache, section-aware split
    ├── knowledge_tools.py   [F3] RAG eval: Recall@k + grounded-answer accuracy
    ├── weather_tools.py     [F5] multi-waypoint per-corridor weather risk
    ├── resource_tools.py    [F5] demand computation + penalty allocator
    └── email_tools.py       SMTP send with attachments + timeout handling

tests/
├── conftest.py              sys.path → src/
├── test_audit_logic.py      [F1] 6 unit tests
├── test_dq_tools.py         [F3] 10 unit tests
├── test_trend_tools.py      [F3] 4 unit tests
├── test_resource_tools.py   [F5] 8 unit tests
└── test_smoke.py            Import + graph assembly smoke test

data-for-enhancement/
├── SeeWeeS Specialty Dispatch Playbook.md     Source of truth (RAG + DQ rules)
├── Incoming_shipments_14d_multi_corridor.csv  14-day shipment data (C1 + C2)
└── Resource_availability_48h.csv              Driver and truck pool per day

outputs/
├── report_output.html        Full leadership report (generated)
├── report_appendix.txt       Plain-text deep-dive tables (generated)
└── Technical_Business_Report.docx / .pdf   Generated technical documentation
```

---

<div align="center">

**UCLA MSBA · AI Agents · 2026**

Built with [LangGraph](https://github.com/langchain-ai/langgraph) · [LangChain](https://github.com/langchain-ai/langchain) · [ChromaDB](https://www.trychroma.com/) · [Open-Meteo](https://open-meteo.com/)

</div>
