"""按版本维护网格与责任配置。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .clock import PolicyBook, refresh_case_cycles
from .contracts import DomainError, GridInput, RouteRuleInput, utc
from .store import Store

@dataclass(frozen=True)
class Route:
    grid_id: str
    district: str
    category: str
    department: str
    route_revision: int
    response_due: str
    resolution_due: str

class RoutingService:
    def __init__(self, store: Store):
        self.store = store

    def put_grid(self, data: GridInput, actor: str, at: datetime) -> dict:
        if not actor.strip():
            raise DomainError("BAD_ACTOR", "Actor is required")
        with self.store.transaction() as db:
            old = self.store.grid(db, data.grid_id)
            new = data.model_dump()
            if old and all(old[key] == (int(new[key]) if key == "active" else new[key])
                           for key in ("grid_id", "district", "active")):
                return old
            revision = 1 if old is None else old["revision"] + 1
            db.execute("INSERT INTO grids(grid_id,district,active,revision) VALUES(?,?,?,?) "
                       "ON CONFLICT(grid_id) DO UPDATE SET district=excluded.district,"
                       "active=excluded.active,revision=excluded.revision",
                       (data.grid_id, data.district, int(data.active), revision))
            current = self.store.grid(db, data.grid_id)
            sequence = self.store.journal(db, "grid_changed", "grid", data.grid_id,
                                          {"actor": actor, "old": old, "new": current}, utc(at))
            self.store.save_policy_change(db, "grid", data.grid_id, revision, old, current, sequence)
            return current

    def put_rule(self, data: RouteRuleInput, actor: str, at: datetime) -> dict:
        if not actor.strip():
            raise DomainError("BAD_ACTOR", "Actor is required")
        effective = utc(data.effective_from) if data.effective_from else utc(at)
        with self.store.transaction() as db:
            if data.calendar_id is not None and self.store.calendar(db, data.calendar_id) is None:
                raise DomainError("CALENDAR_UNKNOWN", "Commitment calendar does not exist", 422,
                                  {"calendar_id": data.calendar_id})
            old = self.store.rule(db, data.category)
            new = data.model_dump(mode="json")
            comparable = ("category", "department", "priority", "response_hours",
                          "resolution_hours", "response_kind", "resolution_kind", "calendar_id")
            if old and all(old[key] == new[key] for key in comparable):
                if data.effective_from is None or old["effective_from"] == effective:
                    return old
            revision = 1 if old is None else old["revision"] + 1
            db.execute("INSERT INTO route_rules(category,department,priority,response_hours,"
                       "resolution_hours,response_kind,resolution_kind,calendar_id,"
                       "effective_from,revision) VALUES(?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(category) DO UPDATE SET department=excluded.department,"
                       "priority=excluded.priority,response_hours=excluded.response_hours,"
                       "resolution_hours=excluded.resolution_hours,"
                       "response_kind=excluded.response_kind,"
                       "resolution_kind=excluded.resolution_kind,"
                       "calendar_id=excluded.calendar_id,"
                       "effective_from=excluded.effective_from,revision=excluded.revision",
                       (data.category, data.department, data.priority, data.response_hours,
                        data.resolution_hours, data.response_kind.value,
                        data.resolution_kind.value, data.calendar_id, effective, revision))
            current = self.store.rule(db, data.category)
            sequence = self.store.journal(db, "route_changed", "category", data.category,
                                          {"actor": actor, "old": old, "new": current}, utc(at))
            db.execute("INSERT INTO route_rule_versions(category,revision,department,priority,"
                       "response_hours,resolution_hours,response_kind,resolution_kind,"
                       "calendar_id,effective_from,sequence) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (data.category, revision, data.department, data.priority,
                        data.response_hours, data.resolution_hours, data.response_kind.value,
                        data.resolution_kind.value, data.calendar_id, effective, sequence))
            self.store.save_policy_change(db, "route", data.category, revision, old, current, sequence)
            reevaluated = refresh_case_cycles(db, PolicyBook.load(db), category=data.category)
            if reevaluated:
                self.store.journal(db, "cycles_reevaluated", "category", data.category,
                                   {"actor": actor, "revision": revision,
                                    "cases": reevaluated}, utc(at))
            current["reevaluated_cases"] = reevaluated
            return current

    @staticmethod
    def resolve(db: sqlite3.Connection, grid_id: str, category: str,
                accepted_at: datetime, book: PolicyBook | None = None) -> Route:
        grid = Store.grid(db, grid_id)
        if grid is None:
            raise DomainError("GRID_UNKNOWN", "Grid has no registered boundary", 422,
                              {"grid_id": grid_id})
        if not grid["active"]:
            raise DomainError("GRID_INACTIVE", "Grid is inactive", 422, {"grid_id": grid_id})
        rule = Store.rule(db, category)
        if rule is None:
            raise DomainError("ROUTE_UNKNOWN", "Category has no responsibility rule", 422,
                              {"category": category})
        if book is None:
            book = PolicyBook.load(db)
        started = accepted_at.astimezone(timezone.utc)
        response_due = book.due(rule, started, rule["response_hours"], rule["response_kind"])
        resolution_due = book.due(rule, started, rule["resolution_hours"],
                                  rule["resolution_kind"])
        return Route(grid_id, grid["district"], category, rule["department"],
                     rule["revision"], utc(response_due), utc(resolution_due))

    def list_grids(self) -> list[dict]:
        with self.store.connection() as db:
            return [dict(item) for item in db.execute("SELECT * FROM grids ORDER BY grid_id")]

    def list_rules(self) -> list[dict]:
        with self.store.connection() as db:
            return [dict(item) for item in db.execute("SELECT * FROM route_rules ORDER BY category")]

    def rule_versions(self, category: str) -> list[dict]:
        with self.store.connection() as db:
            return [row for row in self.store.rule_versions(db)
                    if row["category"] == category]

    def history(self, kind: str, key: str) -> list[dict]:
        if kind not in {"grid", "route", "calendar"}:
            raise DomainError("BAD_POLICY", "Unknown policy kind")
        with self.store.connection() as db:
            return self.store.policy_history(db, kind, key)
