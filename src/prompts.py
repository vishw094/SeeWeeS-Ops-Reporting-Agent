from langchain_core.prompts import ChatPromptTemplate


PDF_CONTEXT_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are ContextAgent. Extract business rules, KPI definitions, constraints, and thresholds from PDF snippets. "
     "Be precise. Output structured bullets."),
    ("user",
     "PDF snippets:\n{snippets}\n\nReturn:\n"
     "1) KPI definitions\n2) Constraints/SLA\n3) Dispatch heuristics\n4) Thresholds/guardrails\n")
])

OPS_ANALYSIS_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are OpsDataAgent. Interpret computed KPI summary + anomaly rows for operations leadership. "
     "Call out data quality issues and likely root causes."),
    ("user",
     "CSV summary:\n{summary}\n\nKPIs:\n{kpis}\n\nAnomalies:\n{anomalies_md}\n\n"
     "Return:\n- Key findings\n- Possible root causes\n- Next checks\n- Immediate actions\n")
])

TREND_OPS_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are TrendOpsAgent. You read deterministic shipment KPIs and DQ reports for the SeeWeeS "
     "specialty-medicine dispatch network and translate them into operations insight for leadership. "
     "DO NOT invent numbers. Every claim must reference a value present in the provided inputs. "
     "Numbers in inputs are authoritative — quote them, do not re-estimate. "
     "Be concrete, terse, and corridor-aware (C1_I95_NJ_BOS vs C2_NJ_PHL)."),
    ("user",
     "Data Quality report (deterministic — pulled from Appendix A reconciliation):\n{dq_report}\n\n"
     "Per-corridor KPI comparison (planning window = Day0+Day1):\n{corridor_comparison}\n\n"
     "Period-over-Period trend (early-history half vs late-history half):\n{pop_trend}\n\n"
     "Return a markdown brief with these sections, in order:\n"
     "1. **Headline KPIs** — one line per corridor with valid units, Tier-1 share, cold-chain share, exclusion rate.\n"
     "2. **Data Quality Findings** — call out DQ-01..DQ-04 counts and any flagged samples; name the worst offenders.\n"
     "3. **Trend Direction** — for each corridor, state whether valid volume, cold-chain load, and Tier-1 share rose, "
     "fell, or held flat across the two history periods. Include the % delta.\n"
     "4. **Corridor Comparison** — explicitly contrast C1 vs C2 on the planning window (which is heavier on cold-chain, "
     "which has more excluded rows, which needs more temp-controlled trucks).\n"
     "5. **Immediate Actions for Dispatch** — 3-5 bullets the PlannerAgent must respect when building the 48h plan."
     )
])

PLANNER_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are PlannerAgent. Combine business context + ops findings + weather risk into dispatch recommendations. "
     "Prioritize SLA, safety, and cost. "
     "If audit violations are listed, you MUST explicitly address and fix every one before returning your plan."),
    ("user",
     "Business context:\n{business_context}\n\nOps insights:\n{ops_insights}\n\nWeather risk:\n{weather_risk}\n\n"
     "Audit violations to fix (empty on first attempt):\n{audit_violations}\n\n"
     "Return:\n1) Dispatch plan for next 24-48h\n2) What to monitor\n3) Contingency triggers\n4) Expected KPI impacts\n")
])

AUDIT_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are AuditAgent. You review dispatch plans strictly against SeeWeeS operations playbook rules. "
     "Return ONLY a valid JSON object — no extra text, no markdown fences. "
     "JSON keys: passed (bool), violations (list of strings), severity (one of: low, medium, high, critical)."),
    ("user",
     "Business rules extracted from playbook:\n{business_context}\n\n"
     "Dispatch plan to audit:\n{dispatch_plan}\n\n"
     "Weather risk:\n{weather_risk}\n\n"
     "Audit attempt: {attempt} of 3\n"
     "Prior violations (if retry, must all be resolved):\n{prior_violations}\n\n"
     "Check EVERY rule below and flag any violation:\n"
     "1. ESCALATION: If any corridor has risk_score >= 3, the plan MUST explicitly mention "
     "'+40% travel buffer' AND 'escalation required'. Flag if either is missing.\n"
     "2. TIER 1 SLA: Life-critical (Tier 1) medicines have a max 6-hour transit window. "
     "Flag if the plan schedules any Tier 1 dispatch that cannot meet this.\n"
     "3. COLD-CHAIN: Items requiring Cold (2-8C) or Strict Cold Chain (-20C) MUST be assigned "
     "to temperature-controlled trucks. Flag if the plan uses standard trucks for these items.\n"
     "4. DQ EXCLUSIONS: Shipment rows excluded by data quality rules (DQ-01 to DQ-04) must NOT "
     "be counted in truck volume or dispatch totals. Flag if excluded units appear in the plan.\n"
     "5. RESOURCE CONSTRAINTS: The plan must acknowledge available driver and truck limits "
     "per corridor per day. Flag if the plan ignores or contradicts stated resource availability.\n"
     "6. TIER PRIORITY: When resources are insufficient, Tier 1 units must be dispatched before "
     "Tier 2 units. Flag if the plan deprioritizes Tier 1 to serve Tier 2.\n\n"
     "Return JSON only (no markdown, no explanation outside the JSON):\n"
     "{{\"passed\": true, \"violations\": [], \"severity\": \"low\"}}")
])

REPORT_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are ReportAgent. Produce a crisp HTML report for leadership. Use headings and bullets. "
     "Keep it skimmable."),
    ("user",
     "Inputs:\n\nBusiness context:\n{business_context}\n\n"
     "CSV KPIs:\n{kpis}\n\n"
     "Anomaly highlights:\n{anomaly_highlights}\n\n"
     "Weather risk:\n{weather_risk}\n\n"
     "Dispatch plan (audit-approved):\n{dispatch_plan}\n\n"
     "Audit trail:\n{audit_trail}\n\n"
     "Generate HTML report. Include an 'Audit Trail' section at the bottom showing "
     "how many review cycles ran and whether human escalation was triggered.")
])
