"""Athlete discovery: name -> ESPN athlete id, plus biographical detail.

ESPN's athlete index is the only place to turn a name into the numeric id that
every other endpoint requires. It is paginated at 1000 records/page and includes
every athlete ESPN has ever carried (~20k), so we cache it aggressively.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from . import config
from .espn import ESPNClient

log = logging.getLogger(__name__)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


class AthleteNotFound(LookupError):
    pass


class AmbiguousAthlete(LookupError):
    def __init__(self, name: str, candidates: list[dict]) -> None:
        self.candidates = candidates
        lines = [
            f"  {c['id']:>8}  {c.get('displayName', '?'):<28}"
            f"{'active' if c.get('active') else 'inactive':<10}"
            f"exp={(c.get('experience') or {}).get('years', '?')}"
            for c in candidates
        ]
        super().__init__(
            f"{len(candidates)} athletes match {name!r}; pass the id explicitly:\n"
            + "\n".join(lines)
        )


def normalize_name(name: str) -> str:
    """Fold a display name to a comparable key: no accents, punctuation, or suffix."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9 ]+", "", text.lower())
    parts = [p for p in text.split() if p not in _SUFFIXES]
    return " ".join(parts)


def fetch_athlete_index(client: ESPNClient, force: bool = False) -> list[dict]:
    """Return every athlete record ESPN publishes, following pagination."""
    items: list[dict] = []
    page = 1
    page_count = 1
    while page <= page_count:
        payload = client.get_json(
            config.ATHLETE_INDEX_URL,
            params={"limit": config.INDEX_PAGE_SIZE, "page": page},
            cache_key=f"athlete_index/page_{page:03d}",
            force=force,
        )
        page_count = int(payload.get("pageCount") or 1)
        batch = payload.get("items") or []
        items.extend(batch)
        log.info("athlete index page %d/%d (%d records)", page, page_count, len(batch))
        page += 1
    return items


def find_athletes(index: Iterable[dict], name: str) -> list[dict]:
    """All index entries whose name normalizes to the same key as `name`."""
    target = normalize_name(name)
    matches = [
        item
        for item in index
        if normalize_name(item.get("displayName") or item.get("fullName") or "") == target
    ]
    # Active players first, then the most experienced - the likely intent.
    matches.sort(
        key=lambda i: (not i.get("active", False), -((i.get("experience") or {}).get("years") or 0))
    )
    return matches


def resolve_athlete_id(client: ESPNClient, name_or_id: str, force: bool = False) -> str:
    """Turn a player name (or a raw id) into an ESPN athlete id."""
    text = str(name_or_id).strip()
    if text.isdigit():
        return text

    index = fetch_athlete_index(client, force=force)
    matches = find_athletes(index, text)
    if not matches:
        log.info("%r not in the athlete index; falling back to site search", text)
        matches = search_athletes(client, text, force=force)
    if not matches:
        raise AthleteNotFound(
            f"No NFL athlete named {text!r} in ESPN's index "
            f"(searched {len(index)} records) or site search. "
            "Check spelling, or pass the numeric id."
        )
    if len(matches) > 1:
        active = [m for m in matches if m.get("active")]
        if len(active) != 1:
            raise AmbiguousAthlete(text, matches)
        matches = active
    return str(matches[0]["id"])


# ESPN player URLs, e.g. https://www.espn.com/nfl/player/_/id/15683/justin-tucker
_NFL_PLAYER_LINK = re.compile(r"espn\.com/nfl/player/(?:[a-z]+/)*_/id/(\d+)")


def search_athletes(client: ESPNClient, name: str, force: bool = False) -> list[dict]:
    """Resolve a name through ESPN's site search.

    The athlete index is not exhaustive - some active players are missing from
    it entirely - so search is the backstop. Results span every sport, and the
    only reliable athlete id is the one embedded in the NFL player URL.
    """
    payload = client.get_json(
        config.SEARCH_URL,
        params={"query": name, "limit": 20, "sport": "football", "region": "us", "lang": "en"},
        cache_key=f"search/{normalize_name(name).replace(' ', '_') or 'blank'}",
        force=force,
    )
    target = normalize_name(name)
    found: dict[str, dict] = {}
    for group in payload.get("results") or []:
        if group.get("type") != "player":
            continue
        for content in group.get("contents") or []:
            web = ((content.get("link") or {}).get("web")) or ""
            match = _NFL_PLAYER_LINK.search(web)
            if not match:
                continue
            display = content.get("displayName") or ""
            if normalize_name(display) != target:
                continue
            found.setdefault(
                match.group(1), {"id": match.group(1), "displayName": display, "active": True}
            )
    return list(found.values())


def _inches(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def fetch_profile(client: ESPNClient, athlete_id: str, force: bool = False) -> dict:
    """Merge the two athlete endpoints into one flat record for the `athletes` table.

    The core v3 record carries the biographical fields; the site profile carries
    current position, team and roster status.
    """
    bio = client.get_json(
        config.ATHLETE_BIO_URL.format(athlete_id=athlete_id),
        cache_key=f"athlete_bio/{athlete_id}",
        force=force,
    )
    profile_payload = client.get_json(
        config.ATHLETE_PROFILE_URL.format(athlete_id=athlete_id),
        cache_key=f"athlete_profile/{athlete_id}",
        force=force,
    )
    profile = profile_payload.get("athlete") or {}

    position = profile.get("position") or {}
    team = profile.get("team") or {}
    status = profile.get("status") or {}
    birth_place = bio.get("birthPlace") or {}
    experience = bio.get("experience") or {}

    return {
        "athlete_id": str(athlete_id),
        "display_name": profile.get("displayName") or bio.get("displayName"),
        "first_name": bio.get("firstName"),
        "last_name": bio.get("lastName"),
        "position_abbr": position.get("abbreviation"),
        "position_name": position.get("displayName"),
        "team_id": str(team["id"]) if team.get("id") else None,
        "team_abbr": team.get("abbreviation"),
        "jersey": bio.get("jersey") or profile.get("jersey"),
        "height_inches": _inches(bio.get("height")),
        "weight_lbs": _inches(bio.get("weight")),
        "birth_date": (bio.get("dateOfBirth") or "")[:10] or None,
        "birth_city": birth_place.get("city"),
        "birth_state": birth_place.get("state"),
        "birth_country": birth_place.get("country"),
        "experience_years": experience.get("years"),
        "active": 1 if bio.get("active") else 0,
        "status": status.get("name"),
    }
