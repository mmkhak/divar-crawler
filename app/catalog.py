"""Bundled Divar categories and cities, with a best-effort refresh."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "bundled"


def load_categories(data_dir: Path) -> list[dict[str, Any]]:
    override = data_dir / "categories.json"
    path = override if override.exists() else BUNDLED / "categories_flat.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_cities(data_dir: Path) -> list[dict[str, Any]]:
    override = data_dir / "cities.json"
    path = override if override.exists() else BUNDLED / "cities_slim.json"
    return json.loads(path.read_text(encoding="utf-8"))


def search_cities(cities: list[dict[str, Any]], query: str, limit: int = 30) -> list[dict[str, Any]]:
    needle = (query or "").strip()
    if not needle:
        preferred = [city for city in cities if city["id"] in {"1", "2", "3", "4", "5", "6"}]
        rest = [city for city in cities if city not in preferred][: limit - len(preferred)]
        return preferred + rest
    hits = [
        city
        for city in cities
        if needle in city["name"] or needle.lower() in city.get("slug", "").lower() or needle == city["id"]
    ]
    return hits[:limit]


def normalize_city_payload(payload: Any) -> list[dict[str, str]] | None:
    """Accept a few plausible Divar city-list shapes. Return None if unrecognized."""
    items = payload
    if isinstance(payload, dict):
        for key in ("cities", "items", "places"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            return None
    if not isinstance(items, list) or not items:
        return None
    cities: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        city_id = item.get("id") or item.get("city_id")
        name = item.get("name") or item.get("title") or item.get("name_fa")
        if city_id is None or not name:
            continue
        cities.append(
            {
                "id": str(city_id),
                "name": str(name),
                "slug": str(item.get("slug") or ""),
            }
        )
    return cities or None


async def refresh_cities(data_dir: Path, client: Any) -> dict[str, Any]:
    status, payload = await client.fetch_cities()
    cities = normalize_city_payload(payload) if status == 200 else None
    if not cities:
        return {"updated": False, "status": status, "count": len(load_cities(data_dir))}
    path = data_dir / "cities.json"
    path.write_text(json.dumps(cities, ensure_ascii=False), encoding="utf-8")
    return {"updated": True, "status": status, "count": len(cities)}
