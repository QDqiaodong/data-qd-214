# Reconciliation handoff archive

`ReconciliationArchive` adds local, persistent UTC-day handoff packages to the
baseline axle reconciliation. It is an **offline data-version handoff**: it
does not connect to or control signalling equipment, and it is not a section
safety certification.

The baseline `process(data)` entry point and its input/output behavior remain
unchanged. The archive and tests use only the Python standard library.

## Run the tests

From the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The suite keeps the original baseline tests and adds public-API business,
replay, validation and independent-process crash-recovery tests.

## Creating and reopening an archive

```python
from axle_reconciliation import ReconciliationArchive

sections = [
    {"id": "s1", "entry": "a", "exit": "b"},
    {"id": "s2", "entry": "c", "exit": "d"},
]

archive = ReconciliationArchive(
    "/var/lib/axle-handoff/example",  # directory or .json snapshot path
    sections,
    "2026-04-10",
    "2026-04-11",
)
```

- Days are strict calendar strings `YYYY-MM-DD`; the range is closed and may
  contain at most 366 days.
- Section definitions use the baseline `id`, `entry` and `exit` fields and
  baseline checkpoint ownership rules.
- Section array order is not a configuration difference.
- A new archive has an empty event set and `revision == 0`.
- Reopening the same path restores the archive. Different section definitions
  or date ranges raise `ValueError`; the existing snapshot is not overwritten.
- The archive expects one writer at a time. It does not provide concurrent
  write or multi-machine coordination.

When `path` is an existing directory, the snapshot file is
`reconciliation-archive.json` inside it. An extensionless nonexistent path is
created as an archive directory; an explicit `.json` path is used as the
snapshot file.

## UTC day and cross-day facts

Every event time must be an ISO-8601 string with a timezone offset. It is
converted to UTC before validation and grouping. An event at
`00:00:00Z` belongs to the new UTC day. Event output uses UTC and a trailing
`Z`; zero microseconds are omitted by Python's canonical ISO formatting, while
nonzero microsecond precision is retained.

For each section/day, a package contains:

```text
package_id, section_id, day, revision, parent_package_id,
events, opening, movements, anomalies, closing
```

- `events` are that section's observations for the UTC day, sorted by
  `(UTC time, event id)`.
- `opening` is the previous day package's `closing`. On the first range day it
  is `[]` and `parent_package_id` is `null`.
- Every later day requires the **immediately preceding calendar day** to have
  a currently valid package for the same section. Days cannot be skipped and
  parents cannot come from another section.
- Entries remain active until a matching exit. A repeated entry for a train
  that is already open produces `duplicate_entry` and does not replace the
  original entry, including an entry carried in `opening`.
- An exit closes the active entry and creates a movement. An exit with no
  active entry produces `exit_without_entry`. Unequal axle counts produce
  `axle_mismatch` and still close the entry.
- Movement duration is calculated from the real entry timestamp, which may be
  on an earlier day. Movement and anomaly fields and sorting follow baseline
  `process`.
- `closing` is the sorted set of still-open full entry observations after the
  day's events.

Zero-event days can be sealed and propagate `opening` unchanged to `closing`,
so an entry can remain evidenced across one or more empty days.

## Revisions and source-triggered withdrawal

`revise(request_id, expected_revision, upserts, deletes)` atomically validates
and applies an event correction batch.

- `expected_revision` must be a nonnegative integer; `bool` is not accepted as
  an integer. For a new request it must equal the current revision.
- Each request may contain at most 100,000 upserts and 100,000 deletes. The
  resulting event set may contain at most 100,000 events.
- All events are validated before any change. A duplicate ID inside either
  array, an ID present in both arrays, a delete of a missing event, or any
  invalid event rejects the entire request.
- An actually changed event set increments `revision` by exactly one. Equivalent
  upserts (including equivalent UTC offsets) and two empty arrays are no-ops:
  `changed` is false and the revision does not increase.

For every actual change, the archive finds each affected section's earliest
changed UTC day. Both old and new locations are considered:

- deletes and replacements consider the old section/day;
- additions and replacements consider the new section/day.

All currently valid packages for that section on that day and every later day
are withdrawn, together with takes issued from those packages. The first
withdrawing revision is retained in `invalidated_by_revision` and is never
overwritten by later corrections. Earlier days and unaffected sections remain
valid. Moving one event from section `s1` on day 11 to section `s2` on day 12
therefore withdraws `s1` from day 11 and `s2` from day 12, while `s2` day 11
and unrelated sections remain valid.

This is a source-lineage rule. Withdrawal does not depend on whether
recomputed movements, statuses or closing occupancy happen to be unchanged.

After withdrawal, days must be resealed in calendar order. A withdrawn package
is immutable and is never returned by `current`; resealing creates a new
`package_id` and rebinds its direct parent to the then-valid previous-day
package. Old packages and old takes are never resurrected.

## Package and take validity versus historical results

A package body is immutable after issuance. Its `revision` is the global
archive revision at issuance; validity must be checked with `read_package`,
not by comparing that field with the latest revision.

```python
read_package(package_id)
# {"package": <immutable body>, "valid": bool,
#  "invalidated_by_revision": int | None}

current(section_id, day)
# current valid body only; ValueError if none

take(package_id, request_id)
# {"receipt_id": "...", "package_id": "..."}

read_take(receipt_id)
# {"receipt": <original credential>, "valid": bool,
#  "invalidated_by_revision": int | None}
```

A new `take` on an unknown or currently invalid package raises `ValueError`.
After source withdrawal, `read_take` reports that the previously issued
credential is invalid. A newly issued replacement package may be taken with a
new request.

## Request replay namespace

`request_id` is a nonempty string. `revise`, `seal` and `take` share one
request-ID namespace in an archive.

Request identity contains the operation name and all parameters. Event times
are compared as UTC instants, and upsert/delete array order does not affect
identity. Structural and type validation happens before identity comparison.

When a previously **successful** request is retried with the same identity,
the archive returns the first successful result exactly and performs no new
side effect. This replay check takes precedence over:

- a stale `expected_revision`;
- a missing or withdrawn parent package;
- withdrawal of the target package or take credential;
- the current state being a no-op.

Thus a replayed successful seal can return an old, now-invalid package body,
and a replayed successful take can return an old, now-invalid credential.
Those historical results are distinct from the present-day question “may this
package be used now?”, which is answered by `read_package`, `current` and
`read_take`.

Reusing a successful `request_id` for another operation or any different
parameter raises `ValueError`. Failed requests do not occupy their IDs; after
correcting the validation, revision or parent condition, the same ID may be
used for the first successful attempt.

## Persistence and crash recovery

The archive keeps one JSON snapshot containing configuration, events, package
bodies, validity markers, takes and successful request records. A commit:

1. writes a temporary file in the same directory;
2. flushes and `fsync`s it;
3. atomically calls `os.replace`;
4. `fsync`s the directory.

The business change and its success receipt are in the same snapshot. A
process exit before rename leaves the old complete snapshot; an exit after
rename leaves the new complete snapshot and receipt. Reopening in a new process
therefore gives either the full before-state or full after-state, and retrying
the same request recovers the original result without issuing a second package
or receipt.

The automated crash tests use these internal fault points:

| `RECONCILIATION_ARCHIVE_FAULT` | Exit code | Location/state |
| --- | ---: | --- |
| `mid_write` | 86 | half-written temporary file; old snapshot remains |
| `before_replace` | 87 | complete temporary file before rename; old snapshot remains |
| `after_replace` | 88 | new snapshot and receipt are atomically visible; response not returned |
| `after_directory_fsync` | 89 | fully durable post-commit state; response not returned |

Set `RECONCILIATION_ARCHIVE_FAULT_REQUEST=<request_id>` to arm the point only
for that write request. The tests launch independent `python -I` subprocesses,
reopen the path from the parent test process, and assert the public API state.

Disk-media failure, whole-machine power loss and concurrent writers are outside
the supported boundary.
