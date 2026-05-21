from __future__ import annotations
import json
from typing import Dict, Any, List
from langchain_openai import ChatOpenAI
from prompts import (
    PDF_CONTEXT_PROMPT,
    OPS_ANALYSIS_PROMPT,
    TREND_OPS_PROMPT,
    PLANNER_PROMPT,
    AUDIT_PROMPT,
    REPORT_PROMPT,
)

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0.2,
    tags=["msba-demo", "multi-agent"],
    metadata={"repo": "MSBA_AI_Agents_Demo"}
)


def run_context_agent(snippets: str) -> str:
    return llm.invoke(PDF_CONTEXT_PROMPT.format_messages(snippets=snippets)).content

def run_ops_agent(summary: Dict[str, Any], kpis: Dict[str, Any], anomalies_md: str) -> str:
    return llm.invoke(OPS_ANALYSIS_PROMPT.format_messages(
        summary=summary, kpis=kpis, anomalies_md=anomalies_md
    )).content


def run_trend_agent(
    dq_report: Dict[str, Any],
    corridor_comparison: Dict[str, Any],
    pop_trend: Dict[str, Any],
) -> str:
    """Feature 3 — narrate deterministic corridor/PoP KPIs for the planner."""
    return llm.invoke(TREND_OPS_PROMPT.format_messages(
        dq_report=dq_report,
        corridor_comparison=corridor_comparison,
        pop_trend=pop_trend,
    )).content

def run_planner_agent(
    business_context: str,
    ops_insights: str,
    weather_risk: Dict[str, Any],
    audit_violations: List[str] | None = None,
) -> str:
    violations_text = "\n".join(f"- {v}" for v in audit_violations) if audit_violations else "None"
    return llm.invoke(PLANNER_PROMPT.format_messages(
        business_context=business_context,
        ops_insights=ops_insights,
        weather_risk=weather_risk,
        audit_violations=violations_text,
    )).content


def run_audit_agent(
    business_context: str,
    dispatch_plan: str,
    weather_risk: Dict[str, Any],
    audit_retries: int,
    prior_violations: List[str],
) -> Dict[str, Any]:
    prior_text = "\n".join(f"- {v}" for v in prior_violations) if prior_violations else "None"
    raw = llm.invoke(AUDIT_PROMPT.format_messages(
        business_context=business_context,
        dispatch_plan=dispatch_plan,
        weather_risk=weather_risk,
        attempt=audit_retries + 1,
        prior_violations=prior_text,
    )).content

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        result = json.loads(raw[start:end])
        return {
            "passed": bool(result.get("passed", False)),
            "violations": list(result.get("violations", [])),
            "severity": str(result.get("severity", "low")),
        }
    except Exception:
        return {
            "passed": False,
            "violations": [f"AuditAgent parse error — raw response: {raw[:300]}"],
            "severity": "high",
        }

def run_report_agent(
    business_context: str,
    kpis: Dict[str, Any],
    anomaly_highlights: str,
    weather_risk: Dict[str, Any],
    dispatch_plan: str,
    audit_trail: str = "",
) -> str:
    return llm.invoke(REPORT_PROMPT.format_messages(
        business_context=business_context,
        kpis=kpis,
        anomaly_highlights=anomaly_highlights,
        weather_risk=weather_risk,
        dispatch_plan=dispatch_plan,
        audit_trail=audit_trail,
    )).content
