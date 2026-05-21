from __future__ import annotations
import os
import time
from functools import wraps
from html import escape
from typing import Any, Callable, Dict, List, TypedDict

from langgraph.graph import StateGraph, END
from dotenv import load_dotenv

from tools.pdf_tools import PdfRag, load_cached_business_context, save_cached_business_context
from tools.weather_tools import get_weather_forecast, derive_dispatch_weather_risk
from tools.email_tools import send_email_smtp
from agents import (
    run_context_agent,
    run_trend_agent,
    run_planner_agent,
    run_audit_agent,
    run_report_agent,
)

# NOTE: `pandas`, `tools.dq_tools`, and `tools.trend_tools` are imported lazily
# inside `node_dq_reconcile`. Importing pandas at module load time on Windows
# has been observed to corrupt chromadb's rust client and segfault the very
# first `vectordb.add_documents()` call in `node_pdf_context`.

load_dotenv()

MAX_AUDIT_RETRIES = 3


class AppState(TypedDict, total=False):
    pdf_path: str
    csv_path: str

    business_context: str
    rag_eval_results: Dict[str, Any]

    # Feature 3 — DQ reconciliation + trend analysis
    dq_report: Dict[str, Any]
    corridor_comparison: Dict[str, Any]
    pop_trend: Dict[str, Any]
    deep_dive_tables: Dict[str, Any]
    appendix_text: str              # deterministic deep-dive tables, emailed as attachment
    canonical_df_json: str          # serialized reconciled rows for downstream agents
    ops_insights: str               # narrated by run_trend_agent

    weather_forecast: Dict[str, Any]
    weather_risk: Dict[str, Any]

    planner_draft: Dict[str, Any]
    dispatch_plan: str

    # Feature 1 — audit loop state
    audit_retries: int
    audit_violations: List[str]
    audit_passed: bool
    human_approved: bool

    report_html: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timed(label: str) -> Callable:
    """Wrap a node so its wall-clock duration is printed when it returns."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                print(f"[Timing] {label} took {time.perf_counter() - t0:.2f}s")
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Deep-dive appendix rendering
# ---------------------------------------------------------------------------

_DEEP_DIVE_SPECS = [
    ("Daily Valid Shipment Trend", "daily_valid_units", ["shipment_date", "planning_day", "valid_units"]),
    ("Planning Window Corridor by Day", "corridor_day_breakdown", ["corridor_id", "planning_day", "valid_units"]),
    ("Top Item Spikes vs Historical Baseline", "item_spikes",
     ["canonical_item_name", "planning_window_units", "historical_avg_daily_units", "spike_ratio"]),
    ("Correction Breakdown (alias / legacy)", "correction_breakdown", ["reason_code", "units"]),
    ("Exclusion Breakdown by Reason", "exclusion_breakdown", ["reason_code", "units"]),
    ("Excluded Rows by Day and Reason", "daily_excluded_units", ["planning_day", "reason_code", "excluded_units"]),
    ("Sample Corrected Rows", "corrected_samples",
     ["item_id", "item_name", "canonical_item_id", "canonical_item_name", "reason_code", "planning_day", "corridor_id"]),
    ("Sample Excluded / Unresolved Rows", "unresolved_samples",
     ["item_id", "item_name", "unique_item_id", "reason_code", "planning_day", "corridor_id"]),
]


def _text_table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "(none)"
    safe = [{c: "" if r.get(c) is None else str(r.get(c)) for c in columns} for r in rows]
    widths = {c: max(len(c), *(len(r[c]) for r in safe)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    divider = "-+-".join("-" * widths[c] for c in columns)
    body = [" | ".join(r[c].ljust(widths[c]) for c in columns) for r in safe]
    return "\n".join([header, divider, *body])


def render_deep_dive_text(deep: Dict[str, Any]) -> str:
    if not deep:
        return ""
    out = [
        "MSBA Ops Deep-Dive Analytics Appendix",
        "=" * 37,
        "",
        "Deterministic tables computed from the reconciled shipment data. "
        "Separated from the executive report to keep that decision-focused.",
    ]
    for title, key, cols in _DEEP_DIVE_SPECS:
        rows = deep.get(key, [])
        if rows:
            out.extend(["", title, "-" * len(title), _text_table(rows, cols)])
    return "\n".join(out)


def render_deep_dive_html(deep: Dict[str, Any]) -> str:
    if not deep:
        return ""
    parts = ["<section><h2>Deep-Dive Analytics Appendix</h2>"]
    for title, key, cols in _DEEP_DIVE_SPECS:
        rows = deep.get(key, [])
        if not rows:
            continue
        head = "".join(f"<th>{escape(str(c))}</th>" for c in cols)
        body = "".join(
            "<tr>" + "".join(
                f"<td>{escape('' if r.get(c) is None else str(r.get(c)))}</td>" for c in cols
            ) + "</tr>"
            for r in rows
        )
        parts.append(f"<h3>{escape(title)}</h3>")
        parts.append(
            "<table border='1' cellspacing='0' cellpadding='6' "
            "style='border-collapse:collapse;width:100%;margin:10px 0;'>"
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        )
    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@_timed("pdf_context")
def node_pdf_context(state: AppState) -> AppState:
    from tools.knowledge_tools import run_rag_eval

    persist_dir = "chroma_db"
    rag = PdfRag(persist_dir=persist_dir)
    vectordb = rag.build(state["pdf_path"])

    # Retrieval-quality scorecard (cheap: 5 similarity searches, no LLM).
    rag_eval = run_rag_eval(rag, vectordb, k=5)
    print(
        f"[RAGEval] Recall@{rag_eval['k']}={rag_eval['recall_at_k']} "
        f"grounded_accuracy={rag_eval['grounded_answer_accuracy']}"
    )

    # If the source is unchanged, reuse the previously-extracted context and
    # skip the ContextAgent LLM round-trip.
    cached = load_cached_business_context(persist_dir)
    if cached:
        print("[ContextAgent] Cache hit — reusing business_context.")
        return {"business_context": cached, "rag_eval_results": rag_eval}

    docs = rag.retrieve(
        vectordb,
        "Extract KPI definitions, thresholds, SLAs, constraints, dispatch rules, exceptions.",
        k=6,
    )
    snippets = "\n\n---\n\n".join(d.page_content for d in docs)

    business_context = run_context_agent(snippets)
    save_cached_business_context(persist_dir, business_context)
    print("[ContextAgent] Extracted and cached business_context.")
    return {"business_context": business_context, "rag_eval_results": rag_eval}


@_timed("dq_reconcile")
def node_dq_reconcile(state: AppState) -> AppState:
    """Feature 3 — Item Master reconciliation + corridor / PoP trend analysis.

    Replaces the legacy generic-CSV analysis node. Output is fully deterministic
    so the AuditAgent can verify it against the playbook.
    """
    # Local imports: see top-of-file note on the pandas/chromadb DLL conflict.
    import pandas as pd
    from tools.dq_tools import reconcile_shipments
    from tools.trend_tools import compare_corridors, compute_pop_trend, compute_deep_dive_tables

    df = pd.read_csv(state["csv_path"])

    result = reconcile_shipments(df)
    comparison = compare_corridors(result.reconciled)
    pop = compute_pop_trend(result.reconciled)
    deep_dive = compute_deep_dive_tables(result.reconciled)

    print(
        f"\n[DQReconcile] total={result.report['total_rows']} "
        f"valid={result.report['valid_rows']} "
        f"excluded={result.report['excluded_rows']} "
        f"flagged={result.report['flagged_rows']} "
        f"(DQ-01={result.report['dq01_missing_uid']}, "
        f"DQ-02={result.report['dq02_invalid_id']}, "
        f"DQ-03={result.report['dq03_name_mismatch']}, "
        f"DQ-04={result.report['dq04_duplicate_uid']})"
    )

    ops_insights = run_trend_agent(
        dq_report=result.report,
        corridor_comparison=comparison,
        pop_trend=pop,
    )

    # Keep the reconciled rows on the state for Feature 5 (resource allocation).
    # JSON-serialized to stay safely picklable inside LangGraph state.
    canonical_df_json = result.valid.to_json(orient="records", date_format="iso")

    return {
        "dq_report": result.report,
        "corridor_comparison": comparison,
        "pop_trend": pop,
        "deep_dive_tables": deep_dive,
        "canonical_df_json": canonical_df_json,
        "ops_insights": ops_insights,
    }


@_timed("weather")
def node_weather(state: AppState) -> AppState:
    lat = os.getenv("WEATHER_LAT", "40.7282")
    lon = os.getenv("WEATHER_LON", "-74.0776")
    tz = os.getenv("WEATHER_TZ", "America/New_York")

    forecast = get_weather_forecast(lat, lon, tz)
    risk = derive_dispatch_weather_risk(forecast)
    return {"weather_forecast": forecast, "weather_risk": risk}


def _expected_buffer_pct(risk_score: Any) -> int:
    """Playbook §5.2 buffer mapping. Unknown score -> 0."""
    return {0: 0, 1: 10, 2: 25, 3: 40}.get(int(risk_score or 0), 0)


def _render_dispatch_plan_text(draft: Dict[str, Any]) -> str:
    """Flatten the structured planner draft into narrative text for the report."""
    def _bullets(items: Any) -> str:
        if not items:
            return "  - (none)"
        if isinstance(items, str):
            return f"  - {items}"
        return "\n".join(f"  - {i}" for i in items)

    return (
        f"Recommended travel buffer: {draft.get('recommended_buffer_pct')}%\n"
        f"Escalation required: {draft.get('escalation_required')}\n"
        f"Cited rules:\n{_bullets(draft.get('cited_rules'))}\n\n"
        f"Dispatch plan (next 24-48h):\n{draft.get('dispatch_plan', '')}\n\n"
        f"What to monitor:\n{_bullets(draft.get('what_to_monitor'))}\n\n"
        f"Contingency triggers:\n{_bullets(draft.get('contingency_triggers'))}\n\n"
        f"Expected KPI impacts:\n{_bullets(draft.get('expected_kpi_impacts'))}\n"
    )


def apply_deterministic_audit_checks(
    state: AppState, draft: Dict[str, Any], llm_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Layer hard, code-verifiable checks on top of the LLM audit verdict.

    These are NOT judgment calls — they compare the planner's structured
    fields against the playbook's fixed numeric policy. Any mismatch is a
    violation regardless of what the LLM concluded.
    """
    risk_score = state.get("weather_risk", {}).get("risk_score_0_3", 0)
    expected_buffer = _expected_buffer_pct(risk_score)

    violations = list(llm_result.get("violations", []))

    if draft.get("recommended_buffer_pct") != expected_buffer:
        violations.append(
            f"recommended_buffer_pct={draft.get('recommended_buffer_pct')} does not match the required "
            f"{expected_buffer}% for weather risk_score {risk_score} (§5.2)."
        )
    if int(risk_score or 0) == 3 and not draft.get("escalation_required"):
        violations.append("risk_score is 3 but escalation_required is not true (§5.2 requires escalation).")
    if not draft.get("cited_rules"):
        violations.append("Planner draft cites no playbook rules (cited_rules is empty).")

    passed = bool(llm_result.get("passed", False)) and not violations
    severity = llm_result.get("severity", "low")
    if violations and severity == "low":
        severity = "medium"

    return {"passed": passed, "violations": violations, "severity": severity}


@_timed("planner")
def node_planner(state: AppState) -> AppState:
    violations = state.get("audit_violations", [])
    draft = run_planner_agent(
        business_context=state.get("business_context", ""),
        ops_insights=state.get("ops_insights", ""),
        weather_risk=state.get("weather_risk", {}),
        audit_violations=violations if violations else None,
    )
    return {
        "planner_draft": draft,
        "dispatch_plan": _render_dispatch_plan_text(draft),
    }


@_timed("audit")
def node_audit(state: AppState) -> AppState:
    retries = state.get("audit_retries", 0)
    prior_violations = state.get("audit_violations", [])
    draft = state.get("planner_draft", {})

    llm_result = run_audit_agent(
        business_context=state.get("business_context", ""),
        dispatch_plan=state.get("dispatch_plan", ""),
        weather_risk=state.get("weather_risk", {}),
        audit_retries=retries,
        prior_violations=prior_violations,
    )

    # Hard, code-verifiable checks on top of the LLM verdict.
    result = apply_deterministic_audit_checks(state, draft, llm_result)

    status = "PASSED" if result["passed"] else f"FAILED (severity: {result['severity']})"
    print(f"\n[AuditAgent] Attempt {retries + 1}/{MAX_AUDIT_RETRIES}: {status}")
    if result["violations"]:
        for v in result["violations"]:
            print(f"  - {v}")

    return {
        "audit_passed": result["passed"],
        "audit_violations": result["violations"],
        "audit_retries": retries + 1,
    }


def node_human_checkpoint(state: AppState) -> AppState:
    violations = state.get("audit_violations", [])
    risk_score = state.get("weather_risk", {}).get("risk_score_0_3", 0)

    print("\n" + "=" * 60)
    print("  !! HUMAN ESCALATION REQUIRED !!")
    print(f"  Weather risk score: {risk_score}/3 (max)")
    print(f"  Audit failed after {MAX_AUDIT_RETRIES} attempts.")
    print("  Outstanding violations:")
    for v in violations:
        print(f"    - {v}")
    print("=" * 60)

    try:
        response = input("\nManager: approve dispatch report anyway? (yes/no): ").strip().lower()
        approved = response in ("yes", "y")
    except EOFError:
        approved = False

    if not approved:
        raise RuntimeError(
            "Report halted: manager did not approve plan after audit loop exhausted. "
            f"Unresolved violations: {violations}"
        )

    print("[HumanCheckpoint] Manager approved — proceeding to report.")
    return {"human_approved": True}


@_timed("report")
def node_report(state: AppState) -> AppState:
    retries = state.get("audit_retries", 0)
    violations = state.get("audit_violations", [])
    human_approved = state.get("human_approved", False)

    audit_trail = (
        f"Audit cycles completed: {retries}\n"
        f"Final audit result: {'PASSED' if state.get('audit_passed') else 'WARNINGS (max retries reached)'}\n"
        f"Human escalation triggered: {'Yes — manager approved' if human_approved else 'No'}\n"
    )
    if violations:
        audit_trail += "Remaining violations logged:\n" + "\n".join(f"  - {v}" for v in violations)

    dq_report = state.get("dq_report", {})
    comparison = state.get("corridor_comparison", {})

    # Build the "kpis" + "anomaly_highlights" blocks from the new Feature-3 outputs
    # so the existing REPORT_PROMPT contract is honored without invasive prompt edits.
    kpis_for_report: Dict[str, Any] = {
        "by_corridor_planning_window": comparison.get("planning_window", {}),
        "by_corridor_history": comparison.get("history", {}),
        "pop_overall_delta": state.get("pop_trend", {}).get("overall", {}).get("delta", {}),
    }
    anomaly_highlights = (
        f"DQ-01 missing uid: {dq_report.get('dq01_missing_uid', 0)} | "
        f"DQ-02 invalid id: {dq_report.get('dq02_invalid_id', 0)} | "
        f"DQ-03 name mismatch: {dq_report.get('dq03_name_mismatch', 0)} | "
        f"DQ-04 duplicate uid: {dq_report.get('dq04_duplicate_uid', 0)}"
    )
    excluded_sample = dq_report.get("excluded_sample", [])
    if excluded_sample:
        anomaly_highlights += "\nExcluded sample (first 10):\n" + "\n".join(
            f"  - corridor={r.get('corridor_id')} item={r.get('item_id')} "
            f"name={r.get('item_name')!r} uid={r.get('unique_item_id')!r} "
            f"reason={r.get('reason_code')}"
            for r in excluded_sample
        )

    html = run_report_agent(
        business_context=state.get("business_context", ""),
        kpis=kpis_for_report,
        anomaly_highlights=anomaly_highlights,
        weather_risk=state.get("weather_risk", {}),
        dispatch_plan=state.get("dispatch_plan", ""),
        audit_trail=audit_trail,
    )

    deep = state.get("deep_dive_tables", {})
    appendix_text = render_deep_dive_text(deep)
    deep_html = render_deep_dive_html(deep)
    if deep_html:
        html = f"{html}\n{deep_html}"

    return {"report_html": html, "appendix_text": appendix_text}


def node_email(state: AppState) -> AppState:
    to_email = os.getenv("REPORT_EMAIL_TO", "").strip()
    if not to_email:
        print("REPORT_EMAIL_TO not set -> skipping email send.")
        return {}

    subject = "MSBA Ops Multi-Agent Dispatch Report"
    attachments = []
    if state.get("appendix_text"):
        attachments.append({
            "filename": "msba_ops_deep_dive_appendix.txt",
            "mime_type": "text/plain",
            "content": state["appendix_text"],
        })
    send_email_smtp(
        subject=subject,
        html_body=state["report_html"],
        to_email=to_email,
        attachments=attachments,
    )
    return {}


# ---------------------------------------------------------------------------
# Routing — Feature 1 conditional edge
# ---------------------------------------------------------------------------

def route_audit(state: AppState) -> str:
    retries = state.get("audit_retries", 0)
    passed = state.get("audit_passed", False)
    risk_score = state.get("weather_risk", {}).get("risk_score_0_3", 0)

    if passed:
        return "approved"
    if retries >= MAX_AUDIT_RETRIES and risk_score >= 3:
        return "escalate"
    if retries >= MAX_AUDIT_RETRIES:
        return "approved"
    return "retry"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(AppState)

    g.add_node("pdf_context", node_pdf_context)
    g.add_node("dq_reconcile", node_dq_reconcile)
    g.add_node("weather", node_weather)
    g.add_node("planner", node_planner)
    g.add_node("audit", node_audit)
    g.add_node("human_checkpoint", node_human_checkpoint)
    g.add_node("report", node_report)
    g.add_node("email", node_email)

    g.set_entry_point("pdf_context")
    g.add_edge("pdf_context", "dq_reconcile")
    g.add_edge("dq_reconcile", "weather")
    g.add_edge("weather", "planner")
    g.add_edge("planner", "audit")
    g.add_conditional_edges(
        "audit",
        route_audit,
        {
            "retry":    "planner",
            "escalate": "human_checkpoint",
            "approved": "report",
        },
    )
    g.add_edge("human_checkpoint", "report")
    g.add_edge("report", "email")
    g.add_edge("email", END)

    return g.compile()
