"""SourceAdapter: the only contract between a platform and the pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from tracker.http import Http
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement


class SourceAdapter(ABC):
    name: str  # slug, e.g. "rosebud"
    display_name: str
    base_url: str
    segment: str  # "aggregator" | "creation"
    needs_browser: bool = False

    def __init__(self, http: Http, config: dict):
        """`config` is the platform's sources.yaml entry (lists, top_n, rate_limit_rps, ...)."""
        self.http = http
        self.config = config

    @abstractmethod
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        """Walk each list page (top, trending, most_played, new, ...) up to top_n games
        per list and yield the games seen with their position on that list."""

    @abstractmethod
    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        """Return current counters for the given games. Batch where the platform allows it.
        Missing games (404, private) are returned with status='gone'."""

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        """Optional. Profile name, URL, followers, external links (x, youtube, tiktok, discord, website)."""
        return None
