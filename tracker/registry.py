"""Loads sources.yaml and imports adapters by module path."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import yaml

from tracker.base import SourceAdapter

ROOT = Path(__file__).resolve().parent.parent
SOURCES_YAML = ROOT / "sources.yaml"

DEFAULTS = {
    "enabled": True,
    "lists": ["top"],
    "top_n": 100,
    "poll_every_hours": 2,
    "rate_limit_rps": 1,
    "needs_browser": False,
    "stale_after_days": 30,
    "max_watchlist": 400,
    "metric_map": {},
}


def load_sources(path: Path = SOURCES_YAML) -> dict[str, dict]:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    out = {}
    for name, cfg in raw.items():
        merged = {**DEFAULTS, **(cfg or {})}
        merged["name"] = name
        if "module" not in merged:
            raise ValueError(f"sources.yaml: {name} has no module")
        if merged["needs_browser"] and merged["poll_every_hours"] < 6:
            merged["poll_every_hours"] = 6
        out[name] = merged
    return out


def adapter_class(cfg: dict) -> type[SourceAdapter]:
    mod = importlib.import_module(cfg["module"])
    classes = [
        c for _, c in inspect.getmembers(mod, inspect.isclass)
        if issubclass(c, SourceAdapter) and c is not SourceAdapter and c.__module__ == mod.__name__
    ]
    if len(classes) != 1:
        raise ValueError(f"{cfg['module']} must define exactly one SourceAdapter subclass, found {len(classes)}")
    cls = classes[0]
    if cls.name != cfg["name"]:
        raise ValueError(f"{cfg['module']}: adapter.name={cls.name!r} != sources.yaml key {cfg['name']!r}")
    return cls
