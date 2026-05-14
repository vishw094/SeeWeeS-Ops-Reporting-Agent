from __future__ import annotations
from typing import Dict, Any
import requests


def get_weather_forecast(lat: str, lon: str, tz: str) -> Dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m",
        "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,wind_gusts_10m_max",
        "timezone": tz,
        "forecast_days": 2,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def derive_dispatch_weather_risk(forecast: Dict[str, Any]) -> Dict[str, Any]:
    daily = forecast.get("daily", {})
    precip = daily.get("precipitation_sum", []) or []
    gusts = daily.get("wind_gusts_10m_max", []) or []
    tmin = daily.get("temperature_2m_min", []) or []

    max_precip = max(precip) if precip else 0.0
    max_gusts = max(gusts) if gusts else 0.0
    min_temp = min(tmin) if tmin else None

    flags = {
        "heavy_rain_risk": max_precip >= 15.0,
        "high_wind_risk": max_gusts >= 45.0,
        "freezing_risk": (min_temp is not None and min_temp <= 0.0),
    }
    score = int(flags["heavy_rain_risk"]) + int(flags["high_wind_risk"]) + int(flags["freezing_risk"])

    return {
        "max_precip_mm_day": float(max_precip),
        "max_wind_gust_kmh": float(max_gusts),
        "min_temp_c": float(min_temp) if min_temp is not None else None,
        "risk_flags": flags,
        "risk_score_0_3": score,
    }
