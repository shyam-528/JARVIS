"""Weather skill: current conditions + forecast via the Open-Meteo API (no API key)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-WeatherSkill/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _geocode(location: str) -> dict:
    q = urllib.parse.quote(location)
    data = _get_json(
        f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json"
    )
    results = data.get("results") or []
    if not results:
        raise ValueError(f"Location not found: {location}")
    r = results[0]
    return {
        "name": r.get("name", location),
        "country": r.get("country", ""),
        "lat": r["latitude"],
        "lon": r["longitude"],
    }


def _weather_code_text(code: int) -> str:
    mapping = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "drizzle",
        55: "dense drizzle",
        61: "light rain",
        63: "rain",
        65: "heavy rain",
        66: "freezing rain",
        67: "heavy freezing rain",
        71: "light snow",
        73: "snow",
        75: "heavy snow",
        77: "snow grains",
        80: "rain showers",
        81: "rain showers",
        82: "violent rain showers",
        85: "snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with hail",
        99: "severe thunderstorm with hail",
    }
    return mapping.get(code, f"code {code}")


def register(box):
    from core.tools import Tool

    async def get_weather(location: str, days: int = 3) -> str:
        """Current weather + short forecast for a location."""
        try:
            geo = _geocode(location)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Geocoding failed: {e}"

        d = max(1, min(int(days), 7))
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={geo['lat']}&longitude={geo['lon']}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&forecast_days={d}&timezone=auto"
        )
        try:
            data = _get_json(url)
        except Exception as e:
            return f"Weather lookup failed: {e}"

        cur = data.get("current", {})
        daily = data.get("daily", {})
        place = geo["name"] + (f", {geo['country']}" if geo["country"] else "")

        lines = [
            f"Weather for {place}:",
            f"Now: {cur.get('temperature_2m', '?')}°C (feels {cur.get('apparent_temperature', '?')}°C), "
            f"{_weather_code_text(int(cur.get('weather_code', -1)))}, "
            f"humidity {cur.get('relative_humidity_2m', '?')}%, wind {cur.get('wind_speed_10m', '?')} km/h",
        ]

        dates = daily.get("time", [])
        for i in range(min(d, len(dates))):
            lines.append(
                f"{dates[i]}: {_weather_code_text(int(daily['weather_code'][i]))}, "
                f"{daily['temperature_2m_min'][i]}–{daily['temperature_2m_max'][i]}°C, "
                f"precip {daily['precipitation_probability_max'][i]}%"
            )
        return "\n".join(lines)

    box.register(
        Tool(
            name="get_weather",
            description="Get current weather and a multi-day forecast for any city/location",
            parameters={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City or place name"},
                    "days": {"type": "integer", "description": "Forecast days (1-7)", "default": 3},
                },
                "required": ["location"],
            },
            handler=get_weather,
        )
    )
