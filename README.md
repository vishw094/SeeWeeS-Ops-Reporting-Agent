# SeeWeeS Ops Reporting Agent

Multi-agent AI system for **time-critical medicine dispatch planning** across New Jersey distribution corridors. Built with LangGraph + LangChain + GPT-4.1-mini.

---

## What It Does

1. **Extracts business rules** from the SeeWeeS Dispatch Playbook PDF using RAG (ChromaDB)
2. **Analyzes shipment data** from CSV — KPIs, anomaly detection, data quality
3. **Fetches live weather** from Open-Meteo and derives per-corridor dispatch risk
4. **Plans the 48-hour dispatch** — corridor-specific, SLA-aware, resource-constrained
5. **Self-audits the plan** — cyclic audit loop checks the plan against playbook rules before it reaches leadership
6. **Escalates to a human** if risk score is maximal (3/3) and the audit loop cannot resolve violations
7. **Generates a leadership-ready HTML report** with audit trail included
8. **Emails the report** via SMTP

---

## Agent Graph

### Current Architecture (Feature 1 complete)

```
┌─────────────────┐
│   pdf_context   │  RAG over Dispatch Playbook PDF → business rules + KPI definitions
└────────┬────────┘
         │
┌────────▼────────┐
│  csv_analysis   │  Shipment CSV → KPIs, anomaly detection, ops insights
└────────┬────────┘
         │
┌────────▼────────┐
│     weather     │  Open-Meteo forecast → dispatch risk score (0–3)
└────────┬────────┘
         │
┌────────▼────────┐
│     planner     │  Business context + ops insights + weather → 48h dispatch plan
└────────┬────────┘        ▲
         │                 │ retry (with violations list)
┌────────▼────────┐        │
│      audit      │────────┘
│   (AuditAgent)  │
└────────┬────────┘
         │
         ├── passed ──────────────────────────────┐
         │                                        │
         ├── retries < 3, not passed ─► [retry]   │
         │                                        │
         └── retries = 3, risk_score = 3 ──►      │
                                         │        │
                               ┌─────────▼──────┐ │
                               │human_checkpoint│ │
                               │ (stdin prompt) │ │
                               └─────────┬──────┘ │
                                         │        │
                                         └───┬────┘
                                             │
                                    ┌────────▼────────┐
                                    │     report      │  HTML report with audit trail
                                    └────────┬────────┘
                                             │
                                    ┌────────▼────────┐
                                    │      email      │  Gmail / Zoho SMTP
                                    └─────────────────┘
```

### Target Architecture (Features 3 + 5 in progress)

```
┌─────────────────┐
│   pdf_context   │
└──────┬──────────┘
       │
  ┌────┴─────────────────────────┐   parallel fan-out
  │                              │
┌─▼──────────────┐   ┌───────────▼──────────────────┐
│ dq_reconcile   │   │  weather_multi_corridor       │
│ (Feature 3)    │   │  (Feature 5)                  │
│                │   │                               │
│ • DQ-01..04    │   │  9 waypoints across 2 corridors│
│ • Item Master  │   │  C1: NJ→Boston (5 waypoints)  │
│ • Alias/Legacy │   │  C2: NJ→Philadelphia (4 wp)   │
│ • PoP trends   │   │  risk per corridor per day    │
└────────┬───────┘   └───────────┬───────────────────┘
         │                       │
         └──────────┬────────────┘   fan-in
                    │
         ┌──────────▼──────────┐
         │  resource_allocator │  penalty-minimizing allocation
         │  (Feature 5)        │  Tier1=100pt, Tier2=40pt, cold-chain=+80pt
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────┐
         │       planner       │ ◄── retry with violations
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────┐
         │        audit        │
         └──────────┬──────────┘
                    │
         [same audit routing as above]
                    │
         ┌──────────▼──────────┐
         │        report       │
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────┐
         │        email        │
         └─────────────────────┘
```

---

## Audit Loop (Feature 1)

The `AuditAgent` reviews the `PlannerAgent` output against 6 playbook rules before the report is generated:

| # | Rule | Playbook Reference |
|---|---|---|
| 1 | risk_score ≥ 3 → plan must include +40% buffer AND escalation | Section 5.2 |
| 2 | Tier 1 (life-critical) dispatches must fit within 6h transit window | Section 7 |
| 3 | Cold-chain items must be assigned to temperature-controlled trucks only | Section 8 |
| 4 | DQ-excluded rows must not appear in truck volume or dispatch counts | Section 11–12 |
| 5 | Plan must acknowledge driver/truck availability constraints | Section 13.1 |
| 6 | Tier 1 units must be prioritized over Tier 2 when resources are scarce | Section 13.2 |

**Routing logic:**

```
audit result → passed?           → report
audit result → failed, retries < 3  → back to planner (with violations injected)
audit result → failed, retries = 3, risk ≥ 3  → human_checkpoint (stdin approval)
audit result → failed, retries = 3, risk < 3  → report (warnings included)
```

---

## Project Structure

```
.
├── src/
│   ├── main.py            # Entry point
│   ├── graph.py           # LangGraph StateGraph definition
│   ├── agents.py          # LLM agent functions
│   ├── prompts.py         # ChatPromptTemplates for all agents
│   ├── tracing.py         # LangSmith tracing setup
│   └── tools/
│       ├── pdf_tools.py   # RAG over PDF (ChromaDB)
│       ├── csv_tools.py   # CSV loading, KPI computation, anomaly detection
│       ├── weather_tools.py  # Open-Meteo API + risk scoring
│       └── email_tools.py    # SMTP email sender
├── data/
│   ├── SeeWeeS Specialty distribution.pdf
│   └── Incoming_shipment_02_08.csv
├── data-for-enhancement/
│   ├── Incoming_shipments_14d_multi_corridor.csv  # 14-day, 2-corridor shipment feed
│   ├── Resource_availability_48h.csv              # Driver + truck availability
│   ├── SeeWeeS Specialty Dispatch Playbook.md     # Operational rules + Item Master
│   └── README.md
├── chroma_db/             # Local vector store (not committed)
├── .env                   # Secrets (not committed)
├── .env.example
├── requirements.txt
└── plan.txt               # Full solution plan and progress tracker
```

---

## Setup

```bash
# 1. Create and activate virtual environment
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and fill in:
#   OPENAI_API_KEY       — required
#   SMTP_* + REPORT_EMAIL_TO — optional (report emailed if set)
#   LANGCHAIN_API_KEY    — optional (LangSmith tracing)
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key (GPT-4.1-mini) |
| `WEATHER_LAT` | No | Latitude for weather (default: 40.7282 — Newark NJ) |
| `WEATHER_LON` | No | Longitude for weather (default: -74.0776) |
| `WEATHER_TZ` | No | Timezone (default: America/New_York) |
| `SMTP_HOST` | No | SMTP host for email (e.g. smtp.zoho.com) |
| `SMTP_PORT` | No | SMTP port (default: 465) |
| `SMTP_USER` | No | SMTP login address |
| `SMTP_PASSWORD` | No | SMTP app password |
| `REPORT_EMAIL_TO` | No | Recipient address — skipped if blank |
| `LANGCHAIN_TRACING_V2` | No | Set `true` to enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | No | LangSmith API key |
| `LANGCHAIN_PROJECT` | No | LangSmith project name |

---

## Running

```bash
cd src
python main.py
```

The pipeline prints audit loop status to stdout as it runs:

```
[AuditAgent] Attempt 1/3: FAILED (severity: high)
  - Plan does not mention +40% travel buffer for risk_score=3 corridor
  - Cold-chain items not explicitly routed to temp-controlled trucks
[AuditAgent] Attempt 2/3: PASSED

=== REPORT (first 2000 chars) ===
<html>...
```

If risk score is 3/3 and the audit fails all 3 attempts, the system pauses for manager approval:

```
============================================================
  !! HUMAN ESCALATION REQUIRED !!
  Weather risk score: 3/3 (max)
  Audit failed after 3 attempts.
  Outstanding violations:
    - Plan does not include escalation language for score=3 corridor
============================================================

Manager: approve dispatch report anyway? (yes/no):
```

---

## Agents

| Agent | Role | Implementation |
|---|---|---|
| `ContextAgent` | Extracts KPI definitions, SLAs, dispatch rules from PDF | RAG + LLM |
| `OpsDataAgent` | Interprets CSV KPIs and anomalies for ops leadership | LLM |
| `PlannerAgent` | Produces 48h dispatch plan from all upstream context | LLM |
| `AuditAgent` | Checks plan against 6 playbook rules, returns JSON verdict | LLM |
| `ReportAgent` | Compiles HTML report for leadership with audit trail | LLM |

---

## LangGraph State

```python
class AppState(TypedDict, total=False):
    # Inputs
    pdf_path: str
    csv_path: str

    # Context
    business_context: str

    # CSV analysis
    csv_summary: Dict[str, Any]
    csv_kpis: Dict[str, Any]
    anomalies_md: str
    ops_insights: str

    # Weather
    weather_forecast: Dict[str, Any]
    weather_risk: Dict[str, Any]

    # Planning
    dispatch_plan: str

    # Audit loop (Feature 1)
    audit_retries: int
    audit_violations: List[str]
    audit_passed: bool
    human_approved: bool

    # Output
    report_html: str
```
