"""Parse tests for the Kongregate adapter against fixtures in tests/fixtures/kongregate/.

Fixtures were built from the research captures of 2026-11-10 (scratchpad research/kongregate/), trimmed with
the adapter's own trim_list_html / trim_game_html:
  list_hot.html               ← hot-new-games.html                 (/en/hot-new-games)
  list_new.html               ← newest_p1.html                     (/en/new-and-updated-games?sort=newest)
  list_action_plays.html      ← action-games_sort_plays_page_1     (/en/action-games?sort=plays)
  list_action_newest_p1/p2    ← action_newest_p1 / action-games_page_2_sort_newest
  game_mardrom__hedgemoot     ← game_hedgemoot.html  (declares AI use)
  game_hwonze__birb           ← gp_birb.html         (declares no generative AI)
  metrics_*.json              ← legacy_*metrics*.json (raw, untrimmed)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracker.models import GameRef
from tracker.sources.kongregate import (
    Kongregate,
    approx_count,
    classify_ai,
    game_id_from_path,
    ld_fields,
    ld_video_game,
    list_path,
    parse_cards,
    parse_list_name,
    parse_metrics,
    slugify,
    trim_list_html,
)

FIX = Path(__file__).parent / "fixtures" / "kongregate"
B = "https://www.kongregate.com"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = ""):
        self.text = text
        self.status_code = status_code
        self.url = url


DISALLOWED = re.compile(r"/accounts/|/developer/|/games/.*/(comments|embed)|/games/.*/rating\?|\.rss$")


class FakeHttp:
    """Serves fixtures by URL, records every URL, and fails on robots-disallowed paths."""

    def __init__(self, overrides: dict[str, object] | None = None, default_metrics: str = "metrics_siread__retro-bowl.json"):
        self.calls: list[str] = []
        self.overrides = overrides or {}
        self.default_metrics = default_metrics
        self.requests_made = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        self.requests_made += 1
        assert not DISALLOWED.search(url), f"robots-disallowed path requested: {url}"
        for pattern, payload in self.overrides.items():
            if re.search(pattern, url):
                return payload(url) if callable(payload) else payload
        routes = {
            f"{B}/en/hot-new-games": "list_hot.html",
            f"{B}/en/new-and-updated-games?sort=newest": "list_new.html",
            f"{B}/en/action-games?sort=plays": "list_action_plays.html",
            f"{B}/en/action-games?sort=newest": "list_action_newest_p1.html",
            f"{B}/en/action-games?sort=newest&page=2": "list_action_newest_p2.html",
            f"{B}/games/mardrom/hedgemoot/metrics.json": "metrics_mardrom__hedgemoot.json",
            f"{B}/en/games/mardrom/hedgemoot": "game_mardrom__hedgemoot.html",
            f"{B}/en/games/hwonze/birb": "game_hwonze__birb.html",
        }
        if url in routes:
            return FakeResponse(load(routes[url]), url=url)
        if url.endswith("/metrics.json"):
            return FakeResponse(load(self.default_metrics), url=url)
        if url.startswith(f"{B}/en/games/"):
            return FakeResponse("<html><head><title>Page not found</title></head></html>", status_code=404, url=url)
        raise AssertionError(f"unexpected URL {url}")


def make(config=None, overrides=None) -> tuple[Kongregate, FakeHttp]:
    http = FakeHttp(overrides)
    cfg = {"name": "kongregate", "lists": ["hot", "new"], "top_n": 100, "poll_every_hours": 2,
           "game_page_every_hours": 0, **(config or {})}
    return Kongregate(http, cfg), http


def ref(eid: str) -> GameRef:
    return GameRef(source="kongregate", external_id=eid, url=f"{B}/en/games/{eid}")


# ---------------------------------------------------------------- list names and paths
def test_list_names_map_to_paths():
    assert list_path("hot") == "/en/hot-new-games"
    assert list_path("new") == "/en/new-and-updated-games?sort=newest"
    assert list_path("new", 2) is None and list_path("hot", 2) is None
    assert list_path("action_top") == "/en/action-games?sort=top"
    assert list_path("action_plays", 2) == "/en/action-games?sort=plays&page=2"
    assert list_path("top_adventure-rpg") == "/en/adventure-rpg-games?sort=top"
    assert list_path("plays_strategy-defense") == "/en/strategy-defense-games?sort=plays"
    assert parse_list_name("idle_newest") == ("idle", "newest")
    assert list_path("bogus") is None


def test_game_id_lowercases_path():
    assert game_id_from_path("/en/games/JamesG466/incremancer") == ("jamesg466/incremancer", "JamesG466", "incremancer")
    assert game_id_from_path(f"{B}/en/games/Monumental/Surviv")[0] == "monumental/surviv"
    assert game_id_from_path("/en/games/JamesG466/incremancer/comments") is None
    assert slugify("Adventure & RPG") == "adventure-rpg" and slugify("Strategy & Defense") == "strategy-defense"
    assert approx_count("7.7", "M") == 7_700_000 and approx_count("208.0", "K") == 208_000 and approx_count("4", None) == 4


# ---------------------------------------------------------------- cards
def test_hot_cards_parse_badge_plays_rating():
    cards = parse_cards(load("list_hot.html"))
    assert len(cards) == 49
    c = cards[0]
    assert c["external_id"] == "jamesg466/incremancer" and c["dev"] == "JamesG466"
    assert c["title"] == "Incremancer"
    assert c["badge"] == "Hot"
    assert c["plays_approx"] == 2_400_000 and c["rating"] == 4.5
    assert c["thumbnail"].startswith("https://cdn3.kongcdn.com/game_icons/0069/1970/")
    assert all(x["external_id"] == x["external_id"].lower() for x in cards)
    assert all(x["ld"] is not None for x in cards), "every card is matched to its JSON-LD ItemList item"
    surviv = next(x for x in cards if x["external_id"] == "monumental/surviv")
    assert surviv["plays_approx"] == 208_000


def test_new_cards_new_and_updated_badges_and_unrated_card():
    cards = parse_cards(load("list_new.html"))
    assert len(cards) == 50
    assert {c["badge"] for c in cards} == {"New", "Updated"}
    neon = cards[0]
    assert neon["external_id"] == "breymantech/neon-syndicate-idle-crime-empire"
    assert neon["title"] == "Neon Syndicate: Idle Crime Empire"
    assert neon["badge"] == "New" and neon["rating"] is None and neon["plays_approx"] == 4
    hedge = cards[1]
    assert hedge["external_id"] == "mardrom/hedgemoot"
    f = ld_fields(hedge["ld"])
    assert f["author"] == "mardrom" and f["genre"] == "Idle" and f["rating_count"] == 4
    assert f["published_at"] == datetime(2026, 11, 2, 12, 56, 26, tzinfo=timezone.utc)
    assert "assistance from AI tools" in f["description"]


def test_trim_list_html_is_idempotent():
    html = load("list_hot.html")
    assert parse_cards(trim_list_html(html)) == parse_cards(html)


# ---------------------------------------------------------------- discover
def test_discover_positions_start_at_one_per_list():
    k, http = make()
    out = list(k.discover(["hot", "new"], 100))
    hot = [p for _, p in out if p.list_name == "hot"]
    new = [p for _, p in out if p.list_name == "new"]
    assert [p.position for p in hot] == list(range(1, 50))
    assert [p.position for p in new] == list(range(1, 51))
    r, p = out[0]
    assert r.external_id == "jamesg466/incremancer" and r.url == f"{B}/en/games/jamesg466/incremancer"
    assert http.calls == [f"{B}/en/hot-new-games", f"{B}/en/new-and-updated-games?sort=newest"]


def test_discover_category_paginates_to_top_n():
    k, http = make()
    out = list(k.discover(["action_newest"], 100))
    assert len(out) == 100 and out[-1][1].position == 100
    assert http.calls == [f"{B}/en/action-games?sort=newest", f"{B}/en/action-games?sort=newest&page=2"]
    k2, http2 = make()
    assert len(list(k2.discover(["action_newest"], 30))) == 30
    assert len(http2.calls) == 1, "top_n within page 1 needs one request"


def test_discover_unknown_list_is_skipped():
    k, http = make()
    assert list(k.discover(["nonsense"], 100)) == []
    assert http.calls == []


# ---------------------------------------------------------------- metrics.json
def test_parse_metrics_with_rating_count():
    m = parse_metrics(json.loads(load("metrics_siread__retro-bowl.json")))
    assert m == {"plays": 7_746_977, "favorites": 6_743, "rating": 4.61, "rating_count": 12_151}


def test_parse_metrics_under_judgement_has_no_count():
    m = parse_metrics(json.loads(load("metrics_mardrom__hedgemoot.json")))
    assert m == {"plays": 42, "favorites": 2, "rating": 3.0, "rating_count": None}
    assert parse_metrics({"error": "nope"}) is None


def test_fetch_maps_metrics_and_list_json_ld():
    k, http = make()
    list(k.discover(["hot", "new"], 100))
    n = len(http.calls)
    snaps = list(k.fetch([ref("mardrom/hedgemoot"), ref("jamesg466/incremancer")]))
    assert http.calls[n:] == [f"{B}/games/mardrom/hedgemoot/metrics.json", f"{B}/games/jamesg466/incremancer/metrics.json"]
    hedge, inc = snaps
    assert hedge.status == "ok" and hedge.plays_total == 42 and hedge.likes == 2 and hedge.votes is None
    assert hedge.rating is None  # under judgement: no public average
    assert hedge.title == "Hedgemoot" and hedge.creator_external_id == "mardrom" and hedge.creator_name == "mardrom"
    assert hedge.creator_url is None
    assert hedge.published_at == datetime(2026, 11, 2, 12, 56, 26, tzinfo=timezone.utc)
    assert hedge.ai_disclosure == "ai"
    assert hedge.tags == ["idle"]
    assert hedge.thumbnail_url.startswith("https://cdn1.kongcdn.com/game_icons/0070/3499/")
    # retro-bowl metrics served as the default payload for incremancer
    assert inc.plays_total == 7_746_977 and inc.likes == 6_743 and inc.votes == 12_151
    assert inc.rating == 4.61
    assert inc.creator_name == "JamesG466" and inc.creator_external_id == "jamesg466"
    assert inc.ai_disclosure is None  # describes the game, no declaration
    assert inc.players_now is None and inc.plays_24h is None and inc.comments is None


def test_category_list_adds_category_tag():
    k, _ = make()
    found = list(k.discover(["action_plays"], 50))
    eid = found[0][0].external_id
    snap = next(iter(k.fetch([ref(eid)])))
    assert "action" in snap.tags


def test_fetch_404_is_gone_and_makes_no_game_page_request():
    k, http = make(overrides={r"/games/gone/game/metrics\.json": FakeResponse("", 404)})
    snaps = list(k.fetch([ref("gone/game")]))
    assert len(snaps) == 1 and snaps[0].status == "gone"
    assert http.calls == [f"{B}/games/gone/game/metrics.json"]


def test_fetch_redirect_away_from_metrics_is_gone():
    k, _ = make(overrides={r"/games/old/game/metrics\.json": lambda u: FakeResponse("<html></html>", 200, url=f"{B}/en/")})
    assert next(iter(k.fetch([ref("old/game")]))).status == "gone"


def test_fetch_non_json_metrics_is_skipped():
    k, _ = make(overrides={r"/games/odd/game/metrics\.json": FakeResponse("<html>oops</html>", 200)})
    assert list(k.fetch([ref("odd/game")])) == []


def test_fetch_lowercases_ref_for_requests():
    k, http = make()
    list(k.fetch([GameRef(source="kongregate", external_id="mardrom/hedgemoot", url=f"{B}/en/games/Mardrom/Hedgemoot")]))
    assert http.calls[0] == f"{B}/games/mardrom/hedgemoot/metrics.json"


# ---------------------------------------------------------------- game page (watchlist games without list JSON-LD)
def test_game_page_json_ld_parse():
    ld = ld_video_game(load("game_hwonze__birb.html"))
    f = ld_fields(ld)
    assert f["title"] == "Birb"
    assert f["author"] == "hwonze" and f["rating_count"] == 258
    assert f["published_at"] == datetime(2026, 6, 3, 20, 40, 52, tzinfo=timezone.utc)
    assert classify_ai(f["description"]) == "no_ai"


def test_watchlist_game_fetches_game_page_once():
    k, http = make()
    snap = next(iter(k.fetch([ref("hwonze/birb")])))
    assert http.calls == [f"{B}/games/hwonze/birb/metrics.json", f"{B}/en/games/hwonze/birb"]
    assert snap.ai_disclosure == "no_ai" and snap.creator_name == "hwonze" and snap.title
    assert snap.published_at is not None and snap.tags  # genre from JSON-LD


def test_listed_game_never_fetches_game_page():
    k, http = make()
    list(k.discover(["new"], 100))
    n = len(http.calls)
    list(k.fetch([ref("mardrom/hedgemoot")]))
    assert http.calls[n:] == [f"{B}/games/mardrom/hedgemoot/metrics.json"]


def test_game_page_404_is_gone():
    k, http = make()
    snap = next(iter(k.fetch([ref("nobody/missing")])))
    assert snap.status == "gone"
    assert http.calls[-1] == f"{B}/en/games/nobody/missing"


def test_game_page_fetch_cap():
    k, http = make({"max_game_pages_per_run": 3}, overrides={r"/en/games/w/": lambda u: FakeResponse(load("game_hwonze__birb.html"), url=u)})
    refs = [ref(f"w/game{i}") for i in range(10)]
    snaps = list(k.fetch(refs))
    assert len(snaps) == 10 and all(s.status == "ok" for s in snaps)
    pages = [c for c in http.calls if "/en/games/" in c]
    assert len(pages) == 3 and k.game_pages_fetched == 3
    assert sum(1 for s in snaps if s.ai_disclosure) == 3


def test_game_page_rotation_spreads_refetches():
    k, _ = make({"game_page_every_hours": 168, "poll_every_hours": 2, "max_game_pages_per_run": 1000})
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    due = [i for i in range(840) if k._rotation_due(f"dev/game{i}", now)]
    assert 0 < len(due) < 840 / 84 * 3, "about 1/84 of watchlist-only games are due per 2-hour run"


def test_never_requests_disallowed_paths():
    k, _ = make()
    with pytest.raises(ValueError):
        k._get(f"{B}/games/a/b/embed")
    with pytest.raises(ValueError):
        k._get(f"{B}/en/accounts/mardrom")


# ---------------------------------------------------------------- AI declaration classifier
@pytest.mark.parametrize("text", [
    # research-note examples (real Kongregate descriptions, 2026)
    "This game was made with some assistance from AI tools to create the code, art and music.",
    "AI used for the artwork and HUD on this game",
    "AI was used in the creation of this game.",
    "AI disclosure: AI tools assisted development, including programming and creation of some visual assets.",
    "Graphics and code were made with AI assistance.",
    "Artwork and text were created with generative AI assistance; all code is hand-written JavaScript.",
    "AI Disclosure: This game was created with the help of AI using the VeltosAI platform.",
    "I directed all art direction, design, and gameplay; the code was written by an AI coding assistant (Claude) under my direction.",
    "AI, mostly gemini 3.8 flash was used to generate parts of this game.",
    "Generative AI was used solely to create the title image/cover art of the game.",
    "🤖 AI-assisted development",
    "Some marketing assets were AI updated.",
    "The game was developed with creative and technical assistance from generative artificial intelligence technologies.",
    # yes + a gameplay-scope or partial "no" → still a declared AI user
    "AI disclosure: AI-assisted tools were used for visual asset creation. No generative AI is used during gameplay.",
    "AI disclosure: the game's code was written with the help of an AI assistant (Claude by Anthropic). "
    "All graphics are generated by the game's own code: there is no AI-generated artwork, music or voice.",
    "Created using AI tools.",
    "We used AI to create the soundtrack.",
])
def test_classify_ai_yes(text):
    assert classify_ai(text) == "ai"


@pytest.mark.parametrize("text", [
    "This game does not use generative AI.",
    "AI Usage: 0% Generative AI tools were used",
    "No Generative AI was used in the making of this game.",
    "AI disclosure: no Ai tool is used in this game",
    "No use of AI",
    "* This game does not contain any input of AI of any type",
    "AI transparency: No AI was used.",
    "AI Disclosure: Generative AI was not used.",
    "AI Transparency Notice: No AI-generated assets.",
    "This game is not made with help of AI , Ai is only  used in description for better explanation",
    "Made without the use of AI.",
    "100% human-made art and code.",
    "AI usage: 0%",
])
def test_classify_ai_no(text):
    assert classify_ai(text) == "no_ai"


@pytest.mark.parametrize("text", [
    "Try the two player mode as well, where you can play either with a friend or with the AI!",
    "A Wacky Races style racing game with amazing stunts, cool missions and crazy AI cars.",
    "Battle smart AI opponents across 30 levels.",
    "Play against the AI or a friend.",
    "Choose AI difficulty: easy, normal, hard.",
    "The enemy AI adapts to your strategy.",
    "You and an AI commander each defend a mirrored battlefield.",
    "AI-powered enemies hunt you through the maze.",
    "The AI generates a new maze each run.",
    "NOTE: the \"FAQ\" section was automatically generated by Kongregate's AI and I cannot find a way to remove it.",
    "Semi-idle game where you play as a necromancer summoning zombies.",
    "",
    None,
])
def test_classify_ai_none(text):
    assert classify_ai(text) is None


def test_classify_ai_on_fixture_descriptions():
    """Every listed game's description is classified; the research sample had 7 of 13 new uploads declaring."""
    cards = parse_cards(load("list_new.html"))
    labels = {c["external_id"]: classify_ai(ld_fields(c["ld"]).get("description")) for c in cards}
    assert labels["mardrom/hedgemoot"] == "ai"
    assert labels["breymantech/neon-syndicate-idle-crime-empire"] == "ai"
    assert sum(1 for v in labels.values() if v == "ai") >= 5
