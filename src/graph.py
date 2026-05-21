from __future__ import annotations
import os
from typing import TypedDict, Dict, Any, List

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

    # Feature 3 — DQ reconciliation + trend analysis
    dq_report: Dict[str, Any]
    corridor_comparison: Dict[str, Any]
    pop_trend: Dict[str, Any]
    canonical_df_json: str          # serialized reconciled rows for downstream agents
    ops_insights: str               # narrated by run_trend_agent

    weather_forecast: Dict[str, Any]
    weather_risk: Dict[str, Any]

    dispatch_plan: str

    # Feature 1 — audit loop state
    audit_retries: int
    audit_violations: List[str]
    audit_passed: bool
    human_approved: bool

    report_html: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def node_pdf_context(state: AppState) -> AppState:
    persist_dir = "chroma_db"
    rag = PdfRag(persist_dir=persist_dir)
    vectordb = rag.build(state["pdf_path"])

    # If the source is unchanged, reuse the previously-extracted context and
    # skip both the retriever call and the ContextAgent LLM round-trip.
    cached = load_cached_business_context(persist_dir)
    if cached:
        print("[ContextAgent] Cache hit — reusing business_context.")
        return {"business_context": cached}

    retriever = rag.retriever(vectordb, k=6)
    query = "Extract KPI definitions, thresholds, SLAs, constraints, dispatch rules, exceptions."
    docs = retriever.invoke(query)
    snippets = "\n\n---\n\n".join(d.page_content for d in docs)

    business_context = run_context_agent(snippets)
    save_cached_business_context(persist_dir, business_context)
    print("[ContextAgent] Extracted and cached business_context.")
    return {"business_context": business_context}


def node_dq_reconcile(state: AppState) -> AppState:
    """Feature 3 — Item Master reconciliation + corridor / PoP trend analysis.

    Replaces the legacy generic-CSV analysis node. Output is fully deterministic
    so the AuditAgent can verify it against the playbook.
    """
    # Local imports: see top-of-file note on the pandas/chromadb DLL conflict.
    import pandas as pd
    from tools.dq_tools import reconcile_shipments
    from tools.trend_tools import compare_corridors, compute_pop_trend

    df = pd.read_csv(state["csv_path"])

    result = reconcile_shipments(df)
    comparison = compare_corridors(result.reconciled)
    pop = compute_pop_trend(result.reconciled)

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
        "canonical_df_json": canonical_df_json,
        "ops_insights": ops_insights,
    }


def node_weather(state: AppState) -> AppState:
    lat = os.getenv("WEATHER_LAT", "40.7282")
    lon = os.getenv("WEATHER_LON", "-74.0776")
    tz = os.getenv("WEATHER_TZ", "America/New_York")

    forecast = get_weather_forecast(lat, lon, tz)
    risk = derive_dispatch_weather_risk(forecast)
    return {"weather_forecast": forecast, "weather_risk": risk}


def node_planner(state: AppState) -> AppState:
    violations = state.get("audit_violations", [])
    plan = run_planner_agent(
        business_context=state.get("business_context", ""),
        ops_insights=state.get("ops_insights", ""),
        weather_risk=state.get("weather_risk", {}),
        audit_violations=violations if violations else None,
    )
    return {"dispatch_plan": plan}


def node_audit(state: AppState) -> AppState:
    retries = state.get("audit_retries", 0)
    prior_violations = state.get("audit_violations", [])

    result = run_audit_agent(
        business_context=state.get("business_context", ""),
        dispatch_plan=state.get("dispatch_plan", ""),
        weather_risk=state.get("weather_risk", {}),
        audit_retries=retries,
        prior_violations=prior_violations,
    )

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
    return {"report_html": html}


def node_email(state: AppState) -> AppState:
    to_email = os.getenv("REPORT_EMAIL_TO", "").strip()
    if not to_email:
        print("REPORT_EMAIL_TO not set -> skipping email send.")
        return {}

    subject = "MSBA Ops Multi-Agent Dispatch Report"
    send_email_smtp(subject=subject, html_body=state["report_html"], to_email=to_email)
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
