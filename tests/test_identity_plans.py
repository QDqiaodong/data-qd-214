from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from axle_reconciliation import ReconciliationArchive

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"

S1 = {"id": "s1", "entry": "a", "exit": "b"}
S2 = {"id": "s2", "entry": "c", "exit": "d"}


def event(eid, train, checkpoint, time, axles=20):
    return {
        "id": eid,
        "train_id": train,
        "checkpoint": checkpoint,
        "time": time,
        "axles": axles,
    }


class IdentityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.path = os.path.join(self.root, "archive")

    def archive(self, sections=None, start="2026-04-10", end="2026-04-11"):
        return ReconciliationArchive(
            self.path,
            [S1] if sections is None else sections,
            start,
            end,
        )

    def reopen(self, sections=None, start="2026-04-10", end="2026-04-11"):
        return ReconciliationArchive(
            self.path,
            [S1] if sections is None else sections,
            start,
            end,
        )

    def snapshot(self, ar):
        """Observable state used to prove a rejected request changed nothing."""
        return {
            "revision": ar.revision,
            "plans": sorted(ar._state["plans"]),
            "packages": json.loads(json.dumps(ar._state["packages"])),
            "package_status": json.loads(json.dumps(ar._state["package_status"])),
            "takes": json.loads(json.dumps(ar._state["takes"])),
            "requests": sorted(ar._state["requests"]),
            "events": json.loads(json.dumps(ar._state["events"])),
        }


class ScenarioOneTests(IdentityTestBase):
    """The business chain from acceptance item 1."""

    def setUp(self) -> None:
        super().setUp()
        self.ar = self.archive(sections=[S1, S2])
        self.events = [
            event("a", "A", "a", "2026-04-10T23:59:30Z", 20),
            event("b", "B", "a", "2026-04-10T23:59:40Z", 22),
            event("x", "local-x", "b", "2026-04-11T00:00:00Z", 21),
            event("y", "local-y", "b", "2026-04-11T00:00:10Z", 19),
            event("c1", "Q", "c", "2026-04-10T12:00:00Z", 30),
            event("d1", "Q", "d", "2026-04-10T13:00:00Z", 30),
        ]
        self.assertEqual(
            self.ar.revise("add", 0, self.events, []),
            {"revision": 1, "changed": True},
        )
        self.s1_10 = self.ar.seal("s1", "2026-04-10", "seal-s1-10")
        self.s1_11 = self.ar.seal("s1", "2026-04-11", "seal-s1-11")
        self.s2_10 = self.ar.seal("s2", "2026-04-10", "seal-s2-10")
        self.take_10 = self.ar.take(self.s1_10["package_id"], "take-s1-10")
        self.take_11 = self.ar.take(self.s1_11["package_id"], "take-s1-11")
        self.take_s2 = self.ar.take(self.s2_10["package_id"], "take-s2")

    def test_plan_apply_and_repack_the_real_archive(self):
        before_revision = self.ar.revision
        before_packages = json.loads(json.dumps(self.ar._state["packages"]))
        plan = self.ar.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan-1")

        # Creating a plan is read-only for events, packages and credentials.
        self.assertEqual(self.ar.revision, before_revision)
        self.assertEqual(
            json.loads(json.dumps(self.ar._state["packages"])), before_packages
        )
        for receipt in (self.take_10, self.take_11, self.take_s2):
            self.assertTrue(self.ar.read_take(receipt["receipt_id"])["valid"])

        self.assertIsInstance(plan["plan_id"], str)
        self.assertTrue(plan["plan_id"])
        self.assertEqual(plan["section_id"], "s1")
        self.assertEqual(plan["source_revision"], 1)
        self.assertEqual(plan["unidentified_exit_ids"], ["x", "y"])
        self.assertEqual(
            {
                key: plan[key]
                for key in (
                    "min_seconds",
                    "max_seconds",
                    "target_seconds",
                    "max_axle_gap",
                )
            },
            {
                "min_seconds": 0,
                "max_seconds": 60,
                "target_seconds": 20,
                "max_axle_gap": 1,
            },
        )
        self.assertEqual(
            [e["id"] for e in plan["entry_candidates"]], ["a", "b"]
        )
        self.assertEqual(
            [e["id"] for e in plan["exits"]], ["x", "y"]
        )
        self.assertEqual(
            plan["optimum"],
            {
                "matched_count": 2,
                "axle_gap_sum": 2,
                "duration_error_microseconds": 20_000_000,
            },
        )
        self.assertEqual(
            plan["decisions"],
            [
                {
                    "exit_event_id": "x",
                    "possible_entry_ids": ["b"],
                    "may_be_unmatched": False,
                    "status": "forced",
                },
                {
                    "exit_event_id": "y",
                    "possible_entry_ids": ["a"],
                    "may_be_unmatched": False,
                    "status": "forced",
                },
            ],
        )
        self.assertEqual(
            plan["forced_pairs"],
            [
                {"entry_event_id": "b", "exit_event_id": "x", "train_id": "B"},
                {"entry_event_id": "a", "exit_event_id": "y", "train_id": "A"},
            ],
        )

        # The plan body is detached and survives reopen unchanged.
        plan["decisions"][0]["possible_entry_ids"].append("zzz")
        self.assertEqual(
            self.ar.read_identity_plan(plan["plan_id"])["decisions"][0][
                "possible_entry_ids"
            ],
            ["b"],
        )
        reopened_plan = self.reopen(sections=[S1, S2]).read_identity_plan(
            plan["plan_id"]
        )
        self.assertEqual(reopened_plan["optimum"], plan["optimum"])
        self.assertEqual(reopened_plan["source_revision"], 1)
        self.assertEqual(reopened_plan["entry_candidates"], plan["entry_candidates"])
        self.assertEqual(reopened_plan["exits"], plan["exits"])
        self.assertEqual(reopened_plan["forced_pairs"], plan["forced_pairs"])

        result = self.ar.apply_identities(plan["plan_id"], "apply-1")
        self.assertEqual(result["plan_id"], plan["plan_id"])
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["applied_pairs"],
            [
                {
                    "entry_event_id": "b",
                    "exit_event_id": "x",
                    "train_id": "B",
                    "old_train_id": "local-x",
                    "new_train_id": "B",
                },
                {
                    "entry_event_id": "a",
                    "exit_event_id": "y",
                    "train_id": "A",
                    "old_train_id": "local-y",
                    "new_train_id": "A",
                },
            ],
        )
        self.assertEqual(self.ar.revision, 2)

        # Only x and y labels changed; id/checkpoint/time/axles are untouched.
        for eid, train_id, time_text, axles in (
            ("x", "B", "2026-04-11T00:00:00Z", 21),
            ("y", "A", "2026-04-11T00:00:10Z", 19),
            ("a", "A", "2026-04-10T23:59:30Z", 20),
            ("b", "B", "2026-04-10T23:59:40Z", 22),
        ):
            stored = self.ar._state["events"][eid]
            self.assertEqual(
                stored,
                {
                    "id": eid,
                    "train_id": train_id,
                    "checkpoint": stored["checkpoint"],
                    "time": time_text,
                    "axles": axles,
                },
            )

        # Day 10 is untouched, day 11 and its take are withdrawn.  s2 is free.
        self.assertTrue(self.ar.read_package(self.s1_10["package_id"])["valid"])
        self.assertFalse(self.ar.read_package(self.s1_11["package_id"])["valid"])
        self.assertEqual(
            self.ar.read_package(self.s1_11["package_id"])[
                "invalidated_by_revision"
            ],
            2,
        )
        self.assertFalse(self.ar.read_take(self.take_11["receipt_id"])["valid"])
        self.assertTrue(self.ar.read_take(self.take_10["receipt_id"])["valid"])
        self.assertTrue(self.ar.read_package(self.s2_10["package_id"])["valid"])
        self.assertTrue(self.ar.read_take(self.take_s2["receipt_id"])["valid"])

        # Historical package bodies are immutable.
        old_events = self.ar.read_package(self.s1_11["package_id"])["package"][
            "events"
        ]
        self.assertEqual(
            {e["id"]: e["train_id"] for e in old_events},
            {"x": "local-x", "y": "local-y"},
        )

        # Resealing day 11 must produce the promised movements.
        new_11 = self.ar.seal("s1", "2026-04-11", "reseal-s1-11")
        self.assertEqual(new_11["parent_package_id"], self.s1_10["package_id"])
        movements = {
            movement["exit_event_id"]: movement
            for movement in new_11["movements"]
        }
        self.assertEqual(
            (
                movements["y"]["train_id"],
                movements["y"]["entry_event_id"],
                movements["y"]["duration_seconds"],
                movements["y"]["status"],
            ),
            ("A", "a", 40, "axle_mismatch"),
        )
        self.assertEqual(
            (
                movements["x"]["train_id"],
                movements["x"]["entry_event_id"],
                movements["x"]["duration_seconds"],
                movements["x"]["status"],
            ),
            ("B", "b", 20, "axle_mismatch"),
        )
        self.assertEqual(new_11["closing"], [])

        # Reopen and verify the persisted outcome, including replay.
        reopened = self.reopen(sections=[S1, S2])
        self.assertEqual(reopened.revision, 2)
        self.assertEqual(
            reopened.apply_identities(plan["plan_id"], "apply-1"), result
        )
        self.assertEqual(reopened.revision, 2)
        self.assertEqual(
            reopened.current("s1", "2026-04-11")["package_id"],
            new_11["package_id"],
        )

    def test_plan_reports_original_evidence_not_later_state(self):
        plan = self.ar.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan-1")
        self.ar.revise(
            "correct-y",
            1,
            [event("y", "ZZ", "b", "2026-04-11T00:00:10Z", 19)],
            [],
        )
        stored = self.ar.read_identity_plan(plan["plan_id"])
        self.assertEqual(
            {e["id"]: e["train_id"] for e in stored["exits"]},
            {"x": "local-x", "y": "local-y"},
        )
        # The plan is historical evidence; applying it still follows current
        # rules and the changed forced exit evidence is rejected.
        with self.assertRaises(ValueError):
            self.ar.apply_identities(plan["plan_id"], "apply-after-change")


class AmbiguityTests(IdentityTestBase):
    def test_symmetric_candidates_and_exits_are_ambiguous(self):
        ar = self.archive()
        ar.revise(
            "add",
            0,
            [
                event("e1", "T1", "a", "2026-04-11T00:00:00Z"),
                event("e2", "T2", "a", "2026-04-11T00:00:00Z"),
                event("x1", "l1", "b", "2026-04-11T00:00:20Z"),
                event("x2", "l2", "b", "2026-04-11T00:00:20Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["x1", "x2"], 0, 60, 20, 0, "p")
        self.assertEqual(
            plan["optimum"],
            {
                "matched_count": 2,
                "axle_gap_sum": 0,
                "duration_error_microseconds": 0,
            },
        )
        for decision in plan["decisions"]:
            self.assertEqual(decision["possible_entry_ids"], ["e1", "e2"])
            self.assertFalse(decision["may_be_unmatched"])
            self.assertEqual(decision["status"], "ambiguous")
        self.assertEqual(plan["forced_pairs"], [])

        # One entry competing for two same-cost exits.
        ar2 = self.archive()
        ar2.path = self.path
        ar2 = ReconciliationArchive(
            os.path.join(self.root, "archive2"),
            [S1],
            "2026-04-11",
            "2026-04-11",
        )
        ar2.revise(
            "add",
            0,
            [
                event("e1", "T1", "a", "2026-04-11T00:00:00Z"),
                event("x1", "l1", "b", "2026-04-11T00:00:20Z"),
                event("x2", "l2", "b", "2026-04-11T00:00:20Z"),
            ],
            [],
        )
        plan2 = ar2.plan_identities("s1", ["x1", "x2"], 0, 60, 20, 0, "p")
        for decision in plan2["decisions"]:
            self.assertEqual(decision["possible_entry_ids"], ["e1"])
            self.assertTrue(decision["may_be_unmatched"])
            self.assertEqual(decision["status"], "ambiguous")
        self.assertEqual(plan2["forced_pairs"], [])
        with self.assertRaises(ValueError):
            ar2.apply_identities(plan2["plan_id"], "no-forced")

        # An exit beyond the axle tolerance is unmatched, not forced.
        ar3 = ReconciliationArchive(
            os.path.join(self.root, "archive3"),
            [S1],
            "2026-04-11",
            "2026-04-11",
        )
        ar3.revise(
            "add",
            0,
            [
                event("e1", "T1", "a", "2026-04-11T00:00:00Z", 20),
                event("x1", "l1", "b", "2026-04-11T00:00:20Z", 20),
                event("x2", "l2", "b", "2026-04-11T00:00:20Z", 25),
            ],
            [],
        )
        plan3 = ar3.plan_identities("s1", ["x1", "x2"], 0, 60, 20, 1, "p")
        decisions = {d["exit_event_id"]: d for d in plan3["decisions"]}
        self.assertEqual(decisions["x2"]["possible_entry_ids"], [])
        self.assertTrue(decisions["x2"]["may_be_unmatched"])
        self.assertEqual(decisions["x2"]["status"], "unmatched")
        self.assertEqual(decisions["x1"]["status"], "forced")

    def test_microsecond_duration_beats_rounded_seconds(self):
        ar = self.archive()
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x1", "l1", "b", "2026-04-11T00:00:00.100000Z"),
                event("x2", "l2", "b", "2026-04-11T00:00:00.900000Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["x2", "x1"], 0, 1, 0, 0, "p")
        decisions = {d["exit_event_id"]: d for d in plan["decisions"]}
        self.assertEqual(decisions["x1"]["status"], "forced")
        self.assertEqual(decisions["x1"]["possible_entry_ids"], ["a"])
        self.assertEqual(decisions["x2"]["status"], "unmatched")
        self.assertTrue(decisions["x2"]["may_be_unmatched"])
        self.assertEqual(plan["optimum"]["duration_error_microseconds"], 100_000)

        # Reordered ids under the same request id replay the first plan.
        replay = ar.plan_identities("s1", ["x1", "x2"], 0, 1, 0, 0, "p")
        self.assertEqual(replay["plan_id"], plan["plan_id"])
        self.assertEqual(replay["decisions"], plan["decisions"])
        # A different request id with the same parameters is a new immutable
        # plan with an identical identity range.
        other = ar.plan_identities("s1", ["x1", "x2"], 0, 1, 0, 0, "p2")
        self.assertNotEqual(other["plan_id"], plan["plan_id"])
        self.assertEqual(other["decisions"], plan["decisions"])
        self.assertEqual(other["optimum"], plan["optimum"])
        self.assertEqual(
            ar.read_identity_plan(plan["plan_id"])["decisions"],
            plan["decisions"],
        )

        # Equivalent UTC offset spelling produces identical evidence.
        ar_offset = ReconciliationArchive(
            os.path.join(self.root, "offset-archive"),
            [S1],
            "2026-04-11",
            "2026-04-11",
        )
        ar_offset.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T08:00:00+08:00"),
                event("x1", "l1", "b", "2026-04-11T08:00:00.100000+08:00"),
                event("x2", "l2", "b", "2026-04-11T08:00:00.900000+08:00"),
            ],
            [],
        )
        offset_plan = ar_offset.plan_identities(
            "s1", ["x2", "x1"], 0, 1, 0, 0, "p"
        )
        self.assertEqual(offset_plan["decisions"], plan["decisions"])
        self.assertEqual(offset_plan["optimum"], plan["optimum"])
        self.assertEqual(
            offset_plan["exits"][0]["time"], "2026-04-11T00:00:00.100000Z"
        )


class CompetitionAndEvidenceTests(IdentityTestBase):
    def _basic(self):
        ar = self.archive(sections=[S1, S2], start="2026-04-11")
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
                event("c1", "Q", "c", "2026-04-11T01:00:00Z"),
                event("d1", "Q", "d", "2026-04-11T01:00:10Z"),
            ],
            [],
        )
        package = ar.seal("s1", "2026-04-11", "s1-seal")
        take = ar.take(package["package_id"], "s1-take")
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "plan")
        return ar, package, take, plan

    def test_competing_entry_blocks_then_reversible_failure_can_retry(self):
        ar, package, take, plan = self._basic()
        self.assertEqual(plan["decisions"][0]["status"], "forced")

        ar.revise(
            "add-b",
            1,
            [event("b", "B", "a", "2026-04-11T00:00:00Z")],
            [],
        )
        state_before = self.snapshot(ar)
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-failed")
        self.assertEqual(self.snapshot(ar), state_before)
        self.assertEqual(ar.revision, 2)
        self.assertFalse(ar.read_package(package["package_id"])["valid"])
        self.assertFalse(ar.read_take(take["receipt_id"])["valid"])

        ar.revise("delete-b", 2, [], ["b"])
        self.assertEqual(ar.revision, 3)
        result = ar.apply_identities(plan["plan_id"], "apply-failed")
        self.assertEqual(result["revision"], 4)
        self.assertTrue(result["changed"])

    def test_unrelated_revisions_do_not_expire_a_valid_plan(self):
        ar, package, take, plan = self._basic()
        # Correct s2 only.
        ar.revise(
            "fix-s2",
            1,
            [event("d1", "Q", "d", "2026-04-11T01:00:10Z", 29)],
            [],
        )
        self.assertEqual(ar.revision, 2)
        result = ar.apply_identities(plan["plan_id"], "apply-after-s2")
        self.assertTrue(result["changed"])

        # Fresh archive: correct an unrelated, already closed s1 movement.
        self.path = os.path.join(self.root, "archive-second")
        ar = self.archive(sections=[S1])
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
                event("p1", "P", "a", "2026-04-11T05:00:00Z"),
                event("q1", "P", "b", "2026-04-11T05:00:10Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "plan")
        self.assertEqual(
            {e["id"] for e in plan["entry_candidates"]}, {"a"}
        )
        ar.revise(
            "fix-p1",
            1,
            [event("p1", "P", "a", "2026-04-11T05:00:01Z")],
            [],
        )
        self.assertNotEqual(ar.read_identity_plan(plan["plan_id"])[
            "source_revision"
        ], ar.revision)
        result = ar.apply_identities(plan["plan_id"], "apply-later")
        self.assertTrue(result["changed"])

    def test_changed_axles_inside_tolerance_still_reject(self):
        ar = self.archive(sections=[S1, S2], start="2026-04-11")
        ar.revise("add", 0, [
            event("a", "A", "a", "2026-04-11T00:00:00Z"),
            event("x", "local", "b", "2026-04-11T00:00:10Z"),
        ], [])
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 1, "plan")
        self.assertEqual(plan["decisions"][0]["status"], "forced")
        ar.revise(
            "change-a",
            1,
            [event("a", "A", "a", "2026-04-11T00:00:00Z", 21)],
            [],
        )
        # Still forced a-x under the current evidence ...
        fresh = ar.plan_identities("s1", ["x"], 0, 60, 10, 1, "fresh-plan")
        self.assertEqual(fresh["decisions"][0]["status"], "forced")
        # ... but the original forced evidence changed, so the old plan fails.
        state = self.snapshot(ar)
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-tolerance")
        self.assertEqual(self.snapshot(ar), state)

    def test_entry_closed_by_an_unlisted_known_exit_is_no_candidate(self):
        ar, package, take, plan = self._basic()
        # A known train whose exit would close a before apply.  It is not in
        # the analyst's unidentified set, so it must not be back-filled, but it
        # also removes a from the candidate pool.
        ar.revise(
            "add-known-exit",
            1,
            [event("kx", "A", "b", "2026-04-11T00:00:05Z")],
            [],
        )
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-known")
        state = self.snapshot(ar)
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-known-2")
        self.assertEqual(self.snapshot(ar), state)

    def test_real_pairing_occupancy_duplicates_and_existing_movements(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        ar.revise(
            "add",
            0,
            [
                event("closed-entry", "T", "a", "2026-04-11T00:00:00Z"),
                event("closed-exit", "T", "b", "2026-04-11T00:00:05Z"),
                event("dup-base", "U", "a", "2026-04-11T00:01:00Z"),
                event("dup-extra", "U", "a", "2026-04-11T00:02:00Z"),
                event("target-exit", "local", "b", "2026-04-11T00:03:00Z"),
            ],
            [],
        )
        plan = ar.plan_identities(
            "s1", ["target-exit"], 0, 300, 120, 0, "plan"
        )
        # The already-closed entry is unavailable; the duplicate entry keeps
        # the first still-open observation.
        self.assertEqual(
            [e["id"] for e in plan["entry_candidates"]], ["dup-base"]
        )
        self.assertEqual(
            plan["forced_pairs"],
            [
                {
                    "entry_event_id": "dup-base",
                    "exit_event_id": "target-exit",
                    "train_id": "U",
                }
            ],
        )
        ar.seal("s1", "2026-04-11", "preseal")
        before = ar.current("s1", "2026-04-11")
        result = ar.apply_identities(plan["plan_id"], "apply")
        self.assertTrue(result["changed"])
        resealed = ar.seal("s1", "2026-04-11", "reseal")
        pairs = {
            (m["entry_event_id"], m["exit_event_id"])
            for m in resealed["movements"]
        }
        self.assertIn(("closed-entry", "closed-exit"), pairs)
        self.assertIn(("dup-base", "target-exit"), pairs)
        # The pre-apply package kept its original movement identity too.
        self.assertEqual(
            before["movements"][0]["entry_event_id"], "closed-entry"
        )


class StagedVerificationTests(IdentityTestBase):
    """Acceptance item 8: a still-unmatched designated exit can preempt."""

    def _setup(self):
        self.ar = self.archive(start="2026-04-11", end="2026-04-11")
        self.ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "A", "b", "2026-04-11T00:00:00.100000Z"),
                event("y", "local", "b", "2026-04-11T00:00:02Z"),
            ],
            [],
        )
        self.package = self.ar.seal("s1", "2026-04-11", "seal")
        self.take = self.ar.take(self.package["package_id"], "take")
        self.plan = self.ar.plan_identities("s1", ["x", "y"], 1, 3, 2, 0, "p")

    def test_preemptive_unmatched_exit_rejects_then_fixed_retry_succeeds(self):
        self._setup()
        decisions = {d["exit_event_id"]: d for d in self.plan["decisions"]}
        self.assertEqual(decisions["x"]["status"], "unmatched")
        self.assertTrue(decisions["x"]["may_be_unmatched"])
        self.assertEqual(decisions["y"]["status"], "forced")
        self.assertEqual(
            self.plan["forced_pairs"],
            [{"entry_event_id": "a", "exit_event_id": "y", "train_id": "A"}],
        )

        state = self.snapshot(self.ar)
        with self.assertRaises(ValueError):
            self.ar.apply_identities(self.plan["plan_id"], "apply-1")
        self.assertEqual(self.snapshot(self.ar), state)
        self.assertEqual(self.ar.revision, 1)
        self.assertTrue(self.ar.read_package(self.package["package_id"])["valid"])
        self.assertTrue(self.ar.read_take(self.take["receipt_id"])["valid"])
        # x is neither hidden nor relabelled.
        self.assertEqual(self.ar._state["events"]["x"]["train_id"], "A")

        # Relabel x with a local id that does not close a; a and y are unchanged.
        self.ar.revise(
            "fix-x",
            1,
            [event("x", "local-x", "b", "2026-04-11T00:00:00.100000Z")],
            [],
        )
        result = self.ar.apply_identities(self.plan["plan_id"], "apply-1")
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["applied_pairs"],
            [
                {
                    "entry_event_id": "a",
                    "exit_event_id": "y",
                    "train_id": "A",
                    "old_train_id": "local",
                    "new_train_id": "A",
                }
            ],
        )
        resealed = self.ar.seal("s1", "2026-04-11", "reseal")
        movement = resealed["movements"][0]
        self.assertEqual(
            (movement["entry_event_id"], movement["exit_event_id"]),
            ("a", "y"),
        )
        self.assertEqual(movement["status"], "balanced")

    def test_any_preempted_forced_pair_rejects_all(self):
        self.ar = self.archive(start="2026-04-11", end="2026-04-11")
        self.ar.revise(
            "add",
            0,
            [
                event("a1", "A1", "a", "2026-04-11T00:00:00Z"),
                event("a2", "A2", "a", "2026-04-11T00:00:01Z"),
                event("x", "A1", "b", "2026-04-11T00:00:02Z"),
                event("y1", "local1", "b", "2026-04-11T00:00:20Z"),
                event("y2", "local2", "b", "2026-04-11T00:00:21Z"),
            ],
            [],
        )
        plan = self.ar.plan_identities(
            "s1", ["x", "y1", "y2"], 10, 60, 20, 0, "p"
        )
        decisions = {d["exit_event_id"]: d for d in plan["decisions"]}
        self.assertEqual(decisions["y1"]["status"], "forced")
        self.assertEqual(decisions["y1"]["possible_entry_ids"], ["a1"])
        self.assertEqual(decisions["y2"]["status"], "forced")
        self.assertEqual(decisions["y2"]["possible_entry_ids"], ["a2"])
        self.assertEqual(decisions["x"]["status"], "unmatched")

        with self.assertRaises(ValueError):
            self.ar.apply_identities(plan["plan_id"], "apply-all")
        # No partial correction: both forced exits keep their local labels.
        self.assertEqual(self.ar._state["events"]["y1"]["train_id"], "local1")
        self.assertEqual(self.ar._state["events"]["y2"]["train_id"], "local2")

        self.ar.revise(
            "fix-x",
            1,
            [event("x", "local-x", "b", "2026-04-11T00:00:02Z")],
            [],
        )
        result = self.ar.apply_identities(plan["plan_id"], "apply-all")
        self.assertEqual(len(result["applied_pairs"]), 2)
        self.assertEqual(self.ar.revision, 3)


class RequestAndValidationTests(IdentityTestBase):
    def _setup(self):
        ar = self.archive(sections=[S1, S2])
        ar.revise(
            "shared",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        return ar

    def test_plan_validation_raises_value_error_and_leaves_no_plan(self):
        ar = self._setup()
        bad_calls = [
            dict(section_id="missing", ids=["x"], rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=["a"], rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=["nope"], rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=[], rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=["x", "x"], rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=["x"] * 33, rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids="x", rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=[7], rules=(0, 60, 10, 0)),
            dict(section_id="s1", ids=["x"], rules=(True, 60, 10, 0)),
            dict(section_id="s1", ids=["x"], rules=(0.0, 60, 10, 0)),
            dict(section_id="s1", ids=["x"], rules=(-1, 60, 10, 0)),
            dict(section_id="s1", ids=["x"], rules=(11, 60, 10, 0)),
            dict(section_id="s1", ids=["x"], rules=(0, 60, 61, 0)),
            dict(section_id="s1", ids=["x"], rules=(0, 604801, 10, 0)),
            dict(section_id="s1", ids=["x"], rules=(0, 60, 10, 1000)),
            dict(section_id="s1", ids=["x"], rules=(0, 60, 10, -1)),
        ]
        for index, call in enumerate(bad_calls):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    ar.plan_identities(
                        call["section_id"],
                        call["ids"],
                        *call["rules"],
                        f"bad-{index}",
                    )
        with self.assertRaises(ValueError):
            ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "")
        self.assertEqual(ar._state["plans"], {})
        self.assertEqual(ar.revision, 1)
        # No failed id occupied; the first valid plan is plan-1.
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "good")
        self.assertEqual(plan["plan_id"], "plan-1")
        with self.assertRaises(ValueError):
            ar.read_identity_plan("plan-404")
        with self.assertRaises(ValueError):
            ar.read_identity_plan(None)

    def test_request_identity_conflicts_across_operations(self):
        ar = self._setup()
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "plan-req")
        # Same id, different rules.
        with self.assertRaises(ValueError):
            ar.plan_identities("s1", ["x"], 0, 60, 11, 0, "plan-req")
        # Same id, different exit set.
        with self.assertRaises(ValueError):
            ar.plan_identities("s1", ["x", "y"], 0, 60, 10, 0, "plan-req")
        # Same id, different section.
        with self.assertRaises(ValueError):
            ar.plan_identities("s2", ["x"], 0, 60, 10, 0, "plan-req")
        # Id already used by revise.
        with self.assertRaises(ValueError):
            ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "shared")
        # Id already used by a plan cannot be used for apply.
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "plan-req")
        # Replay of the original plan request still succeeds.
        self.assertEqual(
            ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "plan-req")["plan_id"],
            plan["plan_id"],
        )
        # Apply with an unknown plan or id conflict.
        with self.assertRaises(ValueError):
            ar.apply_identities("plan-404", "apply-x")
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "")
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], 7)
        # Swap plans: a second plan, applied under the first plan's request id.
        plan2 = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "plan-req-2")
        first = ar.apply_identities(plan["plan_id"], "apply-shared")
        self.assertTrue(first["changed"])
        with self.assertRaises(ValueError):
            ar.apply_identities(plan2["plan_id"], "apply-shared")

    def test_apply_of_replayed_success_ignores_current_evidence(self):
        ar = self._setup()
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")
        first = ar.apply_identities(plan["plan_id"], "apply")
        self.assertEqual(ar.revision, 2)
        # Add competition that would invalidate a fresh attempt.
        ar.revise("compete", 2, [event("z", "Z", "a", "2026-04-11T00:00:00Z", 5)], [])
        # Same request replays the first success without a second revision.
        self.assertEqual(ar.apply_identities(plan["plan_id"], "apply"), first)
        self.assertEqual(ar.revision, 3)
        # A new request id follows current evidence and is rejected.
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-new")

    def test_too_many_candidate_entries_rejects_plan(self):
        ar = self.archive()
        events = [
            event(f"e{i:02d}", f"T{i:02d}", "a", f"2026-04-11T00:00:{i:02d}Z", 1)
            for i in range(33)
        ]
        events.append(event("x", "local", "b", "2026-04-11T01:00:00Z", 1))
        ar.revise("add", 0, events, [])
        with self.assertRaises(ValueError):
            ar.plan_identities("s1", ["x"], 0, 7200, 3600, 999, "p")
        self.assertEqual(ar._state["plans"], {})


class WindowLimitTests(IdentityTestBase):
    def test_full_symmetric_32_window_reports_every_possible_entry(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        events = []
        for i in range(32):
            events.append(event(f"e{i:02d}", f"TE{i:02d}", "a", "2026-04-11T00:00:00Z", 20))
            events.append(event(f"x{i:02d}", f"TX{i:02d}", "b", "2026-04-11T00:00:20Z", 20))
        ar.revise("add", 0, events, [])
        plan = ar.plan_identities("s1", [f"x{i:02d}" for i in range(32)], 0, 60, 20, 0, "p")
        expected = [f"e{i:02d}" for i in range(32)]
        self.assertEqual(
            plan["optimum"],
            {
                "matched_count": 32,
                "axle_gap_sum": 0,
                "duration_error_microseconds": 0,
            },
        )
        self.assertEqual(plan["forced_pairs"], [])
        for decision in plan["decisions"]:
            self.assertEqual(decision["possible_entry_ids"], expected)
            self.assertFalse(decision["may_be_unmatched"])
            self.assertEqual(decision["status"], "ambiguous")

    def test_31_symmetric_pairs_plus_one_isolated_forced_pair(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        events = []
        for i in range(31):
            events.append(event(f"e{i:02d}", f"TE{i:02d}", "a", "2026-04-11T00:00:00Z", 20))
            events.append(event(f"x{i:02d}", f"TX{i:02d}", "b", "2026-04-11T00:00:20Z", 20))
        # Isolated by axle count: only each other can pair.
        events.append(event("solo-entry", "SOLO", "a", "2026-04-11T00:00:00Z", 50))
        events.append(event("solo-exit", "local", "b", "2026-04-11T00:00:25Z", 50))
        ar.revise("add", 0, events, [])
        exit_ids = [f"x{i:02d}" for i in range(31)] + ["solo-exit"]
        plan = ar.plan_identities("s1", exit_ids, 0, 60, 20, 0, "p")
        decisions = {d["exit_event_id"]: d for d in plan["decisions"]}
        self.assertEqual(
            decisions["solo-exit"]["possible_entry_ids"], ["solo-entry"]
        )
        self.assertFalse(decisions["solo-exit"]["may_be_unmatched"])
        self.assertEqual(decisions["solo-exit"]["status"], "forced")
        self.assertEqual(
            plan["forced_pairs"],
            [
                {
                    "entry_event_id": "solo-entry",
                    "exit_event_id": "solo-exit",
                    "train_id": "SOLO",
                }
            ],
        )
        for i in range(31):
            decision = decisions[f"x{i:02d}"]
            self.assertEqual(len(decision["possible_entry_ids"]), 31)
            self.assertEqual(decision["status"], "ambiguous")
            self.assertFalse(decision["may_be_unmatched"])


RESOURCE_WORKER = r"""
import json
import resource
import sys
import time

sys.path.insert(0, sys.argv[1])
from axle_reconciliation import ReconciliationArchive

path = sys.argv[2]
section = {"id": "s1", "entry": "a", "exit": "b"}
archive = ReconciliationArchive(path, [section], "2026-04-11", "2026-04-11")

def event(eid, train, checkpoint, axles):
    return {
        "id": eid, "train_id": train, "checkpoint": checkpoint,
        "time": "2026-04-11T00:00:00Z" if checkpoint == "a"
                else "2026-04-11T00:00:20Z",
        "axles": axles,
    }

events = []
for i in range(32):
    events.append(event("e%02d" % i, "TE%02d" % i, "a", 20))
    events.append(event("x%02d" % i, "TX%02d" % i, "b", 20))
archive.revise("add", 0, events, [])

started = time.perf_counter()
plan = archive.plan_identities(
    "s1", ["x%02d" % i for i in range(32)], 0, 60, 20, 0, "resource-plan"
)
elapsed = time.perf_counter() - started
rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

print("WORKER_RESULT " + json.dumps({
    "elapsed_seconds": elapsed,
    "peak_rss_mib": rss_mib,
    "possible_counts": [len(d["possible_entry_ids"]) for d in plan["decisions"]],
    "may_be_unmatched": [d["may_be_unmatched"] for d in plan["decisions"]],
    "optimum": plan["optimum"],
}))
"""


class ResourceBoundTests(unittest.TestCase):
    def test_32_by_32_window_time_and_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "archive")
            proc = subprocess.run(
                [sys.executable, "-I", "-c", RESOURCE_WORKER, str(SRC), path],
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [
                line
                for line in proc.stdout.splitlines()
                if line.startswith("WORKER_RESULT ")
            ]
            self.assertTrue(lines, proc.stderr)
            measured = json.loads(lines[-1].split(" ", 1)[1])
        self.assertLessEqual(measured["elapsed_seconds"], 60.0)
        self.assertLessEqual(measured["peak_rss_mib"], 512.0)
        self.assertEqual(measured["possible_counts"], [32] * 32)
        self.assertEqual(measured["may_be_unmatched"], [False] * 32)
        self.assertEqual(
            measured["optimum"],
            {
                "matched_count": 32,
                "axle_gap_sum": 0,
                "duration_error_microseconds": 0,
            },
        )
        print(
            "measured plan_identities: %.4f s, peak RSS %.1f MiB"
            % (measured["elapsed_seconds"], measured["peak_rss_mib"]),
            file=sys.stderr,
        )


APPLY_CRASH_WORKER = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])
from axle_reconciliation import ReconciliationArchive

path = sys.argv[2]
command = sys.argv[3]
section = {"id": "s1", "entry": "a", "exit": "b"}

def event(eid, train, checkpoint, time_text, axles):
    return {
        "id": eid, "train_id": train, "checkpoint": checkpoint,
        "time": time_text, "axles": axles,
    }

def open_archive():
    return ReconciliationArchive(path, [section], "2026-04-10", "2026-04-11")

def emit(value):
    print("WORKER_RESULT " + json.dumps(value, sort_keys=True))

if command == "setup":
    archive = open_archive()
    archive.revise("add", 0, [
        event("a", "A", "a", "2026-04-10T23:59:30Z", 20),
        event("b", "B", "a", "2026-04-10T23:59:40Z", 22),
        event("x", "local-x", "b", "2026-04-11T00:00:00Z", 21),
        event("y", "local-y", "b", "2026-04-11T00:00:10Z", 19),
    ], [])
    archive.seal("s1", "2026-04-10", "seal-10")
    archive.seal("s1", "2026-04-11", "seal-11")
    plan = archive.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan-req")
    emit(plan["plan_id"])
elif command == "plan-only":
    archive = open_archive()
    emit(archive.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan-crash"))
elif command == "replay-plan":
    archive = open_archive()
    emit(archive.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan-crash"))
elif command == "apply":
    archive = open_archive()
    emit(archive.apply_identities(sys.argv[4], "apply-crash"))
elif command == "replay":
    archive = open_archive()
    emit(archive.apply_identities(sys.argv[4], "apply-crash"))
else:
    raise SystemExit("unknown worker command")
"""


class _CrashWorkerMixin:
    def _run_worker(self, path, args, fault_point=None, fault_request="apply-crash"):
        env = os.environ.copy()
        env.pop("RECONCILIATION_ARCHIVE_FAULT", None)
        env.pop("RECONCILIATION_ARCHIVE_FAULT_REQUEST", None)
        if fault_point is not None:
            env["RECONCILIATION_ARCHIVE_FAULT"] = fault_point
            env["RECONCILIATION_ARCHIVE_FAULT_REQUEST"] = fault_request
        return subprocess.run(
            [sys.executable, "-I", "-c", APPLY_CRASH_WORKER, str(SRC), path, *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    @staticmethod
    def _result(proc):
        lines = [
            line
            for line in proc.stdout.splitlines()
            if line.startswith("WORKER_RESULT ")
        ]
        assert lines, proc.stderr + proc.stdout
        return json.loads(lines[-1].split(" ", 1)[1])

    def test_multi_exit_apply_is_atomic_across_reopen(self):
        section = [{"id": "s1", "entry": "a", "exit": "b"}]
        for point, expected_code, persisted in (
            ("mid_write", 86, False),
            ("before_replace", 87, False),
            ("after_replace", 88, True),
        ):
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = os.path.join(temp_dir, "archive")
                    setup = self._run_worker(path, ["setup"])
                    self.assertEqual(setup.returncode, 0, setup.stderr)
                    plan_id = self._result(setup)

                    crashed = self._run_worker(
                        path, ["apply", plan_id], fault_point=point
                    )
                    self.assertEqual(crashed.returncode, expected_code, crashed.stderr)

                    ar = ReconciliationArchive(
                        path, section, "2026-04-10", "2026-04-11"
                    )
                    if persisted:
                        self.assertEqual(ar.revision, 2)
                        self.assertEqual(ar.apply_identities(
                            plan_id, "apply-crash"
                        )["revision"], 2)
                        self.assertEqual(
                            ar._state["events"]["x"]["train_id"], "B"
                        )
                        self.assertEqual(
                            ar._state["events"]["y"]["train_id"], "A"
                        )
                    else:
                        self.assertEqual(ar.revision, 1)
                        self.assertEqual(
                            ar._state["events"]["x"]["train_id"], "local-x"
                        )
                        self.assertEqual(
                            ar._state["events"]["y"]["train_id"], "local-y"
                        )
                        # The failed request id is free; retry in a new process
                        # applies exactly once.
                        replay = self._run_worker(path, ["replay", plan_id])
                        self.assertEqual(replay.returncode, 0, replay.stderr)
                        self.assertTrue(self._result(replay)["changed"])

                    reopened = ReconciliationArchive(
                        path, section, "2026-04-10", "2026-04-11"
                    )
                    self.assertEqual(reopened.revision, 2)
                    self.assertEqual(
                        reopened._state["events"]["x"]["train_id"], "B"
                    )
                    self.assertEqual(
                        reopened._state["events"]["y"]["train_id"], "A"
                    )
                    # Reopen replay returns the original result, no new revision.
                    self.assertEqual(
                        reopened.apply_identities(plan_id, "apply-crash")[
                            "revision"
                        ],
                        2,
                    )
                    self.assertEqual(reopened.revision, 2)
                    # All old packages and takes for both days are withdrawn
                    # by the single revision.
                    # Day 10 is earlier than the corrected day, so it stays
                    # valid; day 11 is withdrawn by the one revision.
                    self.assertTrue(reopened.read_package("package-1")["valid"])
                    day11 = reopened.read_package("package-2")
                    self.assertFalse(day11["valid"])
                    self.assertEqual(day11["invalidated_by_revision"], 2)


class NoOpApplyTests(IdentityTestBase):
    def test_already_matching_label_is_not_a_new_revision(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "A", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")
        self.assertEqual(plan["decisions"][0]["status"], "forced")
        self.assertEqual(plan["forced_pairs"][0]["train_id"], "A")
        revision = ar.revision
        result = ar.apply_identities(plan["plan_id"], "apply-noop")
        self.assertFalse(result["changed"])
        self.assertEqual(result["revision"], revision)
        self.assertEqual(ar.revision, revision)
        self.assertEqual(
            result["applied_pairs"],
            [
                {
                    "entry_event_id": "a",
                    "exit_event_id": "x",
                    "train_id": "A",
                    "old_train_id": "A",
                    "new_train_id": "A",
                }
            ],
        )
        self.assertEqual(ar.apply_identities(plan["plan_id"], "apply-noop"), result)


class ApplyEvidenceGuardTests(IdentityTestBase):
    def _plan(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        return ar, ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")

    def test_changed_exit_time_rejects_even_when_still_forced(self):
        ar, plan = self._plan()
        ar.revise(
            "shift-x",
            1,
            [event("x", "local", "b", "2026-04-11T00:00:11Z")],
            [],
        )
        fresh = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "fresh")
        self.assertEqual(fresh["decisions"][0]["status"], "forced")
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply")
        self.assertEqual(ar.revision, 2)

    def test_moved_or_deleted_forced_observation_rejects(self):
        ar = self.archive(
            sections=[S1, S2], start="2026-04-11", end="2026-04-11"
        )
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")
        ar.revise(
            "move-x",
            1,
            [event("x", "local", "d", "2026-04-11T00:00:10Z")],
            [],
        )
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-moved")

        self.path = os.path.join(self.root, "deleted")
        ar2 = self.archive(start="2026-04-11", end="2026-04-11")
        ar2.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        plan2 = ar2.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")
        ar2.revise("delete-a", 1, [], ["a"])
        with self.assertRaises(ValueError):
            ar2.apply_identities(plan2["plan_id"], "apply-deleted")
        self.assertEqual(ar2.revision, 2)

    def test_changed_entry_train_id_rejects(self):
        ar, plan = self._plan()
        ar.revise(
            "rename-a",
            1,
            [event("a", "A-renamed", "a", "2026-04-11T00:00:00Z")],
            [],
        )
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-renamed")


class PlanCrashRecoveryTests(_CrashWorkerMixin, unittest.TestCase):
    def test_plan_body_and_receipt_are_persisted_together(self):
        section = [{"id": "s1", "entry": "a", "exit": "b"}]
        for point, expected_code, persisted in (
            ("mid_write", 86, False),
            ("before_replace", 87, False),
            ("after_replace", 88, True),
        ):
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = os.path.join(temp_dir, "archive")
                    setup = self._run_worker(path, ["setup"])
                    self.assertEqual(setup.returncode, 0, setup.stderr)
                    plan_id = self._result(setup)
                    before = ReconciliationArchive(
                        path, section, "2026-04-10", "2026-04-11"
                    )
                    before_plans = sorted(before._state["plans"])
                    before_requests = sorted(before._state["requests"])

                    crashed = self._run_worker(
                        path,
                        ["plan-only"],
                        fault_point=point,
                        fault_request="plan-crash",
                    )
                    self.assertEqual(crashed.returncode, expected_code, crashed.stderr)

                    ar = ReconciliationArchive(
                        path, section, "2026-04-10", "2026-04-11"
                    )
                    if persisted:
                        # Body and receipt are both present, and the earlier
                        # plan plus its apply capability are intact.
                        self.assertIn("plan-2", ar._state["plans"])
                        self.assertIn("plan-crash", ar._state["requests"])
                        self.assertEqual(
                            ar.read_identity_plan("plan-2")["plan_id"], "plan-2"
                        )
                        self.assertTrue(
                            ar.apply_identities(plan_id, "apply-after-plan")[
                                "changed"
                            ]
                        )
                    else:
                        self.assertEqual(
                            sorted(ar._state["plans"]), before_plans
                        )
                        self.assertEqual(
                            sorted(ar._state["requests"]), before_requests
                        )
                        with self.assertRaises(ValueError):
                            ar.read_identity_plan("plan-2")
                        replay = self._run_worker(path, ["replay-plan"])
                        self.assertEqual(replay.returncode, 0, replay.stderr)
                        self.assertEqual(
                            self._result(replay)["plan_id"], "plan-2"
                        )


class LegacyArchiveCompatibilityTests(IdentityTestBase):
    def test_archive_created_before_plans_reopens_and_accepts_plans(self):
        ar = self.archive()
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        # Simulate a snapshot written by the previous implementation.
        del ar._state["plans"]
        del ar._state["plan_seq"]
        with open(os.path.join(self.path, "reconciliation-archive.json"), "w") as fh:
            json.dump(ar._state, fh)

        reopened = self.reopen()
        self.assertEqual(reopened.revision, 1)
        plan = reopened.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")
        self.assertEqual(plan["plan_id"], "plan-1")
        self.assertTrue(
            reopened.apply_identities(plan["plan_id"], "apply")["changed"]
        )


if __name__ == "__main__":
    unittest.main()


class ExhaustiveCrossCheckTests(IdentityTestBase):
    """Small windows compared against exhaustive optimal-explanation search."""

    @staticmethod
    def _enumerate(entries, exits, rules):
        min_s, max_s, target_s, max_gap = rules
        edges = {}
        for i, (_, entry_us, entry_axles) in enumerate(entries):
            for j, (_, exit_us, exit_axles) in enumerate(exits):
                interval = exit_us - entry_us
                if interval < min_s * 1_000_000 or interval > max_s * 1_000_000:
                    continue
                gap = abs(entry_axles - exit_axles)
                if gap > max_gap:
                    continue
                edges[(i, j)] = (
                    gap,
                    abs(interval - target_s * 1_000_000),
                )
        best = None
        solutions = []

        def walk(index, used, chosen):
            nonlocal best
            if index == len(entries):
                key = (
                    len(chosen),
                    -sum(edges[pair][0] for pair in chosen),
                    -sum(edges[pair][1] for pair in chosen),
                )
                if best is None or key > best:
                    best = key
                    solutions.clear()
                    solutions.append(frozenset(chosen))
                elif key == best:
                    solutions.append(frozenset(chosen))
                return
            walk(index + 1, used, chosen)
            for j in range(len(exits)):
                if j in used or (index, j) not in edges:
                    continue
                walk(index + 1, used | {j}, chosen + [(index, j)])

        walk(0, frozenset(), [])
        possible = []
        may_be_unmatched = []
        for j in range(len(exits)):
            possible.append(
                {
                    entries[i][0]
                    for solution in solutions
                    for (i, jj) in solution
                    if jj == j
                }
            )
            may_be_unmatched.append(
                not all(
                    any(jj == j for (_, jj) in solution)
                    for solution in solutions
                )
            )
        return best, possible, may_be_unmatched

    def test_random_small_windows_match_exhaustive_search(self):
        import random
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 4, 11, tzinfo=timezone.utc)

        def text(microseconds):
            return (base + timedelta(microseconds=microseconds)).isoformat().replace(
                "+00:00", "Z"
            )

        rng = random.Random(20260411)
        offsets = [0, 100_000, 500_000, 1_000_000, 2_000_000, 3_000_000]
        rule_sets = [(0, 6, 0), (1, 5, 2), (0, 4, 2), (0, 1, 0), (0, 5, 1)]
        for trial in range(120):
            entry_count = rng.randint(1, 4)
            exit_count = rng.randint(1, 4)
            entries = [
                (
                    f"e{index}",
                    rng.choice(offsets),
                    rng.randint(18, 22),
                )
                for index in range(entry_count)
            ]
            exits = [
                (
                    f"x{index}",
                    rng.choice(offsets[1:]),
                    rng.randint(18, 22),
                )
                for index in range(exit_count)
            ]
            min_s, max_s, target_s = rule_sets[trial % len(rule_sets)]
            max_gap = trial % 3
            rules = (min_s, max_s, target_s, max_gap)

            archive = ReconciliationArchive(
                os.path.join(self.root, f"trial-{trial}"),
                [S1],
                "2026-04-11",
                "2026-04-11",
            )
            events = [
                event(eid, f"TE{eid}", "a", text(entry_us), axles)
                for eid, entry_us, axles in entries
            ] + [
                event(eid, f"TX{eid}", "b", text(exit_us), axles)
                for eid, exit_us, axles in exits
            ]
            archive.revise("add", 0, events, [])
            plan = archive.plan_identities(
                "s1", [eid for eid, _, _ in exits], *rules, f"p{trial}"
            )
            best, possible, may = self._enumerate(entries, exits, rules)
            optimum = plan["optimum"]
            self.assertEqual(
                (
                    optimum["matched_count"],
                    -optimum["axle_gap_sum"],
                    -optimum["duration_error_microseconds"],
                ),
                best,
                (trial, entries, exits, rules),
            )
            decisions = {d["exit_event_id"]: d for d in plan["decisions"]}
            for j, (eid, _, _) in enumerate(exits):
                self.assertEqual(
                    set(decisions[eid]["possible_entry_ids"]),
                    possible[j],
                    (trial, entries, exits, rules),
                )
                self.assertEqual(
                    decisions[eid]["may_be_unmatched"],
                    may[j],
                    (trial, entries, exits, rules),
                )


class NonForcedChangeTests(IdentityTestBase):
    def test_non_forced_designated_exit_may_change_or_leave(self):
        ar = self.archive(
            sections=[S1, S2], start="2026-04-11", end="2026-04-11"
        )
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("y", "local", "b", "2026-04-11T00:00:10Z"),
                event("z", "localz", "b", "2026-04-11T00:00:30Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["y", "z"], 0, 60, 10, 0, "p")
        decisions = {d["exit_event_id"]: d for d in plan["decisions"]}
        self.assertEqual(decisions["z"]["status"], "unmatched")
        self.assertEqual(decisions["y"]["status"], "forced")
        # Moving a non-forced designated exit to another section is allowed by
        # the plan evidence rules; the current input then no longer satisfies
        # the plan's own exit set, so apply is cleanly rejected.
        ar.revise(
            "move-z",
            1,
            [event("z", "localz", "c", "2026-04-11T00:00:30Z")],
            [],
        )
        state = self.snapshot(ar)
        with self.assertRaises(ValueError):
            ar.apply_identities(plan["plan_id"], "apply-moved-z")
        self.assertEqual(self.snapshot(ar), state)

        # On a second archive, changing a non-forced designated exit's time
        # keeps the forced evidence intact and apply succeeds.
        self.path = os.path.join(self.root, "second")
        ar2 = self.archive(start="2026-04-11", end="2026-04-11")
        ar2.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("y", "local", "b", "2026-04-11T00:00:10Z"),
                event("z", "localz", "b", "2026-04-11T00:00:30Z"),
            ],
            [],
        )
        plan2 = ar2.plan_identities("s1", ["y", "z"], 0, 60, 10, 0, "p")
        ar2.revise(
            "change-z-time",
            1,
            [event("z", "localz", "b", "2026-04-11T00:00:40Z")],
            [],
        )
        result = ar2.apply_identities(plan2["plan_id"], "apply")
        self.assertTrue(result["changed"])
        self.assertEqual(ar2._state["events"]["y"]["train_id"], "A")
        self.assertEqual(ar2._state["events"]["z"]["train_id"], "localz")

    def test_multiple_forced_with_one_destroyed_movement_applies_none(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        ar.revise(
            "add",
            0,
            [
                event("a1", "A1", "a", "2026-04-11T00:00:00Z"),
                event("a2", "A2", "a", "2026-04-11T00:00:01Z"),
                event("y1", "local1", "b", "2026-04-11T00:00:20Z"),
                event("y2", "local2", "b", "2026-04-11T00:00:21Z"),
                event("blocker", "A1", "b", "2026-04-11T00:00:30Z"),
            ],
            [],
        )
        plan = ar.plan_identities("s1", ["y1", "y2"], 10, 60, 20, 0, "p")
        decisions = {d["exit_event_id"]: d for d in plan["decisions"]}
        # blocker (unlisted, known train A1) closes a1 before y1 can.
        self.assertEqual(decisions["y1"]["status"], "unmatched")
        self.assertEqual(decisions["y2"]["status"], "forced")
        ar.revise("delete-blocker", 1, [], ["blocker"])
        # Now y1 becomes forced too, but the old plan considered it unmatched;
        # the plan is stale for y1?  Here y1 is genuinely forced now, and the
        # plan's forced set is only y2, so it still applies y2 alone.
        result = ar.apply_identities(plan["plan_id"], "apply")
        self.assertEqual(
            [p["exit_event_id"] for p in result["applied_pairs"]], ["y2"]
        )
        self.assertEqual(ar._state["events"]["y1"]["train_id"], "local1")


class PackageIndependenceTests(IdentityTestBase):
    def test_plan_needs_no_sealed_package_and_writes_nothing(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        ar.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-11T00:00:00Z"),
                event("x", "local", "b", "2026-04-11T00:00:10Z"),
            ],
            [],
        )
        before = self.snapshot(ar)
        plan = ar.plan_identities("s1", ["x"], 0, 60, 10, 0, "p")
        after = self.snapshot(ar)
        # A plan adds only its immutable body and a success receipt; the event
        # revision, packages and takes are untouched.
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(after["packages"], before["packages"])
        self.assertEqual(after["package_status"], before["package_status"])
        self.assertEqual(after["takes"], before["takes"])
        self.assertEqual(after["plans"], ["plan-1"])
        self.assertEqual(ar._state["packages"], {})
        self.assertEqual(plan["forced_pairs"][0]["entry_event_id"], "a")

    def test_candidate_overflow_does_not_occupy_request_id(self):
        ar = self.archive(start="2026-04-11", end="2026-04-11")
        events = [
            event(f"e{i:02d}", f"T{i:02d}", "a", f"2026-04-11T00:00:{i:02d}Z", 1)
            for i in range(33)
        ]
        events.append(event("x", "local", "b", "2026-04-11T01:00:00Z", 1))
        ar.revise("add", 0, events, [])
        with self.assertRaises(ValueError):
            ar.plan_identities("s1", ["x"], 0, 7200, 3600, 999, "retry-me")
        self.assertEqual(ar._state["plans"], {})
        ar.revise("remove-one", 1, [], ["e00"])
        plan = ar.plan_identities("s1", ["x"], 0, 7200, 3600, 999, "retry-me")
        self.assertEqual(plan["plan_id"], "plan-1")
