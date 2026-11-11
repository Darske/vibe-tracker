"""Pydantic models shared by every adapter. Nothing else in the system knows platform specifics.

Rule: every counter is `int | None`. A platform that does not expose a number yields None — never 0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class GameRef(BaseModel):
    source: str
    external_id: str
    url: str


class ListPlacement(BaseModel):
    source: str
    list_name: str
    position: int
    external_id: str
    captured_at: datetime


class GameSnapshot(BaseModel):
    source: str
    external_id: str
    url: str
    title: str | None = None
    thumbnail_url: str | None = None
    creator_external_id: str | None = None
    creator_name: str | None = None
    creator_url: str | None = None

    # counters — int | None, never coerced to 0
    plays_total: int | None = None
    plays_24h: int | None = None
    players_now: int | None = None
    players_total: int | None = None
    likes: int | None = None
    remixes: int | None = None
    comments: int | None = None
    votes: int | None = None
    followers: int | None = None
    rating: float | None = None  # average star rating where the platform shows one (Kongregate, itch.io top-rated)

    ai_tool: str | None = None
    engine: str | None = None
    tags: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    ai_disclosure: Literal["ai", "no_ai"] | None = None  # legacy platforms: the game's own AI declaration, None = unknown
    status: Literal["ok", "gone"] = "ok"
    captured_at: datetime

    @field_validator(
        "plays_total", "plays_24h", "players_now", "players_total",
        "likes", "remixes", "comments", "votes", "followers", mode="before",
    )
    @classmethod
    def _non_negative_or_none(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("counter cannot be bool")
        if isinstance(v, str):
            v = v.strip().replace(",", "")
            if v == "":
                return None
        v = int(v)
        if v < 0:
            raise ValueError("counter cannot be negative")
        return v


class CreatorProfile(BaseModel):
    source: str
    external_id: str
    name: str | None = None
    url: str | None = None
    avatar_url: str | None = None
    followers: int | None = None
    bio: str | None = None
    links: dict[str, str] = Field(default_factory=dict)  # x, youtube, tiktok, discord, website, ...
