"""Shared HTTP client. Politeness rules live here so adapters cannot skip them.

- per-source rate limit (default 1 request/second)
- exponential backoff on 429/5xx, max 3 retries, honours Retry-After
- fixed User-Agent identifying the project + CONTACT_EMAIL
- robots.txt respected at request time (cached per host per run)
- ETag / If-Modified-Since cache (in-memory, per run)
- a hard time budget shared by the whole run
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

PROJECT_URL = "https://github.com/vibe-tracker"  # replaced by the real repo URL in README


class Blocked(Exception):
    """403 / CAPTCHA / Cloudflare challenge. The adapter must stop working on that platform."""


class RobotsDisallowed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass
class TimeBudget:
    seconds: float
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    def check(self) -> None:
        if self.remaining() <= 0:
            raise BudgetExceeded(f"time budget of {self.seconds:.0f}s exhausted")


def user_agent() -> str:
    email = os.environ.get("CONTACT_EMAIL")
    if not email:
        raise RuntimeError("CONTACT_EMAIL is not set; it is required for the User-Agent string")
    from tracker import __version__

    return f"vibe-tracker/{__version__} (+{PROJECT_URL}; contact: {email})"


class Http:
    def __init__(
        self,
        source: str,
        rate_limit_rps: float = 1.0,
        budget: TimeBudget | None = None,
        timeout: float = 30.0,
        respect_robots: bool = True,
        default_headers: dict[str, str] | None = None,
    ):
        self.source = source
        self.min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self.budget = budget
        self.respect_robots = respect_robots
        self._last_request = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._etag: dict[str, tuple[str | None, str | None, httpx.Response]] = {}
        self.requests_made = 0
        headers = {"User-Agent": user_agent(), "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
        if default_headers:
            headers.update(default_headers)
        self._client = httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = self._client.get(host + "/robots.txt")
            if r.status_code == 200 and "<" not in r.text[:20]:
                rp.parse(r.text.splitlines())
            else:  # no robots.txt (404, XML error page, redirect to HTML) → nothing disallowed
                rp = None
        except httpx.HTTPError as e:
            log.warning("%s: could not fetch robots.txt for %s (%s); treating as allowed", self.source, host, e)
            rp = None
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self._client.headers["User-Agent"], url) and rp.can_fetch("*", url)

    # ------------------------------------------------------------------ core
    def _throttle(self) -> None:
        if self.budget:
            self.budget.check()
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def request(self, method: str, url: str, *, allow_404: bool = True, **kwargs) -> httpx.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        headers = dict(kwargs.pop("headers", {}) or {})
        cached = self._etag.get(url) if method == "GET" else None
        if cached:
            etag, last_mod, _ = cached
            if etag:
                headers["If-None-Match"] = etag
            if last_mod:
                headers["If-Modified-Since"] = last_mod

        delay = 2.0
        for attempt in range(4):
            self._throttle()
            self.requests_made += 1
            try:
                resp = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise
                log.warning("%s: %s %s failed (%s), retry in %.0fs", self.source, method, url, e, delay)
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 304 and cached:
                return cached[2]
            if resp.status_code in (401, 403) or _looks_like_challenge(resp):
                raise Blocked(f"{resp.status_code} on {url}")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning("%s: %s on %s, retry in %.0fs", self.source, resp.status_code, url, sleep_for)
                time.sleep(min(sleep_for, 60))
                delay *= 2
                continue
            if resp.status_code in (404, 410) and allow_404:  # gone/private: the adapter maps this to status='gone'
                return resp
            resp.raise_for_status()
            if method == "GET" and (resp.headers.get("ETag") or resp.headers.get("Last-Modified")):
                self._etag[url] = (resp.headers.get("ETag"), resp.headers.get("Last-Modified"), resp)
            return resp
        raise RuntimeError("unreachable")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs):
        r = self.get(url, **kwargs)
        if r.status_code in (404, 410):
            return None
        return r.json()

    def post_json(self, url: str, body, **kwargs):
        r = self.request("POST", url, content=json.dumps(body), headers={"Content-Type": "application/json", **(kwargs.pop("headers", {}) or {})}, **kwargs)
        if r.status_code in (404, 410):
            return None
        return r.json()

    def close(self) -> None:
        self._client.close()


def _looks_like_challenge(resp: httpx.Response) -> bool:
    if resp.status_code not in (200, 403, 503):
        return False
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype and resp.status_code in (403, 503):
        head = resp.text[:3000].lower()
        return "cf-challenge" in head or "just a moment" in head or "captcha" in head
    return False
