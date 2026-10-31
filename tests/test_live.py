"""Live smoke test: hits each enabled source once with a small limit. Run manually:
    CONTACT_EMAIL=you@example.com python -m pytest -m live --override-ini="addopts="
"""

import pytest

from tracker.http import Http
from tracker.registry import adapter_class, load_sources

SOURCES = {k: v for k, v in load_sources().items() if v["enabled"]}


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(SOURCES))
def test_live_smoke(name):
    cfg = SOURCES[name]
    http = Http(name, rate_limit_rps=float(cfg["rate_limit_rps"]))
    try:
        adapter = adapter_class(cfg)(http, cfg)
        pairs = list(adapter.discover(cfg["lists"][:1], 10))
        assert pairs, f"{name}: discover returned nothing"
        refs = []
        seen = set()
        for ref, _ in pairs:
            if ref.external_id not in seen:
                seen.add(ref.external_id)
                refs.append(ref)
        snaps = list(adapter.fetch(refs[:5]))
        assert any(s.status == "ok" for s in snaps), f"{name}: no snapshot"
        assert any(
            s.plays_total is not None or s.players_now is not None or s.likes is not None for s in snaps
        ), f"{name}: no counter populated"
    finally:
        http.close()
