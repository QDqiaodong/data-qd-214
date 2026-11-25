# Axle-counter reconciliation contract

Rail operations analysts define 1..1000 independent sections, each with one entry and one exit checkpoint. A checkpoint belongs to exactly one section in this bounded baseline. Events have globally unique IDs, train identity, checkpoint, offset-aware timestamp and integer axle count 1..1000. Events are processed in UTC time then event-ID order, regardless of input order.

An entry opens occupancy for that train and section. The matching exit closes it, records elapsed seconds and compares axle counts. An unmatched exit, repeated entry, count mismatch and still-open entry remain distinct evidence. The baseline does not drive signalling, infer train identity, merge consists, handle direction reversal or certify section safety.

The output is a structured event reconciliation for post-run data quality, not a dashboard, ticketing system or physical simulator. Future reset-authorisation records can attach to section and event identities without rewriting the observed movement evidence.

The persistent UTC-day handoff archive extends this baseline with immutable package bodies, parent-day lineage, source-revision withdrawal, take receipts and idempotent request replay. Its detailed contract and recovery semantics are in [reconciliation-archive.md](reconciliation-archive.md). Like the baseline, it is offline data-version handoff only and is not signalling control or section-safety certification.

