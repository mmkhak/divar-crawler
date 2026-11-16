"""Parse Divar search listing URLs into crawl job fields."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

PRELOADED_RE = re.compile(
    r"window\.__PRELOADED_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>",
    re.S,
)
PATH_RE = re.compile(
    r"^/s/(?P<city>[^/]+)(?:/(?P<category>[^/?#]+))?",
    re.I,
)

# Virtual nationwide place used by divar.ir/s/iran/...
IRAN_PLACE = {"id": "715", "name": "کل ایران", "slug": "iran"}


def parse_divar_search_url(url: str) -> dict[str, Any]:
    """Extract city/category/query from a /s/... Divar URL (no network)."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("لینک خالی است")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host and "divar.ir" not in host:
        raise ValueError("لینک باید مربوط به divar.ir باشد")
    match = PATH_RE.match(parsed.path or "")
    if not match:
        raise ValueError("فرمت لینک درست نیست؛ مثال: https://divar.ir/s/iran/beauty-services")
    city_slug = unquote(match.group("city") or "").strip().strip("/")
    category_slug = unquote(match.group("category") or "").strip().strip("/")
    if not city_slug:
        raise ValueError("اسلاگ شهر در لینک پیدا نشد")
    query_params = parse_qs(parsed.query or "")
    keywords: list[str] = []
    for key in ("q", "query"):
        for value in query_params.get(key) or []:
            value = unquote(value).strip()
            if value:
                keywords.append(value)
    return {
        "city_slug": city_slug,
        "category_slug": category_slug or "root",
        "keywords": keywords,
        "url": f"https://divar.ir{parsed.path}" + (f"?{parsed.query}" if parsed.query else ""),
    }


def extract_search_from_preloaded(html: str) -> dict[str, Any] | None:
    """Read city/category/query from Divar SSR/CSR preloaded state."""
    match = PRELOADED_RE.search(html or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    category = _dig(data, "search", "searchData", "formData", "category", "str", "value")
    if not category:
        category = _dig(data, "nb", "seoDetails", "canonical", "formData", "category", "str", "value")
    cities = _dig(data, "multiCity", "selectedCities") or []
    city = cities[0] if isinstance(cities, list) and cities else None
    query = _dig(data, "search", "searchData", "query")
    keywords = [str(query).strip()] if isinstance(query, str) and query.strip() else []
    if not category or not isinstance(city, dict):
        return None
    city_id = city.get("id")
    if city_id is None:
        return None
    return {
        "city_id": str(city_id),
        "city_name": str(city.get("name") or city.get("slug") or city_id),
        "city_slug": str(city.get("slug") or ""),
        "category": str(category),
        "keywords": keywords,
    }


def resolve_city_slug(cities: list[dict[str, Any]], slug: str) -> dict[str, str] | None:
    needle = (slug or "").strip().lower()
    if not needle:
        return None
    if needle == "iran":
        return dict(IRAN_PLACE)
    for city in cities:
        if str(city.get("slug") or "").lower() == needle:
            return {
                "id": str(city["id"]),
                "name": str(city.get("name") or city["id"]),
                "slug": str(city.get("slug") or needle),
            }
        if str(city.get("id")) == needle:
            return {
                "id": str(city["id"]),
                "name": str(city.get("name") or city["id"]),
                "slug": str(city.get("slug") or ""),
            }
    return None


def resolve_category_slug(categories: list[dict[str, Any]], slug: str) -> str:
    needle = (slug or "").strip()
    if not needle:
        return "root"
    for item in categories:
        if item.get("slug") == needle:
            return needle
    # Common Divar marketing/SEO aliases that still open a real category page.
    aliases = {
        "beauty-services": "beauty-and-haircare",
    }
    return aliases.get(needle, needle)


def _dig(data: Any, *path: str) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


async def resolve_divar_search_url(
    url: str,
    *,
    cities: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    fetch_html,
) -> dict[str, Any]:
    """Resolve a Divar /s/... URL into job fields.

    Prefers live page state (handles aliases like beauty-services → beauty-and-haircare),
    falls back to local city/category catalogs.
    """
    parsed = parse_divar_search_url(url)
    resolved: dict[str, Any] = {
        "source_url": parsed["url"],
        "city_id": None,
        "city_name": parsed["city_slug"],
        "category": resolve_category_slug(categories, parsed["category_slug"]),
        "keywords": list(parsed["keywords"]),
        "resolved_from": "local",
    }
    try:
        status, html = await fetch_html(parsed["url"])
    except Exception:
        status, html = 0, ""
    if status == 200 and html:
        live = extract_search_from_preloaded(html)
        if live:
            resolved.update(
                {
                    "city_id": live["city_id"],
                    "city_name": live["city_name"],
                    "category": live["category"],
                    "keywords": live["keywords"] or resolved["keywords"],
                    "resolved_from": "page",
                }
            )
            return resolved
    city = resolve_city_slug(cities, parsed["city_slug"])
    if not city:
        raise ValueError(f"شهر «{parsed['city_slug']}» در فهرست محلی پیدا نشد")
    resolved["city_id"] = city["id"]
    resolved["city_name"] = city["name"]
    return resolved
