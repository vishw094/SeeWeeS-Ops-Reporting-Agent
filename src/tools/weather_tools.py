"""Multi-corridor, multi-waypoint weather risk for SeeWeeS dispatch planning.

Implements the risk model from Dispatch Playbook §5 and §6:
  - Waypoints fetched per corridor (C1: 5 waypoints, C2: 4 waypoints)
  - Waypoint score = count of triggered conditions (precip≥15, gusts≥45, tmin≤0)
  - Corridor day risk  = max(waypoint scores) for that forecast day
  - 48h corridor risk  = max(Day0 risk, Day1 risk)
  - Travel buffer: §5.2 mapping {0: 0%, 1: 10%, 2: 25%, 3: 40% + escalation}
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import requests


# ---------------------------------------------------------------------------
# Corridor waypoint catalog — Playbook §3.2 (authoritative)
# ---------------------------------------------------------------------------

_CORRIDORS: Dict[str, List[Dict[str, Any]]] = {
    "C1_I95_NJ_BOS": [
        {"id": "C1_W1", "city": "Newark NJ",     "lat": 40.7357, "lon": -74.1724},
        {"id": "C1_W2", "city": "Bronx NY",       "lat": 40.8448, "lon": -73.8648},
        {"id": "C1_W3", "city": "New Haven CT",   "lat": 41.3083, "lon": -72.9279},
        {"id": "C1_W4", "city": "Providence RI",  "lat": 41.8240, "lon": -71.4128},
        {"id": "C1_W5", "city": "Boston MA",      "lat": 42.3601, "lon": -71.0589},
    ],
    "C2_NJ_PHL": [
        {"id": "C2_W1", "city": "Newark NJ",        "lat": 40.7357, "lon": -74.1724},
        {"id": "C2_W2", "city": "New Brunswick NJ", "lat": 40.4862, "lon": -74.4518},
        {"id": "C2_W3", "city": "Trenton NJ",       "lat": 40.2204, "lon": -74.7643},
        {"id": "C2_W4", "city": "Philadelphia PA",  "lat": 39.9526, "lon": -75.1652},
    ],
}

_BUFFER_BY_SCORE: Dict[int, int] = {0: 0, 1: 10, 2: 25, 3: 40}


# ---------------------------------------------------------------------------
# Per-waypoint fetching and scoring
# ---------------------------------------------------------------------------

def _fetch_waypoint_daily(lat: float, lon: float, tz: str) -> Dict[str, Any]:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "precipitation_sum,temperature_2m_min,wind_gusts_10m_max",
            "timezone": tz,
            "forecast_days": 2,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("daily", {})


def _waypoint_day_score(daily: Dict[str, Any], day_idx: int) -> Tuple[int, Dict[str, bool]]:
    """Score one waypoint on one forecast day (§6.1 trigger conditions)."""
    precip = daily.get("precipitation_sum", []) or []
    gusts  = daily.get("wind_gusts_10m_max", []) or []
    tmin   = daily.get("temperature_2m_min", []) or []

    p = precip[day_idx] if day_idx < len(precip) else 0.0
    g = gusts[day_idx]  if day_idx < len(gusts)  else 0.0
    t = tmin[day_idx]   if day_idx < len(tmin)   else None

    flags: Dict[str, bool] = {
        "heavy_rain": (p or 0) >= 15.0,
        "high_wind":  (g or 0) >= 45.0,
        "freezing":   t is not None and t <= 0.0,
    }
    return sum(flags.values()), flags


# ---------------------------------------------------------------------------
# Public corridor-level interface
# ---------------------------------------------------------------------------

def get_corridor_weather(corridor_id: str, tz: str = "America/New_York") -> Dict[str, Any]:
    """Fetch multi-waypoint weather for one corridor; compute corridor risk.

    Returns a dict consumable by PlannerAgent and the deep-dive appendix.
    """
    waypoints = _CORRIDORS[corridor_id]

    day0_scores: List[int] = []
    day1_scores: List[int] = []
    waypoint_detail: List[Dict[str, Any]] = []

    for wp in waypoints:
        daily = _fetch_waypoint_daily(wp["lat"], wp["lon"], tz)
        s0, f0 = _waypoint_day_score(daily, 0)
        s1, f1 = _waypoint_day_score(daily, 1)
        day0_scores.append(s0)
        day1_scores.append(s1)
        waypoint_detail.append({
            "waypoint_id": wp["id"],
            "city":        wp["city"],
            "day0_score":  s0,
            "day0_flags":  f0,
            "day1_score":  s1,
            "day1_flags":  f1,
        })

    day0_risk = max(day0_scores)
    day1_risk = max(day1_scores)
    risk_48h  = max(day0_risk, day1_risk)
    buffer    = _BUFFER_BY_SCORE.get(risk_48h, 0)

    return {
        "corridor_id":         corridor_id,
        "day0_risk_score":     day0_risk,
        "day1_risk_score":     day1_risk,
        "risk_48h":            risk_48h,
        "risk_score_0_3":      risk_48h,    # canonical field name for deterministic audit
        "travel_buffer_pct":   buffer,
        "escalation_required": risk_48h == 3,
        "risk_flags": {
            "heavy_rain_risk": any(w["day0_flags"]["heavy_rain"] or w["day1_flags"]["heavy_rain"] for w in waypoint_detail),
            "high_wind_risk":  any(w["day0_flags"]["high_wind"]  or w["day1_flags"]["high_wind"]  for w in waypoint_detail),
            "freezing_risk":   any(w["day0_flags"]["freezing"]   or w["day1_flags"]["freezing"]   for w in waypoint_detail),
        },
        "waypoint_detail": waypoint_detail,
    }


def get_all_corridors_weather(tz: str = "America/New_York") -> Dict[str, Any]:
    """Fetch weather for all corridors in _CORRIDORS; return keyed by corridor_id."""
    return {cid: get_corridor_weather(cid, tz) for cid in _CORRIDORS}


def worst_corridor_risk(weather_by_corridor: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a single worst-case risk dict for the deterministic audit check.

    Picks the corridor with the highest 48h risk score and returns its risk
    block so that ``apply_deterministic_audit_checks`` keeps a single code path.
    """
    if not weather_by_corridor:
        return {"risk_score_0_3": 0, "risk_flags": {}, "travel_buffer_pct": 0}
    return max(weather_by_corridor.values(), key=lambda c: c.get("risk_48h", 0))


# ---------------------------------------------------------------------------
# Legacy single-point interface (preserved for backward compatibility)
# ---------------------------------------------------------------------------

def get_weather_forecast(lat: str, lon: str, tz: str) -> Dict[str, Any]:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m",
            "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,wind_gusts_10m_max",
            "timezone": tz,
            "forecast_days": 2,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def derive_dispatch_weather_risk(forecast: Dict[str, Any]) -> Dict[str, Any]:
    daily      = forecast.get("daily", {})
    precip     = daily.get("precipitation_sum", []) or []
    gusts      = daily.get("wind_gusts_10m_max", []) or []
    tmin       = daily.get("temperature_2m_min", []) or []
    max_precip = max(precip) if precip else 0.0
    max_gusts  = max(gusts)  if gusts  else 0.0
    min_temp   = min(tmin)   if tmin   else None
    flags = {
        "heavy_rain_risk": max_precip >= 15.0,
        "high_wind_risk":  max_gusts  >= 45.0,
        "freezing_risk":   min_temp is not None and min_temp <= 0.0,
    }
    score = sum(flags.values())
    return {
        "max_precip_mm_day": float(max_precip),
        "max_wind_gust_kmh": float(max_gusts),
        "min_temp_c":        float(min_temp) if min_temp is not None else None,
        "risk_flags":        flags,
        "risk_score_0_3":    score,
    }
