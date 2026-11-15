"""In-memory ring of outbound Divar requests for the panel."""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

TOKEN_RE = re.compile(r"(Bearer |Basic |sAccessToken=|token=)([^\s&\"']+)", re.I)


def redact(text: str) -> str:
    return TOKEN_RE.sub(r"\1***", text or "")


class TrafficBuffer:
    def __init__(self, limit: int = 400) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=limit)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def on_entry(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def record(
        self,
        method: str,
        url: str,
        status: int,
        elapsed_ms: int,
        note: str = "",
    ) -> dict[str, Any]:
        entry = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "method": method.upper(),
            "url": redact(url),
            "status": status,
            "elapsed_ms": elapsed_ms,
            "note": redact(note)[:240],
        }
        self._entries.append(entry)
        for listener in self._listeners:
            try:
                listener(entry)
            except Exception:
                pass
        return entry

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        items = list(self._entries)
        if limit < len(items):
            return items[-limit:]
        return items

    def clear(self) -> None:
        self._entries.clear()
