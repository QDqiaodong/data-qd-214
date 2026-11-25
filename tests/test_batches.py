import copy
import unittest

from axle_reconciliation import process


SECTIONS = [{"id": "s1", "entry": "a", "exit": "b"}]


def entry(eid, train="t1", time="2026-01-01T00:00:00Z", axles=20):
    return {"id": eid, "train_id": train, "checkpoint": "a", "time": time, "axles": axles}


def exit_(eid, train="t1", time="2026-01-01T00:02:00Z", axles=20):
    return {"id": eid, "train_id": train, "checkpoint": "b", "time": time, "axles": axles}


def batch(bid, received_at, events):
    return {"id": bid, "received_at": received_at, "events": events}


class BatchNormalizationTests(unittest.TestCase):
    # --- 1. equivalent replays, timezone variants, order invariance ---

    def test_equivalent_replays_different_timezone_notation_collapse(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("collector-a-20260101-01", "2026-01-01T00:03:00+08:00",
                      [entry("e1", time="2026-01-01T00:00:00Z")]),
                batch("collector-b-20260101-01", "2026-01-01T00:05:00+08:00",
                      # 08:00:00+08:00 == 00:00:00Z: same UTC instant, different notation
                      [entry("e1", time="2026-01-01T08:00:00+08:00"),
                       exit_("e2", time="2026-01-01T00:02:00Z")]),
            ],
        }
        result = process(copy.deepcopy(data))
        self.assertEqual(len(result["movements"]), 1)
        row = result["movements"][0]
        self.assertEqual(
            (row["status"], row["duration_seconds"], row["entry_event_id"], row["exit_event_id"]),
            ("balanced", 120, "e1", "e2"),
        )
        ingestion = result["ingestion"]
        self.assertEqual(ingestion["accepted_event_ids"], ["e1", "e2"])
        self.assertEqual(ingestion["quarantined_event_ids"], [])
        self.assertEqual(ingestion["conflicts"], [])
        self.assertEqual(
            ingestion["duplicates"],
            [{"event_id": "e1",
              "canonical_batch_id": "collector-a-20260101-01",
              "batch_ids": ["collector-a-20260101-01", "collector-b-20260101-01"]}],
        )

    def test_output_is_independent_of_batch_and_event_order(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("b-a", "2026-01-01T00:03:00+08:00", [entry("e1"), exit_("e3")]),
                batch("b-b", "2026-01-01T00:04:00+08:00", [exit_("e2"), entry("e1")]),
                batch("b-c", "2026-01-01T00:05:00+08:00", [entry("e4"), entry("e1")]),
            ],
        }
        reference = process(copy.deepcopy(data))

        shuffled = copy.deepcopy(data)
        shuffled["batches"].reverse()
        for item in shuffled["batches"]:
            item["events"].reverse()
        self.assertEqual(process(shuffled), reference)

        # canonical copy always comes from the earliest (received_at, batch.id)
        self.assertEqual(reference["ingestion"]["duplicates"][0]["canonical_batch_id"], "b-a")
        self.assertEqual(
            reference["ingestion"]["duplicates"][0]["batch_ids"], ["b-a", "b-b", "b-c"]
        )

    def test_canonical_batch_tie_breaks_on_batch_id(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("batch-z", "2026-01-01T00:00:00Z", [entry("e1")]),
                batch("batch-a", "2026-01-01T00:00:00Z", [entry("e1"), exit_("e2")]),
            ],
        }
        result = process(data)
        dup = result["ingestion"]["duplicates"][0]
        self.assertEqual(dup["canonical_batch_id"], "batch-a")
        self.assertEqual(dup["batch_ids"], ["batch-a", "batch-z"])

    def test_single_copy_events_are_accepted_without_duplicate_entry(self):
        data = {
            "sections": SECTIONS,
            "batches": [batch("b1", "2026-01-01T00:03:00+08:00", [entry("e1"), exit_("e2")])],
        }
        result = process(data)
        self.assertEqual(result["ingestion"]["duplicates"], [])
        self.assertEqual(result["ingestion"]["conflicts"], [])
        self.assertEqual(result["ingestion"]["accepted_event_ids"], ["e1", "e2"])
        self.assertEqual(len(result["movements"]), 1)

    # --- 2. conflicts: quarantine every copy, downstream linkage ---

    def test_conflicting_entry_quarantines_all_copies_exit_becomes_anomaly(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("b1", "2026-01-01T00:01:00Z", [entry("e1", axles=20)]),
                batch("b2", "2026-01-01T00:02:00Z", [entry("e1", axles=20)]),
                batch("b3", "2026-01-01T00:03:00Z", [entry("e1", axles=21), exit_("e2")]),
            ],
        }
        result = process(copy.deepcopy(data))
        ingestion = result["ingestion"]
        self.assertEqual(ingestion["accepted_event_ids"], ["e2"])
        self.assertEqual(ingestion["quarantined_event_ids"], ["e1"])
        self.assertEqual(ingestion["duplicates"], [])
        self.assertEqual(len(ingestion["conflicts"]), 1)
        conflict = ingestion["conflicts"][0]
        self.assertEqual(conflict["event_id"], "e1")
        self.assertEqual([r["batch_id"] for r in conflict["records"]], ["b1", "b2", "b3"])
        # original fields are preserved verbatim, including source values
        self.assertEqual(
            [r["event"]["axles"] for r in conflict["records"]], [20, 20, 21]
        )
        for record in conflict["records"]:
            self.assertEqual(set(record["event"].keys()),
                             {"id", "train_id", "checkpoint", "time", "axles"})
        # no version of e1 reaches any reconciliation result
        encoded = str(result["movements"]) + str(result["anomalies"]) + str(result["occupied"])
        self.assertNotIn("e1", encoded)
        # surviving exit cannot be balanced against the quarantined entry
        self.assertEqual(result["movements"], [])
        self.assertEqual(result["occupied"], [])
        self.assertEqual(result["anomalies"][0]["type"], "exit_without_entry")
        self.assertEqual(result["anomalies"][0]["event_id"], "e2")

    def test_conflicting_exit_leaves_entry_occupied(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("b1", "2026-01-01T00:01:00Z", [entry("e1"), exit_("e2", axles=20)]),
                batch("b2", "2026-01-01T00:02:00Z", [exit_("e2", axles=99)]),
            ],
        }
        result = process(data)
        self.assertEqual(result["ingestion"]["quarantined_event_ids"], ["e2"])
        self.assertEqual(result["movements"], [])
        self.assertEqual(result["anomalies"], [])
        self.assertEqual(
            result["occupied"],
            [{"section_id": "s1", "train_id": "t1", "entry_event_id": "e1", "entry_axles": 20}],
        )

    def test_conflicting_time_and_train_fields_are_detected(self):
        base = entry("e1")
        cases = [
            exit_("e1"),                                       # checkpoint differs
            entry("e1", train="t9"),                           # train differs
            entry("e1", axles=21),                             # axles differ
            entry("e1", time="2026-01-01T00:00:01Z"),         # UTC instant differs
        ]
        for rival in cases:
            with self.subTest(rival=rival):
                data = {
                    "sections": SECTIONS,
                    "batches": [
                        batch("b1", "2026-01-01T00:01:00Z", [dict(base)]),
                        batch("b2", "2026-01-01T00:02:00Z", [rival]),
                    ],
                }
                result = process(data)
                self.assertEqual(result["ingestion"]["quarantined_event_ids"], ["e1"])
                self.assertEqual(result["ingestion"]["accepted_event_ids"], [])

    def test_audit_lists_are_sorted_and_disjoint(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("b1", "2026-01-01T00:01:00Z",
                      [entry("e9", axles=1), entry("e3"), exit_("e4")]),
                batch("b2", "2026-01-01T00:02:00Z",
                      [entry("e9", axles=2), entry("e3"), exit_("e5")]),
                batch("b3", "2026-01-01T00:03:00Z",
                      [exit_("e5"), entry("e6"), entry("e3")]),
            ],
        }
        ingestion = process(data)["ingestion"]
        self.assertEqual(ingestion["accepted_event_ids"], sorted(ingestion["accepted_event_ids"]))
        self.assertEqual(ingestion["quarantined_event_ids"],
                         sorted(ingestion["quarantined_event_ids"]))
        self.assertEqual(
            [d["event_id"] for d in ingestion["duplicates"]],
            sorted(d["event_id"] for d in ingestion["duplicates"]),
        )
        self.assertEqual(
            [c["event_id"] for c in ingestion["conflicts"]],
            sorted(c["event_id"] for c in ingestion["conflicts"]),
        )
        self.assertEqual(ingestion["quarantined_event_ids"], ["e9"])
        self.assertEqual(ingestion["accepted_event_ids"], ["e3", "e4", "e5", "e6"])
        dup_ids = {d["event_id"] for d in ingestion["duplicates"]}
        conflict_ids = {c["event_id"] for c in ingestion["conflicts"]}
        self.assertTrue(dup_ids.isdisjoint(conflict_ids))
        # conflict records ordered by (received_at, batch.id)
        self.assertEqual(
            [r["batch_id"] for r in ingestion["conflicts"][0]["records"]], ["b1", "b2"]
        )

    # --- 3. replays must never double-count ---

    def test_equivalent_replays_do_not_duplicate_movement_or_occupancy(self):
        data = {
            "sections": SECTIONS,
            "batches": [
                batch("b1", "2026-01-01T00:01:00Z", [entry("e1")]),
                batch("b2", "2026-01-01T00:02:00Z", [entry("e1"), exit_("e2")]),
                batch("b3", "2026-01-01T00:03:00Z", [entry("e1"), exit_("e2")]),
            ],
        }
        result = process(data)
        self.assertEqual(len(result["movements"]), 1)
        self.assertEqual(result["anomalies"], [])
        self.assertEqual(result["occupied"], [])
        duplicates = {d["event_id"]: d for d in result["ingestion"]["duplicates"]}
        self.assertEqual(duplicates["e1"]["batch_ids"], ["b1", "b2", "b3"])
        self.assertEqual(duplicates["e1"]["canonical_batch_id"], "b1")
        self.assertEqual(duplicates["e2"]["batch_ids"], ["b2", "b3"])
        self.assertEqual(duplicates["e2"]["canonical_batch_id"], "b2")

    # --- 4. validation boundaries keep raising ValueError ---

    def assert_value_error(self, data):
        with self.assertRaises(ValueError):
            process(copy.deepcopy(data) if isinstance(data, dict) else data)

    def test_top_level_combinations_rejected(self):
        good_event = entry("e1")
        self.assert_value_error({"sections": SECTIONS,
                                 "events": [good_event],
                                 "batches": [batch("b1", "2026-01-01T00:01:00Z", [good_event])]})
        self.assert_value_error({"sections": SECTIONS})                      # neither mode
        self.assert_value_error([1, 2, 3])                                   # data not a dict
        self.assert_value_error("not-a-dict")
        self.assert_value_error(None)

    def test_batch_container_validation(self):
        self.assert_value_error({"sections": SECTIONS, "batches": []})
        self.assert_value_error({"sections": SECTIONS, "batches": {}})
        self.assert_value_error({"sections": SECTIONS, "batches": "x"})
        self.assert_value_error({"sections": SECTIONS, "batches": None})
        self.assert_value_error(
            {"sections": SECTIONS, "batches": [["not", "an", "object"]]})
        self.assert_value_error(
            {"sections": SECTIONS, "batches": ["not-an-object"]})

    def test_batch_id_validation(self):
        base_evt = entry("e1")
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("", "2026-01-01T00:01:00Z", [base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [{"received_at": "2026-01-01T00:01:00Z",
                                              "events": [base_evt]}]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch(123, "2026-01-01T00:01:00Z", [base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch(["b"], "2026-01-01T00:01:00Z", [base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b", "2026-01-01T00:01:00Z", [base_evt]),
                                             batch("b", "2026-01-01T00:02:00Z", [exit_("e2")])]})

    def test_received_at_validation(self):
        base_evt = entry("e1")
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", "2026-01-01T00:01:00", [base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", "not-a-time", [base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", 20260101, [base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", ["2026-01-01T00:01:00Z"], [base_evt])]})

    def test_batch_events_array_validation(self):
        base_evt = entry("e1")
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", "2026-01-01T00:01:00Z", [])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", "2026-01-01T00:01:00Z", base_evt)]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [{"id": "b1",
                                              "received_at": "2026-01-01T00:01:00Z"}]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", "2026-01-01T00:01:00Z",
                                                   ["not-an-event", base_evt])]})
        self.assert_value_error({"sections": SECTIONS,
                                 "batches": [batch("b1", "2026-01-01T00:01:00Z",
                                                   [[base_evt]])]})

    def test_event_field_validation_inside_batches(self):
        def bad(**overrides):
            evt = entry("e1")
            evt.update(overrides)
            return {"sections": SECTIONS,
                    "batches": [batch("b1", "2026-01-01T00:01:00Z", [evt])]}

        self.assert_value_error(bad(id=""))
        self.assert_value_error(bad(id=7))
        self.assert_value_error(bad(train_id=""))
        self.assert_value_error(bad(train_id=42))
        self.assert_value_error(bad(checkpoint="unknown-cp"))
        self.assert_value_error(bad(axles=0))
        self.assert_value_error(bad(axles=1001))
        self.assert_value_error(bad(axles=20.0))
        self.assert_value_error(bad(axles="20"))
        self.assert_value_error(bad(axles=True))
        self.assert_value_error(bad(time="2026-01-01T00:00:00"))       # naive
        self.assert_value_error(bad(time="nope"))
        self.assert_value_error(bad(time=1767225600))
        self.assert_value_error(bad(time=["2026-01-01T00:00:00Z"]))

    def test_batch_count_and_raw_event_limits(self):
        too_many_batches = [
            batch(f"b{i:04d}", "2026-01-01T00:00:00Z", [entry(f"e{i}")]) for i in range(1001)
        ]
        self.assert_value_error({"sections": SECTIONS, "batches": too_many_batches})

        too_many_events = [
            batch("b1", "2026-01-01T00:00:00Z",
                  [entry(f"e{i}") for i in range(100001)])
        ]
        self.assert_value_error({"sections": SECTIONS, "batches": too_many_events})

    def test_sections_still_validated_in_batch_mode(self):
        data = {
            "sections": [{"id": "s1", "entry": "a", "exit": "a"}],
            "batches": [batch("b1", "2026-01-01T00:01:00Z", [entry("e1")])],
        }
        self.assert_value_error(data)

    # --- 5. legacy mode unchanged ---

    def test_legacy_output_has_no_ingestion_block(self):
        data = {
            "sections": SECTIONS,
            "events": [exit_("e2"), entry("e1")],
        }
        result = process(data)
        self.assertEqual(set(result.keys()), {"movements", "anomalies", "occupied"})
        row = result["movements"][0]
        self.assertEqual((row["status"], row["duration_seconds"]), ("balanced", 120))

    def test_legacy_rejects_duplicate_event_ids(self):
        data = {
            "sections": SECTIONS,
            "events": [entry("e1"), entry("e1")],
        }
        self.assert_value_error(data)


if __name__ == "__main__":
    unittest.main()
