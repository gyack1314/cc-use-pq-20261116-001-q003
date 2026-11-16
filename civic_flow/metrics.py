"""按业务时点重建案件并计算回应指标。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .clock import PolicyBook, evaluate_case
from .contracts import DomainError, utc
from .store import Store

def reconstruct(base: dict, events: Iterable[dict], as_of: str) -> dict:
    item = dict(base)
    item["status"] = "accepted"
    item["department"] = base["initial_department"]
    item["resolution"] = None
    item["first_response_at"] = None
    item["version"] = 1
    item["acknowledged"] = False
    item["explanation_count"] = 0
    item["response_count"] = 0
    item["reopen_count"] = 0
    item["last_event_at"] = base["accepted_at"]
    for event in events:
        if event["occurred_at"] > as_of:
            continue
        kind = event["kind"]
        item["last_event_at"] = event["occurred_at"]
        item["version"] = event["case_version"]
        if kind == "assigned":
            item["department"] = event["department"]
            item["status"] = "assigned"
        elif kind in {"responded", "explained"}:
            item["status"] = kind
            item["response_count"] += 1
            if kind == "explained":
                item["explanation_count"] += 1
            if item["first_response_at"] is None:
                item["first_response_at"] = event["occurred_at"]
        elif kind == "acknowledged":
            item["status"] = "acknowledged"
            item["acknowledged"] = True
        elif kind == "closed":
            item["status"] = "closed"
            item["resolution"] = event["resolution"]
        elif kind == "reopened":
            item["status"] = "reopened"
            item["resolution"] = None
            item["reopen_count"] += 1
        else:
            raise DomainError("CORRUPT_EVENT", "Unknown stored event kind", 500)
    return item

def flags_for(case: dict, evaluation: dict) -> dict:
    """组合状态重建与服务时钟评估，得到统一的逾期判断。"""
    clock = evaluation["flags"]
    closed = case["status"] == "closed"
    return {
        "response_on_time": clock["response_on_time"],
        "response_overdue": clock["response_overdue"],
        "resolution_overdue": clock["resolution_overdue"],
        "responded": case["first_response_at"] is not None,
        "closed": closed,
        "solved": closed and case["resolution"] == "solved",
        "explained_close": closed and case["resolution"] == "explained",
        "out_of_scope": closed and case["resolution"] == "out_of_scope",
        "acknowledged": case["acknowledged"],
        "reopened": case["reopen_count"] > 0,
        "explained": case["explanation_count"] > 0,
        "cycles": clock["cycles"],
        "cycles_response_late": clock["cycles_response_late"],
        "cycles_resolution_late": clock["cycles_resolution_late"],
    }

COUNTERS = (
    "accepted", "open", "responded", "response_on_time", "response_overdue",
    "resolution_overdue", "closed", "solved", "explained_close", "out_of_scope",
    "acknowledged", "reopened", "explained", "priority_one",
    "cycles_total", "cycles_response_late", "cycles_resolution_late",
)

def empty_counters() -> dict[str, int]:
    return {key: 0 for key in COUNTERS}

def accrue(counters: dict, case: dict, flags: dict) -> None:
    counters["accepted"] += 1
    counters["open"] += int(not flags["closed"])
    for field in ("responded", "response_on_time", "response_overdue",
                  "resolution_overdue", "closed", "solved", "explained_close",
                  "out_of_scope", "acknowledged", "reopened", "explained"):
        counters[field] += int(flags[field])
    counters["priority_one"] += int(case["priority"] == 1)
    counters["cycles_total"] += flags["cycles"]
    counters["cycles_response_late"] += flags["cycles_response_late"]
    counters["cycles_resolution_late"] += flags["cycles_resolution_late"]

def ratios(counters: dict) -> dict:
    accepted = counters["accepted"]
    responded = counters["responded"]
    closed = counters["closed"]
    return {
        "response_rate": round(responded / accepted, 4) if accepted else None,
        "on_time_response_rate": round(counters["response_on_time"] / responded, 4)
                                 if responded else None,
        "closure_rate": round(closed / accepted, 4) if accepted else None,
        "solution_share_of_closed": round(counters["solved"] / closed, 4) if closed else None,
        "acknowledgment_rate": round(counters["acknowledged"] / responded, 4)
                               if responded else None,
    }

class MetricsService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _group(case: dict, key: str) -> str:
        return str(case[{"district": "district", "grid": "grid_id", "category": "category",
                         "department": "department"}[key]])

    def calculate(self, db, *, as_of: datetime, group_by: str,
                  district: str | None = None, category: str | None = None,
                  watermark: int | None = None, include_cases: bool = False) -> dict:
        stamp = utc(as_of)
        if group_by not in {"district", "grid", "category", "department"}:
            raise DomainError("BAD_GROUP", "Unsupported grouping")
        if watermark is None:
            watermark = self.store.watermark(db)
        book = PolicyBook.load(db, watermark)
        filters = ["created_sequence<=?", "accepted_at<=?"]
        params: list[object] = [watermark, stamp]
        if district is not None:
            filters.append("district=?")
            params.append(district)
        if category is not None:
            filters.append("category=?")
            params.append(category)
        query = "SELECT * FROM cases WHERE " + " AND ".join(filters) + " ORDER BY case_id"
        rows = [dict(value) for value in db.execute(query, params)]
        totals = empty_counters()
        grouped: dict[str, dict] = defaultdict(empty_counters)
        details = []
        for base in rows:
            events = self.store.events(db, base["case_id"], watermark)
            case = reconstruct(base, events, stamp)
            evaluation = evaluate_case(base, events, stamp, book)
            flags = flags_for(case, evaluation)
            accrue(totals, case, flags)
            accrue(grouped[self._group(case, group_by)], case, flags)
            if include_cases:
                current = evaluation["current"]
                details.append({"case_id": case["case_id"], "status": case["status"],
                                "version": case["version"], "department": case["department"],
                                "district": case["district"],
                                "cycle": current["cycle"],
                                "response_due": current["response"]["due"],
                                "resolution_due": current["resolution"]["due"],
                                "response_remaining_hours":
                                    current["response"]["remaining_hours"],
                                "resolution_remaining_hours":
                                    current["resolution"]["remaining_hours"],
                                "breaches": current["breaches"],
                                "flags": flags})
        return {
            "as_of": stamp, "watermark": watermark, "group_by": group_by,
            "filters": {"district": district, "category": category},
            "totals": {**totals, **ratios(totals)},
            "groups": [{"key": key, **value, **ratios(value)} for key, value in sorted(grouped.items())],
            **({"cases": details} if include_cases else {}),
        }

    def preview(self, *, as_of: datetime, group_by: str = "district",
                district: str | None = None, category: str | None = None,
                include_cases: bool = False) -> dict:
        with self.store.transaction() as db:
            return self.calculate(db, as_of=as_of, group_by=group_by,
                                  district=district, category=category,
                                  include_cases=include_cases)

    def case_at(self, case_id: int, as_of: datetime) -> dict:
        with self.store.transaction() as db:
            base = self.store.case(db, case_id)
            stamp = utc(as_of)
            if base["accepted_at"] > stamp:
                raise DomainError("CASE_NOT_YET_ACCEPTED", "Case was not accepted at time", 404)
            watermark = self.store.watermark(db)
            events = self.store.events(db, case_id, watermark)
            book = PolicyBook.load(db, watermark)
            evaluation = evaluate_case(base, events, stamp, book)
            current = reconstruct(base, events, stamp)
            current["indicators"] = flags_for(current, evaluation)
            current["clock"] = {"cycles": evaluation["cycles"],
                                "responsibility": evaluation["responsibility"]}
            current["watermark"] = watermark
            return current
