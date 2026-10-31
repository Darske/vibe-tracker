"""Contract test: every adapter in sources.yaml loads, subclasses SourceAdapter, declares the
required attributes, has a fixture folder, and matches its yaml key."""

from pathlib import Path

import pytest

from tracker.base import SourceAdapter
from tracker.registry import ROOT, adapter_class, load_sources

SOURCES = load_sources()


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_adapter_contract(name):
    cfg = SOURCES[name]
    cls = adapter_class(cfg)
    assert issubclass(cls, SourceAdapter)
    assert cls.name == name
    for attr in ("display_name", "base_url", "segment"):
        assert isinstance(getattr(cls, attr), str) and getattr(cls, attr)
    assert cls.segment in ("aggregator", "creation")
    assert cls.base_url.startswith("https://")
    assert cfg["segment"] == cls.segment, "sources.yaml segment must match the adapter"
    assert cfg["lists"], "every source needs at least one list to sweep"
    assert cfg["metric_map"], "metric_map is required so the dashboard can label figures"
    if cfg["needs_browser"]:
        assert cfg["poll_every_hours"] >= 6
    fixtures = ROOT / "tests" / "fixtures" / name
    assert fixtures.is_dir() and any(fixtures.iterdir()), f"record fixtures with: python -m tracker.pipeline record --source {name}"
    assert (ROOT / "tests" / f"test_{name}.py").exists(), "every adapter needs a parse test"


def test_sources_doc_mentions_every_source():
    doc = (ROOT / "docs" / "SOURCES.md").read_text().lower()
    for name, cfg in SOURCES.items():
        assert cfg["display_name"].lower() in doc, f"{name}: add its research row to docs/SOURCES.md before writing code"
