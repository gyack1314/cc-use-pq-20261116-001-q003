"""工作日历的版本化维护与补录后的周期重判。"""
from __future__ import annotations

import json
from datetime import datetime

from .clock import CalendarSpec, PolicyBook, refresh_case_cycles
from .contracts import CalendarInput, DomainError, utc
from .store import Store

class CalendarService:
    def __init__(self, store: Store):
        self.store = store

    def put(self, data: CalendarInput, actor: str, at: datetime) -> dict:
        if not actor.strip():
            raise DomainError("BAD_ACTOR", "Actor is required")
        spec = CalendarSpec.build(data.model_dump(mode="json"))
        canonical = spec.canonical()
        effective = utc(data.effective_from) if data.effective_from else utc(at)
        with self.store.transaction() as db:
            old = self.store.calendar(db, data.calendar_id)
            if (old and old["effective_from"] == effective
                    and json.loads(old["spec_json"]) == canonical):
                return {"calendar": old, "replayed": True, "reevaluated_cases": []}
            revision = 1 if old is None else old["revision"] + 1
            sequence = self.store.journal(
                db, "calendar_changed", "calendar", data.calendar_id,
                {"actor": actor, "revision": revision, "effective_from": effective,
                 "spec": canonical}, utc(at))
            db.execute("INSERT INTO calendar_versions(calendar_id,revision,effective_from,"
                       "spec_json,sequence) VALUES(?,?,?,?,?)",
                       (data.calendar_id, revision, effective,
                        json.dumps(canonical, ensure_ascii=False, sort_keys=True), sequence))
            db.execute("INSERT INTO calendars(calendar_id,revision,effective_from,spec_json,"
                       "sequence) VALUES(?,?,?,?,?) ON CONFLICT(calendar_id) DO UPDATE SET "
                       "revision=excluded.revision,effective_from=excluded.effective_from,"
                       "spec_json=excluded.spec_json,sequence=excluded.sequence",
                       (data.calendar_id, revision, effective,
                        json.dumps(canonical, ensure_ascii=False, sort_keys=True), sequence))
            current = self.store.calendar(db, data.calendar_id)
            self.store.save_policy_change(db, "calendar", data.calendar_id, revision,
                                          old, current, sequence)
            # 日历补录：重新判断所有使用该日历的未结案件当前周期
            reevaluated = refresh_case_cycles(db, PolicyBook.load(db),
                                              calendar_id=data.calendar_id)
            if reevaluated:
                self.store.journal(db, "cycles_reevaluated", "calendar", data.calendar_id,
                                   {"actor": actor, "revision": revision,
                                    "cases": reevaluated}, utc(at))
            return {"calendar": current, "replayed": False,
                    "reevaluated_cases": reevaluated}

    def list(self) -> dict:
        with self.store.connection() as db:
            rows = [dict(item) for item in db.execute(
                "SELECT * FROM calendars ORDER BY calendar_id")]
            return {"items": [self._unpack(row) for row in rows],
                    "watermark": self.store.watermark(db)}

    def versions(self, calendar_id: str) -> dict:
        with self.store.connection() as db:
            rows = self.store.calendar_versions(db, calendar_id=calendar_id)
            if not rows:
                raise DomainError("CALENDAR_UNKNOWN", "Calendar does not exist", 404,
                                  {"calendar_id": calendar_id})
            return {"calendar_id": calendar_id,
                    "versions": [self._unpack(row) for row in rows]}

    @staticmethod
    def _unpack(row: dict) -> dict:
        return {"calendar_id": row["calendar_id"], "revision": row["revision"],
                "effective_from": row["effective_from"],
                "spec": json.loads(row["spec_json"]), "sequence": row["sequence"]}
