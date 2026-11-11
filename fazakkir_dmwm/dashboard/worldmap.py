"""Places the visitors of the daily usage page on a world map.

Requests are recorded with the country name the IP lookup returns (see
``geo.py``). A request is placed by matching that name against the countries in
``data/countries.json``: each country's common and official names and its
alternative spellings, plus the names the lookup uses that differ from them.
Requests that cannot be matched, or come from an "Unknown" or "Local" address,
are still counted in the tables but not drawn.

Coordinates are country centroids from the ``world-countries`` package. The
outline in ``static/img/world-map.svg`` is an equirectangular projection of the
Natural Earth land polygons (via ``world-atlas``) for latitudes 84 to -58, so a
place's position is a fixed percentage of the map box.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

LATITUDE_TOP = 84.0
LATITUDE_BOTTOM = -58.0

DATA_FILE = Path(__file__).resolve().parent / "data" / "countries.json"

# Names the lookup service uses that the dataset spells differently.
ALIASES = {
    "Burma": "MM",
    "Cape Verde": "CV",
    "Congo-Brazzaville": "CG",
    "Congo-Kinshasa": "CD",
    "Democratic Republic of the Congo": "CD",
    "Republic of the Congo": "CG",
    "Ivory Coast": "CI",
    "Côte d'Ivoire": "CI",
    "Macedonia": "MK",
    "Swaziland": "SZ",
    "Turkiye": "TR",
    "Türkiye": "TR",
    "Russia": "RU",
    "Brunei Darussalam": "BN",
    "East Timor": "TL",
    "Palestinian Territory": "PS",
    "Vietnam": "VN",
    "South Korea": "KR",
    "North Korea": "KP",
    "Laos": "LA",
    "Syria": "SY",
    "Iran": "IR",
    "Bolivia": "BO",
    "Venezuela": "VE",
    "Tanzania": "TZ",
    "Moldova": "MD",
    "Taiwan": "TW",
    "Czechia": "CZ",
}

# Places the lookup reports that are not countries in the dataset.
EXTRA_PLACES = {
    "Hong Kong": ("HK", "Hong Kong", 22.3, 114.2),
    "Macau": ("MO", "Macau", 22.2, 113.55),
    "Macao": ("MO", "Macau", 22.2, 113.55),
    "Kosovo": ("XK", "Kosovo", 42.6, 20.9),
}


def _key(name: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", name or "")
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", plain.lower().replace("&", "and"))


@lru_cache(maxsize=1)
def _index() -> dict[str, tuple[str, str, float, float]]:
    rows = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    by_code = {row["code"]: row for row in rows}
    index: dict[str, tuple[str, str, float, float]] = {}

    def add(name: str, place: tuple[str, str, float, float]) -> None:
        index.setdefault(_key(name), place)

    for row in rows:
        place = (row["code"], row["name"], row["lat"], row["lng"])
        for name in [row["name"], *row["names"]]:
            add(name, place)
    for name, code in ALIASES.items():
        row = by_code.get(code)
        if row is not None:
            add(name, (row["code"], row["name"], row["lat"], row["lng"]))
    for name, place in EXTRA_PLACES.items():
        add(name, place)
    return index


def locate(name: str | None) -> tuple[str, str, float, float] | None:
    """The ``(code, name, latitude, longitude)`` of a country name, or ``None``."""
    if not name:
        return None
    return _index().get(_key(name))


def _project(lat: float, lng: float) -> tuple[float, float]:
    left = (lng + 180.0) / 360.0 * 100.0
    top = (LATITUDE_TOP - lat) / (LATITUDE_TOP - LATITUDE_BOTTOM) * 100.0
    return round(left, 3), round(top, 3)


def pins_for(countries: list[dict]) -> tuple[list[dict], int]:
    """Pins for ``countries`` (rows with ``country`` and ``count``).

    Returns ``(pins, unplaced)``: one pin per country, sized by its share of the
    requests, largest first, and the number of requests that could not be placed.
    """
    totals: dict[str, dict] = {}
    unplaced = 0
    for row in countries:
        place = locate(row["country"])
        if place is None:
            unplaced += row["count"]
            continue
        code, name, lat, lng = place
        entry = totals.setdefault(code, {"name": name, "lat": lat, "lng": lng, "count": 0})
        entry["count"] += row["count"]
    if not totals:
        return [], unplaced

    largest = max(entry["count"] for entry in totals.values())
    pins = []
    for entry in totals.values():
        left, top = _project(entry["lat"], entry["lng"])
        pins.append(
            {
                "name": entry["name"],
                "count": entry["count"],
                "left": left,
                "top": top,
                "size": round(20 + 24 * math.sqrt(entry["count"] / largest), 1),
            }
        )
    pins.sort(key=lambda pin: (-pin["count"], pin["name"]))
    return pins, unplaced
