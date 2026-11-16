"""使用本地合成数据运行离线验收。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from .api import create_app

def required(response, expected: int = 200) -> dict:
    if response.status_code != expected:
        raise RuntimeError(f"Expected HTTP {expected}, got {response.status_code}: {response.text}")
    return response.json()

def run() -> dict:
    with tempfile.TemporaryDirectory() as folder:
        client = TestClient(create_app(Path(folder) / "demo.sqlite3"))
        required(client.put("/grids/g-1?actor=analyst",
                            json={"grid_id": "g-1", "district": "north", "active": True}))
        required(client.put("/grids/g-2?actor=analyst",
                            json={"grid_id": "g-2", "district": "south", "active": True}))
        required(client.put("/routes/lighting?actor=analyst", json={
            "category": "lighting", "department": "public_works", "priority": 5,
            "response_hours": 24, "resolution_hours": 168}))
        required(client.put("/routes/fitness?actor=analyst", json={
            "category": "fitness", "department": "parks", "priority": 6,
            "response_hours": 48, "resolution_hours": 240}))
        batch = {"batch_key": "demo-001", "submitted_at": "2026-01-02T12:00:00Z", "cases": [
            {"external_id": "n-1", "grid_id": "g-1", "category": "lighting",
             "summary": "Street lamp has failed", "occurred_at": "2026-01-01T08:00:00Z",
             "resident_token": "synthetic-person-1", "priority": 1},
            {"external_id": "s-1", "grid_id": "g-2", "category": "fitness",
             "summary": "Outdoor exercise equipment is damaged",
             "occurred_at": "2026-01-01T09:00:00Z",
             "resident_token": "synthetic-person-2", "priority": 2},
        ]}
        imported = required(client.post("/batches", json=batch))
        if imported["imported_count"] != 2:
            raise RuntimeError("Synthetic batch count changed")
        again = required(client.post("/batches", json=batch))
        if not again["replayed"]:
            raise RuntimeError("Batch replay was not idempotent")
        items = required(client.get("/cases"))["items"]
        first = items[0]["case_id"]
        event = {"event_key": "answer-1", "kind": "responded",
                 "occurred_at": "2026-01-02T08:00:00Z", "actor": "public_works",
                 "expected_version": 1, "note": "Repair team scheduled"}
        required(client.post(f"/cases/{first}/events", json=event))
        required(client.post(f"/cases/{first}/events", json={
            "event_key": "close-1", "kind": "closed", "actor": "public_works",
            "occurred_at": "2026-01-03T08:00:00Z", "expected_version": 2,
            "resolution": "solved"}))
        before = required(client.post("/reports", json={
            "report_key": "before", "as_of": "2026-01-02T07:00:00Z",
            "group_by": "district"}))
        after = required(client.post("/reports", json={
            "report_key": "after", "as_of": "2026-01-04T00:00:00Z",
            "group_by": "district"}))
        if before["body"]["totals"]["closed"] != 0:
            raise RuntimeError("Historic snapshot contains future closure")
        if after["body"]["totals"]["closed"] != 1:
            raise RuntimeError("Closure missing from later snapshot")
        quality = required(client.get("/quality/inspect", params={
            "as_of": "2026-01-04T00:00:00Z"}))
        if quality["issue_count"]:
            raise RuntimeError("Synthetic data failed reconciliation")
        check = required(client.get("/reports/after/verify"))
        if not check["matches"]:
            raise RuntimeError("Frozen report verification failed")
        clocked = run_service_clock(client)
        return {"accepted": imported["imported_count"],
                "closed_as_of_later_report": after["body"]["totals"]["closed"],
                "historic_closed": before["body"]["totals"]["closed"],
                "quality_issues": quality["issue_count"], "report_verified": check["matches"],
                **clocked}

def run_service_clock(client) -> dict:
    """带生效时间的工作日历：跨周末期限、补录重判与归档报表还原。"""
    weekdays = {day: [["09:00", "17:00"]] for day in
                ("mon", "tue", "wed", "thu", "fri")}
    required(client.put("/calendars/city?actor=analyst", json={
        "calendar_id": "city", "effective_from": "2026-01-01T00:00:00Z",
        "utc_offset": "+00:00", "workweek": weekdays, "holidays": []}))
    required(client.put("/routes/traffic?actor=analyst", json={
        "category": "traffic", "department": "roads", "priority": 4,
        "response_hours": 8, "resolution_hours": 16,
        "response_kind": "working", "resolution_kind": "working",
        "calendar_id": "city"}))
    # 2026-01-02 是周五 15:00：周五 2h + 周一 6h → 期限跳过周末
    required(client.post("/batches", json={
        "batch_key": "demo-clock", "submitted_at": "2026-01-03T00:00:00Z",
        "cases": [{"external_id": "t-1", "grid_id": "g-1", "category": "traffic",
                   "summary": "Signal timing is unsafe", "priority": 2,
                   "occurred_at": "2026-01-02T15:00:00Z",
                   "resident_token": "synthetic-person-3"}]}))
    clocked_id = [item["case_id"] for item in required(client.get("/cases"))["items"]
                  if item["external_id"] == "t-1"][0]
    view = required(client.get(f"/cases/{clocked_id}/clock",
                               params={"as_of": "2026-01-03T00:00:00Z"}))
    due = view["cycles"][0]["response"]["due"]
    if due != "2026-01-05T15:00:00+00:00":
        raise RuntimeError("Working clock did not skip the weekend: " + due)
    frozen = required(client.post("/reports", json={
        "report_key": "clock-frozen", "as_of": "2026-01-05T16:00:00Z",
        "group_by": "district", "category": "traffic"}))
    if frozen["body"]["totals"]["response_overdue"] != 1:
        raise RuntimeError("Frozen report should mark the missed working deadline")
    # 日历补录：周一 2026-01-05 为节假日，生效时间早于受理时刻
    backfill = required(client.put("/calendars/city?actor=analyst", json={
        "calendar_id": "city", "effective_from": "2026-01-01T00:00:00Z",
        "utc_offset": "+00:00", "workweek": weekdays,
        "holidays": ["2026-01-05"]}))
    if backfill["reevaluated_cases"] != [clocked_id]:
        raise RuntimeError("Calendar backfill did not re-evaluate the open cycle")
    queue = required(client.get("/operations/queue",
                                params={"as_of": "2026-01-05T16:00:00Z"}))
    item = [row for row in queue["items"] if row["case_id"] == clocked_id][0]
    if item["response_due"] != "2026-01-06T15:00:00+00:00" or item["response_overdue"]:
        raise RuntimeError("Queue did not adopt the re-evaluated service clock")
    live = required(client.get("/metrics", params={"as_of": "2026-01-05T16:00:00Z",
                                                   "include_cases": True}))
    matched = [row for row in live["cases"] if row["case_id"] == clocked_id][0]
    if matched["flags"]["response_overdue"] != item["response_overdue"]:
        raise RuntimeError("Queue and statistics disagree after calendar backfill")
    if not required(client.get("/reports/clock-frozen/verify"))["matches"]:
        raise RuntimeError("Archived report failed to restore the original policy")
    quality = required(client.get("/quality/inspect",
                                  params={"as_of": "2026-01-06T00:00:00Z"}))
    if quality["issue_count"]:
        raise RuntimeError("Service clock drift detected after backfill")
    return {"clock_cycle": view["current_cycle"],
            "clock_due_before_backfill": due,
            "clock_due_after_backfill": item["response_due"],
            "archived_report_verified": True}

if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
