"""Estimated spend on cloud providers (CL-01).

Vendors bill in many ways (per second, per picture, per resolution, credits). The app only knows the one unit
price the user typed on the provider's settings, so every number here is an estimate, never an invoice.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .events import EventBus
from .util import read_json, write_json_atomic

KEEP_MONTHS = 24


def estimate(kind: str, price: float, seconds: float) -> float | None:
    """US dollars for one job: per second of video, per picture otherwise; None without a price."""
    if price <= 0:
        return None
    return round(price * (seconds if kind == "video" else 1), 4)


def estimate_note(usd: float) -> str:
    return f"預估費用約 US${usd:.2f}（依「引擎」頁填的單價，實際金額以供應商帳單為準）"


def _month(entry: object) -> dict | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("usd"), (int, float)):
        return None
    by = entry.get("byProvider") if isinstance(entry.get("byProvider"), dict) else {}
    return {"usd": float(entry["usd"]), "jobs": int(entry.get("jobs") or 0),
            "byProvider": {str(k): float(v) for k, v in by.items() if isinstance(v, (int, float))}}


class CostLedger:
    """Monthly totals of the estimates of finished cloud jobs, kept in costs.json as {"YYYY-MM": {...}}."""

    def __init__(self, path: Path, bus: EventBus | None = None):
        self._path = path
        self._bus = bus
        self._lock = threading.Lock()
        data = read_json(path, {})
        loaded = {k: _month(v) for k, v in data.items()} if isinstance(data, dict) else {}
        self._months = {k: v for k, v in loaded.items() if v is not None}

    def add(self, provider: str, usd: float) -> dict:
        with self._lock:
            key = time.strftime("%Y-%m")
            month = self._months.setdefault(key, {"usd": 0.0, "jobs": 0, "byProvider": {}})
            month["usd"] = round(month["usd"] + usd, 4)
            month["jobs"] += 1
            month["byProvider"][provider] = round(month["byProvider"].get(provider, 0.0) + usd, 4)
            for old in sorted(self._months)[:-KEEP_MONTHS]:
                del self._months[old]
            write_json_atomic(self._path, self._months)
            summary = self._summary(key)
        if self._bus:
            self._bus.publish("costs", summary)
        return summary

    def summary(self) -> dict:
        """This month's total: {"month", "usd", "jobs", "byProvider"}."""
        with self._lock:
            return self._summary(time.strftime("%Y-%m"))

    def _summary(self, key: str) -> dict:
        month = self._months.get(key) or {"usd": 0.0, "jobs": 0, "byProvider": {}}
        return {"month": key, "usd": month["usd"], "jobs": month["jobs"], "byProvider": dict(month["byProvider"])}
