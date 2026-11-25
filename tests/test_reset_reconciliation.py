from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from axle_reconciliation import ReconciliationArchive
from axle_reconciliation import archive as archive_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"

S1 = {"id": "s1", "entry": "a", "exit": "b"}
S2 = {"id": "s2", "entry": "c", "exit": "d"}
TWO_SECTIONS = [S1, S2]


def ev(eid, train, checkpoint, time, axles=20):
    return {
        "id": eid,
        "train_id": train,
        "checkpoint": checkpoint,
        "time": time,
        "axles": axles,
    }


def reset(rid, section_id, time, authorization_ref, opening):
    return {
        "id": rid,
        "section_id": section_id,
        "time": time,
        "authorization_ref": authorization_ref,
        "opening": opening,
    }


class ResetTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.path = os.path.join(self.root, "archive")

    def archive(self, sections=None, start="2026-04-10", end="2026-04-13"):
        return ReconciliationArchive(
            self.path,
            TWO_SECTIONS if sections is None else sections,
            start,
            end,
        )

    def reopen(self, sections=None, start="2026-04-10", end="2026-04-13"):
        return ReconciliationArchive(
            self.path,
            TWO_SECTIONS if sections is None else sections,
            start,
            end,
        )

    def snapshot(self, ar):
        """Observable state proving a rejected request changed nothing."""
        return {
            "revision": ar.revision,
            "events": json.loads(json.dumps(ar._state["events"])),
            "resets": json.loads(json.dumps(ar._state["resets"])),
            "reset_active": json.loads(json.dumps(ar._state["reset_active"])),
            "packages": json.loads(json.dumps(ar._state["packages"])),
            "package_status": json.loads(json.dumps(ar._state["package_status"])),
            "takes": json.loads(json.dumps(ar._state["takes"])),
            "requests": sorted(ar._state["requests"]),
        }


class ScenarioOneTests(ResetTestBase):
    """Acceptance item 1: review write-offs vs. the raw sealed package."""

    def setUp(self) -> None:
        super().setUp()
        self.ar = self.archive(sections=[S1])
        self.a = ev("a", "T", "a", "2026-04-10T23:59:30Z")
        self.b = ev("b", "T", "a", "2026-04-11T00:00:00Z")
        self.x = ev("x", "T", "b", "2026-04-11T00:02:00Z")
        self.assertEqual(
            self.ar.revise("add", 0, [self.a, self.b, self.x], []),
            {"revision": 1, "changed": True},
        )
        self.r1 = reset(
            "r1", "s1", "2026-04-11T00:00:00Z", "AUTH-1", [self.a]
        )
        self.r2 = reset(
            "r2", "s1", "2026-04-11T00:01:00Z", "AUTH-2", [self.b]
        )
        self.reg = self.ar.revise_with_resets(
            "register-resets", 1, [], [], [self.r1, self.r2], []
        )

    def test_registration_result_shape_and_no_source_revision(self):
        self.assertEqual(
            self.reg,
            {
                "revision": 1,
                "changed": False,
                "added_reset_ids": ["r1", "r2"],
                "withdrawn_reset_ids": [],
            },
        )
        # Reset-only write does not move the source revision.
        self.assertEqual(self.ar.revision, 1)

    def test_reviewed_day_replays_resets_before_same_instant_events(self):
        day10 = self.ar.reviewed_day("s1", "2026-04-10")
        self.assertEqual(day10["section_id"], "s1")
        self.assertEqual(day10["day"], "2026-04-10")
        self.assertEqual(day10["opening"], [])
        self.assertEqual([e["id"] for e in day10["closing"]], ["a"])
        self.assertEqual(day10["movements"], [])
        self.assertEqual(day10["anomalies"], [])
        self.assertEqual(day10["resets"], [])

        day11 = self.ar.reviewed_day("s1", "2026-04-11")
        # Midnight opening still lists a, before any reset or observation.
        self.assertEqual([e["id"] for e in day11["opening"]], ["a"])
        # r1 runs first and removes a, so b is NOT a duplicate; r2 removes b;
        # x then has no entry and is an anomaly; no movement is produced.
        self.assertEqual(
            [r["id"] for r in day11["resets"]], ["r1", "r2"]
        )
        self.assertEqual(day11["movements"], [])
        self.assertEqual(
            day11["anomalies"],
            [
                {
                    "event_id": "x",
                    "section_id": "s1",
                    "train_id": "T",
                    "type": "exit_without_entry",
                }
            ],
        )
        self.assertEqual(day11["closing"], [])

        # Resets are returned as canonical records sorted by (time, id).
        self.assertEqual(
            day11["resets"][0]["opening"],
            [
                {
                    "id": "a",
                    "train_id": "T",
                    "checkpoint": "a",
                    "time": "2026-04-10T23:59:30Z",
                    "axles": 20,
                }
            ],
        )

    def test_raw_package_keeps_original_semantics(self):
        p10 = self.ar.seal("s1", "2026-04-10", "seal-10")
        p11 = self.ar.seal("s1", "2026-04-11", "seal-11")
        # Raw package: b is a duplicate entry and x closes a.
        self.assertEqual(
            [an["event_id"] for an in p11["anomalies"]], ["b"]
        )
        self.assertEqual(p11["anomalies"][0]["type"], "duplicate_entry")
        self.assertEqual(len(p11["movements"]), 1)
        movement = p11["movements"][0]
        self.assertEqual(
            (movement["entry_event_id"], movement["exit_event_id"]),
            ("a", "x"),
        )
        self.assertEqual(p10["closing"][0]["id"], "a")

    def test_pure_reset_change_keeps_revision_package_and_take(self):
        p10 = self.ar.seal("s1", "2026-04-10", "seal-10")
        p11 = self.ar.seal("s1", "2026-04-11", "seal-11")
        receipt = self.ar.take(p10["package_id"], "take-10")

        before = self.snapshot(self.ar)
        # A legal reset-only batch: withdraw both, then register replacements.
        self.ar.revise_with_resets(
            "withdraw", 1, [], [], [], ["r1", "r2"]
        )
        r3 = reset("r3", "s1", "2026-04-11T00:00:00Z", "AUTH-3", [self.a])
        r4 = reset("r4", "s1", "2026-04-11T00:01:00Z", "AUTH-4", [self.b])
        result = self.ar.revise_with_resets(
            "replace", 1, [], [], [r3, r4], []
        )
        self.assertEqual(result["changed"], False)
        self.assertEqual(self.ar.revision, 1)
        # Packages and take eligibility are untouched by reset-only changes.
        self.assertTrue(
            self.ar.read_package(p10["package_id"])["valid"]
        )
        self.assertTrue(
            self.ar.read_package(p11["package_id"])["valid"]
        )
        self.assertTrue(
            self.ar.read_take(receipt["receipt_id"])["valid"]
        )
        # Source events and package bodies are byte-identical.
        self.assertEqual(
            self.ar._state["events"], before["events"]
        )
        self.assertEqual(
            self.ar._state["packages"], before["packages"]
        )
        self.assertEqual(
            self.ar._state["package_status"], before["package_status"]
        )

    def test_reviewed_day_creates_no_package_or_credential(self):
        self.ar.reviewed_day("s1", "2026-04-11")
        self.assertEqual(self.ar._state["packages"], {})
        self.assertEqual(self.ar._state["takes"], {})
        self.assertEqual(self.ar.revision, 1)

    def test_reviewed_day_returns_detached_copies(self):
        day11 = self.ar.reviewed_day("s1", "2026-04-11")
        day11["resets"][0]["opening"].append({"id": "tampered"})
        again = self.ar.reviewed_day("s1", "2026-04-11")
        self.assertEqual(
            [e["id"] for e in again["resets"][0]["opening"]], ["a"]
        )


class ScenarioTwoTests(ResetTestBase):
    """Acceptance item 2: evidence-protected corrections and withdrawal."""

    def _build(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-10T23:59:30Z")
        b = ev("b", "T", "a", "2026-04-11T00:00:00Z")
        x = ev("x", "T", "b", "2026-04-11T00:02:00Z")
        ar.revise("add", 0, [a, b, x], [])
        r1 = reset("r1", "s1", "2026-04-11T00:00:00Z", "AUTH-1", [a])
        r2 = reset("r2", "s1", "2026-04-11T00:01:00Z", "AUTH-2", [b])
        ar.revise_with_resets("resets", 1, [], [], [r1, r2], [])
        return ar, a, b, x, r1, r2

    def test_withdrawing_only_earlier_reset_is_rejected(self):
        ar, a, b, x, r1, r2 = self._build()
        before = self.snapshot(ar)
        # Withdrawing r1 alone leaves a open, makes b a duplicate, and removes
        # the basis of the still-active r2; the whole batch must fail.
        with self.assertRaises(ValueError) as ctx:
            ar.revise_with_resets("only-r1", 1, [], [], [], ["r1"])
        self.assertIn("r2", str(ctx.exception))
        self.assertEqual(self.snapshot(ar), before)
        self.assertTrue(ar.read_reset("r1")["active"])
        self.assertTrue(ar.read_reset("r2")["active"])

    def test_withdrawing_both_succeeds_and_keeps_history(self):
        ar, a, b, x, r1, r2 = self._build()
        result = ar.revise_with_resets(
            "both", 1, [], [], [], ["r1", "r2"]
        )
        self.assertEqual(
            result,
            {
                "revision": 1,
                "changed": False,
                "added_reset_ids": [],
                "withdrawn_reset_ids": ["r1", "r2"],
            },
        )
        self.assertFalse(ar.read_reset("r1")["active"])
        self.assertFalse(ar.read_reset("r2")["active"])
        # The canonical bodies survive withdrawal unchanged.
        self.assertEqual(
            ar.read_reset("r1")["record"]["authorization_ref"], "AUTH-1"
        )
        self.assertEqual(
            ar.read_reset("r2")["record"]["opening"][0]["id"], "b"
        )

    def test_combined_axles_correction_with_reset_replacement(self):
        # Fresh initial state in its own archive.
        self.path = os.path.join(self.root, "combo")
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-10T23:59:30Z", 20)
        b = ev("b", "T", "a", "2026-04-11T00:00:00Z")
        x = ev("x", "T", "b", "2026-04-11T00:02:00Z")
        ar.revise("add", 0, [a, b, x], [])
        r1 = reset("r1", "s1", "2026-04-11T00:00:00Z", "AUTH-1", [a])
        r2 = reset("r2", "s1", "2026-04-11T00:01:00Z", "AUTH-2", [b])
        ar.revise_with_resets("resets", 1, [], [], [r1, r2], [])

        a_fixed = ev("a", "T", "a", "2026-04-10T23:59:30Z", 24)
        # Correcting a without withdrawing r1 must fail even though the event
        # id is unchanged: the registered evidence no longer matches.
        before = self.snapshot(ar)
        with self.assertRaises(ValueError):
            ar.revise_with_resets("fix-alone", 1, [a_fixed], [], [], [])
        self.assertEqual(self.snapshot(ar), before)
        self.assertEqual(ar._state["events"]["a"]["axles"], 20)

        # One batch: fix a, withdraw r1, register r3 at the same instant citing
        # the new a; r2 stays active.
        r3 = reset(
            "r3", "s1", "2026-04-11T00:00:00Z", "AUTH-3", [a_fixed]
        )
        result = ar.revise_with_resets(
            "combo", 1, [a_fixed], [], [r3], ["r1"]
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(
            result["added_reset_ids"], ["r3"]
        )
        self.assertEqual(
            result["withdrawn_reset_ids"], ["r1"]
        )
        self.assertEqual(ar._state["events"]["a"]["axles"], 24)
        self.assertFalse(ar.read_reset("r1")["active"])
        self.assertTrue(ar.read_reset("r3")["active"])
        self.assertTrue(ar.read_reset("r2")["active"])
        self.assertEqual(
            ar.read_reset("r3")["record"]["opening"][0]["axles"], 24
        )
        # r1's historical body keeps the original 20-axle evidence.
        self.assertEqual(
            ar.read_reset("r1")["record"]["opening"][0]["axles"], 20
        )

    def test_unlisted_extra_open_entry_is_not_a_subset_match(self):
        self.path = os.path.join(self.root, "subset")
        ar = self.archive(sections=[S1])
        e_a = ev("a", "T", "a", "2026-04-10T10:00:00Z")
        extra = ev("extra", "OTHER", "a", "2026-04-10T11:00:00Z")
        ar.revise("add", 0, [e_a, extra], [])
        r = reset(
            "rr", "s1", "2026-04-10T12:00:00Z", "AUTH", [e_a]
        )
        before = self.snapshot(ar)
        with self.assertRaises(ValueError) as ctx:
            ar.revise_with_resets("subset", 1, [], [], [r], [])
        self.assertIn("rr", str(ctx.exception))
        self.assertEqual(self.snapshot(ar), before)
        self.assertNotIn("rr", ar._state["resets"])

    def test_unrelated_corrections_still_pass(self):
        ar, a, b, x, r1, r2 = self._build()
        # Another section is independent.  The build only configured s1, so use
        # a two-section archive for this case.
        self.path = os.path.join(self.root, "unrelated")
        ar = self.archive(sections=TWO_SECTIONS)
        ar.revise("add", 0, [a, b, x], [])
        ar.revise_with_resets("resets", 1, [], [], [r1, r2], [])
        s2_entry = ev("s2e", "Q", "c", "2026-04-12T10:00:00Z", 30)
        out = ar.revise_with_resets(
            "s2-corr", 1, [s2_entry], [], [], []
        )
        self.assertTrue(out["changed"])
        self.assertEqual(ar.revision, 2)
        # A correction after the section's last reset, on an already-closed
        # movement, does not touch any reset evidence.
        late = ev("late", "Z", "b", "2026-04-13T00:00:00Z", 5)
        out2 = ar.revise_with_resets(
            "late", 2, [late], [], [], []
        )
        self.assertTrue(out2["changed"])
        self.assertTrue(ar.read_reset("r1")["active"])
        self.assertTrue(ar.read_reset("r2")["active"])


class ScenarioThreeTests(ResetTestBase):
    """Acceptance item 3: reset guard over identity back-fill."""

    def _build(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-11T00:00:00Z", 20)
        x = ev("x", "local-x", "b", "2026-04-11T00:00:10Z", 20)
        ar.revise("add", 0, [a, x], [])
        plan = ar.plan_identities(
            "s1", ["x"], 0, 60, 10, 0, "plan"
        )
        return ar, a, x, plan

    def test_forced_pair_back_fills_without_reset(self):
        self.path = os.path.join(self.root, "no-reset")
        ar, a, x, plan = self._build()
        self.assertEqual(
            plan["forced_pairs"],
            [
                {
                    "entry_event_id": "a",
                    "exit_event_id": "x",
                    "train_id": "T",
                }
            ],
        )
        result = ar.apply_identities(plan["plan_id"], "apply")
        self.assertTrue(result["changed"])
        self.assertEqual(ar._state["events"]["x"]["train_id"], "T")

    def test_reset_blocks_back_fill_then_withdrawal_allows_retry(self):
        ar, a, x, plan = self._build()
        r = reset(
            "r1", "s1", "2026-04-11T00:00:30Z", "AUTH", [a]
        )
        ar.revise_with_resets("add-reset", 1, [], [], [r], [])
        revision_before = ar.revision
        ar.seal("s1", "2026-04-10", "seal-10")
        package = ar.seal("s1", "2026-04-11", "seal-11")
        receipt = ar.take(package["package_id"], "take")
        before = self.snapshot(ar)

        # Back-filling x would close a before the reset can write it off.
        with self.assertRaises(ValueError) as ctx:
            ar.apply_identities(plan["plan_id"], "apply-blocked")
        self.assertIn("r1", str(ctx.exception))
        self.assertEqual(self.snapshot(ar), before)
        self.assertEqual(
            ar._state["events"]["x"]["train_id"], "local-x"
        )
        self.assertEqual(ar.revision, revision_before)
        self.assertTrue(
            ar.read_package(package["package_id"])["valid"]
        )
        self.assertTrue(ar.read_take(receipt["receipt_id"])["valid"])

        # Explicitly withdraw the reset, then the SAME request id retries and
        # succeeds under the old back-fill rules.
        ar.revise_with_resets(
            "withdraw", 1, [], [], [], ["r1"]
        )
        result = ar.apply_identities(plan["plan_id"], "apply-blocked")
        self.assertTrue(result["changed"])
        self.assertEqual(ar._state["events"]["x"]["train_id"], "T")
        # The source revision now withdraws the raw package and take.
        self.assertFalse(
            ar.read_package(package["package_id"])["valid"]
        )
        self.assertFalse(ar.read_take(receipt["receipt_id"])["valid"])

    def test_revise_also_guards_resets(self):
        ar, a, x, plan = self._build()
        r = reset(
            "r1", "s1", "2026-04-11T00:00:30Z", "AUTH", [a]
        )
        ar.revise_with_resets("add-reset", 1, [], [], [r], [])
        # Plain revise has no reset permission and cannot weaken the guard.
        a_changed = ev("a", "T", "a", "2026-04-11T00:00:00Z", 99)
        before = self.snapshot(ar)
        with self.assertRaises(ValueError):
            ar.revise("plain-fix", 1, [a_changed], [])
        self.assertEqual(self.snapshot(ar), before)
        self.assertEqual(ar._state["events"]["a"]["axles"], 20)


class ResetValidationTests(ResetTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.ar = self.archive()
        self.a = ev("a", "T", "a", "2026-04-10T10:00:00Z")
        self.ar.revise("add", 0, [self.a], [])

    def test_bad_reset_structures_raise_value_error(self):
        good = reset(
            "r", "s1", "2026-04-10T11:00:00Z", "AUTH", [self.a]
        )
        bad_cases = [
            None,
            [],
            "reset",
            123,
            dict(good, id=""),
            dict(good, id=7),
            dict(good, section_id=""),
            dict(good, section_id="missing"),
            dict(good, authorization_ref=""),
            dict(good, authorization_ref=None),
            dict(good, time="2026-04-10T11:00:00"),  # naive
            dict(good, time="not-a-time"),
            dict(good, time="2026-05-01T00:00:00Z"),  # out of range
            dict(good, extra=1),
            {k: good[k] for k in ("id", "section_id", "time", "opening")},
            dict(good, opening=[]),
            dict(good, opening="x"),
            # Opening entry belongs to a different section.
            dict(
                good,
                opening=[ev("o", "Q", "c", "2026-04-10T09:00:00Z")],
            ),
            # Opening entry is an exit, not an entry.
            dict(
                good,
                opening=[ev("o", "T", "b", "2026-04-10T09:00:00Z")],
            ),
            # Opening entry does not strictly precede the reset.
            dict(
                good,
                time="2026-04-10T10:00:00Z",
                opening=[ev("o", "T", "a", "2026-04-10T10:00:00Z")],
            ),
            # Duplicate opening event ids.
            dict(
                good,
                opening=[
                    ev("dup", "T", "a", "2026-04-10T09:00:00Z"),
                    ev("dup", "U", "a", "2026-04-10T09:30:00Z"),
                ],
            ),
        ]
        for index, bad in enumerate(bad_cases):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.ar.revise_with_resets(
                        f"bad-{index}", 1, [], [], [bad], []
                    )
        self.assertEqual(self.ar._state["resets"], {})
        self.assertEqual(self.ar.revision, 1)

    def test_duplicate_reset_ids_in_request_rejected(self):
        r = reset("r", "s1", "2026-04-10T11:00:00Z", "AUTH", [self.a])
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets(
                "dup", 1, [], [], [r, dict(r, authorization_ref="B")], []
            )
        self.assertEqual(self.ar._state["resets"], {})

    def test_withdraw_validation(self):
        r = reset("rw", "s1", "2026-04-10T11:00:00Z", "AUTH", [self.a])
        self.ar.revise_with_resets("wv-add", 1, [], [], [r], [])
        for bad in (["rw", "rw"], [1], [""], ["missing"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.ar.revise_with_resets(
                        f"wv-{bad!r}", 1, [], [], [], bad
                    )
        # Withdrawing an already withdrawn record fails.
        self.ar.revise_with_resets("wv-w1", 1, [], [], [], ["rw"])
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets("wv-w2", 1, [], [], [], ["rw"])

    def test_reset_id_permanently_unique_and_separate_namespace(self):
        r = reset("ru1", "s1", "2026-04-10T11:00:00Z", "AUTH", [self.a])
        self.ar.revise_with_resets("ru-add", 1, [], [], [r], [])
        self.ar.revise_with_resets("ru-w", 1, [], [], [], ["ru1"])
        # Even after withdrawal, the id can never be registered again.
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets("ru-reuse", 1, [], [], [r], [])
        # Reset ids and observation ids share no uniqueness constraint.
        same_name = reset(
            "a", "s1", "2026-04-10T11:30:00Z", "AUTH", [self.a]
        )
        out = self.ar.revise_with_resets(
            "ru-same-name-as-event", 1, [], [], [same_name], []
        )
        self.assertEqual(out["added_reset_ids"], ["a"])
        self.assertIn("a", self.ar._state["events"])
        self.assertIn("a", self.ar._state["resets"])

    def test_same_instant_conflict_and_section_independence(self):
        other = ev("a2", "U", "a", "2026-04-10T09:00:00Z")
        self.ar.revise("ic-add2", 1, [other], [])
        # Two resets at the same instant of the same section cannot both be
        # active, even when their evidence would be valid at different times.
        r1 = reset("ic1", "s1", "2026-04-10T12:00:00Z", "A", [self.a, other])
        r2 = reset("ic2", "s1", "2026-04-10T12:00:00Z", "B", [self.a, other])
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets(
                "ic-conflict", 2, [], [], [r1, r2], []
            )
        # The same instant in another section is allowed.
        s2_evidence = ev("q", "Q", "c", "2026-04-10T09:00:00Z")
        self.ar.revise("ic-s2add", 2, [s2_evidence], [])
        r_s2 = reset(
            "icrs2", "s2", "2026-04-10T12:00:00Z", "C", [s2_evidence]
        )
        self.ar.revise_with_resets(
            "ic-cross", 3, [], [], [r1, r_s2], []
        )
        self.assertTrue(self.ar.read_reset("ic1")["active"])
        self.assertTrue(self.ar.read_reset("icrs2")["active"])

    def test_same_instant_replacement_inside_one_batch(self):
        r1 = reset("ir1", "s1", "2026-04-10T11:00:00Z", "A", [self.a])
        self.ar.revise_with_resets("ir-add", 1, [], [], [r1], [])
        # Withdraw r1 and add r2 at the same instant in one batch; the final
        # active set has a single owner for that instant.
        r2 = reset("ir2", "s1", "2026-04-10T11:00:00Z", "B", [self.a])
        out = self.ar.revise_with_resets(
            "ir-replace", 1, [], [], [r2], ["ir1"]
        )
        self.assertEqual(out["added_reset_ids"], ["ir2"])
        self.assertEqual(out["withdrawn_reset_ids"], ["ir1"])
        self.assertFalse(self.ar.read_reset("ir1")["active"])
        self.assertTrue(self.ar.read_reset("ir2")["active"])

    def test_active_reset_cap_uses_final_set(self):
        # Dedicated clean archive: the shared setUp entry would otherwise stay
        # open and widen every reset's required unclosed set.
        self.path = os.path.join(self.root, "cap")
        ar = self.archive(sections=TWO_SECTIONS)

        # Sequential entry/reset pairs: each entry is released by its reset
        # before the next entry opens, so every reset matches exactly one
        # unclosed entry.
        def entry(i):
            return ev(f"e{i}", f"T{i}", "a", f"2026-04-10T0{i + 1}:00:00Z", 1)

        def rec(i):
            return reset(
                f"cr{i}",
                "s1",
                f"2026-04-10T0{i + 1}:30:00Z",
                f"A{i}",
                [entry(i)],
            )

        ar.revise_with_resets("cap-pair-0", 0, [entry(0)], [], [rec(0)], [])
        ar.revise_with_resets("cap-pair-1", 1, [entry(1)], [], [rec(1)], [])
        self.assertEqual(
            len([k for k, v in ar._state["reset_active"].items() if v]),
            2,
        )

        # A third valid entry/reset pair would push the active set past a cap
        # tightened to two.  The whole batch is rejected without landing.
        with mock.patch.object(
            archive_module, "_MAX_ACTIVE_RESETS", 2
        ):
            before = self.snapshot(ar)
            with self.assertRaisesRegex(ValueError, "at most 1000 active"):
                ar.revise_with_resets(
                    "cap-over", 2, [entry(2)], [], [rec(2)], []
                )
            self.assertEqual(self.snapshot(ar), before)
            self.assertNotIn("cr2", ar._state["resets"])

            # Withdrawing a reset in another section frees a slot without
            # disturbing this section's replay, so a same-batch swap fits.
            other = ev("q", "Q", "c", "2026-04-10T01:00:00Z", 1)
            other_reset = reset(
                "cro", "s2", "2026-04-10T01:30:00Z", "AO", [other]
            )
        ar.revise_with_resets(
            "cap-other", 2, [other], [], [other_reset], []
        )
        with mock.patch.object(
            archive_module, "_MAX_ACTIVE_RESETS", 3
        ):
            out = ar.revise_with_resets(
                "cap-swap",
                3,
                [entry(2)],
                [],
                [rec(2)],
                ["cro"],
            )
        self.assertEqual(out["withdrawn_reset_ids"], ["cro"])
        self.assertEqual(out["added_reset_ids"], ["cr2"])
        self.assertTrue(ar.read_reset("cr2")["active"])
        self.assertFalse(ar.read_reset("cro")["active"])
        self.assertTrue(ar.read_reset("cr0")["active"])
        self.assertTrue(ar.read_reset("cr1")["active"])

    def test_batch_limits(self):
        r = reset("r", "s1", "2026-04-10T11:00:00Z", "A", [self.a])
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets(
                "too-many-add",
                1,
                [],
                [],
                [dict(r, id=f"m{i}") for i in range(1001)],
                [],
            )
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets(
                "too-many-w",
                1,
                [],
                [],
                [],
                [f"w{i}" for i in range(1001)],
            )
        # Wrong container types.
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets("type", 1, [], [], {}, [])
        with self.assertRaises(ValueError):
            self.ar.revise_with_resets("type2", 1, [], [], r, [])

    def test_unknown_references_and_bad_reads(self):
        with self.assertRaises(ValueError):
            self.ar.read_reset("missing")
        with self.assertRaises(ValueError):
            self.ar.read_reset(None)
        with self.assertRaises(ValueError):
            self.ar.reviewed_day("missing", "2026-04-10")
        with self.assertRaises(ValueError):
            self.ar.reviewed_day("s1", "2026-05-01")
        with self.assertRaises(ValueError):
            self.ar.reviewed_day(["s1"], "2026-04-10")


class ResetReplayTests(ResetTestBase):
    def test_utc_equivalent_and_order_insensitive_identity(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-12T00:00:00Z")
        ar.revise("add", 0, [a], [])
        r = reset("r1", "s1", "2026-04-12T01:00:00Z", "AUTH", [a])
        first = ar.revise_with_resets("req", 1, [], [], [r], [])

        # Equivalent offset spelling and a reordered opening replay the same
        # result and do not create a second record.
        equivalent = dict(
            r,
            time="2026-04-12T09:00:00+08:00",
            opening=list(reversed(r["opening"])),
        )
        replay = ar.revise_with_resets(
            "req", 1, [], [], [equivalent], []
        )
        self.assertEqual(replay, first)
        self.assertEqual(
            ar.read_reset("r1")["record"]["time"],
            "2026-04-12T01:00:00Z",
        )
        self.assertEqual(len(ar._state["resets"]), 1)

        # Withdrawal arrays and reset list order do not affect identity either.
        ar.revise_with_resets("w", 1, [], [], [], ["r1"])
        again = ar.revise_with_resets(
            "w", 1, [], [], [], list(reversed(["r1"]))
        )
        self.assertEqual(again["withdrawn_reset_ids"], ["r1"])

    def test_same_request_id_different_operation_or_params_conflicts(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-10T10:00:00Z")
        ar.revise("add", 0, [a], [])
        r = reset("r1", "s1", "2026-04-10T11:00:00Z", "AUTH", [a])
        ar.revise_with_resets("shared", 1, [], [], [r], [])
        # Same id, different parameters.
        with self.assertRaises(ValueError):
            ar.revise_with_resets(
                "shared", 1, [], [], [], ["r1"]
            )
        # Same id used by a different operation.
        with self.assertRaises(ValueError):
            ar.revise("shared", 1, [], [])
        # Original successful request still replays.
        replay = ar.revise_with_resets(
            "shared", 1, [], [], [r], []
        )
        self.assertEqual(replay["added_reset_ids"], ["r1"])

    def test_failed_request_does_not_occupy_id(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-10T10:00:00Z")
        ar.revise("add", 0, [a], [])
        bad = reset(
            "r1", "s1", "2026-04-10T09:00:00Z", "AUTH", [a]
        )  # entry not earlier
        with self.assertRaises(ValueError):
            ar.revise_with_resets("retry", 1, [], [], [bad], [])
        good = reset("r1", "s1", "2026-04-10T11:00:00Z", "AUTH", [a])
        out = ar.revise_with_resets("retry", 1, [], [], [good], [])
        self.assertEqual(out["added_reset_ids"], ["r1"])
        self.assertIn("retry", ar._state["requests"])

    def test_revision_conflict_and_expected_revision_types(self):
        ar = self.archive()
        for bad in (True, -1, 1.0, "0", None):
            with self.assertRaises(ValueError):
                ar.revise_with_resets(
                    f"bad-{bad!r}", bad, [], [], [], []
                )
        with self.assertRaises(ValueError):
            ar.revise_with_resets("stale", 99, [], [], [], [])


class EmptyDayAndMidnightTests(ResetTestBase):
    def test_entry_carries_across_empty_days_into_reset(self):
        ar = self.archive(
            sections=[S1], start="2026-05-01", end="2026-05-04"
        )
        entry = ev("long", "L", "a", "2026-05-01T23:00:00Z", 12)
        ar.revise("add", 0, [entry], [])
        r = reset(
            "r1", "s1", "2026-05-04T00:00:00Z", "AUTH", [entry]
        )
        ar.revise_with_resets("reset", 1, [], [], [r], [])

        may2 = ar.reviewed_day("s1", "2026-05-02")
        may3 = ar.reviewed_day("s1", "2026-05-03")
        may4 = ar.reviewed_day("s1", "2026-05-04")
        self.assertEqual([e["id"] for e in may2["opening"]], ["long"])
        self.assertEqual([e["id"] for e in may2["closing"]], ["long"])
        self.assertEqual(may2["movements"], [])
        self.assertEqual([e["id"] for e in may3["opening"]], ["long"])
        # Midnight reset on the 4th sees the carried entry and writes it off.
        self.assertEqual([e["id"] for e in may4["opening"]], ["long"])
        self.assertEqual(may4["closing"], [])
        self.assertEqual([x["id"] for x in may4["resets"]], ["r1"])

    def test_midnight_head_tail_and_reset_before_event(self):
        ar = self.archive(
            sections=[S1], start="2026-04-10", end="2026-04-11"
        )
        # An entry and a reset at the same first-day midnight instant: the
        # reset precedes the observation, so nothing is open yet and it fails.
        entry = ev("e", "T", "a", "2026-04-10T00:00:00Z")
        ar.revise("add", 0, [entry], [])
        r = reset(
            "r1", "s1", "2026-04-10T00:00:00Z", "AUTH", [entry]
        )
        with self.assertRaises(ValueError):
            ar.revise_with_resets("head", 1, [], [], [r], [])

        # One second later the entry is open and the same-evidence reset works.
        r_ok = reset(
            "r2", "s1", "2026-04-10T00:00:01Z", "AUTH", [entry]
        )
        out = ar.revise_with_resets("ok", 1, [], [], [r_ok], [])
        self.assertEqual(out["added_reset_ids"], ["r2"])
        day = ar.reviewed_day("s1", "2026-04-10")
        self.assertEqual(day["opening"], [])
        self.assertEqual(day["closing"], [])


class ReopenAndCompatibilityTests(ResetTestBase):
    def test_state_persists_across_reopen(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-10T10:00:00Z")
        ar.revise("rp-add-event", 0, [a], [])
        r = reset("rp1", "s1", "2026-04-10T11:00:00Z", "AUTH", [a])
        ar.revise_with_resets("rp-add", 1, [], [], [r], [])
        ar.revise_with_resets("rp-w", 1, [], [], [], ["rp1"])

        reopened = self.reopen(sections=[S1])
        self.assertEqual(reopened.revision, 1)
        self.assertFalse(reopened.read_reset("rp1")["active"])
        self.assertEqual(
            reopened.read_reset("rp1")["record"]["authorization_ref"], "AUTH"
        )
        review = reopened.reviewed_day("s1", "2026-04-10")
        self.assertEqual([e["id"] for e in review["closing"]], ["a"])

    def test_legacy_archive_without_reset_keys_reopens(self):
        ar = self.archive(sections=[S1])
        a = ev("a", "T", "a", "2026-04-10T10:00:00Z")
        ar.revise("add", 0, [a], [])
        del ar._state["resets"]
        del ar._state["reset_active"]
        with open(
            os.path.join(self.path, "reconciliation-archive.json"), "w"
        ) as handle:
            json.dump(ar._state, handle)

        reopened = self.reopen(sections=[S1])
        self.assertEqual(reopened.revision, 1)
        r = reset("r1", "s1", "2026-04-10T11:00:00Z", "AUTH", [a])
        out = reopened.revise_with_resets(
            "legacy", 1, [], [], [r], []
        )
        self.assertEqual(out["added_reset_ids"], ["r1"])
        self.assertTrue(
            self.reopen(sections=[S1]).read_reset("r1")["active"]
        )


CRASH_WORKER = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])
from axle_reconciliation import ReconciliationArchive

path = sys.argv[2]
command = sys.argv[3]
section = {"id": "s1", "entry": "a", "exit": "b"}


def ev(eid, train, checkpoint, time_text, axles=20):
    return {
        "id": eid, "train_id": train, "checkpoint": checkpoint,
        "time": time_text, "axles": axles,
    }


def open_archive():
    return ReconciliationArchive(path, [section], "2026-04-10", "2026-04-13")


def emit(value):
    print("WORKER_RESULT " + json.dumps(value, sort_keys=True))


a = ev("a", "T", "a", "2026-04-10T23:59:30Z")
b = ev("b", "T", "a", "2026-04-11T00:00:00Z")
a24 = ev("a", "T", "a", "2026-04-10T23:59:30Z", 24)
r1 = {
    "id": "r1", "section_id": "s1",
    "time": "2026-04-11T00:00:00Z",
    "authorization_ref": "AUTH-1", "opening": [a],
}
r3 = {
    "id": "r3", "section_id": "s1",
    "time": "2026-04-11T00:00:00Z",
    "authorization_ref": "AUTH-3", "opening": [a24],
}

if command == "setup":
    archive = open_archive()
    archive.revise("add", 0, [a, b], [])
    archive.revise_with_resets("resets", 1, [], [], [r1], [])
    archive.seal("s1", "2026-04-10", "seal-10")
    package = archive.seal("s1", "2026-04-11", "seal-11")
    archive.take(package["package_id"], "take-11")
    emit("ready")
elif command == "combo":
    archive = open_archive()
    emit(archive.revise_with_resets(
        "combo-req", 1, [a24], [], [r3], ["r1"]
    ))
elif command == "replay":
    archive = open_archive()
    emit(archive.revise_with_resets(
        "combo-req", 1, [a24], [], [r3], ["r1"]
    ))
else:
    raise SystemExit("unknown worker command")
"""


class ResetCrashRecoveryTests(unittest.TestCase):
    def _run(self, path, command, fault=None, fault_request="combo-req"):
        env = os.environ.copy()
        env.pop("RECONCILIATION_ARCHIVE_FAULT", None)
        env.pop("RECONCILIATION_ARCHIVE_FAULT_REQUEST", None)
        if fault is not None:
            env["RECONCILIATION_ARCHIVE_FAULT"] = fault
            env["RECONCILIATION_ARCHIVE_FAULT_REQUEST"] = fault_request
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                CRASH_WORKER,
                str(SRC),
                path,
                command,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    @staticmethod
    def _result(proc):
        lines = [
            line
            for line in proc.stdout.splitlines()
            if line.startswith("WORKER_RESULT ")
        ]
        assert lines, proc.stderr + "\n" + proc.stdout
        return json.loads(lines[-1].split(" ", 1)[1])

    def test_combined_commit_is_atomic_across_process_death(self):
        section = [{"id": "s1", "entry": "a", "exit": "b"}]
        expected = {
            "revision": 2,
            "changed": True,
            "added_reset_ids": ["r3"],
            "withdrawn_reset_ids": ["r1"],
        }
        for point, code, persisted in (
            ("mid_write", 86, False),
            ("before_replace", 87, False),
            ("after_replace", 88, True),
        ):
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = os.path.join(temp_dir, "archive")
                    self.assertEqual(
                        self._run(path, "setup").returncode, 0
                    )
                    crashed = self._run(path, "combo", fault=point)
                    self.assertEqual(crashed.returncode, code, crashed.stderr)

                    ar = ReconciliationArchive(
                        path, section, "2026-04-10", "2026-04-13"
                    )
                    if persisted:
                        self.assertEqual(ar.revision, 2)
                        self.assertEqual(
                            ar._state["events"]["a"]["axles"], 24
                        )
                        self.assertFalse(
                            ar.read_reset("r1")["active"]
                        )
                        self.assertTrue(ar.read_reset("r3")["active"])
                        self.assertEqual(
                            ar.read_reset("r3")["record"]["opening"][0][
                                "axles"
                            ],
                            24,
                        )
                    else:
                        self.assertEqual(ar.revision, 1)
                        self.assertEqual(
                            ar._state["events"]["a"]["axles"], 20
                        )
                        self.assertTrue(ar.read_reset("r1")["active"])
                        self.assertNotIn("r3", ar._state["resets"])
                        self.assertNotIn("combo-req", ar._state["requests"])
                        self.assertTrue(
                            ar.read_package("package-2")["valid"]
                        )
                        self.assertTrue(
                            ar.read_take("receipt-1")["valid"]
                        )

                    # Response-loss replay returns the exact first result.
                    replay = self._run(path, "replay")
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    self.assertEqual(self._result(replay), expected)

                    reopened = ReconciliationArchive(
                        path, section, "2026-04-10", "2026-04-13"
                    )
                    self.assertEqual(reopened.revision, 2)
                    self.assertTrue(
                        reopened.read_reset("r3")["active"]
                    )
                    self.assertFalse(
                        reopened.read_reset("r1")["active"]
                    )
                    # Historical reset body and raw package take state survive.
                    self.assertEqual(
                        reopened.read_reset("r1")["record"]["opening"][0][
                            "axles"
                        ],
                        20,
                    )


if __name__ == "__main__":
    unittest.main()
