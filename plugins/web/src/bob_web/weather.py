"""Weather from Open-Meteo, which needs no key: find the place, then ask for its forecast."""

from __future__ import annotations

from typing import Any

import httpx
from bob_sdk.runtime import ToolError

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
SOURCE_URL = "https://open-meteo.com/"

# WMO weather codes, as Open-Meteo documents them.
CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}  # fmt: skip


def describe(code: Any) -> str:
    try:
        return CODES.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


def _at(series: dict[str, Any], key: str, index: int) -> Any:
    values = series.get(key)
    return values[index] if isinstance(values, list) and index < len(values) else None


def weather(place: str, unit: str, days: int, client: httpx.Client | None = None) -> dict[str, Any]:
    place = " ".join((place or "").split())
    if not place:
        raise ToolError("Which place? Say a city, or set your usual place in the Web settings.")
    unit = "fahrenheit" if str(unit).lower().startswith("f") else "celsius"
    days = max(1, min(int(days), 7))
    own = client is None
    http = client or httpx.Client(timeout=12.0, trust_env=False)
    try:
        try:
            found = http.get(
                GEOCODE, params={"name": place, "count": 1, "language": "en", "format": "json"}
            )
            found.raise_for_status()
            places = found.json().get("results") or []
            if not places:
                raise ToolError(f"No place called {place!r} was found. Try the city and country.")
            spot = places[0]
            forecast = http.get(
                FORECAST,
                params={
                    "latitude": spot["latitude"],
                    "longitude": spot["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
                    "forecast_days": days,
                    "timezone": "auto",
                    "temperature_unit": unit,
                    "wind_speed_unit": "kmh",
                },
            )
            forecast.raise_for_status()
            data = forecast.json()
        except httpx.HTTPError as exc:
            raise ToolError(f"The weather service could not be reached: {exc}") from exc
        except (ValueError, KeyError) as exc:
            raise ToolError(
                "The weather service answered with something Bob could not read."
            ) from exc
    finally:
        if own:
            http.close()

    sign = "°F" if unit == "fahrenheit" else "°C"
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    name = ", ".join(
        str(x) for x in (spot.get("name"), spot.get("admin1"), spot.get("country")) if x
    )
    return {
        "place": name,
        "timezone": data.get("timezone", spot.get("timezone", "")),
        "now": {
            "time": current.get("time"),
            "conditions": describe(current.get("weather_code")),
            "temperature": f"{current.get('temperature_2m')}{sign}",
            "feels_like": f"{current.get('apparent_temperature')}{sign}",
            "humidity": f"{current.get('relative_humidity_2m')}%",
            "wind": f"{current.get('wind_speed_10m')} km/h",
            "rain_now": f"{current.get('precipitation')} mm",
        },
        "days": [
            {
                "date": date,
                "conditions": describe(_at(daily, "weather_code", i)),
                "high": f"{_at(daily, 'temperature_2m_max', i)}{sign}",
                "low": f"{_at(daily, 'temperature_2m_min', i)}{sign}",
                "chance_of_rain": f"{_at(daily, 'precipitation_probability_max', i)}%",
                "rain_total": f"{_at(daily, 'precipitation_sum', i)} mm",
            }
            for i, date in enumerate(daily.get("time") or [])
        ],
    }
