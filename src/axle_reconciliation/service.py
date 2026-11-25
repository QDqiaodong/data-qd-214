from __future__ import annotations

from datetime import datetime, timezone


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("time must be an ISO string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if result.tzinfo is None:
        raise ValueError("timestamp needs an offset")
    return result.astimezone(timezone.utc)


def _parse_sections(sections: object) -> tuple[dict, dict]:
    if not isinstance(sections, list) or not 1 <= len(sections) <= 1000:
        raise ValueError("sections must contain 1..1000 items")
    section_map: dict = {}
    checkpoint_owner: dict = {}
    for section in sections:
        sid = section.get("id") if isinstance(section, dict) else None
        entry = section.get("entry") if isinstance(section, dict) else None
        exit_ = section.get("exit") if isinstance(section, dict) else None
        if (
            not all(isinstance(x, str) and x for x in (sid, entry, exit_))
            or sid in section_map
            or entry == exit_
        ):
            raise ValueError("invalid or duplicate section")
        for cp in (entry, exit_):
            if cp in checkpoint_owner:
                raise ValueError("checkpoints may belong to one section only")
            checkpoint_owner[cp] = sid
        section_map[sid] = (entry, exit_)
    return section_map, checkpoint_owner


def _parse_event(event: object, checkpoint_owner: dict) -> dict:
    """Validate one raw event against the baseline field contract."""
    eid = event.get("id") if isinstance(event, dict) else None
    train = event.get("train_id") if isinstance(event, dict) else None
    checkpoint = event.get("checkpoint") if isinstance(event, dict) else None
    count = event.get("axles") if isinstance(event, dict) else None
    if not isinstance(eid, str) or not eid:
        raise ValueError("event ids must be non-empty strings")
    if not isinstance(train, str) or not train or checkpoint not in checkpoint_owner:
        raise ValueError("invalid train or checkpoint")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
        raise ValueError("axles must be an integer 1..1000")
    return {
        "id": eid,
        "train_id": train,
        "checkpoint": checkpoint,
        "time": _time(event.get("time")),
        "axles": count,
    }


def _reconcile(canonical: list[dict], section_map: dict, checkpoint_owner: dict) -> dict:
    """Run the baseline reconciliation over already-normalized canonical events."""
    parsed = sorted(
        (e["time"], e["id"], e["train_id"], e["checkpoint"], e["axles"]) for e in canonical
    )
    state: dict = {sid: {} for sid in section_map}
    movements: list = []
    anomalies: list = []
    for time, eid, train, checkpoint, count in parsed:
        sid = checkpoint_owner[checkpoint]
        entry, exit_ = section_map[sid]
        active = state[sid]
        if checkpoint == entry:
            if train in active:
                anomalies.append(
                    {"event_id": eid, "section_id": sid, "train_id": train, "type": "duplicate_entry"}
                )
            else:
                active[train] = (eid, time, count)
        else:
            opened = active.pop(train, None)
            if opened is None:
                anomalies.append(
                    {"event_id": eid, "section_id": sid, "train_id": train, "type": "exit_without_entry"}
                )
            else:
                entry_id, entry_time, entry_count = opened
                movements.append(
                    {
                        "section_id": sid,
                        "train_id": train,
                        "entry_event_id": entry_id,
                        "exit_event_id": eid,
                        "entry_axles": entry_count,
                        "exit_axles": count,
                        "duration_seconds": int((time - entry_time).total_seconds()),
                        "status": "balanced" if entry_count == count else "axle_mismatch",
                    }
                )
    occupied: list = []
    for sid, active in state.items():
        for train, (eid, _, count) in active.items():
            occupied.append(
                {"section_id": sid, "train_id": train, "entry_event_id": eid, "entry_axles": count}
            )
    movements.sort(key=lambda x: (x["section_id"], x["entry_event_id"]))
    anomalies.sort(key=lambda x: x["event_id"])
    occupied.sort(key=lambda x: (x["section_id"], x["train_id"]))
    return {"movements": movements, "anomalies": anomalies, "occupied": occupied}


def _normalize_batches(batches: object, checkpoint_owner: dict) -> tuple[list[dict], dict]:
    """Normalize multi-batch event delivery into canonical events plus audit evidence."""
    if not isinstance(batches, list) or not 1 <= len(batches) <= 1000:
        raise ValueError("batches must contain 1..1000 items")

    # parsed copies: (received_at_utc, batch_id, raw_event_dict, parsed_event_dict)
    parsed_batches: list[tuple[datetime, str, dict, dict]] = []
    batch_ids: set = set()
    total_events = 0
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError("each batch must be an object")
        bid = batch.get("id")
        received = batch.get("received_at")
        raw_events = batch.get("events")
        if not isinstance(bid, str) or not bid:
            raise ValueError("batch id must be a non-empty string")
        if bid in batch_ids:
            raise ValueError("batch ids must be globally unique")
        received_at = _time(received)
        if not isinstance(raw_events, list) or not raw_events:
            raise ValueError("each batch must contain a non-empty events array")
        batch_ids.add(bid)
        total_events += len(raw_events)
        if total_events > 100000:
            raise ValueError("total raw events must not exceed 100000")
        for raw_event in raw_events:
            # Validation covers every raw copy; failures stay ValueError even for
            # non-dict/array inputs that would otherwise raise AttributeError.
            event = _parse_event(raw_event, checkpoint_owner)
            parsed_batches.append((received_at, bid, raw_event, event))

    # Deterministic source order independent of input array order.
    parsed_batches.sort(key=lambda item: (item[0], item[1]))

    # group_id -> ordered list of (received_at, batch_id, raw_event, event) copies
    grouped: dict[str, list[tuple[datetime, str, dict, dict]]] = {}
    for received_at, bid, raw_event, event in parsed_batches:
        grouped.setdefault(event["id"], []).append((received_at, bid, raw_event, event))

    canonical: list[dict] = []
    duplicates: list[dict] = []
    conflicts: list[dict] = []
    for eid in sorted(grouped):
        copies = grouped[eid]
        observations = {
            (event["train_id"], event["checkpoint"], event["axles"], event["time"])
            for _, _, _, event in copies
        }
        source_batch_ids = [bid for _, bid, _, _ in copies]
        if len(observations) > 1:
            # Conflicting evidence: quarantine every copy, no version reaches reconciliation.
            # Original event fields (including the source time string) are echoed unchanged.
            conflicts.append(
                {
                    "event_id": eid,
                    "records": [
                        {"batch_id": bid, "event": dict(raw_event)}
                        for _, bid, raw_event, _ in copies
                    ],
                }
            )
            continue
        # Equivalent replays (or a single copy): one canonical event only.
        canonical.append(copies[0][3])
        if len(copies) >= 2:
            duplicates.append(
                {
                    "event_id": eid,
                    "canonical_batch_id": copies[0][1],
                    "batch_ids": source_batch_ids,
                }
            )

    ingestion = {
        "accepted_event_ids": sorted(event["id"] for event in canonical),
        "quarantined_event_ids": [item["event_id"] for item in conflicts],
        "duplicates": duplicates,
        "conflicts": conflicts,
    }
    return canonical, ingestion


def process(data: dict) -> dict:
    sections = data.get("sections") if isinstance(data, dict) else None
    legacy_events = data.get("events") if isinstance(data, dict) else None
    batches = data.get("batches") if isinstance(data, dict) else None
    if not isinstance(data, dict) or ("events" in data and "batches" in data):
        raise ValueError("provide either top-level events or batches, not both")
    section_map, checkpoint_owner = _parse_sections(sections)

    if batches is not None:
        canonical, ingestion = _normalize_batches(batches, checkpoint_owner)
        result = _reconcile(canonical, section_map, checkpoint_owner)
        result["ingestion"] = ingestion
        return result

    if not isinstance(legacy_events, list) or not 1 <= len(legacy_events) <= 100000:
        raise ValueError("events must contain 1..100000 items")
    ids: set = set()
    parsed: list[dict] = []
    for event in legacy_events:
        parsed_event = _parse_event(event, checkpoint_owner)
        if parsed_event["id"] in ids:
            raise ValueError("event ids must be unique")
        ids.add(parsed_event["id"])
        parsed.append(parsed_event)
    return _reconcile(parsed, section_map, checkpoint_owner)
