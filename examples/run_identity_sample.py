"""Demonstrate analyst-directed exit identity back-fill on a fresh archive.

Run from the repository root:

    PYTHONPATH=src python3 examples/run_identity_sample.py

The archive is created under a temporary directory and deleted afterwards.
This is offline data processing; it does not connect to signalling equipment.
"""

from __future__ import annotations

import json
import os
import tempfile

from axle_reconciliation import ReconciliationArchive

SECTIONS = [
    {"id": "s1", "entry": "a", "exit": "b"},
    {"id": "s2", "entry": "c", "exit": "d"},
]


def event(eid, train, checkpoint, time, axles):
    return {
        "id": eid,
        "train_id": train,
        "checkpoint": checkpoint,
        "time": time,
        "axles": axles,
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        archive = ReconciliationArchive(
            os.path.join(root, "handoff"), SECTIONS, "2026-04-10", "2026-04-11"
        )
        archive.revise(
            "add",
            0,
            [
                event("a", "A", "a", "2026-04-10T23:59:30Z", 20),
                event("b", "B", "a", "2026-04-10T23:59:40Z", 22),
                event("x", "local-x", "b", "2026-04-11T00:00:00Z", 21),
                event("y", "local-y", "b", "2026-04-11T00:00:10Z", 19),
            ],
            [],
        )
        old_10 = archive.seal("s1", "2026-04-10", "seal-10")
        old_11 = archive.seal("s1", "2026-04-11", "seal-11")
        archive.take(old_11["package_id"], "take-11")

        plan = archive.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan")
        print("PLAN")
        print(json.dumps({
            "optimum": plan["optimum"],
            "decisions": plan["decisions"],
            "forced_pairs": plan["forced_pairs"],
        }, indent=2, ensure_ascii=False))

        result = archive.apply_identities(plan["plan_id"], "apply")
        print("APPLY")
        print(json.dumps(result, indent=2, ensure_ascii=False))

        new_11 = archive.seal("s1", "2026-04-11", "reseal-11")
        print("RESEALED DAY 11 MOVEMENTS")
        print(json.dumps(new_11["movements"], indent=2, ensure_ascii=False))

        print("old day 10 still valid:", archive.read_package(old_10["package_id"])["valid"])
        print("old day 11 still valid:", archive.read_package(old_11["package_id"])["valid"])
        print(
            "old day 11 historical labels:",
            {e["id"]: e["train_id"] for e in archive.read_package(old_11["package_id"])["package"]["events"]},
        )


if __name__ == "__main__":
    main()
