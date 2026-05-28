# SeeWeeS Ops Reporting Agent
## Feature 1 — Self-Correction Audit Loop  &  Feature 3 — Deep-Dive Trend Analysis
**UCLA MSBA AI Agents Project · 2026**

---

## Table of Contents

1. [Project Context](#1-project-context)
2. [Feature 1 — Self-Correction & Quality Assurance Audit Loop](#2-feature-1--self-correction--quality-assurance-audit-loop)
3. [Feature 3 — Deep-Dive Trend Analysis + Item Master Reconciliation](#3-feature-3--deep-dive-trend-analysis--item-master-reconciliation)
4. [How the Two Features Interact](#4-how-the-two-features-interact)
5. [Running the Pipeline](#5-running-the-pipeline)
6. [Running the Tests](#6-running-the-tests)
7. [File Reference](#7-file-reference)

---

## 1. Project Context

SeeWeeS operates a specialty-medicine dispatch network shipping life-critical drugs from New Jersey distribution centers to hospitals across two corridors:

| Corridor | Route | Default SLA |
|---|---|---|
| C1_I95_NJ_BOS | Newark NJ → Boston MA (I-95) | Tier 1 (≤ 6h) |
| C2_NJ_PHL | Newark NJ → Philadelphia PA | Tier 2 (≤ 12h) |

The multi-agent pipeline reads a 14-day shipment CSV, reconciles item identity against the Dispatch Playbook, analyses trends and data quality, then produces an audit-approved HTML dispatch report.

Features 1 and 3 are the two core pillars of the system. Feature 1 ensures the plan is self-correcting and verifiable. Feature 3 ensures the data feeding that plan is clean, correctly labelled, and trend-aware.

---

## 2. Feature 1 — Self-Correction & Quality Assurance Audit Loop

### What it does

The PlannerAgent drafts a 48-hour dispatch recommendation. Before that plan reaches the ReportAgent, it passes through an **AuditAgent loop** that can reject the plan and force the PlannerAgent to revise. If the plan keeps failing after three attempts and the weather risk is at maximum severity, the system escalates to a human manager for approval.

This turns the pipeline from a one-shot pipeline into a **self-correcting, policy-verifiable system**.

### Graph architecture

```
node_planner  ◄──────────────────── retry (violations fed back)
      │
      ▼
node_audit
      │
      ├── passed=True             ──► node_report
      │
      ├── retries < 3, failed    ──► node_planner  (with violations list)
      │
      ├── retries ≥ 3, score = 3 ──► node_human_checkpoint
      │                                     │
      │                                     └── approved ──► node_report
      │
      └── retries ≥ 3, score < 3 ──► node_report  (warnings logged)
```

### Two-layer audit design

The audit combines an LLM check with a deterministic hard-check layer. Both must pass.

**Layer 1 — AuditAgent (LLM, `AUDIT_PROMPT`)**

The LLM auditor checks six qualitative rules from the Dispatch Playbook and returns structured JSON:

```json
{"passed": true, "violations": [], "severity": "low"}
```

Rules checked:
1. Risk score ≥ 3 → plan must mention `+40% travel buffer` and `escalation required`
2. Tier 1 medicines must meet the ≤ 6h SLA transit window
3. Cold-chain items (Cold 2-8°C / Strict Cold Chain −20°C) must use temp-controlled trucks
4. DQ-excluded rows must not appear in truck volumes or dispatch totals
5. Driver and truck limits per corridor per day must be acknowledged
6. When resources are scarce, Tier 1 must be dispatched before Tier 2

**Layer 2 — Deterministic hard checks (`apply_deterministic_audit_checks`)**

Code-level checks that verify the planner's *structured JSON fields* against fixed policy constants. These fire regardless of what the LLM concluded:

| Check | Policy reference | What is verified |
|---|---|---|
| Buffer % | §5.2 | `recommended_buffer_pct` must equal exactly `{0:0, 1:10, 2:25, 3:40}[risk_score]` |
| Escalation flag | §5.2 | `escalation_required` must be `true` when `risk_score == 3` |
| Rule citations | D8 | `cited_rules` must be non-empty |

The plan passes only if **both** the LLM returned `passed=true` AND no deterministic violation fired.

### Structured-JSON planner output

The PlannerAgent returns a strict JSON object (no prose, no markdown fences):

```json
{
  "recommended_buffer_pct": 25,
  "escalation_required": false,
  "cited_rules": ["Tier 1 SLA = 6h (§7)", "risk_score 2 → +25% buffer (§5.2)"],
  "dispatch_plan": "Dispatch C1 Tier-1 cold-chain units by 08:00...",
  "what_to_monitor": ["Precipitation at New Haven", "Temp truck availability Day1"],
  "contingency_triggers": ["Precip exceeds 20mm → activate Tier-1 air freight"],
  "expected_kpi_impacts": ["C1 cold-chain SLA compliance target: 98%"]
}
```

The deterministic audit layer reads `recommended_buffer_pct`, `escalation_required`, and `cited_rules` directly from this object — making the self-correction loop **code-verifiable**, not judgment-based.

### Human-in-the-loop escalation (Feature 4 lite)

If the audit loop exhausts three retries with `risk_score == 3`, the pipeline pauses at `node_human_checkpoint` and prints a structured escalation block to the terminal:

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

A `no` response raises `RuntimeError` and halts the pipeline. A `yes` response lets the report proceed with all violations logged in the Audit Trail section.

### Key files — Feature 1

| File | Role |
|---|---|
| `src/prompts.py` | `PLANNER_PROMPT` (structured-JSON contract), `AUDIT_PROMPT` (6 checks, JSON output) |
| `src/agents.py` | `run_planner_agent()`, `run_audit_agent()`, `_extract_json_block()` |
| `src/graph.py` | `node_planner`, `node_audit`, `node_human_checkpoint`, `route_audit()`, `apply_deterministic_audit_checks()`, `_expected_buffer_pct()` |
| `tests/test_audit_logic.py` | 6 unit tests covering buffer mapping, pass/fail logic, escalation, cited-rules check |

---

## 3. Feature 3 — Deep-Dive Trend Analysis + Item Master Reconciliation

### What it does

The legacy pipeline ran IsolationForest on whatever numeric columns happened to be present and called it analysis. Feature 3 replaces this entirely with a **playbook-faithful, fully deterministic** data quality and trend pipeline:

1. **Item Master Reconciliation** — every shipment row is resolved to a canonical item identity using the three Appendix A reference tables and decision rules D1–D8
2. **Data Quality Enforcement** — DQ-01 through DQ-04 are applied, with reason codes and audit-ready counts for every violation
3. **Corridor KPI computation** — valid vs excluded counts, cold-chain split, Tier-1 share, truck requirements — per corridor, per time window
4. **Period-over-Period (PoP) trend** — history window split at its date midpoint; KPI deltas surfaced for both corridors
5. **Deep-dive appendix** — 8 deterministic tables rendered to HTML + emailed as a plain-text attachment

### Item Master Reconciliation (Appendix A)

Three reference tables are hard-coded as module constants (no external CSV dependency):

**A.1 — Canonical Item Master (11 items)**

```
canonical_item_id | item_id | canonical_item_name           | medicine_type       | temp_control
RMD-100           | 10021   | Remdesivir 100mg              | Antiviral           | Cold (2-8C)
INS-LIS           | 10022   | Insulin Lispro                | Hormone             | Cold (2-8C)
PMB-KEY           | 10035   | Pembrolizumab                 | Monoclonal Antibody | Cold (2-8C)
EPI-AI            | 10040   | Epinephrine Auto-Injector     | Emergency Drug      | Room Temp (20-25C)
HEP-SOD           | 10050   | Heparin Sodium                | Anticoagulant       | Room Temp (20-25C)
MOR-SUL           | 10060   | Morphine Sulfate              | Opioid Analgesic    | Controlled Storage
ALB-INH           | 10070   | Albuterol Inhaler             | Bronchodilator      | Room Temp (20-25C)
EXP-ONC-CT        | 99999   | Experimental Oncology Drug    | Clinical Trial Drug | Strict Cold Chain (-20C)
... (+ 3 more)
```

**A.2 — Name Alias Table (7 aliases)**

Accepted name variants (space variants, brand names, abbreviations, spelling variants):

```
"Heparin Na"                 → HEP-SOD   (abbreviation)
"Pembrolizumab (Keytruda)"   → PMB-KEY   (brand in parentheses)
"Morphine Sulphate"          → MOR-SUL   (US/UK spelling)
"Albuterol Inhaler 90mcg"    → ALB-INH   (dose suffix)
... (+ 3 more)
```

**A.3 — Legacy / Deprecated ID Mapping (4 mappings)**

```
10020  → RMD-100    (vendor legacy ID)
1070   → ALB-INH    (truncated ID in older CSV exports)
... (+ 2 more)
```

### Decision tree per row (D1 → D8)

Each row is resolved through a strict precedence chain:

```
Row arrives
  │
  ├─ unique_item_id is null/empty?         → DQ-01 (exclude)
  │
  ├─ (item_id, item_name) exact hit in A.1? → EXACT_MATCH ✓
  │
  ├─ item_name matches A.2 alias?
  │    ├─ alias canonical's item_id agrees with row's item_id?  → ALIAS_MATCH ✓
  │    ├─ row's item_id is a valid master item_id (different)?  → DQ-03 (flag, name vs id conflict)
  │    └─ row's item_id unknown?                                → ALIAS_MATCH ✓
  │
  ├─ item_id in A.3 legacy map?            → LEGACY_ID_MAP ✓
  │
  ├─ item_id in master but name unclear?   → DQ-03 (flag)
  │
  └─ item_id not in master or legacy?      → DQ-02 (exclude)

Post-resolution:
  └─ duplicate unique_item_id detected?   → DQ-04 (flag, first occurrence kept)
```

Every resolved row gets: `canonical_item_id`, `canonical_item_name`, `medicine_type`, `temp_control`, `reason_code`, `confidence_tier`, `sla_tier`, `is_valid`.

### Data Quality rules

| Rule | Condition | Action | Reason code |
|---|---|---|---|
| DQ-01 | `unique_item_id` null or empty | Exclude from planning | `excluded_dq01_missing_uid` |
| DQ-02 | `item_id` not in master or legacy | Exclude | `excluded_dq02_invalid_id` |
| DQ-03 | `item_name` conflicts with `item_id`'s known canonical | Flag (keep for investigation) | `flagged_dq03_name_mismatch` |
| DQ-04 | `unique_item_id` appears more than once | Flag duplicate occurrences | `flagged_dq04_duplicate_uid` |

DQ-excluded rows are **never counted** in truck volume or dispatch totals. Every exclusion is logged with a reason code and reported in the audit trail.

### SLA tier back-fill

At reconciliation time, `sla_tier` is computed from `medicine_type` and stored as a first-class column on every valid row. This single source of truth feeds every downstream consumer — trend tools, resource allocator, audit checks — without re-deriving the tier.

```
Tier 1 (life-critical, ≤ 6h SLA):  Antiviral, Hormone, Monoclonal Antibody,
                                     Emergency Drug, Anticoagulant, Clinical Trial Drug
Tier 2 (standard specialty, ≤ 12h): Opioid Analgesic, Bronchodilator
                                     (any unknown type defaults to Tier 2)
```

### Corridor KPIs

For each corridor (`C1_I95_NJ_BOS`, `C2_NJ_PHL`), KPIs are computed separately for:
- **Planning window** — Day0 and Day1 rows (the 48h dispatch horizon)
- **History** — all prior days (baseline)
- **Overall** — everything combined

Metrics per slice:

| Metric | Playbook reference |
|---|---|
| `valid_units` | Count of DQ-passing rows |
| `excluded_rows` / `exclusion_rate` | DQ-excluded count + ratio |
| `cold_chain_units` / `cold_chain_pct` | Items requiring Cold 2-8°C or Strict Cold Chain −20°C |
| `tier1_units` / `tier1_pct` | Life-critical Tier 1 mix |
| `trucks_standard_required` | `ceil(room_temp_units × 1.10 / 10)` (§7.2) |
| `trucks_temp_required` | `ceil(cold_chain_units × 1.10 / 10)` (§7.2) |
| `drivers_required` | `trucks_std + trucks_temp` |

### Period-over-Period (PoP) trend

The history window is split at its **date midpoint** into Period A (earlier) and Period B (later). For each corridor, the delta table shows whether valid volume, cold-chain load, and Tier-1 share rose, fell, or held flat.

```
period_a_window: 2026-02-20 → 2026-02-26
period_b_window: 2026-02-27 → 2026-03-05

C1_I95_NJ_BOS:
  valid_units:      A=45  B=52  Δ=+7  (+15.6%)
  cold_chain_units: A=28  B=31  Δ=+3  (+10.7%)
  tier1_pct:        A=0.73 B=0.75 Δ=+2.7%
```

Planning-window rows are excluded from the PoP baseline — no data leakage between the "test" window and the historical "training" baseline.

### Deep-dive analytics appendix

Eight deterministic tables are computed after reconciliation and emailed as a plain-text attachment alongside the HTML report:

| Table | What it shows |
|---|---|
| Daily Valid Shipment Trend | Valid units per shipment_date + planning_day |
| Planning Window Corridor by Day | Valid units per corridor per Day0/Day1 |
| Top Item Spikes vs Historical Baseline | Planning-window count vs historical daily avg; spike_ratio ≥ 1.5× flagged |
| Correction Breakdown | Alias-match and legacy-ID-map counts |
| Exclusion Breakdown by Reason | DQ-01..04 count by reason_code |
| Excluded Rows by Day and Reason | Per planning_day exclusion counts |
| Sample Corrected Rows | First 8 alias/legacy-corrected rows with before/after IDs |
| Sample Excluded / Unresolved Rows | First 8 excluded rows for investigation |

Item spike analysis uses strictly disjoint data partitions: planning-window counts are never mixed into the historical daily average computation.

### Performance optimizations

| Optimization | Impact |
|---|---|
| `_LOOKUPS_CACHE` — lookup tables built once, not at import time | Avoids Windows pandas/chromadb DLL crash; zero per-row overhead after first call |
| `resolution_cache` — `(item_id, item_name)` memoization | O(1) for repeated SKUs (most shipment CSVs have high repetition) |
| Vectorized merge for attribute back-fill | Single `DataFrame.merge()` replaces per-row attribute lookups |
| `business_context` LLM cache keyed on source fingerprint | Warm rerun skips the ContextAgent LLM call entirely (~73× faster on `node_pdf_context`) |
| Deferred pandas import inside `node_dq_reconcile` | Eliminates access violation in chromadb rust client on Windows |

### Observed results on the 14-day multi-corridor CSV (129 rows)

```
Total rows          : 129
Valid rows          : 124   (96.1%)
Excluded (DQ-01)    :   5   (all missing unique_item_id)
DQ-02 / 03 / 04    :   0 / 0 / 0
exact_match         : 100
alias_match         :  24
Tier 1 valid units  :  95   (76.6%)
Tier 2 valid units  :  29
C1 valid / total    :  63 / 66
C2 valid / total    :  61 / 63
```

### Key files — Feature 3

| File | Role |
|---|---|
| `src/tools/dq_tools.py` | Appendix A tables, `reconcile_shipments()`, `assign_tier()`, DQ-01..04 logic, D3→D8 decision tree |
| `src/tools/trend_tools.py` | `compute_corridor_kpis()`, `compute_pop_trend()`, `compare_corridors()`, `compute_item_spikes()`, `compute_deep_dive_tables()` |
| `src/tools/pdf_tools.py` | `PdfRag` with section-aware markdown split; `load/save_cached_business_context()` |
| `src/tools/knowledge_tools.py` | RAG evaluation harness — Recall@k + grounded-answer accuracy |
| `src/agents.py` | `run_trend_agent()` — narrates corridor/PoP KPIs without inventing numbers |
| `src/prompts.py` | `TREND_OPS_PROMPT` — corridor-grounded prompt with strict no-hallucination constraint |
| `src/graph.py` | `node_dq_reconcile`, `render_deep_dive_text()`, `render_deep_dive_html()` |
| `tests/test_dq_tools.py` | 10 unit tests — exact/alias/legacy match, DQ-01..04, SLA tier back-fill, edge cases |
| `tests/test_trend_tools.py` | 4 unit tests — capacity model, corridor KPIs, PoP trend, deep-dive table shapes |
| `data-for-enhancement/Incoming_shipments_14d_multi_corridor.csv` | Input shipment data (14 days, 2 corridors) |
| `data-for-enhancement/SeeWeeS Specialty Dispatch Playbook.md` | Source of truth for all DQ rules, SLA definitions, and KPI formulas |

---

## 4. How the Two Features Interact

Feature 3 feeds Feature 1. The interaction happens at two points:

**Point 1 — Exclusion enforcement in the audit**

The AuditAgent (Feature 1) checks that "DQ-excluded rows are not counted in truck volumes or dispatch totals." The deterministic reconciler (Feature 3) is the only source of `is_valid` flags — so the audit can verify this rule with certainty rather than asking the LLM to guess.

**Point 2 — Structured planner input**

`run_trend_agent()` produces `ops_insights` — a markdown brief with exact corridor KPI numbers. This text is injected verbatim into `PLANNER_PROMPT`, giving the PlannerAgent precise exclusion rates, cold-chain percentages, and truck requirements per corridor. The deterministic audit then verifies that the planner's output is consistent with those numbers.

**Data flow:**

```
CSV → reconcile_shipments()          DQ report, valid DataFrame, reason codes
         │                                │
         ▼                                ▼
  compute_corridor_kpis()         AuditAgent Rule 4:
  compute_pop_trend()             "Excluded rows must not appear in dispatch totals"
         │                        (enforced via is_valid column)
         ▼
  run_trend_agent()  →  ops_insights  →  PlannerAgent  →  AuditAgent  →  Report
```

---

## 5. Running the Pipeline

### Prerequisites

```
pip install -r requirements.txt
```

Create a `.env` file at the project root:

```env
OPENAI_API_KEY=sk-...
LANGSMITH_API_KEY=ls__...   # optional — tracing disabled if key is invalid/missing
REPORT_EMAIL_TO=ops@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=sender@example.com
SMTP_PASS=...
```

### Run

From the project root:

```
python src/main.py
```

The pipeline will:
1. Index the Dispatch Playbook into ChromaDB (skipped on warm runs if source is unchanged)
2. Reconcile the 14-day shipment CSV against Appendix A
3. Compute corridor KPIs, PoP trend, and deep-dive tables
4. Fetch weather for all 9 corridor waypoints
5. Draft a dispatch plan (PlannerAgent)
6. Audit the plan (up to 3 retry cycles)
7. Generate the HTML report and deep-dive text appendix
8. Email the report (if `REPORT_EMAIL_TO` is set)

Outputs:
- `report_output.html` — full leadership report with embedded deep-dive appendix
- `report_appendix.txt` — plain-text version of the 8 analytics tables

### Console output (typical cold run)

```
[RAGEval] Recall@5=1.0 grounded_accuracy=1.0
[ContextAgent] Cache hit — reusing business_context.
[DQReconcile] total=129 valid=124 excluded=5 flagged=0 (DQ-01=5, DQ-02=0, DQ-03=0, DQ-04=0)
[Timing] dq_reconcile took 7.04s
[Timing] weather_multi_corridor took 2.1s
[AuditAgent] Attempt 1/3: PASSED
[Timing] audit took 1.86s
[Timing] report took 22.40s
```

---

## 6. Running the Tests

```
python -m pytest tests/ -v
```

Expected output: **29 tests, 0 failures**

```
tests/test_audit_logic.py   ::  6 tests   (Feature 1 deterministic checks)
tests/test_dq_tools.py      :: 10 tests   (Feature 3 reconciliation + DQ rules)
tests/test_resource_tools.py::  8 tests   (Feature 5 penalty model)
tests/test_trend_tools.py   ::  4 tests   (Feature 3 KPI + PoP)
tests/test_smoke.py         ::  1 test    (import + build_graph smoke test)
```

No `OPENAI_API_KEY` is required to run the tests — the lazy `_get_llm()` pattern ensures `ChatOpenAI` is never instantiated during the test session.

---

## 7. File Reference

```
src/
├── main.py                      Entry point — sets input paths, invokes graph
├── graph.py                     LangGraph StateGraph — all nodes, routing, AppState
├── agents.py                    LLM agent wrappers (lazy init, JSON parsing)
├── prompts.py                   ChatPromptTemplates for every agent
├── tracing.py                   LangSmith probe — disables tracing on bad key
└── tools/
    ├── dq_tools.py              Feature 3 — Item Master reconciliation, DQ-01..04
    ├── trend_tools.py           Feature 3 — corridor KPIs, PoP trend, deep-dive tables
    ├── pdf_tools.py             RAG: fingerprint cache, markdown section split, context cache
    ├── knowledge_tools.py       RAG evaluation: Recall@k + grounded-answer accuracy
    ├── weather_tools.py         Feature 5 — multi-waypoint per-corridor weather risk
    ├── resource_tools.py        Feature 5 — demand computation + penalty allocator
    └── email_tools.py           SMTP send with attachments + timeout handling

tests/
├── conftest.py                  sys.path → src/
├── test_audit_logic.py          Feature 1 unit tests
├── test_dq_tools.py             Feature 3 unit tests
├── test_trend_tools.py          Feature 3 unit tests
├── test_resource_tools.py       Feature 5 unit tests
└── test_smoke.py                Import + graph assembly smoke test

data-for-enhancement/
├── SeeWeeS Specialty Dispatch Playbook.md     Source of truth (RAG + DQ rules)
├── Incoming_shipments_14d_multi_corridor.csv  14-day shipment data (C1 + C2)
└── Resource_availability_48h.csv              Driver and truck pool per day
```
