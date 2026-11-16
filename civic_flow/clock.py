"""带生效时间的工作日历、服务时钟与案件服务周期评估。

逾期判断的唯一实现：当前待办（operations）、逾期统计（metrics/reports）
与单案服务时钟（cases/{id}/clock）都调用 evaluate_case，保证同一结果。
政策与日历按版本生效时间匹配，报表以日志水位固定所用版本，可还原原政策。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterator

from .contracts import DomainError, WEEKDAY_NAMES, parse_time, utc
from .store import Store

MAX_CLOCK_SPAN_DAYS = 370
MAX_CLOCK_CHUNKS = 4000  # 约 4000 个分段，足够覆盖数十年的预算

@dataclass(frozen=True)
class CalendarSpec:
    """单个日历版本：固定 UTC 偏移、每周工作窗口（分钟）、节假日。"""
    offset: timedelta
    windows: tuple  # windows[weekday] = ((start_min, end_min), ...)
    holidays: frozenset

    @staticmethod
    def parse_offset(text: str) -> timedelta:
        sign = -1 if text.startswith("-") else 1
        body = text.lstrip("+-")
        hours, minutes = (int(part) for part in body.split(":"))
        if hours > 14 or (hours == 14 and minutes):
            raise DomainError("BAD_OFFSET", "UTC offset must stay within 14 hours")
        return sign * timedelta(hours=hours, minutes=minutes)

    @classmethod
    def build(cls, payload: dict) -> "CalendarSpec":
        offset = cls.parse_offset(payload.get("utc_offset") or "+00:00")
        workweek = payload.get("workweek") or {}
        windows: list[tuple] = [tuple() for _ in range(7)]
        total = 0
        for day, spans in workweek.items():
            parsed = sorted((int(s[:2]) * 60 + int(s[3:]), int(e[:2]) * 60 + int(e[3:]))
                            for s, e in spans)
            for index in range(1, len(parsed)):
                if parsed[index][0] < parsed[index - 1][1]:
                    raise DomainError("WINDOW_OVERLAP",
                                      "Working windows overlap on " + str(day))
            total += sum(end - start for start, end in parsed)
            windows[WEEKDAY_NAMES.index(day)] = tuple(parsed)
        if total <= 0:
            raise DomainError("EMPTY_WORKWEEK", "Calendar needs working time in the week")
        holidays = frozenset(
            datetime.strptime(item, "%Y-%m-%d").date() for item in payload.get("holidays") or [])
        return cls(offset, tuple(windows), holidays)

    def canonical(self) -> dict:
        workweek = {}
        for index, spans in enumerate(self.windows):
            if spans:
                workweek[WEEKDAY_NAMES[index]] = [
                    ["%02d:%02d" % (start // 60, start % 60),
                     "%02d:%02d" % (end // 60, end % 60)] for start, end in spans]
        sign = "+" if self.offset >= timedelta(0) else "-"
        magnitude = abs(self.offset)
        hours, remainder = divmod(int(magnitude.total_seconds()), 3600)
        return {"utc_offset": "%s%02d:%02d" % (sign, hours, remainder // 60),
                "workweek": workweek,
                "holidays": sorted(day.isoformat() for day in self.holidays)}

@dataclass(frozen=True)
class ClockVersion:
    effective_from: datetime
    revision: int
    spec: CalendarSpec

class CalendarBook:
    """同一日历的全部版本；按生效时间选择，早于所有版本时用最早版本。"""
    def __init__(self, versions: list[ClockVersion]):
        if not versions:
            raise DomainError("CALENDAR_UNKNOWN", "Calendar has no usable version", 500)
        self.versions = sorted(versions, key=lambda v: (v.effective_from, v.revision))

    def at(self, instant: datetime) -> CalendarSpec:
        chosen = self.versions[0]
        for version in self.versions:
            if version.effective_from <= instant:
                chosen = version
            else:
                break
        return chosen.spec

    def next_boundary(self, instant: datetime) -> datetime | None:
        for version in self.versions:
            if version.effective_from > instant:
                return version.effective_from
        return None

def windows(spec: CalendarSpec, start: datetime, end: datetime) -> Iterator[tuple]:
    """展开 [start, end) 内的有效工作区间（UTC，裁剪到边界）。"""
    day = (start + spec.offset).date()
    while True:
        day_start = datetime.combine(day, time(0, 0), tzinfo=timezone.utc) - spec.offset
        if day_start >= end:
            return
        if day not in spec.holidays:
            for begin, finish in spec.windows[day.weekday()]:
                w_start = day_start + timedelta(minutes=begin)
                w_end = day_start + timedelta(minutes=finish)
                lo = w_start if w_start > start else start
                hi = w_end if w_end < end else end
                if lo < hi:
                    yield lo, hi
        day += timedelta(days=1)

def add_working_time(book: CalendarBook, start: datetime, hours: float) -> datetime:
    """从 start 起累计 hours 个工作小时后的时刻（跨版本、跨假期）。"""
    remaining = timedelta(hours=hours)
    position = start
    for _ in range(MAX_CLOCK_CHUNKS):
        boundary = book.next_boundary(position)
        chunk_end = boundary if boundary else position + timedelta(days=MAX_CLOCK_SPAN_DAYS)
        for lo, hi in windows(book.at(position), position, chunk_end):
            span = hi - lo
            if span >= remaining:
                return lo + remaining
            remaining -= span
        position = chunk_end
    raise DomainError("CLOCK_EXHAUSTED", "Working budget exceeds computable horizon", 500)

def working_time_between(book: CalendarBook, start: datetime, end: datetime) -> timedelta:
    if end <= start:
        return timedelta(0)
    total = timedelta(0)
    position = start
    while position < end:
        boundary = book.next_boundary(position)
        chunk_end = min(boundary, end) if boundary else end
        for lo, hi in windows(book.at(position), position, chunk_end):
            total += hi - lo
        position = chunk_end
    return total

class PolicyBook:
    """按日志水位固定的规则与日历版本集，供评估与重算共用。"""
    def __init__(self, rules: dict, calendars: dict):
        self.rules = rules          # category -> [version dict]，按 (effective_from, revision) 排序
        self.calendars = calendars  # calendar_id -> CalendarBook

    @classmethod
    def load(cls, db, watermark: int | None = None) -> "PolicyBook":
        rules: dict[str, list] = {}
        for row in Store.rule_versions(db, watermark):
            rules.setdefault(row["category"], []).append(row)
        calendars: dict[str, CalendarBook] = {}
        grouped: dict[str, list] = {}
        for row in Store.calendar_versions(db, watermark=watermark):
            grouped.setdefault(row["calendar_id"], []).append(ClockVersion(
                parse_time(row["effective_from"]), row["revision"],
                CalendarSpec.build(json.loads(row["spec_json"]))))
        for calendar_id, versions in grouped.items():
            calendars[calendar_id] = CalendarBook(versions)
        return cls(rules, calendars)

    def rule_for_cycle(self, category: str, pinned_revision: int | None,
                       started_at: datetime) -> dict:
        versions = self.rules.get(category)
        if not versions:
            raise DomainError("ROUTE_UNKNOWN", "Category has no responsibility rule", 500,
                             {"category": category})
        if pinned_revision is not None:
            eligible = [row for row in versions if row["revision"] <= pinned_revision]
            if not eligible:
                return min(versions, key=lambda row: row["revision"])
            exact = [row for row in eligible if row["revision"] == pinned_revision]
            return exact[0] if exact else max(eligible, key=lambda row: row["revision"])
        effective = [row for row in versions
                     if parse_time(row["effective_from"]) <= started_at]
        if effective:
            return max(effective,
                       key=lambda row: (parse_time(row["effective_from"]), row["revision"]))
        return max(versions, key=lambda row: row["revision"])

    def calendar(self, calendar_id: str | None) -> CalendarBook:
        book = self.calendars.get(calendar_id or "")
        if book is None:
            raise DomainError("CALENDAR_UNKNOWN", "Commitment calendar does not exist", 500,
                              {"calendar_id": calendar_id})
        return book

    def due(self, rule: dict, started_at: datetime, hours: int, kind: str) -> datetime:
        if kind == "working":
            return add_working_time(self.calendar(rule["calendar_id"]), started_at, hours)
        return started_at + timedelta(hours=hours)

    def between(self, rule: dict, kind: str, start: datetime, end: datetime) -> timedelta:
        if kind == "working":
            return working_time_between(self.calendar(rule["calendar_id"]), start, end)
        return end - start

def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 4)

def responsibility_intervals(base: dict, events: list[dict], as_of: str) -> list[dict]:
    """责任部门区间：受理部门起，每次转派切换，转派不重置时限。"""
    intervals = []
    department, start = base["initial_department"], base["accepted_at"]
    for event in events:
        if event["kind"] != "assigned" or event["occurred_at"] > as_of:
            continue
        intervals.append({"department": department, "from": start,
                          "to": event["occurred_at"]})
        department, start = event["department"], event["occurred_at"]
    intervals.append({"department": department, "from": start, "to": None})
    return intervals

def department_at(intervals: list[dict], instant: str) -> str:
    chosen = intervals[0]["department"]
    for interval in intervals:
        if interval["from"] <= instant:
            chosen = interval["department"]
    return chosen

def _frames(base: dict, events: list[dict], as_of: datetime) -> list[dict]:
    """从事件流切分服务周期：受理开启第 1 周期，结案结束，重开开启新周期。"""
    frames = []
    current = {"cycle": 1, "started_at": base["accepted_at"],
               "first_response_at": None, "closed_at": None}
    for event in events:
        occurred = parse_time(event["occurred_at"])
        if occurred > as_of:
            continue
        kind = event["kind"]
        if kind in ("responded", "explained"):
            if current is not None and current["first_response_at"] is None:
                current["first_response_at"] = event["occurred_at"]
        elif kind == "closed":
            if current is not None:
                current["closed_at"] = event["occurred_at"]
                frames.append(current)
                current = None
        elif kind == "reopened":
            number = frames[-1]["cycle"] + 1 if frames else 1
            current = {"cycle": number, "started_at": event["occurred_at"],
                       "first_response_at": None, "closed_at": None}
    if current is not None:
        frames.append(current)
    return frames

def _commitment(rule: dict, kind_field: str, hours_field: str, book: PolicyBook,
                started: datetime) -> dict:
    kind = rule[kind_field]
    due = book.due(rule, started, rule[hours_field], kind)
    return {"kind": kind, "budget_hours": rule[hours_field], "due": due}

def _quota(book: PolicyBook, rule: dict, commitment: dict, started: datetime,
           moment: datetime) -> dict:
    due = commitment["due"]
    consumed = book.between(rule, commitment["kind"], started, min(moment, due))
    if moment <= due:
        remaining = book.between(rule, commitment["kind"], moment, due)
    else:
        remaining = -book.between(rule, commitment["kind"], due, moment)
    return {"consumed_hours": _hours(consumed), "remaining_hours": _hours(remaining)}

def evaluate_case(base: dict, events: list[dict], as_of: str,
                  book: PolicyBook) -> dict:
    """评估案件全部服务周期：时限、剩余额度、责任部门与逾期原因。"""
    moment = parse_time(as_of)
    frames = _frames(base, events, moment)
    intervals = responsibility_intervals(base, events, as_of)
    cycles = []
    for frame in frames:
        started = parse_time(frame["started_at"])
        pinned = base["route_revision"] if frame["cycle"] == 1 else None
        rule = book.rule_for_cycle(base["category"], pinned, started)
        response = _commitment(rule, "response_kind", "response_hours", book, started)
        resolution = _commitment(rule, "resolution_kind", "resolution_hours", book, started)
        first_response = frame["first_response_at"]
        closed_at = frame["closed_at"]
        response_moment = parse_time(first_response) if first_response else moment
        resolution_moment = parse_time(closed_at) if closed_at else moment
        breaches = []
        response_late = first_response is not None and first_response > utc(response["due"])
        if first_response is None and moment > response["due"]:
            breaches.append({"commitment": "response", "code": "RESPONSE_DEADLINE_PASSED",
                             "due": utc(response["due"]), "at": utc(moment),
                             "overdue_hours": _hours(book.between(
                                 rule, response["kind"], response["due"], moment)),
                             "department": department_at(intervals, utc(response["due"]))})
        elif response_late:
            breaches.append({"commitment": "response", "code": "RESPONDED_LATE",
                             "due": utc(response["due"]), "at": first_response,
                             "overdue_hours": _hours(book.between(
                                 rule, response["kind"], response["due"], response_moment)),
                             "department": department_at(intervals, utc(response["due"]))})
        resolution_late = closed_at is not None and closed_at > utc(resolution["due"])
        if closed_at is None and moment > resolution["due"]:
            breaches.append({"commitment": "resolution", "code": "RESOLUTION_DEADLINE_PASSED",
                             "due": utc(resolution["due"]), "at": utc(moment),
                             "overdue_hours": _hours(book.between(
                                 rule, resolution["kind"], resolution["due"], moment)),
                             "department": department_at(intervals, utc(resolution["due"]))})
        elif resolution_late:
            breaches.append({"commitment": "resolution", "code": "CLOSED_LATE",
                             "due": utc(resolution["due"]), "at": closed_at,
                             "overdue_hours": _hours(book.between(
                                 rule, resolution["kind"], resolution["due"],
                                 resolution_moment)),
                             "department": department_at(intervals, utc(resolution["due"]))})
        cycles.append({
            "cycle": frame["cycle"], "started_at": frame["started_at"],
            "closed_at": closed_at, "route_revision": rule["revision"],
            "policy": {"category": base["category"],
                       "response_kind": response["kind"],
                       "response_hours": response["budget_hours"],
                       "resolution_kind": resolution["kind"],
                       "resolution_hours": resolution["budget_hours"],
                       "calendar_id": rule["calendar_id"]},
            "response": {**{key: (utc(value) if key == "due" else value)
                            for key, value in response.items()},
                         "first_response_at": first_response,
                         "status": ("late" if response_late else
                                    "on_time" if first_response else
                                    "pending_overdue" if moment > response["due"] else "pending"),
                         **_quota(book, rule, response, started, response_moment)},
            "resolution": {**{key: (utc(value) if key == "due" else value)
                              for key, value in resolution.items()},
                           "closed_at": closed_at,
                           "status": ("late" if resolution_late else
                                      "on_time" if closed_at else
                                      "pending_overdue" if moment > resolution["due"]
                                      else "pending"),
                           **_quota(book, rule, resolution, started, resolution_moment)},
            "breaches": breaches,
            "response_late": response_late or (first_response is None
                                               and moment > response["due"]),
            "resolution_late": resolution_late or (closed_at is None
                                                   and moment > resolution["due"]),
        })
    current = cycles[-1]
    flags = {
        "response_on_time": current["response"]["status"] == "on_time",
        "response_overdue": current["response_late"],
        "resolution_overdue": current["resolution_late"],
        "cycles": len(cycles),
        "cycles_response_late": sum(1 for item in cycles if item["response_late"]),
        "cycles_resolution_late": sum(1 for item in cycles if item["resolution_late"]),
    }
    return {"cycles": cycles, "current": current, "flags": flags,
            "responsibility": intervals}

def cycle_dues(base: dict, events: list[dict], book: PolicyBook) -> dict:
    """当前（最新）周期的时限，用于在日历补录或重开后刷新存储值。"""
    frames = _frames(base, events, datetime.max.replace(tzinfo=timezone.utc))
    frame = frames[-1]
    started = parse_time(frame["started_at"])
    pinned = base["route_revision"] if frame["cycle"] == 1 else None
    rule = book.rule_for_cycle(base["category"], pinned, started)
    return {"cycle": frame["cycle"], "started_at": frame["started_at"],
            "response_due": utc(book.due(rule, started, rule["response_hours"],
                                         rule["response_kind"])),
            "resolution_due": utc(book.due(rule, started, rule["resolution_hours"],
                                           rule["resolution_kind"]))}

def refresh_case_cycles(db, book: PolicyBook, *, category: str | None = None,
                        calendar_id: str | None = None) -> list[int]:
    """日历补录或政策调整后，重算受影响案件的当前周期时限并回写存储值。"""
    updated = []
    clauses, params = [], []
    if category is not None:
        clauses.append("category=?")
        params.append(category)
    sql = "SELECT * FROM cases"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = [dict(item) for item in db.execute(sql + " ORDER BY case_id", params)]
    for base in rows:
        events = Store.events(db, base["case_id"])
        dues = cycle_dues(base, events, book)
        if calendar_id is not None:
            pinned = base["route_revision"] if dues["cycle"] == 1 else None
            rule = book.rule_for_cycle(base["category"], pinned,
                                       parse_time(dues["started_at"]))
            if rule["calendar_id"] != calendar_id:
                continue
        if (dues["response_due"] != base["response_due"]
                or dues["resolution_due"] != base["resolution_due"]
                or dues["cycle"] != base["cycle"]):
            db.execute("UPDATE cases SET response_due=?,resolution_due=?,cycle=?,"
                       "cycle_started_at=? WHERE case_id=?",
                       (dues["response_due"], dues["resolution_due"], dues["cycle"],
                        dues["started_at"], base["case_id"]))
            updated.append(base["case_id"])
    return updated

class ClockService:
    """分析员视角：从案件有效工作区间查剩余额度、责任部门与逾期原因。"""
    def __init__(self, store: Store):
        self.store = store

    def case_clock(self, case_id: int, as_of: datetime | None = None) -> dict:
        moment = as_of or datetime.now(timezone.utc)
        with self.store.transaction() as db:
            base = self.store.case(db, case_id)
            stamp = utc(moment)
            if base["accepted_at"] > stamp:
                raise DomainError("CASE_NOT_YET_ACCEPTED",
                                  "Case was not accepted at time", 404)
            watermark = self.store.watermark(db)
            events = self.store.events(db, case_id, watermark)
            book = PolicyBook.load(db, watermark)
            evaluation = evaluate_case(base, events, stamp, book)
            return {"case_id": case_id, "as_of": stamp, "watermark": watermark,
                    "category": base["category"], "status": base["status"],
                    "current_cycle": evaluation["current"]["cycle"],
                    "stored": {"response_due": base["response_due"],
                               "resolution_due": base["resolution_due"]},
                    "cycles": evaluation["cycles"],
                    "responsibility": evaluation["responsibility"],
                    "flags": evaluation["flags"]}

    def case_intervals(self, case_id: int, *, commitment: str = "response",
                       cycle: int | None = None, limit: int = 200,
                       as_of: datetime | None = None) -> dict:
        if commitment not in {"response", "resolution"}:
            raise DomainError("BAD_COMMITMENT", "Commitment must be response or resolution")
        if not 1 <= limit <= 1000:
            raise DomainError("BAD_PAGE", "Invalid interval limit")
        moment = as_of or datetime.now(timezone.utc)
        with self.store.transaction() as db:
            base = self.store.case(db, case_id)
            stamp = utc(moment)
            watermark = self.store.watermark(db)
            events = self.store.events(db, case_id, watermark)
            book = PolicyBook.load(db, watermark)
            evaluation = evaluate_case(base, events, stamp, book)
            target = evaluation["current"] if cycle is None else next(
                (item for item in evaluation["cycles"] if item["cycle"] == cycle), None)
            if target is None:
                raise DomainError("CYCLE_UNKNOWN", "Case has no such service cycle", 404,
                                  {"cycle": cycle})
            detail = target[commitment]
            start = parse_time(target["started_at"])
            due = parse_time(detail["due"])
            if detail["kind"] == "working":
                rule = book.rule_for_cycle(
                    base["category"],
                    base["route_revision"] if target["cycle"] == 1 else None, start)
                calendar = book.calendar(rule["calendar_id"])
                spans = self._versioned_windows(calendar, start, due, limit)
            else:
                spans = [[utc(start), utc(due)]]
            truncated = len(spans) > limit
            return {"case_id": case_id, "as_of": stamp, "cycle": target["cycle"],
                    "commitment": commitment, "kind": detail["kind"],
                    "from": utc(start), "to": utc(due),
                    "budget_hours": detail["budget_hours"],
                    "remaining_hours": detail["remaining_hours"],
                    "intervals": spans[:limit], "truncated": truncated,
                    "watermark": watermark}

    @staticmethod
    def _versioned_windows(calendar: CalendarBook, start: datetime,
                           due: datetime, limit: int) -> list:
        spans = []
        position = start
        while position < due and len(spans) <= limit:
            boundary = calendar.next_boundary(position)
            chunk_end = min(boundary, due) if boundary else due
            for lo, hi in windows(calendar.at(position), position, chunk_end):
                spans.append([utc(lo), utc(hi)])
                if len(spans) > limit:
                    return spans
            position = chunk_end
        return spans
