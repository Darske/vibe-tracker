"""Rosebud AI adapter — https://rosebud.ai (creation platform).

Data path: the public DRF JSON gateway at
``https://playground-gateway-v2-snduoq54tq-uc.a.run.app`` (no auth, no robots.txt).

- ``GET /api/published-projects/?ordering=-plays_count&page_size=100&page=N`` is both the
  "top" list and the batch counters endpoint: every row carries plays/likes/clones/comments,
  so one discover pass gives the 2-hourly re-poll for free. ``ordering=-created`` is "new".
  The API refuses pages beyond 100 (top 10,000 games).
- ``GET /api/published-projects/<id>/play-detail/`` (1.2 KB) for watchlist carry-overs that
  did not appear on any list this run. We never call ``/api/published-projects/<id>/``
  (2 MB, embeds game source).
- ``GET /api/users/<creator_id>/`` → ``{"username": ...}`` only.

Side effects we must never trigger: the game page fires ``POST /api/plays/`` on load, which
increments ``plays_count``. This adapter never loads a game page and never POSTs anything.
Creator profile pages (``/profile/<uuid>``) are robots-disallowed, so ``creator_url`` is None.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from tracker.base import SourceAdapter
from tracker.models import CreatorProfile, GameRef, GameSnapshot, ListPlacement

log = logging.getLogger(__name__)

API = "https://playground-gateway-v2-snduoq54tq-uc.a.run.app"
PAGE_SIZE = 100
MAX_PAGE = 100  # "Published projects can only be browsed through page 100."

ORDERING = {
    "top": "-plays_count",
    "new": "-created",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Rosebud(SourceAdapter):
    name = "rosebud"
    display_name = "Rosebud AI"
    base_url = "https://rosebud.ai"
    segment = "creation"

    def __init__(self, http, config: dict):
        super().__init__(http, config)
        # list objects seen in this run's discover(), keyed by project uuid. fetch() builds
        # snapshots from these with zero extra requests.
        self._cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _list_url(ordering: str, page: int, page_size: int = PAGE_SIZE) -> str:
        return f"{API}/api/published-projects/?ordering={ordering}&page_size={page_size}&page={page}"

    @staticmethod
    def _detail_url(external_id: str) -> str:
        return f"{API}/api/published-projects/{external_id}/play-detail/"

    @staticmethod
    def _user_url(creator_id: str) -> str:
        return f"{API}/api/users/{creator_id}/"

    def _game_url(self, obj: dict) -> str:
        slug = obj.get("published_name") or obj.get("id")
        return f"{self.base_url}/play/{slug}"

    def _ref(self, obj: dict) -> GameRef:
        return GameRef(source=self.name, external_id=str(obj["id"]), url=self._game_url(obj))

    def _snapshot(self, obj: dict, ref: GameRef, captured_at: datetime) -> GameSnapshot:
        tags = obj.get("tags")
        return GameSnapshot(
            source=self.name,
            external_id=ref.external_id,
            url=ref.url,
            title=obj.get("name"),
            thumbnail_url=obj.get("preview_image"),
            creator_external_id=obj.get("creator_id"),
            creator_name=obj.get("creator_username"),
            # /profile/<uuid> is robots-disallowed on rosebud.ai → no creator URL.
            creator_url=None,
            # counters: .get() → None when absent; never coerced to 0.
            plays_total=obj.get("plays_count"),
            likes=obj.get("likes_count"),
            remixes=obj.get("clones_count"),
            comments=obj.get("comments_count"),
            # Rosebud exposes no unique-player or concurrent-player figures.
            players_now=None,
            players_total=None,
            engine=obj.get("mode"),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            published_at=None,  # not present in list rows or play-detail
            status="ok",
            captured_at=captured_at,
        )

    def _iter_list(self, ordering: str, top_n: int) -> Iterable[dict]:
        """Yield list objects for one ordering, paging until top_n rows or the API runs out."""
        seen = 0
        page = 1
        while seen < top_n and page <= MAX_PAGE:
            data = self.http.get_json(self._list_url(ordering, page))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                break  # 404 / "page" error object / unexpected shape → stop paging
            results = data["results"]
            if not results:
                break
            for obj in results:
                if seen >= top_n:
                    break
                if not obj.get("id"):
                    continue
                self._cache[str(obj["id"])] = obj
                seen += 1
                yield obj
            if not data.get("next"):
                break
            page += 1

    # ------------------------------------------------------------------ contract
    def discover(self, lists: list[str], top_n: int) -> Iterable[tuple[GameRef, ListPlacement]]:
        for list_name in lists:
            ordering = ORDERING.get(list_name)
            if ordering is None:
                log.warning("%s: unknown list %r (known: %s); skipping", self.name, list_name, ", ".join(ORDERING))
                continue
            captured_at = _utcnow()
            position = 0
            for obj in self._iter_list(ordering, top_n):
                position += 1
                ref = self._ref(obj)
                yield ref, ListPlacement(
                    source=self.name,
                    list_name=list_name,
                    position=position,
                    external_id=ref.external_id,
                    captured_at=captured_at,
                )

    def fetch(self, refs: list[GameRef]) -> Iterable[GameSnapshot]:
        for ref in refs:
            captured_at = _utcnow()
            obj = self._cache.get(ref.external_id)
            if obj is None:
                # watchlist carry-over not on any list this run → one small per-game request
                obj = self.http.get_json(self._detail_url(ref.external_id))
                if obj is None or not isinstance(obj, dict) or "id" not in obj:
                    yield GameSnapshot(
                        source=self.name, external_id=ref.external_id, url=ref.url,
                        status="gone", captured_at=captured_at,
                    )
                    continue
            yield self._snapshot(obj, ref, captured_at)

    def fetch_creator(self, creator_ref: str) -> CreatorProfile | None:
        # Only the JSON user endpoint; /profile/<uuid> pages are robots-disallowed.
        data = self.http.get_json(self._user_url(creator_ref))
        if not isinstance(data, dict):
            return None
        return CreatorProfile(
            source=self.name,
            external_id=creator_ref,
            name=data.get("username"),
            url=None,
            followers=None,
            links={},
        )

    # ------------------------------------------------------------------ fixtures
    def record_fixtures(self, out_dir: Path, limit: int) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        page_size = max(1, min(int(limit), PAGE_SIZE))
        written: list[Path] = []

        def save(name: str, data) -> Path:
            p = out_dir / name
            text = json.dumps(data, indent=1, ensure_ascii=False)
            if len(text.encode("utf-8")) > 200_000 and isinstance(data, dict) and isinstance(data.get("results"), list):
                results = data["results"]
                while len(text.encode("utf-8")) > 200_000 and len(results) > 10:
                    results = results[: max(10, len(results) - 10)]
                    text = json.dumps({**data, "results": results}, indent=1, ensure_ascii=False)
            p.write_text(text, encoding="utf-8")
            written.append(p)
            return p

        top = self.http.get_json(self._list_url(ORDERING["top"], 1, page_size))
        save("list_top_p1.json", top)
        new = self.http.get_json(self._list_url(ORDERING["new"], 1, page_size))
        save("list_new_p1.json", new)

        first = None
        if isinstance(top, dict) and top.get("results"):
            first = top["results"][0]
        if first and first.get("id"):
            gid = str(first["id"])
            save(f"play_detail_{gid}.json", self.http.get_json(self._detail_url(gid)))
            cid = first.get("creator_id")
            if cid:
                save(f"user_{cid}.json", self.http.get_json(self._user_url(str(cid))))
        return written
