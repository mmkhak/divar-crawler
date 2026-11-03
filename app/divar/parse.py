"""Pure parsers for Divar search, detail, and contact responses."""
from __future__ import annotations

import json
import re
from typing import Any

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+98|0098|0)?9\d{9}(?!\d)|(?<!\d)0\d{10}(?!\d)"
)
FLEX_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+98|0098|0)?[\s\-–—._]*9(?:[\s\-–—._]*\d){9}(?!\d)"
    r"|(?<!\d)0(?:[\s\-–—._]*\d){10}(?!\d)"
)
LIMIT_RE = re.compile(r"limit|محدودیت|سقف|بیش از حد", re.I)


def normalize_phone_input(raw: str) -> str:
    digits = re.sub(r"\D", "", (raw or "").translate(FA_DIGITS))
    if len(digits) < 10:
        raise ValueError("شماره موبایل نامعتبر است")
    return "0" + digits[-10:]


def extract_token(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    token = payload.get("token") or payload.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def parse_search_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return rows
    for widget in payload.get("list_widgets") or []:
        if not isinstance(widget, dict) or widget.get("widget_type") != "POST_ROW":
            continue
        data = widget.get("data") or {}
        token = data.get("token")
        if not token:
            action = (data.get("action") or {}).get("payload") or {}
            token = action.get("token")
        if not token:
            continue
        web_info = ((data.get("action") or {}).get("payload") or {}).get("web_info") or {}
        rows.append(
            {
                "token": token,
                "title": data.get("title") or "",
                "district": web_info.get("district_persian") or "",
                "price_text": data.get("middle_description_text") or "",
                "list_bottom": data.get("bottom_description_text") or "",
                "red_text": data.get("red_text") or "",
                "image_count": data.get("image_count"),
                "has_chat": bool(data.get("has_chat")),
            }
        )
    return rows


def pagination_state(payload: dict[str, Any] | None) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return None, False
    page = payload.get("pagination") or {}
    if not isinstance(page, dict):
        return None, False
    return page.get("data"), bool(page.get("has_next_page"))


def parse_detail(token: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "token": token,
        "description": "",
        "subtitle": "",
        "specs": {},
        "breadcrumb": [],
        "contact_uuid": None,
        "error": payload is None,
    }
    if not isinstance(payload, dict):
        return result
    contact = payload.get("contact") or {}
    if isinstance(contact, dict):
        result["contact_uuid"] = contact.get("contact_uuid")
    last_title = "?"
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("section_name")
        for widget in section.get("widgets") or []:
            if not isinstance(widget, dict):
                continue
            data = widget.get("data") or {}
            kind = widget.get("widget_type")
            if name == "BREADCRUMB" and kind == "BREADCRUMB":
                parents = [
                    item.get("title")
                    for item in (data.get("parent_items") or [])
                    if isinstance(item, dict)
                ]
                result["breadcrumb"] = parents + [data.get("current_page_title")]
            if name == "DESCRIPTION" and kind == "DESCRIPTION_ROW":
                result["description"] = data.get("text") or ""
            if kind == "EXPANDABLE_SECTION":
                for child in data.get("widget_list") or []:
                    if (
                        isinstance(child, dict)
                        and child.get("widget_type") == "DESCRIPTION_ROW"
                        and not result["subtitle"]
                    ):
                        result["subtitle"] = (child.get("data") or {}).get("text") or ""
            if kind == "UNEXPANDABLE_ROW":
                result["specs"][data.get("title") or "?"] = data.get("value")
            if name == "LIST_DATA" and kind == "TITLE_ROW":
                last_title = data.get("text") or "?"
            if name == "LIST_DATA" and kind == "DESCRIPTION_ROW":
                result["specs"][last_title] = data.get("text")
    return result


def snippet(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def matches_keywords(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = (text or "").lower()
    return any(word.lower() in haystack for word in keywords if word.strip())


def _canon_one(raw: str) -> str | None:
    digits = re.sub(r"\D", "", (raw or "").translate(FA_DIGITS))
    if len(digits) == 10 and digits[0] == "9":
        digits = "0" + digits
    if len(digits) == 12 and digits.startswith("98"):
        digits = "0" + digits[2:]
    if len(digits) == 11 and digits[0] == "0":
        return digits
    return None


def canonical_phones(text: str) -> list[str]:
    translated = (text or "").translate(FA_DIGITS)
    ordered: list[str] = []
    for pattern in (PHONE_RE, FLEX_PHONE_RE):
        for raw in pattern.findall(translated):
            number = _canon_one(raw)
            if number and number not in ordered:
                ordered.append(number)
    return ordered


def phones_from_contact(body: str) -> list[str]:
    """Read numbers from contact_info JSON widgets, then fall back to regex."""
    ordered = canonical_phones(body)
    try:
        payload = json.loads(body or "")
    except Exception:
        return ordered
    if not isinstance(payload, dict):
        return ordered

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("phone_number", "phone", "mobile", "value"):
                value = node.get(key)
                if isinstance(value, str):
                    for number in canonical_phones(value):
                        if number not in ordered:
                            ordered.append(number)
            action = node.get("action")
            if isinstance(action, dict):
                inner = action.get("payload") or {}
                if isinstance(inner, dict):
                    phone = inner.get("phone_number")
                    if isinstance(phone, str):
                        for number in canonical_phones(phone):
                            if number not in ordered:
                                ordered.append(number)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return ordered


def classify_contact(status: int, body: str) -> dict[str, Any]:
    """Map a contact_info response to a defensive outcome.

    kind: phones | empty | expired | limited | retry | missing
    """
    text = body or ""
    if status == 401:
        return {"kind": "expired", "phones": [], "detail": text[:200]}
    if status == 403 or status == 429:
        return {"kind": "limited", "phones": [], "detail": text[:200]}
    if status == 404:
        return {"kind": "missing", "phones": [], "detail": ""}
    if status != 200:
        if LIMIT_RE.search(text):
            return {"kind": "limited", "phones": [], "detail": text[:200]}
        return {"kind": "retry", "phones": [], "detail": text[:200]}
    phones = phones_from_contact(text)
    if not phones and LIMIT_RE.search(text):
        return {"kind": "limited", "phones": [], "detail": text[:200]}
    if not phones:
        return {"kind": "empty", "phones": [], "detail": ""}
    return {"kind": "phones", "phones": phones, "detail": ""}


def search_body(
    city_id: str,
    category: str,
    query: str | None,
    pagination_data: Any,
) -> dict[str, Any]:
    search_data: dict[str, Any] = {
        "form_data": {"data": {"category": {"str": {"value": category}}}}
    }
    if query:
        search_data["query"] = query
    body: dict[str, Any] = {"city_ids": [str(city_id)], "search_data": search_data}
    if pagination_data:
        body["pagination_data"] = pagination_data
    return body
