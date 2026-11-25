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

## Exit identity disambiguation and controlled back-fill

The archive can also repair *local* exit train labels. Analysts explicitly
name the exit observations to re-identify; the archive never guesses which
exits are local. This remains offline data processing: it does not connect to
or control signalling equipment.

```python
plan = archive.plan_identities(
    "s1",                 # section_id
    ["x", "y"],           # unidentified_exit_ids, 1..32, unique
    0, 60, 20, 1,         # min_seconds, max_seconds, target_seconds, max_axle_gap
    "plan-req",           # request_id
)
archive.read_identity_plan(plan["plan_id"])
archive.apply_identities(plan["plan_id"], "apply-req")
```

### Why candidates come from the real occupied set

Candidate entries are not "all entries in the section". The archive first
removes the designated exits from the section's current observations and
applies the **existing real pairing semantics** (`process`'s duplicate-entry,
exit-without-entry and occupancy rules) to what remains. Only entries still
`occupied` after that are candidates. Consequently:

- an entry already closed by some other exit is unavailable;
- a repeated entry keeps its **first** still-open observation, not a later
  duplicate;
- computing this never writes to the archive and does not need a sealed day
  package;
- if nothing remains after exclusion, the internal candidate set is simply
  empty. Only the new planning API sees that empty set; the public
  `process(data)` contract still rejects an empty `events` array.

The empty exclusion result still yields a valid plan (all exits `unmatched`,
no `forced_pairs`), but such a plan cannot be applied.

### Explanations and uncertainty

A candidate pair exists only when the entry is strictly earlier by
`(UTC instant, event id)`, the **exact integer microsecond** interval is within
`[min_seconds, max_seconds]`, and the absolute axle difference is at most
`max_axle_gap`. The legacy rounding `duration_seconds` is not used for
comparison or cost.

An explanation is a one-to-one set of candidate pairs. Explanations are
ordered by strict lexicographic priority: first the largest matched count, then
the smallest sum of axle differences, then the smallest sum of
`abs(interval_us - target_seconds * 1_000_000)`. There is no weighted sum
approximation and no event-id or input-order tie-break. For each exit, the plan
reports every entry that appears in **at least one** globally optimal
explanation and whether **some** optimal explanation leaves it unmatched. A
plan does not nominate a representative optimum:

- `forced`: exactly one possible entry, and no optimal explanation leaves the
  exit unmatched;
- `unmatched`: no possible entry;
- `ambiguous`: everything else, including "one possible entry but it may be
  left unmatched" and "two entries that are each possible in different optima".

`forced_pairs` contains only `forced` decisions, with the entry's original
`train_id`. Presence of a candidate edge never implies presence in an optimum.

The solver is a bounded min-cost max-flow with `_COST_SCALE = 10**20`; 32
pairs times the maximum duration error stay far below the scale, so the
three-level priority is exact rather than weighted. The possible-entry and
may-be-unmatched sets are derived from the residual graph of that flow (all
zero-reduced-cost cycles), so they cover all optima without enumerating them
and without any explanation-count cap.

### Applying is a controlled correction under current evidence

`apply_identities` only back-fills the plan's own `forced_pairs`. It never
widens to `ambiguous` exits, never adopts later-forced associations, and
raises `ValueError` when the plan has no forced pairs. A successful apply does
**not** merely compare `source_revision`; it re-checks:

1. recompute the candidate set and optimal identity range from the **current**
   section observations with the plan's own parameters; every original forced
   pair must still be the same `forced` association;
2. the full five-field observation of every forced entry and exit must still
   match the plan evidence exactly (UTC-equivalent time spelling allowed).
   Disappearance, section move, or changed axles/time/train rejects even if the
   same association would still be forced;
3. stage the label changes on a copy of the section's current events and run
   the **old real pairing** over the whole section. Every forced pair must
   produce exactly that `(entry_event_id, exit_event_id)` movement, and every
   normal movement already present before the change must keep its entry and
   exit identities. A still-`unmatched` (or `ambiguous`) designated exit whose
   old local label would close a forced entry therefore blocks the apply.

All three checks run before any commit. Rejections leave events, revision,
packages, take credentials, plans and success receipts unchanged, and the
failed `request_id` remains free. Later competition can make a plan
unexecutable while leaving it readable; removing that competition restores
executability without the plan being permanently voided. An unrelated
correction in another section, or an unrelated closed movement in the same
section, does not block an otherwise valid plan.

On success, only the train labels of the forced exits change; `id`,
`checkpoint`, `time` and `axles` are preserved. All corrections enter as one
ordinary source revision (one revision if anything changed, none if every
label is already the target) and use the existing withdrawal rules: the
corrected day and later days for that section, plus their takes, are
withdrawn; earlier days and other sections stay valid. Historical package
bodies are immutable. Resealing the day produces the promised movements with
the real entry duration; unequal axles stay `axle_mismatch` and are not
"fixed".

The result is

```json
{
  "plan_id": "plan-1",
  "revision": 2,
  "changed": true,
  "applied_pairs": [
    {"entry_event_id": "b", "exit_event_id": "x", "train_id": "B",
     "old_train_id": "local-x", "new_train_id": "B"}
  ]
}
```

with `applied_pairs` sorted by `exit_event_id` and covering every original
forced pair.

### Plans, replay and persistence

`plan_identities` saves and returns an immutable plan whose body includes
`plan_id`, `section_id`, `source_revision`, the full `unidentified_exit_ids`,
the four rule parameters, `entry_candidates`, `exits`, `optimum`, `decisions`
and `forced_pairs`. `read_identity_plan` returns that original body; later
archive changes never rewrite it. Planning and reading do not change the event
revision or withdraw packages/takes.

`plan_identities` and `apply_identities` join the existing successful-request
namespace (shared with `revise`, `seal` and `take`). Their request identity is
the operation name plus all public parameters; the exit array order is
irrelevant. A retried successful request returns the first result verbatim,
even when the current evidence would no longer license an apply — that replay
takes precedence over current-evidence checks. A *new* request id for the same
plan is judged under current evidence. Failed requests do not occupy their id.

The plan body and its success receipt, and the apply corrections and their
receipt, are in the same atomic snapshot, so a crash leaves the full before-
or after-state and reopen replay never applies twice. Archives created before
this feature reopen in place; no packages, takes or request records are lost.

Resource bound (measured, Python 3.11.2, Linux aarch64, 10 vCPU):

| workload | wall clock | peak RSS |
| --- | ---: | ---: |
| one `plan_identities`, 32 candidates × 32 exits, all-zero cost | 0.012 s | 16 MiB |
| one `plan_identities`, 32 candidates × 32 exits, random costs | 0.011 s | 16 MiB |

Both are far inside the 60 s / 512 MiB budget. Re-run with
`PYTHONPATH=src python3 -m unittest tests.test_identity_plans -v`; the
`ResourceBoundTests` case prints the measured values and asserts the limits in
an isolated `python -I` subprocess.
