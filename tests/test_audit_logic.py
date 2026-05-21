from __future__ import annotations

from graph import _expected_buffer_pct, apply_deterministic_audit_checks


def test_expected_buffer_mapping():
    assert _expected_buffer_pct(0) == 0
    assert _expected_buffer_pct(1) == 10
    assert _expected_buffer_pct(2) == 25
    assert _expected_buffer_pct(3) == 40
    assert _expected_buffer_pct(None) == 0


def _state(risk_score):
    return {"weather_risk": {"risk_score_0_3": risk_score}}


def test_audit_passes_when_buffer_matches_and_rules_cited():
    draft = {"recommended_buffer_pct": 25, "escalation_required": False, "cited_rules": ["§5.2"]}
    llm = {"passed": True, "violations": [], "severity": "low"}
    out = apply_deterministic_audit_checks(_state(2), draft, llm)
    assert out["passed"] is True
    assert out["violations"] == []


def test_audit_flags_wrong_buffer():
    draft = {"recommended_buffer_pct": 10, "escalation_required": False, "cited_rules": ["§5.2"]}
    llm = {"passed": True, "violations": [], "severity": "low"}
    out = apply_deterministic_audit_checks(_state(2), draft, llm)
    assert out["passed"] is False
    assert any("recommended_buffer_pct" in v for v in out["violations"])


def test_audit_requires_escalation_at_score_3():
    draft = {"recommended_buffer_pct": 40, "escalation_required": False, "cited_rules": ["§5.2"]}
    llm = {"passed": True, "violations": [], "severity": "low"}
    out = apply_deterministic_audit_checks(_state(3), draft, llm)
    assert out["passed"] is False
    assert any("escalation_required" in v for v in out["violations"])


def test_audit_requires_cited_rules():
    draft = {"recommended_buffer_pct": 0, "escalation_required": False, "cited_rules": []}
    llm = {"passed": True, "violations": [], "severity": "low"}
    out = apply_deterministic_audit_checks(_state(0), draft, llm)
    assert out["passed"] is False
    assert any("cited_rules" in v for v in out["violations"])


def test_llm_failure_keeps_failing_even_if_deterministic_passes():
    draft = {"recommended_buffer_pct": 0, "escalation_required": False, "cited_rules": ["§5.2"]}
    llm = {"passed": False, "violations": ["LLM flagged something"], "severity": "high"}
    out = apply_deterministic_audit_checks(_state(0), draft, llm)
    assert out["passed"] is False
    assert "LLM flagged something" in out["violations"]
