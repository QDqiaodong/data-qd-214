# Axle-counter reconciliation contract

Rail operations analysts define 1..1000 independent sections, each with one entry and one exit checkpoint. A checkpoint belongs to exactly one section in this bounded baseline. Events have globally unique IDs, train identity, checkpoint, offset-aware timestamp and integer axle count 1..1000. Events are processed in UTC time then event-ID order, regardless of input order.

An entry opens occupancy for that train and section. The matching exit closes it, records elapsed seconds and compares axle counts. An unmatched exit, repeated entry, count mismatch and still-open entry remain distinct evidence. The baseline does not drive signalling, infer train identity, merge consists, handle direction reversal or certify section safety.

The output is a structured event reconciliation for post-run data quality, not a dashboard, ticketing system or physical simulator. Future reset-authorisation records can attach to section and event identities without rewriting the observed movement evidence.

The persistent UTC-day handoff archive extends this baseline with immutable package bodies, parent-day lineage, source-revision withdrawal, take receipts and idempotent request replay. The archive additionally offers analyst-directed exit identity disambiguation:
`plan_identities` reports the identity range supported by every globally
optimal explanation, and `apply_identities` back-fills only forced associations
as one ordinary source revision. Ambiguous associations are never back-filled,
and axle-count differences remain visible evidence. It also records approved
offline reset write-offs through `revise_with_resets`, `read_reset` and the
reset-aware `reviewed_day` view: a reset ends authorized unclosed entries for
reconciliation without fabricating exits or movements, and any later
observation correction or identity back-fill that would overturn a registered
reset's unclosed evidence is rejected unless the affected reset is explicitly
withdrawn in the same batch. Its detailed contract and
recovery semantics are in [reconciliation-archive.md](reconciliation-archive.md). Like the baseline, it is offline data-version handoff only and is not signalling control or section-safety certification.

