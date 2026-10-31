"""Astrocade adapter — https://www.astrocade.com (creation platform; every game is built with Astrocade's own AI builder).

Everything comes from the app's JSON API at https://astrocade-api-prod.com/v3 (AWS API Gateway). The owner approved
using this app-internal API (docs/SOURCES.md, "Policy question"). No HTML parsing, no browser.

Auth: the API only needs `authorization: guest-<uuid4>`; the site mints one client-side per visitor and any uuid
works. We mint ONE per adapter instance (or pin it with the ASTROCADE_GUEST_ID env var) and send
`x-app-platform: web` like the web client does. Requests without the header get `{"message": "Missing
Authentication Token"}` (403 → Http raises Blocked).

Endpoints used (see docs/SOURCES.md and research/astrocade/):
  GET /v3/feed?filter=categorized_feed&category=<key>&categoryPageSize=N  → {category, label, games[], hasMore, paginationToken}
      keys: top_picks ("Players' Choice", ~30), trending (~30), beta, halloween_2026, and ~20 genre keys
      (action, puzzle, platformer, adventure, simulation, horror, arcade, incremental, …) capped at 100 each.
  GET /v3/games?gameId=<ULID>                                            → {game: {…}}   (404 when unknown)
  GET /v3/users?username=<u>                                             → {userId, username, profilePicture, totalPlays, followerCount, bio}

Game object fields we read: gameId, title, thumbnailUrl, username, ownerId, plays, likeCount, commentsCount,
remixCount, ownerFollowerCount, firstPublishedTimestamp (unix s), createdAt (unix s), isPublic.
`plays` = session starts (each time a user starts the game). `shares`, `ownerTotalPlays` are not mapped (no column).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

API_BASE = "https://astrocade-api-prod.com/v3"
MAX_PAGE_SIZE = 100  # categoryPageSize cap observed 2026-10-29 (genre rows stop at 100, hasMore=false)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def slugify(title) -> str:
    """Astrocade's URL slug: lowercase, every run of non-alphanumerics → '-', trimmed ('Era.io' → 'era-io')."""
    if not isinstance(title, str):
        return "game"
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return slug or "game"


def _ts_to_dt(value) -> datetime | None:
    """Unix seconds (int/float/str) → aware UTC datetime; None when missing or unparsable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    if secs > 1e11:  # defensive: milliseconds
        secs /= 1000.0
    try:
        return datetime.fromtimestamp(secs, tz=timezone.utc).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


class Astrocade(SourceAdapter):
    name = "astrocade"
    display_name = "Astrocade"
    base_url = "https://www.astrocade.com"
    segment = "creation"
    needs_browser = False

    def __init__(self, http, config: dict):
        super().__init__(http, config)
        guest = os.environ.get("ASTROCADE_GUEST_ID") or str(uuid.uuid4())
        self.guest_id = guest
        self._headers = {"authorization": f"guest-{guest}", "x-app-platform": "web"}
        self._objects: dict[str, dict] = {}  # gameId -> last full game object seen (feed or /games)

    # ------------------------------------------------------------------ api
    def _api(self, path: str, **params):
        url = API_BASE + path
        return self.http.get_json(url, params=params or None, headers=dict(self._headers))

    # ------------------------------------------------------------------ urls
    def _url_for(self, game_id: str, title=None) -> str:
        return f"{self.base_url}/games/{slugify(title)}/{game_id}"

    def _profile_url(self, username: str) -> str:
        return f"{self.base_url}/profile/{username}"

    def _remember(self, obj: dict) -> None:
        gid = obj.get("gameId")
        if isinstance(gid, str) and gid:
            self._objects[gid] = obj

    # --------------------------------------------------------------- discover
    def _feed(self, category: str, page_size: int) -> list[dict]:
        data = self._api("/feed", filter="categorized_feed", category=category, categoryPageSize=page_size) or {}
        games = data.get("games") if isinstance(data, dict) else None
        if not isinstance(games, list):
            return []
        return [g for g in games if isinstance(g, dict)]

    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        now = _utcnow()
        page_size = max(1, min(int(top_n), MAX_PAGE_SIZE))
        for list_name in lists:
            games = self._feed(list_name, page_size)
            if not games:
                log.warning("astrocade: category %r returned no games", list_name)
                continue
            seen: set[str] = set()
            pos = 0
            for obj in games:
                gid = obj.get("gameId")
                if not isinstance(gid, str) or not gid or gid in seen:
                    continue
                seen.add(gid)
                pos += 1
                if pos > top_n:
                    break
                self._remember(obj)
                ref = GameRef(source=self.name, external_id=gid, url=self._url_for(gid, obj.get("title")))
                yield ref, ListPlacement(source=self.name, list_name=list_name, position=pos, external_id=gid, captured_at=now)

    # ------------------------------------------------------------------ fetch
    def _snapshot(self, ref: GameRef, obj: dict, now: datetime) -> GameSnapshot:
        username = obj.get("username")
        username = username if isinstance(username, str) and username.strip() else None
        owner_id = obj.get("ownerId")
        creator_id = username or (str(owner_id) if owner_id else None)
        published = _ts_to_dt(obj.get("firstPublishedTimestamp")) or _ts_to_dt(obj.get("createdAt"))
        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=ref.url,
            title=obj.get("title") if isinstance(obj.get("title"), str) else None,
            thumbnail_url=obj.get("thumbnailUrl") if isinstance(obj.get("thumbnailUrl"), str) else None,
            creator_external_id=creator_id,
            creator_name=username,
            creator_url=self._profile_url(username) if username else None,
            plays_total=_int_or_none(obj.get("plays")),
            likes=_int_or_none(obj.get("likeCount")),
            comments=_int_or_none(obj.get("commentsCount")),
            remixes=_int_or_none(obj.get("remixCount")),
            votes=None,
            followers=_int_or_none(obj.get("ownerFollowerCount")),
            ai_tool="astrocade",  # every Astrocade game is made with Astrocade's own AI builder
            engine=None,
            tags=[],
            published_at=published,
            status="ok",
            captured_at=now,
        )

    def _gone(self, ref: GameRef, now: datetime) -> GameSnapshot:
        return GameSnapshot(source=self.name, external_id=ref.external_id, url=ref.url, status="gone", captured_at=now)

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        now = _utcnow()
        for ref in refs:
            gid = ref.external_id
            obj = self._objects.get(gid)
            if obj is None:
                data = self._api("/games", gameId=gid)
                obj = data.get("game") if isinstance(data, dict) else None
                if not isinstance(obj, dict) or not obj:
                    yield self._gone(ref, now)
                    continue
                self._remember(obj)
            if obj.get("isPublic") is False:
                yield self._gone(ref, now)
                continue
            yield self._snapshot(ref, obj, now)

    # --------------------------------------------------------------- creators
    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        data = self._api("/users", username=creator_ref)
        if not isinstance(data, dict) or not data.get("username"):
            return None
        username = str(data["username"])
        bio = data.get("bio")
        return CreatorProfile(
            source=self.name,
            external_id=username,
            name=username,
            url=self._profile_url(username),
            avatar_url=data.get("profilePicture") if isinstance(data.get("profilePicture"), str) else None,
            followers=_int_or_none(data.get("followerCount")),
            bio=bio.strip() if isinstance(bio, str) and bio.strip() else None,
            links={},
        )

    # --------------------------------------------------------------- fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def dump(name: str, payload) -> None:
            p = out_dir / name
            p.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
            written.append(p)

        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        feeds = {}
        for cat in ("trending", "top_picks", "action"):
            data = self._api("/feed", filter="categorized_feed", category=cat, categoryPageSize=page_size) or {}
            feeds[cat] = data
            dump(f"feed_{cat}.json", data)

        first = None
        for cat in ("trending", "top_picks"):
            for g in (feeds[cat].get("games") or []):
                if isinstance(g, dict) and g.get("gameId"):
                    first = g
                    break
            if first:
                break
        if first:
            gid = first["gameId"]
            game = self._api("/games", gameId=gid)
            if game:
                dump(f"game_{gid}.json", game)
            username = first.get("username") or (game or {}).get("game", {}).get("username")
            if username:
                user = self._api("/users", username=username)
                if user:
                    dump(f"user_{username}.json", user)
        return written
