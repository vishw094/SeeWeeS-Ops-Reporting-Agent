from __future__ import annotations
import os
from typing import TypedDict, Dict, Any, List

from langgraph.graph import StateGraph, END
from dotenv import load_dotenv

from tools.pdf_tools import PdfRag
from tools.csv_tools import analyze_csv
from tools.weather_tools import get_weather_forecast, derive_dispatch_weather_risk
from tools.email_tools import send_email_smtp
from agents import (
    run_context_agent,
    run_ops_agent,
    run_planner_agent,
    run_audit_agent,
    run_report_agent,
)

load_dotenv()

MAX_AUDIT_RETRIES = 3


class AppState(TypedDict, total=False):
    pdf_path: str
    csv_path: str

    business_context: str

    csv_summary: Dict[str, Any]
    csv_kpis: Dict[str, Any]
    anomalies_md: str
    ops_insights: str

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
    rag = PdfRag(persist_dir="chroma_db")
    vectordb = rag.build(state["pdf_path"])
    retriever = rag.retriever(vectordb, k=6)

    query = "Extract KPI definitions, thresholds, SLAs, constraints, dispatch rules, exceptions."
    docs = retriever.invoke(query)
    snippets = "\n\n---\n\n".join(d.page_content for d in docs)

    business_context = run_context_agent(snippets)
    return {"business_context": business_context}


def node_csv_analysis(state: AppState) -> AppState:
    res = analyze_csv(state["csv_path"])

    anomalies_md = "(none detected or insufficient numeric data)"
    if not res.anomalies.empty:
        anomalies_md = res.anomalies.head(12).to_markdown(index=False)

    ops_insights = run_ops_agent(summary=res.summary, kpis=res.kpis, anomalies_md=anomalies_md)

    return {
        "csv_summary": res.summary,
        "csv_kpis": res.kpis,
        "anomalies_md": anomalies_md,
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

    html = run_report_agent(
        business_context=state.get("business_context", ""),
        kpis=state.get("csv_kpis", {}),
        anomaly_highlights=state.get("anomalies_md", "(none)"),
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
    g.add_node("csv_analysis", node_csv_analysis)
    g.add_node("weather", node_weather)
    g.add_node("planner", node_planner)
    g.add_node("audit", node_audit)
    g.add_node("human_checkpoint", node_human_checkpoint)
    g.add_node("report", node_report)
    g.add_node("email", node_email)

    g.set_entry_point("pdf_context")
    g.add_edge("pdf_context", "csv_analysis")
    g.add_edge("csv_analysis", "weather")
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
