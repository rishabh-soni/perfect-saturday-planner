"""Open-Meteo hourly forecasts, sliced to the original city-local Saturday window."""
from datetime import datetime, timedelta
import math
import os

from providers.cache import ProviderCache
from providers.google import ProviderError
from providers.public_http import request_json


def adverse_hour(hour):
    probability, temperature, code = hour.get("precipitation_probability"), hour.get("temperature_c"), hour.get("weather_code")
    return ((probability is not None and probability >= 60) or
            (temperature is not None and (temperature >= 32 or temperature <= 5)) or
            (code is not None and code in {65, 67, 75, 77, 82, 85, 86, 95, 96, 99}))


class OpenMeteoProvider:
    def __init__(self, *, client=None, cache=None):
        self.client, self.cache = client, cache or ProviderCache()
        self.url = os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")

    def forecast(self, location, preferences):
        try:
            params = {"latitude": location["latitude"], "longitude": location["longitude"],
                      "hourly": "precipitation_probability,temperature_2m,weather_code", "timezone": "auto",
                      "start_date": preferences.saturday.isoformat(), "end_date": preferences.saturday.isoformat()}
            key = self.cache.key("open-meteo", [self.url, params])
            data = self.cache.get(key)
            cached = data is not None
            if data is None:
                self.cache.reserve("Open-Meteo")
                data = request_json("GET", self.url, client=self.client, cache=self.cache,
                                    service="Open-Meteo", params=params)
                if not isinstance(data, dict) or not isinstance(data.get("hourly"), dict):
                    raise ProviderError("Open-Meteo returned no usable hourly forecast")
                self.cache.put(key, data, 1800)
            hourly = data["hourly"]
            start = datetime.fromisoformat(f"{preferences.saturday}T{preferences.start_time}")
            end = start + timedelta(minutes=preferences.duration_minutes)
            times = hourly["time"]
            columns = [hourly[k] for k in ("precipitation_probability", "temperature_2m", "weather_code")]
            if not isinstance(times, list) or any(not isinstance(c, list) or len(c) != len(times) for c in columns):
                raise ProviderError("Open-Meteo hourly arrays are incomplete")
            hours = []
            for i, stamp in enumerate(times):
                instant = datetime.fromisoformat(stamp)
                if not (instant < end and instant + timedelta(hours=1) > start):
                    continue
                probability, temperature, code = (c[i] for c in columns)
                for value in (probability, temperature, code):
                    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                        raise ProviderError("Open-Meteo returned invalid forecast values")
                if probability is not None and not 0 <= probability <= 100:
                    raise ProviderError("Open-Meteo returned invalid precipitation probability")
                hours.append({"time": stamp, "precipitation_probability": probability,
                              "temperature_c": temperature, "weather_code": code})
            if not hours:
                raise ProviderError("Forecast unavailable for the requested Saturday (possibly outside forecast horizon)")
            expected = math.ceil((end - start.replace(minute=0)).total_seconds() / 3600)
            partial = len(hours) != expected or any(any(h[k] is None for k in
                ("precipitation_probability", "temperature_c", "weather_code")) for h in hours)
            prefer_indoor = any(adverse_hour(h) for h in hours)
            return {"status": "partial" if partial else "available", "source": "open_meteo", "cached": cached,
                    "timezone": data.get("timezone", "City-local time"), "hourly": hours,
                    "window_start": start.isoformat(timespec="minutes"), "window_end": end.isoformat(timespec="minutes"),
                    "prefer_indoor": prefer_indoor,
                    "guidance": "Prefer indoor stops; shorten outdoor walks and allow a weather backup." if prefer_indoor else
                                "No adverse conditions indicated by the available hourly values; forecasts can change.",
                    "warnings": ["Some forecast hours or values are missing."] if partial else [],
                    "attribution": "Weather by Open-Meteo.com · CC BY 4.0"}
        except (ProviderError, KeyError, TypeError, ValueError, AttributeError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "Open-Meteo returned incomplete weather data"
            return {"status": "unavailable", "source": "open_meteo", "hourly": [], "prefer_indoor": False,
                    "guidance": "Forecast unavailable; do not assume dry weather. Include an indoor backup and check before departure.",
                    "warnings": [message], "attribution": "Weather service: Open-Meteo.com"}
