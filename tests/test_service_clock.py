import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from civic_flow.api import create_app

WEEKDAYS = {"mon": [["09:00", "17:00"]], "tue": [["09:00", "17:00"]],
            "wed": [["09:00", "17:00"]], "thu": [["09:00", "17:00"]],
            "fri": [["09:00", "17:00"]]}

class ServiceClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
        self.client.put("/grids/g?actor=admin", json={"grid_id": "g", "district": "D"})
        self.client.put("/calendars/city?actor=admin", json={
            "calendar_id": "city", "effective_from": "2026-01-01T00:00:00Z",
            "utc_offset": "+00:00", "workweek": WEEKDAYS, "holidays": []})
        self.client.put("/routes/repair?actor=admin", json={
            "category": "repair", "department": "works",
            "response_hours": 8, "resolution_hours": 16,
            "response_kind": "working", "resolution_kind": "working",
            "calendar_id": "city"})
        self.client.put("/routes/light?actor=admin", json={
            "category": "light", "department": "works",
            "response_hours": 24, "resolution_hours": 72})

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def add_case(self, external_id="c1", category="repair", when="2026-06-02T16:00:00Z",
                 batch="b1"):
        response = self.client.post("/batches", json={
            "batch_key": batch, "submitted_at": "2026-06-20T00:00:00Z",
            "cases": [{"external_id": external_id, "grid_id": "g", "category": category,
                       "summary": "Synthetic clocked request", "resident_token": "person-1",
                       "occurred_at": when}]})
        self.assertEqual(response.status_code, 200, response.text)
        items = self.client.get("/cases").json()["items"]
        return [item["case_id"] for item in items if item["external_id"] == external_id][0]

    def event(self, case_id, key, kind, version, at, **extra):
        return self.client.post(f"/cases/{case_id}/events", json={
            "event_key": key, "kind": kind, "expected_version": version,
            "actor": "worker", "occurred_at": at, **extra})

    def clock(self, case_id, as_of=None):
        params = {} if as_of is None else {"as_of": as_of}
        response = self.client.get(f"/cases/{case_id}/clock", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_working_clock_computes_business_hours(self):
        # 2026-06-02 是周二 16:00，预算 8 工作小时：周二 1h + 周三 7h
        case_id = self.add_case()
        case = self.client.get(f"/cases/{case_id}").json()
        self.assertEqual(case["response_due"], "2026-06-03T16:00:00+00:00")
        self.assertEqual(case["resolution_due"], "2026-06-04T16:00:00+00:00")
        self.assertEqual(case["cycle"], 1)

    def test_calendar_utc_offset_is_honored(self):
        self.client.put("/calendars/shift?actor=admin", json={
            "calendar_id": "shift", "effective_from": "2026-01-01T00:00:00Z",
            "utc_offset": "+08:00", "workweek": WEEKDAYS, "holidays": []})
        self.client.put("/routes/shifted?actor=admin", json={
            "category": "shifted", "department": "works",
            "response_hours": 8, "resolution_hours": 16,
            "response_kind": "working", "resolution_kind": "working",
            "calendar_id": "shift"})
        case_id = self.add_case("shifted-case", "shifted", "2026-06-01T00:00:00Z", "b-shift")
        case = self.client.get(f"/cases/{case_id}").json()
        # 当地 09:00–17:00 即 UTC 01:00–09:00，8 工作小时在当天内完成
        self.assertEqual(case["response_due"], "2026-06-01T09:00:00+00:00")

    def test_natural_and_working_kinds_share_one_analysis(self):
        self.add_case("natural", "light", "2026-06-01T00:00:00Z", "b2")
        self.add_case("working", "repair", "2026-06-01T09:00:00Z", "b3")
        snapshot = self.client.get("/metrics", params={
            "as_of": "2026-06-10T00:00:00Z", "include_cases": True}).json()
        self.assertEqual(snapshot["totals"]["response_overdue"], 2)
        self.assertTrue(all(item["flags"]["response_overdue"]
                            for item in snapshot["cases"]))

    def test_calendar_backfill_reevaluates_affected_cycles(self):
        case_id = self.add_case()
        before = self.client.get(f"/cases/{case_id}").json()
        self.assertEqual(before["response_due"], "2026-06-03T16:00:00+00:00")
        result = self.client.put("/calendars/city?actor=admin", json={
            "calendar_id": "city", "effective_from": "2026-06-01T00:00:00Z",
            "utc_offset": "+00:00", "workweek": WEEKDAYS,
            "holidays": ["2026-06-03"]}).json()
        self.assertEqual(result["calendar"]["revision"], 2)
        self.assertEqual(result["reevaluated_cases"], [case_id])
        after = self.client.get(f"/cases/{case_id}").json()
        # 周三补录为节假日：周二 1h + 周四 7h，期限顺延到周四 16:00
        self.assertEqual(after["response_due"], "2026-06-04T16:00:00+00:00")
        self.assertEqual(after["resolution_due"], "2026-06-05T16:00:00+00:00")
        audit = self.client.get("/audit").json()["items"]
        self.assertTrue(any(row["operation"] == "cycles_reevaluated" for row in audit))
        quality = self.client.get("/quality/inspect", params={
            "as_of": "2026-06-10T00:00:00Z"}).json()
        self.assertEqual(quality["issue_count"], 0)

    def test_archived_report_restores_original_policy_after_backfill(self):
        self.add_case()
        # 报告时点周三 17:00：原日历下回应期限周三 16:00 已过 → 逾期
        report = self.client.post("/reports", json={
            "report_key": "frozen", "as_of": "2026-06-03T17:00:00Z",
            "group_by": "district"}).json()
        self.assertEqual(report["body"]["totals"]["response_overdue"], 1)
        self.client.put("/calendars/city?actor=admin", json={
            "calendar_id": "city", "effective_from": "2026-06-01T00:00:00Z",
            "utc_offset": "+00:00", "workweek": WEEKDAYS, "holidays": ["2026-06-03"]})
        # 归档报表仍还原原政策口径
        check = self.client.get("/reports/frozen/verify").json()
        self.assertTrue(check["matches"])
        stored = self.client.get("/reports/frozen").json()
        self.assertEqual(stored["body"]["totals"]["response_overdue"], 1)
        # 当前待办与逾期统计采用同一（重判后的）结果
        live = self.client.get("/metrics", params={
            "as_of": "2026-06-03T17:00:00Z"}).json()
        self.assertEqual(live["totals"]["response_overdue"], 0)
        queue = self.client.get("/operations/queue", params={
            "as_of": "2026-06-03T17:00:00Z"}).json()
        self.assertEqual(queue["items"][0]["response_overdue"], False)
        self.assertEqual(queue["items"][0]["response_due"], "2026-06-04T16:00:00+00:00")
        fresh = self.client.post("/reports", json={
            "report_key": "fresh", "as_of": "2026-06-03T17:00:00Z",
            "group_by": "district"}).json()
        self.assertEqual(fresh["body"]["totals"]["response_overdue"], 0)

    def test_reassignment_keeps_clock_and_attributes_department(self):
        case_id = self.add_case(category="light", when="2026-03-01T00:00:00Z")
        self.event(case_id, "a", "assigned", 1, "2026-03-01T12:00:00Z",
                   department="parks")
        case = self.client.get(f"/cases/{case_id}").json()
        self.assertEqual(case["response_due"], "2026-03-02T00:00:00+00:00")
        view = self.clock(case_id, "2026-03-02T06:00:00Z")
        self.assertEqual(view["responsibility"], [
            {"department": "works", "from": "2026-03-01T00:00:00+00:00",
             "to": "2026-03-01T12:00:00+00:00"},
            {"department": "parks", "from": "2026-03-01T12:00:00+00:00", "to": None}])
        breach = view["cycles"][0]["breaches"][0]
        self.assertEqual(breach["code"], "RESPONSE_DEADLINE_PASSED")
        self.assertEqual(breach["department"], "parks")
        self.assertEqual(breach["overdue_hours"], 6.0)

    def test_reopen_starts_distinguishable_cycle_and_keeps_history(self):
        case_id = self.add_case(category="light", when="2026-03-01T00:00:00Z")
        self.event(case_id, "r", "responded", 1, "2026-03-02T01:00:00Z", note="Late answer")
        self.event(case_id, "c", "closed", 2, "2026-03-02T02:00:00Z",
                   resolution="solved")
        reopened = self.event(case_id, "o", "reopened", 3, "2026-03-03T01:00:00Z")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        case = self.client.get(f"/cases/{case_id}").json()
        self.assertEqual(case["cycle"], 2)
        self.assertEqual(case["cycle_started_at"], "2026-03-03T01:00:00+00:00")
        self.assertEqual(case["response_due"], "2026-03-04T01:00:00+00:00")
        view = self.clock(case_id, "2026-03-04T00:00:00Z")
        self.assertEqual(view["current_cycle"], 2)
        first, second = view["cycles"]
        self.assertEqual(first["response"]["status"], "late")
        self.assertEqual(first["response"]["first_response_at"],
                         "2026-03-02T01:00:00+00:00")
        self.assertEqual(first["breaches"][0]["code"], "RESPONDED_LATE")
        self.assertEqual(first["breaches"][0]["overdue_hours"], 1.0)
        self.assertEqual(second["response"]["status"], "pending")
        snapshot = self.client.get("/metrics", params={
            "as_of": "2026-03-04T00:00:00Z"}).json()["totals"]
        self.assertEqual(snapshot["reopened"], 1)
        self.assertEqual(snapshot["cycles_total"], 2)
        self.assertEqual(snapshot["cycles_response_late"], 1)
        self.assertEqual(snapshot["response_overdue"], 0)
        self.assertEqual(snapshot["closed"], 0)

    def test_reopen_uses_rule_version_effective_at_reopen(self):
        case_id = self.add_case(category="light", when="2026-03-01T00:00:00Z")
        self.event(case_id, "r", "responded", 1, "2026-03-01T06:00:00Z", note="Answer")
        self.event(case_id, "c", "closed", 2, "2026-03-01T08:00:00Z",
                   resolution="solved")
        self.client.put("/routes/light?actor=admin", json={
            "category": "light", "department": "works",
            "response_hours": 100, "resolution_hours": 200,
            "effective_from": "2026-03-03T01:00:00Z"})
        self.event(case_id, "o", "reopened", 3, "2026-03-03T01:00:00Z")
        view = self.clock(case_id, "2026-03-05T00:00:00Z")
        second = view["cycles"][1]
        self.assertEqual(second["route_revision"], 2)
        self.assertEqual(second["response"]["due"], "2026-03-07T05:00:00+00:00")
        # 政策调整恰逢重开：队列与统计同一结果，都不逾期
        queue = self.client.get("/operations/queue", params={
            "as_of": "2026-03-05T00:00:00Z"}).json()["items"]
        metrics = self.client.get("/metrics", params={
            "as_of": "2026-03-05T00:00:00Z", "include_cases": True}).json()["cases"]
        self.assertEqual(queue[0]["response_overdue"], False)
        self.assertEqual(queue[0]["response_overdue"],
                         metrics[0]["flags"]["response_overdue"])
        self.assertEqual(queue[0]["response_due"], metrics[0]["response_due"])

    def test_calendar_version_effective_exactly_at_response_is_consistent(self):
        case_id = self.add_case()
        # 回应时刻恰好是日历新版本生效时刻：周三 12:00 起周三变成半天工作日
        self.client.put("/calendars/city?actor=admin", json={
            "calendar_id": "city", "effective_from": "2026-06-03T12:00:00Z",
            "utc_offset": "+00:00",
            "workweek": {**WEEKDAYS, "wed": [["09:00", "12:00"]]}, "holidays": []})
        self.event(case_id, "r", "responded", 1, "2026-06-03T12:00:00Z",
                   note="Boundary answer")
        for as_of in ("2026-06-03T11:00:00Z", "2026-06-03T13:00:00Z",
                      "2026-06-04T00:00:00Z"):
            queue = self.client.get("/operations/queue",
                                    params={"as_of": as_of}).json()["items"]
            metrics = self.client.get("/metrics", params={
                "as_of": as_of, "include_cases": True}).json()["cases"]
            self.assertEqual(queue[0]["response_overdue"],
                             metrics[0]["flags"]["response_overdue"], as_of)
            self.assertEqual(queue[0]["response_due"], metrics[0]["response_due"], as_of)

    def test_clock_endpoint_exposes_quota_department_and_reason(self):
        # 周一 09:00 受理，8 工作小时预算当天 17:00 到期
        case_id = self.add_case(when="2026-06-01T09:00:00Z")
        view = self.clock(case_id, "2026-06-01T12:00:00Z")
        response = view["cycles"][0]["response"]
        self.assertEqual(response["consumed_hours"], 3.0)
        self.assertEqual(response["remaining_hours"], 5.0)
        self.assertEqual(response["due"], "2026-06-01T17:00:00+00:00")
        self.assertEqual(view["responsibility"][0]["department"], "works")
        overdue = self.clock(case_id, "2026-06-03T00:00:00Z")
        breach = overdue["cycles"][0]["breaches"][0]
        self.assertEqual(breach["commitment"], "response")
        self.assertEqual(breach["department"], "works")
        # 周一 17:00 到周三 00:00 之间隔了一个完整工作日
        self.assertEqual(breach["overdue_hours"], 8.0)

    def test_clock_intervals_expand_working_windows(self):
        case_id = self.add_case(when="2026-06-01T09:00:00Z")
        result = self.client.get(f"/cases/{case_id}/clock/intervals",
                                 params={"commitment": "response"}).json()
        self.assertEqual(result["kind"], "working")
        self.assertEqual(result["intervals"], [
            ["2026-06-01T09:00:00+00:00", "2026-06-01T17:00:00+00:00"]])
        self.assertFalse(result["truncated"])
        resolution = self.client.get(f"/cases/{case_id}/clock/intervals",
                                     params={"commitment": "resolution"}).json()
        self.assertEqual(resolution["intervals"], [
            ["2026-06-01T09:00:00+00:00", "2026-06-01T17:00:00+00:00"],
            ["2026-06-02T09:00:00+00:00", "2026-06-02T17:00:00+00:00"]])
        natural_id = self.add_case("natural", "light", "2026-06-01T00:00:00Z", "b4")
        natural = self.client.get(f"/cases/{natural_id}/clock/intervals").json()
        self.assertEqual(natural["kind"], "natural")
        self.assertEqual(natural["intervals"], [
            ["2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00"]])
        missing = self.client.get(f"/cases/{case_id}/clock/intervals",
                                  params={"cycle": 99})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "CYCLE_UNKNOWN")

    def test_calendar_and_rule_validation(self):
        bad_rule = self.client.put("/routes/bad?actor=admin", json={
            "category": "bad", "department": "works",
            "response_hours": 8, "resolution_hours": 16,
            "response_kind": "working"})
        self.assertEqual(bad_rule.status_code, 422)
        unknown = self.client.put("/routes/unknown?actor=admin", json={
            "category": "unknown", "department": "works",
            "response_hours": 8, "resolution_hours": 16,
            "response_kind": "working", "calendar_id": "ghost"})
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["code"], "CALENDAR_UNKNOWN")
        overlap = self.client.put("/calendars/overlap?actor=admin", json={
            "calendar_id": "overlap", "workweek": {
                "mon": [["09:00", "12:00"], ["11:00", "13:00"]]}})
        self.assertEqual(overlap.json()["code"], "WINDOW_OVERLAP")
        offset = self.client.put("/calendars/far?actor=admin", json={
            "calendar_id": "far", "workweek": {"sat": [["10:00", "10:30"]]},
            "utc_offset": "+99:00"})
        self.assertEqual(offset.json()["code"], "BAD_OFFSET")
        empty_day = self.client.put("/calendars/empty?actor=admin", json={
            "calendar_id": "empty", "workweek": {"mon": []}})
        self.assertEqual(empty_day.status_code, 422)

    def test_reopen_with_working_calendar_computes_new_cycle_dues(self):
        case_id = self.add_case(when="2026-06-01T09:00:00Z")
        self.event(case_id, "r", "responded", 1, "2026-06-01T10:00:00Z", note="Answer")
        self.event(case_id, "c", "closed", 2, "2026-06-01T11:00:00Z",
                   resolution="solved")
        # 周三 10:00 重开：周三 10–17 共 7h，剩余 1h 顺延到周四 09–10
        self.event(case_id, "o", "reopened", 3, "2026-06-03T10:00:00Z")
        case = self.client.get(f"/cases/{case_id}").json()
        self.assertEqual(case["cycle"], 2)
        self.assertEqual(case["response_due"], "2026-06-04T10:00:00+00:00")
        self.assertEqual(case["resolution_due"], "2026-06-05T10:00:00+00:00")
        quality = self.client.get("/quality/inspect", params={
            "as_of": "2026-06-10T00:00:00Z"}).json()
        self.assertEqual(quality["issue_count"], 0)

    def test_multiple_reopens_produce_distinguishable_cycles(self):
        case_id = self.add_case(category="light", when="2026-03-01T00:00:00Z")
        self.event(case_id, "r1", "responded", 1, "2026-03-01T05:00:00Z", note="One")
        self.event(case_id, "c1", "closed", 2, "2026-03-01T06:00:00Z",
                   resolution="solved")
        self.event(case_id, "o1", "reopened", 3, "2026-03-02T00:00:00Z")
        self.event(case_id, "r2", "responded", 4, "2026-03-02T05:00:00Z", note="Two")
        self.event(case_id, "c2", "closed", 5, "2026-03-02T06:00:00Z",
                   resolution="solved")
        self.event(case_id, "o2", "reopened", 6, "2026-03-03T00:00:00Z")
        view = self.clock(case_id, "2026-03-03T12:00:00Z")
        self.assertEqual([item["cycle"] for item in view["cycles"]], [1, 2, 3])
        self.assertEqual(view["current_cycle"], 3)
        self.assertEqual(view["cycles"][2]["response"]["due"],
                         "2026-03-04T00:00:00+00:00")
        snapshot = self.client.get("/metrics", params={
            "as_of": "2026-03-03T12:00:00Z"}).json()["totals"]
        self.assertEqual(snapshot["cycles_total"], 3)
        self.assertEqual(snapshot["cycles_response_late"], 0)
        self.assertEqual(snapshot["response_overdue"], 0)

    def test_calendar_replay_is_idempotent_and_versioned(self):
        payload = {"calendar_id": "city", "effective_from": "2026-01-01T00:00:00Z",
                   "utc_offset": "+00:00", "workweek": WEEKDAYS, "holidays": []}
        replay = self.client.put("/calendars/city?actor=admin", json=payload).json()
        self.assertTrue(replay["replayed"])
        versions = self.client.get("/calendars/city/versions").json()["versions"]
        self.assertEqual(len(versions), 1)
        listed = self.client.get("/calendars").json()["items"]
        self.assertEqual(listed[0]["calendar_id"], "city")
        history = self.client.get("/policies/calendar/city/history").json()["items"]
        self.assertEqual(len(history), 1)
        rule_versions = self.client.get("/routes/repair/versions").json()["items"]
        self.assertEqual(rule_versions[0]["response_kind"], "working")

if __name__ == "__main__":
    unittest.main()
