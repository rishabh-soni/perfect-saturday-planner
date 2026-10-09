"""Bounded public API calls. No automatic retry storms or raw error bodies."""
import os
from email.utils import parsedate_to_datetime
import httpx

from providers.google import ProviderError


DEFAULT_USER_AGENT = "PerfectSaturdayPlanner/1.0 (+https://github.com/rishabh-soni/perfect-saturday-planner)"


def user_agent():
    return os.getenv("OSM_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def request_json(method, url, *, client=None, service, cache, timeout=12, **kwargs):
    try:
        headers = {"User-Agent": user_agent(), "Accept": "application/json"}
        if client is None:
            with httpx.Client(timeout=timeout, follow_redirects=True) as owned:
                response = owned.request(method, url, headers=headers, **kwargs)
        else:
            response = client.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if response.status_code == 429:
            try:
                retry = max(60, int(response.headers.get("Retry-After", "60")))
            except ValueError:
                try:
                    retry = max(60, parsedate_to_datetime(response.headers["Retry-After"]).timestamp() - cache.clock())
                except (KeyError, TypeError, ValueError, OverflowError):
                    retry = 60
            cache.block(service, retry)
            raise ProviderError(f"{service} rate limit exceeded; retry later")
        if response.status_code in (401, 403):
            cache.block(service, 60)
            raise ProviderError(f"{service} denied access; check endpoint and usage policy")
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        cache.block(service, 30)
        raise ProviderError(f"{service} timed out; retry later") from None
    except (httpx.HTTPError, ValueError):
        cache.block(service, 30)
        raise ProviderError(f"{service} unavailable or returned malformed data") from None
