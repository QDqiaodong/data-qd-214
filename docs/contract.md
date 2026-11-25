# Axle-counter reconciliation contract

Rail operations analysts define 1..1000 independent sections, each with one entry and one exit checkpoint. A checkpoint belongs to exactly one section in this bounded baseline. Events have IDs, train identity, checkpoint, offset-aware timestamp and integer axle count 1..1000. Canonical events are processed in UTC time then event-ID order, regardless of input order.

An entry opens occupancy for that train and section. The matching exit closes it, records elapsed seconds and compares axle counts. An unmatched exit, repeated entry, count mismatch and still-open entry remain distinct evidence. The baseline does not drive signalling, infer train identity, merge consists, handle direction reversal or certify section safety.

The output is a structured event reconciliation for post-run data quality, not a dashboard, ticketing system or physical simulator. Future reset-authorisation records can attach to section and event identities without rewriting the observed movement evidence.

## Two input modes on the same `process(data)` entry point

### Legacy mode: `sections` + top-level `events`

Each event ID must be globally unique inside the input (1..100000 events). Validation errors raise `ValueError`. The result contains exactly the three keys `movements`, `anomalies` and `occupied`, with the field meanings, ordering and status names defined by the baseline. This mode is unchanged: existing callers do not need to migrate and the result never gains an `ingestion` block.

### Batch mode: `sections` + `batches` (no top-level `events`)

`batches` holds 1..1000 collector delivery batches. Each batch is an object with:

- `id`: non-empty string, globally unique across batches;
- `received_at`: offset-aware ISO timestamp. It only deterministically selects the source of an equivalent canonical copy and never replaces the event's own `time`;
- `events`: non-empty array of events. The total number of raw event copies across all batches must not exceed 100000.

Providing both top-level `events` and `batches`, or neither, raises `ValueError`. Every raw copy still obeys the baseline field, checkpoint, axle and timestamp validation; malformed arrays or objects raise `ValueError` rather than `KeyError`/`TypeError`.

The business identity of an observation is the tuple `(id, train_id, checkpoint, axles, time-as-UTC-instant)`. Timestamps written with different offsets but converting to the same UTC instant denote the same observation. Batch and event array order never influence the result.

- **Equivalent replays:** when every copy of an event ID carries the same business observation, exactly one canonical event enters reconciliation. The canonical copy comes from the batch with the earliest `(received_at, batch.id)`. An ID appearing in only one batch once is simply accepted and is not listed as a duplicate.
- **Conflicts:** when at least two copies of one event ID carry different business observations, every copy of that ID is quarantined. No version — neither majority, newest nor first-in-input — enters `movements`, `anomalies` or `occupied`, and no automatic adjudication is performed.

### Batch-mode audit block

Batch-mode results add one top-level `ingestion` object alongside the unchanged three arrays:

- `accepted_event_ids`: IDs of the canonical events actually reconciled; sorted ascending.
- `quarantined_event_ids`: IDs isolated due to conflicts; sorted ascending and disjoint from the accepted list.
- `duplicates`: one entry per ID with at least two equivalent copies, sorted by `event_id`. `batch_ids` lists every source batch ordered by `(received_at, batch.id)`, and `canonical_batch_id` is the batch that supplied the canonical copy.
- `conflicts`: one entry per quarantined ID, sorted by `event_id`. `records` contains every raw copy ordered by `(received_at, batch.id)`, each preserving the source `batch_id` and the original event fields verbatim (including the original timestamp string). Nothing is rewritten, merged or hidden.

An ID never appears in both `duplicates` and `conflicts`. Quarantine drives the real reconciliation: for example, a quarantined exit leaves its entry in `occupied`, while a quarantined entry makes the surviving exit an `exit_without_entry` anomaly. The missing evidence is therefore reflected in the movement results themselves, not only in the audit block.
