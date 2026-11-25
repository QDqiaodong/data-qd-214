from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from axle_reconciliation import ReconciliationArchive, process
from axle_reconciliation import archive as archive_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"

S1_SECTION = {"id": "s1", "entry": "a", "exit": "b"}
S2_SECTION = {"id": "s2", "entry": "c", "exit": "d"}
S3_SECTION = {"id": "s3", "entry": "e", "exit": "f"}
TWO_SECTIONS = [S1_SECTION, S2_SECTION]
THREE_SECTIONS = [S1_SECTION, S2_SECTION, S3_SECTION]


def event(eid, train, checkpoint, time, axles=20):
    return {
        "id": eid,
        "train_id": train,
        "checkpoint": checkpoint,
        "time": time,
        "axles": axles,
    }


class ArchiveTestBase(unittest.TestCase):
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


class CrossDayFactTests(ArchiveTestBase):
    def test_midnight_exit_closes_previous_day_entry_with_60_seconds(self):
        ar = self.archive(sections=[S1_SECTION], end="2026-04-11")
        e1 = event("e1", "T", "a", "2026-04-10T23:59:30Z")
        e2 = event("e2", "T", "b", "2026-04-11T00:00:30Z")
        self.assertEqual(
            ar.revise("add", 0, [e1, e2], []),
            {"revision": 1, "changed": True},
        )

        day10 = ar.seal("s1", "2026-04-10", "seal-10")
        day11 = ar.seal("s1", "2026-04-11", "seal-11")

        self.assertIsNone(day10["parent_package_id"])
        self.assertEqual(day10["revision"], 1)
        self.assertEqual([e["id"] for e in day10["events"]], ["e1"])
        self.assertEqual([e["id"] for e in day10["closing"]], ["e1"])

        self.assertEqual(day11["parent_package_id"], day10["package_id"])
        self.assertEqual([e["id"] for e in day11["opening"]], ["e1"])
        self.assertEqual([e["id"] for e in day11["events"]], ["e2"])
        movement = day11["movements"][0]
        self.assertEqual(
            (
                movement["status"],
                movement["duration_seconds"],
                movement["entry_event_id"],
            ),
            ("balanced", 60, "e1"),
        )
        self.assertEqual(day11["closing"], [])

        # Returned documents are detached copies.
        day11["opening"].append({"id": "mutated"})
        self.assertEqual(
            len(ar.current("s1", "2026-04-11")["opening"]), 1
        )

    def test_empty_middle_day_carries_real_entry_across_full_day(self):
        ar = self.archive(
            sections=[S1_SECTION], start="2026-05-01", end="2026-05-03"
        )
        entry = event(
            "long-entry", "L", "a", "2026-05-01T23:00:00Z", axles=12
        )
        exit_event = event(
            "long-exit", "L", "b", "2026-05-03T01:00:00Z", axles=12
        )
        ar.revise("add", 0, [entry, exit_event], [])

        may1 = ar.seal("s1", "2026-05-01", "seal-0501")
        may2 = ar.seal("s1", "2026-05-02", "seal-0502")
        may3 = ar.seal("s1", "2026-05-03", "seal-0503")

        self.assertEqual(may2["parent_package_id"], may1["package_id"])
        self.assertEqual(may2["events"], [])
        self.assertEqual(may2["opening"], may1["closing"])
        self.assertEqual([e["id"] for e in may2["closing"]], ["long-entry"])

        movement = may3["movements"][0]
        self.assertEqual(movement["entry_event_id"], "long-entry")
        self.assertEqual(movement["duration_seconds"], 26 * 60 * 60)
        self.assertEqual(may3["closing"], [])

    def test_zero_event_day_can_seal_at_initial_revision(self):
        ar = self.archive(sections=[S1_SECTION])
        package = ar.seal("s1", "2026-04-10", "empty-seal")
        self.assertEqual(package["revision"], 0)
        self.assertEqual(package["events"], [])
        self.assertEqual(package["opening"], [])
        self.assertEqual(package["closing"], [])
        self.assertIsNone(package["parent_package_id"])

    def test_duplicate_entry_keeps_cross_day_opening_and_mismatch_still_closes(self):
        ar = self.archive(
            sections=[S1_SECTION], start="2026-06-01", end="2026-06-03"
        )
        e1 = event("e1", "T", "a", "2026-06-01T23:59:00Z", 20)
        e2 = event("e2", "T", "a", "2026-06-02T00:01:00Z", 20)
        e3 = event("e3", "T", "b", "2026-06-02T00:02:00Z", 19)
        ar.revise("add", 0, [e1, e2, e3], [])

        june1 = ar.seal("s1", "2026-06-01", "seal-0601")
        june2 = ar.seal("s1", "2026-06-02", "seal-0602")
        june3 = ar.seal("s1", "2026-06-03", "seal-0603")

        self.assertEqual([e["id"] for e in june1["closing"]], ["e1"])
        self.assertEqual([e["id"] for e in june2["opening"]], ["e1"])
        self.assertEqual(june2["anomalies"][0]["event_id"], "e2")
        self.assertEqual(june2["anomalies"][0]["type"], "duplicate_entry")
        movement = june2["movements"][0]
        self.assertEqual(movement["entry_event_id"], "e1")
        self.assertEqual(movement["status"], "axle_mismatch")
        self.assertEqual(movement["duration_seconds"], 180)
        self.assertEqual(june2["closing"], [])

        # The following handoff visibly starts and ends with no open entry.
        self.assertEqual(june3["opening"], [])
        self.assertEqual(june3["closing"], [])
        self.assertEqual(june3["parent_package_id"], june2["package_id"])


class WithdrawalChainTests(ArchiveTestBase):
    def test_source_replacement_withdraws_only_that_section_chain_and_takes(self):
        ar = self.archive(sections=TWO_SECTIONS, end="2026-04-11")
        e1 = event("e1", "T", "a", "2026-04-10T23:59:30Z")
        e2 = event("e2", "T", "b", "2026-04-11T00:00:30Z")
        s2_entry = event("s2e1", "Q", "c", "2026-04-10T12:00:00Z", 30)
        s2_exit = event("s2e2", "Q", "d", "2026-04-10T13:00:00Z", 30)
        ar.revise("add", 0, [e1, e2, s2_entry, s2_exit], [])

        s1_10 = ar.seal("s1", "2026-04-10", "seal-s1-10")
        s1_11 = ar.seal("s1", "2026-04-11", "seal-s1-11")
        s2_10 = ar.seal("s2", "2026-04-10", "seal-s2-10")

        take_10 = ar.take(s1_10["package_id"], "take-s1-10")
        take_11 = ar.take(s1_11["package_id"], "take-s1-11")
        take_s2 = ar.take(s2_10["package_id"], "take-s2")

        new_e1 = event("e1-new", "T", "a", "2026-04-10T23:59:30Z")
        self.assertEqual(
            ar.revise("fix-e1", 1, [new_e1], ["e1"]),
            {"revision": 2, "changed": True},
        )

        old_10 = ar.read_package(s1_10["package_id"])
        old_11 = ar.read_package(s1_11["package_id"])
        self.assertFalse(old_10["valid"])
        self.assertFalse(old_11["valid"])
        self.assertEqual(old_10["invalidated_by_revision"], 2)
        self.assertEqual(old_11["invalidated_by_revision"], 2)
        self.assertEqual(old_10["package"]["closing"][0]["id"], "e1")
        self.assertEqual(old_11["package"]["opening"][0]["id"], "e1")
        self.assertEqual(
            old_11["package"]["movements"][0]["entry_event_id"], "e1"
        )

        for receipt in (take_10, take_11):
            status = ar.read_take(receipt["receipt_id"])
            self.assertFalse(status["valid"])
            self.assertEqual(status["invalidated_by_revision"], 2)

        self.assertEqual(ar.current("s2", "2026-04-10")["package_id"],
                         s2_10["package_id"])
        s2_take_status = ar.read_take(take_s2["receipt_id"])
        self.assertTrue(s2_take_status["valid"])
        self.assertIsNone(s2_take_status["invalidated_by_revision"])

        # Cannot skip the withdrawn previous day. This failed request id is
        # not stored and can be reused after the parent is resealed.
        with self.assertRaises(ValueError):
            ar.seal("s1", "2026-04-11", "reseal-s1-11")

        new_10 = ar.seal("s1", "2026-04-10", "reseal-s1-10")
        new_11 = ar.seal("s1", "2026-04-11", "reseal-s1-11")
        self.assertNotEqual(new_10["package_id"], s1_10["package_id"])
        self.assertEqual(new_11["parent_package_id"], new_10["package_id"])
        self.assertEqual(new_11["opening"][0]["id"], "e1-new")
        self.assertEqual(
            new_11["movements"][0]["entry_event_id"], "e1-new"
        )

        new_take = ar.take(new_11["package_id"], "take-new-11")
        self.assertTrue(ar.read_take(new_take["receipt_id"])["valid"])

        # Reopen and verify the same validity boundary from disk.
        reopened = ReconciliationArchive(
            self.path, TWO_SECTIONS, "2026-04-10", "2026-04-11"
        )
        self.assertEqual(reopened.revision, 2)
        self.assertFalse(
            reopened.read_package(s1_10["package_id"])["valid"]
        )
        self.assertTrue(
            reopened.read_package(new_11["package_id"])["valid"]
        )
        self.assertFalse(
            reopened.read_take(take_10["receipt_id"])["valid"]
        )
        self.assertTrue(
            reopened.read_take(new_take["receipt_id"])["valid"]
        )

    def test_moving_event_between_sections_and_days_withdraws_each_chain_from_its_day(self):
        ar = self.archive(sections=THREE_SECTIONS)
        moved = event(
            "move-me", "M", "a", "2026-04-11T10:00:00Z", axles=7
        )
        ar.revise("add", 0, [moved], [])

        days = ["2026-04-10", "2026-04-11", "2026-04-12", "2026-04-13"]
        packages = {}
        for sid in ("s1", "s2", "s3"):
            for day in days:
                packages[sid, day] = ar.seal(
                    sid, day, f"seal-{sid}-{day}"
                )

        ar.take(packages["s2", "2026-04-11"]["package_id"], "keep-s2-11")
        ar.take(packages["s3", "2026-04-12"]["package_id"], "keep-s3-12")

        relocated = dict(
            moved,
            checkpoint="c",
            time="2026-04-12T10:00:00+00:00",
        )
        self.assertEqual(
            ar.revise("move-event", 1, [relocated], []),
            {"revision": 2, "changed": True},
        )

        self.assertTrue(
            ar.read_package(packages["s1", "2026-04-10"]["package_id"])["valid"]
        )
        for day in days[1:]:
            status = ar.read_package(packages["s1", day]["package_id"])
            self.assertFalse(status["valid"], day)
            self.assertEqual(status["invalidated_by_revision"], 2)
        with self.assertRaises(ValueError):
            ar.current("s1", "2026-04-11")

        self.assertTrue(
            ar.read_package(packages["s2", "2026-04-10"]["package_id"])["valid"]
        )
        self.assertTrue(
            ar.read_package(packages["s2", "2026-04-11"]["package_id"])["valid"]
        )
        self.assertFalse(
            ar.read_package(packages["s2", "2026-04-12"]["package_id"])["valid"]
        )
        self.assertFalse(
            ar.read_package(packages["s2", "2026-04-13"]["package_id"])["valid"]
        )
        self.assertTrue(ar.read_take("receipt-1")["valid"])

        for day in days:
            self.assertTrue(
                ar.read_package(packages["s3", day]["package_id"])["valid"],
                day,
            )
        self.assertTrue(ar.read_take("receipt-2")["valid"])


class ReplayAndAtomicityTests(ArchiveTestBase):
    def test_equivalent_utc_offset_and_array_order_replay_same_result(self):
        ar = self.archive(sections=[S1_SECTION], end="2026-04-11")
        e1 = event("e1", "T", "a", "2026-04-10T23:59:30Z")
        first = ar.revise("r1", 0, [e1], [])
        equivalent = dict(
            e1, time="2026-04-10T19:59:30-04:00"
        )  # same UTC instant
        replay = ar.revise("r1", 0, [equivalent], [])
        self.assertEqual(first, {"revision": 1, "changed": True})
        self.assertEqual(replay, first)
        self.assertEqual(ar.revision, 1)

        e2 = event("e2", "U", "a", "2026-04-10T20:00:00Z")
        e3 = event("e3", "V", "a", "2026-04-10T21:00:00Z")
        ar.revise("add-more", 1, [e2, e3], [])
        delete_first = ar.revise("delete-two", 2, [], ["e1", "e2"])
        delete_reversed = ar.revise(
            "delete-two", 2, [], ["e2", "e1"]
        )
        self.assertEqual(
            delete_first, {"revision": 3, "changed": True}
        )
        self.assertEqual(delete_reversed, delete_first)
        self.assertEqual(ar.revision, 3)

    def test_new_equivalent_upsert_is_noop_and_keeps_package_and_take(self):
        ar = self.archive(sections=[S1_SECTION], end="2026-04-11")
        e1 = event("e1", "T", "a", "2026-04-10T23:59:30.500000+00:00")
        ar.revise("add", 0, [e1], [])
        package = ar.seal("s1", "2026-04-10", "seal")
        receipt = ar.take(package["package_id"], "take")

        equivalent = dict(e1, time="2026-04-10T19:59:30.500-04:00")
        result = ar.revise("noop", 1, [equivalent], [])
        self.assertEqual(result, {"revision": 1, "changed": False})
        self.assertEqual(ar.revision, 1)
        self.assertTrue(ar.read_package(package["package_id"])["valid"])
        self.assertTrue(ar.read_take(receipt["receipt_id"])["valid"])
        self.assertEqual(
            ar.current("s1", "2026-04-10")["package_id"],
            package["package_id"],
        )
        # Canonical sub-second output uses UTC Z and does not lose precision.
        current = ar.current("s1", "2026-04-10")
        self.assertEqual(
            current["events"][0]["time"],
            "2026-04-10T23:59:30.500000Z",
        )

    def test_invalid_event_rejects_entire_batch_and_request_id_can_retry(self):
        ar = self.archive(sections=[S1_SECTION])
        good = event("good", "T", "a", "2026-04-10T10:00:00Z")
        ar.revise("initial", 0, [good], [])

        added = event("added", "N", "a", "2026-04-10T11:00:00Z")
        bad = event("bad", "B", "a", "2026-04-10T12:00:00Z", axles=0)
        with self.assertRaises(ValueError):
            ar.revise("atomic-request", 1, [added, bad], [])

        self.assertEqual(ar.revision, 1)
        # The failed request id was not occupied; corrected parameters succeed.
        result = ar.revise("atomic-request", 1, [added], [])
        self.assertEqual(result, {"revision": 2, "changed": True})
        self.assertEqual(ar.revision, 2)

    def test_replay_original_results_after_withdrawal_and_later_revision(self):
        ar = self.archive(sections=[S1_SECTION], end="2026-04-11")
        e1 = event("e1", "T", "a", "2026-04-10T23:59:30Z")
        e2 = event("e2", "T", "b", "2026-04-11T00:00:30Z")
        add_result = ar.revise("add", 0, [e1, e2], [])
        p10 = ar.seal("s1", "2026-04-10", "seal-10")
        p11 = ar.seal("s1", "2026-04-11", "seal-11")
        take10 = ar.take(p10["package_id"], "take-10")
        ar.take(p11["package_id"], "take-11")

        new_e1 = event("e1-new", "T", "a", "2026-04-10T23:59:30Z")
        fix_result = ar.revise("fix", 1, [new_e1], ["e1"])

        later = event("later-exit", "Z", "b", "2026-04-10T12:00:00Z", 1)
        ar.revise("advance", 2, [later], [])
        self.assertEqual(ar.revision, 3)

        # Historical successful results are replayed verbatim.
        self.assertEqual(
            ar.revise("add", 0, [e1, e2], []), add_result
        )
        self.assertEqual(
            ar.revise("fix", 1, [new_e1], ["e1"]), fix_result
        )
        self.assertEqual(ar.seal("s1", "2026-04-10", "seal-10"), p10)
        self.assertEqual(ar.take(p10["package_id"], "take-10"), take10)

        # Replay does not restore current eligibility.
        self.assertFalse(ar.read_package(p10["package_id"])["valid"])
        self.assertFalse(ar.read_take(take10["receipt_id"])["valid"])
        with self.assertRaises(ValueError):
            ar.take(p10["package_id"], "brand-new-take")

        # Identity conflicts remain errors, including attempted no-op bypass.
        with self.assertRaises(ValueError):
            ar.revise("fix", 2, [new_e1], ["e1"])
        with self.assertRaises(ValueError):
            ar.revise(
                "fix",
                1,
                [dict(new_e1, axles=21)],
                ["e1"],
            )
        with self.assertRaises(ValueError):
            ar.revise("fix", 1, [], [])
        with self.assertRaises(ValueError):
            ar.seal("s1", "2026-04-11", "fix")
        with self.assertRaises(ValueError):
            ar.take(p11["package_id"], "take-10")


class ValidationConfigCompatibilityTests(ArchiveTestBase):
    def test_constructor_date_and_section_validation(self):
        with self.assertRaises(ValueError):
            ReconciliationArchive(
                os.path.join(self.root, "bad-sections"),
                {},
                "2026-04-10",
                "2026-04-11",
            )
        with self.assertRaises(ValueError):
            ReconciliationArchive(
                os.path.join(self.root, "empty-sections"),
                [],
                "2026-04-10",
                "2026-04-11",
            )
        with self.assertRaises(ValueError):
            ReconciliationArchive(
                os.path.join(self.root, "bad-day"),
                [S1_SECTION],
                "2026-4-1",
                "2026-04-11",
            )
        with self.assertRaises(ValueError):
            ReconciliationArchive(
                os.path.join(self.root, "too-long"),
                [S1_SECTION],
                "2026-04-10",
                "2027-04-11",
            )

    def test_reopen_restores_and_rejects_config_mismatch_without_overwrite(self):
        ar = self.archive()
        e1 = event("e1", "T", "a", "2026-04-10T10:00:00Z")
        ar.revise("r1", 0, [e1], [])
        ar.seal("s1", "2026-04-10", "s1")

        reopened = ReconciliationArchive(
            self.path,
            list(reversed(TWO_SECTIONS)),
            "2026-04-10",
            "2026-04-13",
        )
        self.assertEqual(reopened.revision, 1)
        self.assertEqual(
            reopened.current("s1", "2026-04-10")["events"][0]["id"],
            "e1",
        )

        with self.assertRaises(ValueError):
            ReconciliationArchive(
                self.path, [S1_SECTION], "2026-04-10", "2026-04-13"
            )
        with self.assertRaises(ValueError):
            ReconciliationArchive(
                self.path,
                TWO_SECTIONS,
                "2026-04-11",
                "2026-04-13",
            )
        changed_section = dict(S1_SECTION, entry="other")
        with self.assertRaises(ValueError):
            ReconciliationArchive(
                self.path,
                [changed_section, S2_SECTION],
                "2026-04-10",
                "2026-04-13",
            )

        # Correct configuration still reopens with the original data.
        again = ReconciliationArchive(
            self.path, TWO_SECTIONS, "2026-04-10", "2026-04-13"
        )
        self.assertEqual(again.revision, 1)

    def test_existing_directory_and_explicit_json_file_paths_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_archive = ReconciliationArchive(
                directory, [S1_SECTION], "2026-04-10", "2026-04-10"
            )
            directory_archive.revise(
                "r",
                0,
                [event("e1", "T", "a", "2026-04-10T10:00:00Z")],
                [],
            )
            ReconciliationArchive(
                directory, [S1_SECTION], "2026-04-10", "2026-04-10"
            )

            file_path = os.path.join(directory, "snapshot.json")
            file_archive = ReconciliationArchive(
                file_path, [S1_SECTION], "2026-04-10", "2026-04-10"
            )
            file_archive.seal("s1", "2026-04-10", "seal")
            reopened_file = ReconciliationArchive(
                file_path, [S1_SECTION], "2026-04-10", "2026-04-10"
            )
            self.assertEqual(
                reopened_file.current("s1", "2026-04-10")["revision"], 0
            )

            placeholder = os.path.join(directory, "empty.json")
            open(placeholder, "wb").close()
            ReconciliationArchive(
                placeholder, [S1_SECTION], "2026-04-10", "2026-04-10"
            )

    def test_invalid_revision_types_and_conflict(self):
        ar = self.archive(sections=[S1_SECTION])
        for bad in (True, False, -1, 1.0, "0", None, [0]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ar.revise(f"bad-revision-{bad!r}", bad, [], [])
        with self.assertRaises(ValueError):
            ar.revise("conflict", 1, [], [])

    def test_invalid_events_and_request_arrays_raise_value_error(self):
        ar = self.archive(sections=[S1_SECTION])
        valid = event("ok", "T", "a", "2026-04-10T10:00:00Z")
        bad_events = [
            None,
            [],
            "event",
            123,
            dict(valid, id=""),
            dict(valid, id=7),
            dict(valid, train_id=""),
            dict(valid, train_id=4),
            dict(valid, checkpoint="unknown"),
            dict(valid, checkpoint=None),
            dict(valid, axles=True),
            dict(valid, axles=0),
            dict(valid, axles=1001),
            dict(valid, axles=20.0),
            dict(valid, time=None),
            dict(valid, time=123),
            dict(valid, time="2026-04-10T10:00:00"),
            dict(valid, time="not-a-time"),
            dict(valid, time="2026-05-01T00:00:00Z"),
            dict(valid, extra="not allowed"),
            {k: valid[k] for k in ("id", "train_id", "checkpoint", "time")},
        ]
        for index, bad_event in enumerate(bad_events):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    ar.revise(f"bad-event-{index}", 0, [bad_event], [])

        with self.assertRaises(ValueError):
            ar.revise("duplicate-upserts", 0, [valid, valid], [])
        with self.assertRaises(ValueError):
            ar.revise("duplicate-deletes", 0, [], ["x", "x"])
        with self.assertRaises(ValueError):
            ar.revise("overlap", 0, [valid], [valid["id"]])
        with self.assertRaises(ValueError):
            ar.revise("bad-delete-type", 0, [], [1])
        with self.assertRaises(ValueError):
            ar.revise("upserts-not-list", 0, {}, [])
        with self.assertRaises(ValueError):
            ar.revise("deletes-not-list", 0, [], {})
        with self.assertRaises(ValueError):
            ar.revise("too-many-upserts", 0, [object()] * 100001, [])
        with self.assertRaises(ValueError):
            ar.revise("too-many-deletes", 0, [], ["x"] * 100001)

        self.assertEqual(ar.revision, 0)

    def test_post_commit_event_limit_uses_public_revision_boundary(self):
        ar = self.archive(sections=[S1_SECTION])
        first_three = [
            event(f"e{i}", f"T{i}", "a", f"2026-04-10T1{i}:00:00Z", 1)
            for i in range(3)
        ]
        with mock.patch.object(archive_module, "_MAX_ITEMS", 3):
            ar.revise("three", 0, first_three, [])
            fourth = event("e4", "T4", "a", "2026-04-12T10:00:00Z", 1)
            with self.assertRaises(ValueError):
                ar.revise("fourth", 1, [fourth], [])
        self.assertEqual(ar.revision, 1)

    def test_unknown_references_and_type_errors_become_value_errors(self):
        ar = self.archive(sections=[S1_SECTION])
        with self.assertRaises(ValueError):
            ar.seal("missing", "2026-04-10", "r")
        with self.assertRaises(ValueError):
            ar.seal("s1", "2026-05-01", "r")
        with self.assertRaises(ValueError):
            ar.current("missing", "2026-04-10")
        with self.assertRaises(ValueError):
            ar.current("s1", "2026-05-01")
        with self.assertRaises(ValueError):
            ar.current(["s1"], None)
        with self.assertRaises(ValueError):
            ar.read_package("package-404")
        with self.assertRaises(ValueError):
            ar.read_package(["package-1"])
        with self.assertRaises(ValueError):
            ar.take("package-404", "take")
        with self.assertRaises(ValueError):
            ar.read_take("receipt-404")
        with self.assertRaises(ValueError):
            ar.read_take({"receipt_id": "x"})

    def test_baseline_process_keeps_empty_events_rejected(self):
        data = {
            "sections": [S1_SECTION],
            "events": [
                event("e2", "T", "b", "2026-04-10T00:02:00Z"),
                event("e1", "T", "a", "2026-04-10T00:00:00Z"),
            ],
        }
        self.assertEqual(process(data)["movements"][0]["duration_seconds"], 120)
        data["events"] = []
        with self.assertRaises(ValueError):
            process(data)


CRASH_WORKER = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])
from axle_reconciliation import ReconciliationArchive

path = sys.argv[2]
command = sys.argv[3]
sections = [{"id": "s1", "entry": "a", "exit": "b"}]
e1 = {
    "id": "e1", "train_id": "T", "checkpoint": "a",
    "time": "2026-04-10T23:59:30Z", "axles": 20,
}
e2 = {
    "id": "e2", "train_id": "T", "checkpoint": "b",
    "time": "2026-04-11T00:00:30Z", "axles": 20,
}
e1_new = dict(e1, id="e1-new")

def open_archive():
    return ReconciliationArchive(path, sections, "2026-04-10", "2026-04-11")

def emit(value):
    print("WORKER_RESULT " + json.dumps(value, sort_keys=True))

if command == "correct":
    ar = open_archive()
    ar.revise("add", 0, [e1, e2], [])
    p10 = ar.seal("s1", "2026-04-10", "seal-10")
    p11 = ar.seal("s1", "2026-04-11", "seal-11")
    ar.take(p10["package_id"], "take-10")
    ar.take(p11["package_id"], "take-11")
    emit(ar.revise("correct", 1, [e1_new], ["e1"]))
elif command == "replay-correct":
    ar = open_archive()
    emit(ar.revise("correct", 1, [e1_new], ["e1"]))
elif command == "seal":
    ar = open_archive()
    ar.revise("add", 0, [e1], [])
    emit(ar.seal("s1", "2026-04-10", "seal-crash"))
elif command == "replay-seal":
    ar = open_archive()
    emit(ar.seal("s1", "2026-04-10", "seal-crash"))
elif command == "take":
    ar = open_archive()
    ar.revise("add", 0, [e1], [])
    package = ar.seal("s1", "2026-04-10", "seal-for-take")
    emit(ar.take(package["package_id"], "take-crash"))
elif command == "replay-take":
    ar = open_archive()
    emit(ar.take("package-1", "take-crash"))
else:
    raise SystemExit("unknown worker command")
"""


class CrashRecoveryTests(unittest.TestCase):
    def _run_worker(self, path, command, fault_point=None, fault_request=None):
        env = os.environ.copy()
        env.pop("RECONCILIATION_ARCHIVE_FAULT", None)
        env.pop("RECONCILIATION_ARCHIVE_FAULT_REQUEST", None)
        if fault_point is not None:
            env["RECONCILIATION_ARCHIVE_FAULT"] = fault_point
        if fault_request is not None:
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
    def _worker_result(proc):
        lines = [
            line
            for line in proc.stdout.splitlines()
            if line.startswith("WORKER_RESULT ")
        ]
        assert lines, proc.stderr + "\n" + proc.stdout
        return json.loads(lines[-1].split(" ", 1)[1])

    def test_correction_that_withdraws_chain_is_atomic_across_process_death(self):
        sections = [S1_SECTION]
        cases = [
            ("mid_write", 86, False),
            ("before_replace", 87, False),
            ("after_replace", 88, True),
        ]
        for point, expected_code, after_state in cases:
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = os.path.join(temp_dir, "archive")
                    crashed = self._run_worker(
                        path,
                        "correct",
                        fault_point=point,
                        fault_request="correct",
                    )
                    self.assertEqual(
                        crashed.returncode,
                        expected_code,
                        crashed.stderr,
                    )

                    ar = ReconciliationArchive(
                        path, sections, "2026-04-10", "2026-04-11"
                    )
                    if after_state:
                        self.assertEqual(ar.revision, 2)
                        self.assertFalse(
                            ar.read_package("package-1")["valid"]
                        )
                        self.assertFalse(
                            ar.read_package("package-2")["valid"]
                        )
                        self.assertFalse(
                            ar.read_take("receipt-1")["valid"]
                        )
                        self.assertFalse(
                            ar.read_take("receipt-2")["valid"]
                        )
                    else:
                        self.assertEqual(ar.revision, 1)
                        self.assertTrue(
                            ar.read_package("package-1")["valid"]
                        )
                        self.assertTrue(
                            ar.read_package("package-2")["valid"]
                        )
                        self.assertTrue(ar.read_take("receipt-1")["valid"])
                        self.assertTrue(ar.read_take("receipt-2")["valid"])

                    replay_proc = self._run_worker(path, "replay-correct")
                    self.assertEqual(
                        replay_proc.returncode, 0, replay_proc.stderr
                    )
                    self.assertEqual(
                        self._worker_result(replay_proc),
                        {"revision": 2, "changed": True},
                    )

                    reopened = ReconciliationArchive(
                        path, sections, "2026-04-10", "2026-04-11"
                    )
                    self.assertEqual(reopened.revision, 2)
                    self.assertEqual(
                        reopened.read_package("package-1")[
                            "invalidated_by_revision"
                        ],
                        2,
                    )
                    self.assertFalse(
                        reopened.read_take("receipt-1")["valid"]
                    )

    def test_seal_after_persistence_but_before_response_replays_same_package(self):
        sections = [S1_SECTION]
        for point, expected_code, persisted in [
            ("mid_write", 86, False),
            ("after_replace", 88, True),
        ]:
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = os.path.join(temp_dir, "archive")
                    crashed = self._run_worker(
                        path,
                        "seal",
                        fault_point=point,
                        fault_request="seal-crash",
                    )
                    self.assertEqual(
                        crashed.returncode, expected_code, crashed.stderr
                    )

                    ar = ReconciliationArchive(
                        path, sections, "2026-04-10", "2026-04-11"
                    )
                    self.assertEqual(ar.revision, 1)
                    if persisted:
                        self.assertEqual(
                            ar.current("s1", "2026-04-10")["package_id"],
                            "package-1",
                        )
                        self.assertTrue(
                            ar.read_package("package-1")["valid"]
                        )
                    else:
                        with self.assertRaises(ValueError):
                            ar.current("s1", "2026-04-10")
                        with self.assertRaises(ValueError):
                            ar.read_package("package-1")

                    replay_proc = self._run_worker(path, "replay-seal")
                    self.assertEqual(
                        replay_proc.returncode, 0, replay_proc.stderr
                    )
                    package = self._worker_result(replay_proc)
                    self.assertEqual(package["package_id"], "package-1")

                    reopened = ReconciliationArchive(
                        path, sections, "2026-04-10", "2026-04-11"
                    )
                    self.assertTrue(
                        reopened.read_package("package-1")["valid"]
                    )
                    with self.assertRaises(ValueError):
                        reopened.read_package("package-2")

    def test_take_after_persistence_but_before_response_replays_same_receipt(self):
        sections = [S1_SECTION]
        for point, expected_code, persisted in [
            ("mid_write", 86, False),
            ("after_replace", 88, True),
        ]:
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = os.path.join(temp_dir, "archive")
                    crashed = self._run_worker(
                        path,
                        "take",
                        fault_point=point,
                        fault_request="take-crash",
                    )
                    self.assertEqual(
                        crashed.returncode, expected_code, crashed.stderr
                    )

                    ar = ReconciliationArchive(
                        path, sections, "2026-04-10", "2026-04-11"
                    )
                    self.assertTrue(ar.read_package("package-1")["valid"])
                    if persisted:
                        self.assertTrue(ar.read_take("receipt-1")["valid"])
                    else:
                        with self.assertRaises(ValueError):
                            ar.read_take("receipt-1")

                    replay_proc = self._run_worker(path, "replay-take")
                    self.assertEqual(
                        replay_proc.returncode, 0, replay_proc.stderr
                    )
                    receipt = self._worker_result(replay_proc)
                    self.assertEqual(
                        receipt,
                        {
                            "receipt_id": "receipt-1",
                            "package_id": "package-1",
                        },
                    )

                    reopened = ReconciliationArchive(
                        path, sections, "2026-04-10", "2026-04-11"
                    )
                    self.assertTrue(
                        reopened.read_take("receipt-1")["valid"]
                    )
                    with self.assertRaises(ValueError):
                        reopened.read_take("receipt-2")


if __name__ == "__main__":
    unittest.main()
