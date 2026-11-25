"""Local persistent reconciliation handoff archive.

This module implements the offline UTC-day handoff dataset described by the
project task.  It uses only the Python standard library and stores one JSON
snapshot per archive.  Each successful operation writes that snapshot
atomically (temporary file + ``os.replace``), so an independent process that
reopens the path observes either the complete before-state or complete
after-state.

The archive is an offline data-version handoff only.  It does not connect to
or control signalling equipment and is not a section-safety certification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .identity import build_identity_evidence, pair_events, timestamp_microseconds

__all__ = ["ReconciliationArchive"]

_ARCHIVE_FILE_NAME = "reconciliation-archive.json"
_FORMAT_NAME = "railway-axis-count-handoff-archive-v1"
_EVENT_FIELDS = frozenset(
    {"id", "train_id", "checkpoint", "time", "axles"}
)
_DAY_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_MAX_ITEMS = 100_000
_MAX_SECTIONS = 1_000
_MAX_RANGE_DAYS = 366
_MAX_RESET_BATCH = 1_000
_MAX_ACTIVE_RESETS = 1_000
_RESET_FIELDS = frozenset(
    {"id", "section_id", "time", "authorization_ref", "opening"}
)


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _parse_day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("day must be a strict YYYY-MM-DD string")
    match = _DAY_RE.fullmatch(value)
    if match is None:
        raise ValueError("day must be a strict YYYY-MM-DD string")
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError as exc:
        raise ValueError("invalid calendar day") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("day must be a strict YYYY-MM-DD string")
    return parsed


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("event time must be an ISO-8601 string")
    try:
        # Match the baseline parser's accepted "Z" spelling.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid event timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: datetime) -> str:
    """Return UTC ISO text ending in ``Z``.

    Python's auto timespec omits a zero microsecond field and otherwise uses
    six microsecond digits, preserving all parsed sub-second precision.
    """
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_sections(sections: object) -> tuple[list[dict[str, str]], dict[str, str]]:
    if not isinstance(sections, list) or not 1 <= len(sections) <= _MAX_SECTIONS:
        raise ValueError("sections must be a list of 1..1000 section definitions")

    normalized: list[dict[str, str]] = []
    section_ids: set[str] = set()
    checkpoint_owner: dict[str, str] = {}

    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("each section must be an object")
        sid = section.get("id")
        entry = section.get("entry")
        exit_checkpoint = section.get("exit")
        if not all(_nonempty_str(x) for x in (sid, entry, exit_checkpoint)):
            raise ValueError("section id, entry and exit must be nonempty strings")
        assert isinstance(sid, str)
        assert isinstance(entry, str)
        assert isinstance(exit_checkpoint, str)
        if sid in section_ids:
            raise ValueError("section ids must be unique")
        if entry == exit_checkpoint:
            raise ValueError("section entry and exit must differ")
        for checkpoint in (entry, exit_checkpoint):
            owner = checkpoint_owner.get(checkpoint)
            if owner is not None and owner != sid:
                raise ValueError("a checkpoint may belong to only one section")
            checkpoint_owner[checkpoint] = sid
        section_ids.add(sid)
        normalized.append({"id": sid, "entry": entry, "exit": exit_checkpoint})

    normalized.sort(key=lambda item: item["id"])
    return normalized, checkpoint_owner


def _normalize_event(
    raw: object,
    checkpoint_owner: dict[str, str],
    start_day: date,
    end_day: date,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each event must be an object")
    if frozenset(raw.keys()) != _EVENT_FIELDS:
        raise ValueError("event must contain exactly id, train_id, checkpoint, time and axles")

    eid = raw.get("id")
    train_id = raw.get("train_id")
    checkpoint = raw.get("checkpoint")
    axles = raw.get("axles")

    if not _nonempty_str(eid):
        raise ValueError("event id must be a nonempty string")
    if not _nonempty_str(train_id):
        raise ValueError("train_id must be a nonempty string")
    if not _nonempty_str(checkpoint) or checkpoint not in checkpoint_owner:
        raise ValueError("event checkpoint must belong to a configured section")
    if not _is_real_int(axles) or not 1 <= axles <= 1000:
        raise ValueError("axles must be an integer in 1..1000")

    moment = _parse_timestamp(raw.get("time"))
    event_day = moment.date()
    if not start_day <= event_day <= end_day:
        raise ValueError("event UTC day is outside the archive date range")

    return {
        "id": eid,
        "train_id": train_id,
        "checkpoint": checkpoint,
        "time": _canonical_timestamp(moment),
        "axles": axles,
    }


def _normalize_reset(
    raw: object,
    checkpoint_owner: dict[str, str],
    sections_by_id: dict[str, dict[str, str]],
    start_day: date,
    end_day: date,
) -> dict[str, Any]:
    """Validate and canonically spell one reset submission.

    The opening evidence is supplied by the caller exactly as authorized.  The
    archive never fills in, drops or rewrites any opening observation; it only
    normalizes the accepted timestamp spelling to UTC.
    """
    if not isinstance(raw, dict):
        raise ValueError("each reset must be an object")
    if frozenset(raw.keys()) != _RESET_FIELDS:
        raise ValueError(
            "reset must contain exactly id, section_id, time, "
            "authorization_ref and opening"
        )

    reset_id = raw.get("id")
    section_id = raw.get("section_id")
    authorization_ref = raw.get("authorization_ref")
    if not _nonempty_str(reset_id):
        raise ValueError("reset id must be a nonempty string")
    if not _nonempty_str(section_id) or section_id not in sections_by_id:
        raise ValueError("reset section_id must identify a configured section")
    if not _nonempty_str(authorization_ref):
        raise ValueError("authorization_ref must be a nonempty string")
    assert isinstance(reset_id, str)
    assert isinstance(section_id, str)
    assert isinstance(authorization_ref, str)

    moment = _parse_timestamp(raw.get("time"))
    reset_day = moment.date()
    if not start_day <= reset_day <= end_day:
        raise ValueError("reset UTC time is outside the archive date range")

    raw_opening = raw.get("opening")
    if not isinstance(raw_opening, list) or not 1 <= len(raw_opening) <= _MAX_ITEMS:
        raise ValueError("reset opening must be a nonempty list of events")

    section = sections_by_id[section_id]
    entry_checkpoint = section["entry"]
    opening: list[dict[str, Any]] = []
    opening_ids: set[str] = set()
    for raw_entry in raw_opening:
        entry = _normalize_event(
            raw_entry, checkpoint_owner, start_day, end_day
        )
        if checkpoint_owner[entry["checkpoint"]] != section_id:
            raise ValueError(
                "every reset opening entry must belong to the reset section"
            )
        if entry["checkpoint"] != entry_checkpoint:
            raise ValueError("reset opening entries must be entry observations")
        if entry["id"] in opening_ids:
            raise ValueError("reset opening event ids must be unique")
        opening_ids.add(entry["id"])
        # Entries must be strictly earlier than the reset, compared at UTC
        # microsecond precision.
        if timestamp_microseconds(entry["time"]) >= timestamp_microseconds(
            _canonical_timestamp(moment)
        ):
            raise ValueError("every reset opening entry must strictly precede the reset")
        opening.append(entry)

    opening.sort(key=lambda event: event["id"])
    return {
        "id": reset_id,
        "section_id": section_id,
        "time": _canonical_timestamp(moment),
        "authorization_ref": authorization_ref,
        "opening": opening,
    }


def _identity_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # A digest keeps large bulk revise receipts compact in the snapshot while
    # still giving exact, stable request-identity comparison after reopen.
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReconciliationArchive:
    """Persistent, request-idempotent railway reconciliation handoffs."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        sections: object,
        start_day: object,
        end_day: object,
    ) -> None:
        normalized_sections, checkpoint_owner = _normalize_sections(sections)
        start = _parse_day(start_day)
        end = _parse_day(end_day)
        if start > end:
            raise ValueError("start_day must not be after end_day")
        if (end.toordinal() - start.toordinal()) + 1 > _MAX_RANGE_DAYS:
            raise ValueError("the inclusive day range may contain at most 366 days")

        self._checkpoint_owner = checkpoint_owner
        self._sections_by_id = {item["id"]: item for item in normalized_sections}
        expected_config: dict[str, Any] = {
            "sections": normalized_sections,
            "start_day": start.isoformat(),
            "end_day": end.isoformat(),
        }

        self._directory, self._data_path = self._resolve_path(path)
        self._grouped: dict[tuple[str, str], list[dict[str, Any]]] | None = None
        self._grouped_revision: int | None = None

        if self._data_path.exists() and self._data_path.stat().st_size > 0:
            self._state = self._load_state()
            if self._state.get("format") != _FORMAT_NAME:
                raise ValueError("not a supported reconciliation archive")
            if self._state.get("config") != expected_config:
                raise ValueError("archive sections or date range do not match")
        else:
            # A nonexistent snapshot or a zero-byte placeholder is treated as
            # a new archive.  Non-empty unreadable files are rejected below.
            self._directory.mkdir(parents=True, exist_ok=True)
            self._state = self._initial_state(expected_config)
            self._commit(request_id=None)

    @staticmethod
    def _resolve_path(path: object) -> tuple[Path, Path]:
        try:
            candidate = Path(os.fspath(path))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("path must be a filesystem path") from exc

        if candidate.exists():
            if candidate.is_dir():
                return candidate, candidate / _ARCHIVE_FILE_NAME
            if candidate.is_file():
                return candidate.parent, candidate
            raise ValueError("archive path must be a directory or file")

        # A explicit .json path is treated as the snapshot file.  A extensionless
        # path is treated as an archive directory, which also matches callers
        # that pass os.path.join(temp_dir, "archive").
        if candidate.suffix.lower() == ".json":
            return candidate.parent, candidate
        return candidate, candidate / _ARCHIVE_FILE_NAME

    @staticmethod
    def _initial_state(config: dict[str, Any]) -> dict[str, Any]:
        return {
            "format": _FORMAT_NAME,
            "config": config,
            "revision": 0,
            "events": {},
            "packages": {},
            "package_status": {},
            "day_index": {},
            "takes": {},
            "take_index": {},
            "requests": {},
            "package_seq": 0,
            "receipt_seq": 0,
            "plans": {},
            "plan_seq": 0,
            "resets": {},
            "reset_active": {},
        }

    def _load_state(self) -> dict[str, Any]:
        try:
            with self._data_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("archive is unreadable or corrupt") from exc
        if not isinstance(state, dict):
            raise ValueError("archive is corrupt")
        required = {
            "format",
            "config",
            "revision",
            "events",
            "packages",
            "package_status",
            "day_index",
            "takes",
            "take_index",
            "requests",
            "package_seq",
            "receipt_seq",
        }
        if not required.issubset(state.keys()):
            raise ValueError("archive is corrupt")
        # Archives written before identity plans existed re-open in place.
        state.setdefault("plans", {})
        state.setdefault("plan_seq", 0)
        # Archives written before offline reset reconciliation existed re-open
        # in place without rebuilding anything.
        state.setdefault("resets", {})
        state.setdefault("reset_active", {})
        return state

    @property
    def revision(self) -> int:
        return int(self._state["revision"])

    # ------------------------------------------------------------------
    # Request replay and persistence
    # ------------------------------------------------------------------

    def _replay_saved(self, request_id: str, identity: str) -> Any:
        record = self._state["requests"].get(request_id)
        if record is None:
            return None
        if not isinstance(record, dict) or record.get("identity") != identity:
            raise ValueError("request_id was already used with different parameters")
        return copy.deepcopy(record["result"])

    def _put_request(self, request_id: str, identity: str, result: Any) -> None:
        self._state["requests"][request_id] = {
            "identity": identity,
            "result": copy.deepcopy(result),
        }

    def _fault_armed(self, point: str, request_id: str | None) -> bool:
        if os.environ.get("RECONCILIATION_ARCHIVE_FAULT") != point:
            return False
        wanted = os.environ.get("RECONCILIATION_ARCHIVE_FAULT_REQUEST")
        return not wanted or wanted == request_id

    def _commit(self, request_id: str | None) -> None:
        """Atomically persist the complete state and success receipts."""
        payload = json.dumps(
            self._state,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        fd, temporary_name = tempfile.mkstemp(
            prefix=".reconciliation-archive-",
            suffix=".tmp",
            dir=str(self._directory),
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                if self._fault_armed("mid_write", request_id):
                    cut = max(1, len(payload) // 2)
                    handle.write(payload[:cut])
                    handle.flush()
                    os.fsync(handle.fileno())
                    # Simulate process death while the temporary snapshot is
                    # only half written.  os.replace has not happened.
                    os._exit(86)

                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            if self._fault_armed("before_replace", request_id):
                os._exit(87)

            os.replace(temporary_name, self._data_path)

            if self._fault_armed("after_replace", request_id):
                # The complete new state and receipt are now the archive.  The
                # caller has not received its response yet.
                os._exit(88)

            directory_fd = os.open(str(self._directory), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

            if self._fault_armed("after_directory_fsync", request_id):
                os._exit(89)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    # ------------------------------------------------------------------
    # Revision
    # ------------------------------------------------------------------

    def revise(
        self,
        request_id: str,
        expected_revision: object,
        upserts: object,
        deletes: object,
    ) -> dict[str, Any]:
        if not _nonempty_str(request_id):
            raise ValueError("request_id must be a nonempty string")
        if not _is_real_int(expected_revision) or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(upserts, list) or len(upserts) > _MAX_ITEMS:
            raise ValueError("upserts must be a list of at most 100000 events")
        if not isinstance(deletes, list) or len(deletes) > _MAX_ITEMS:
            raise ValueError("deletes must be a list of at most 100000 event ids")

        start_day = _parse_day(self._state["config"]["start_day"])
        end_day = _parse_day(self._state["config"]["end_day"])

        normalized_upserts: list[dict[str, Any]] = []
        upsert_ids: set[str] = set()
        for raw_event in upserts:
            event = _normalize_event(
                raw_event, self._checkpoint_owner, start_day, end_day
            )
            eid = event["id"]
            if eid in upsert_ids:
                raise ValueError("upsert event ids must be unique within a request")
            upsert_ids.add(eid)
            normalized_upserts.append(event)

        normalized_deletes: list[str] = []
        delete_ids: set[str] = set()
        for raw_id in deletes:
            if not _nonempty_str(raw_id):
                raise ValueError("delete ids must be nonempty strings")
            assert isinstance(raw_id, str)
            if raw_id in delete_ids:
                raise ValueError("delete ids must be unique within a request")
            delete_ids.add(raw_id)
            normalized_deletes.append(raw_id)

        if upsert_ids.intersection(delete_ids):
            raise ValueError("the same event id cannot be upserted and deleted")

        identity = _identity_digest(
            {
                "op": "revise",
                "request_id": request_id,
                "expected_revision": expected_revision,
                # Sorting makes array order irrelevant to request identity.
                "upserts": sorted(normalized_upserts, key=lambda event: event["id"]),
                "deletes": sorted(normalized_deletes),
            }
        )
        replayed = self._replay_saved(request_id, identity)
        if replayed is not None:
            return replayed

        current_revision = int(self._state["revision"])
        if expected_revision != current_revision:
            raise ValueError("revision conflict")

        events: dict[str, dict[str, Any]] = self._state["events"]

        missing = [eid for eid in normalized_deletes if eid not in events]
        if missing:
            raise ValueError("deletes must identify currently existing events")

        added_count = sum(
            1 for event in normalized_upserts if event["id"] not in events
        )
        candidate_count = len(events) - len(normalized_deletes) + added_count
        if candidate_count > _MAX_ITEMS:
            raise ValueError("the revised event set may contain at most 100000 events")

        affected: dict[str, str] = {}
        changed = False

        def mark(event: dict[str, Any]) -> None:
            sid = self._checkpoint_owner[event["checkpoint"]]
            day = event["time"][:10]
            previous = affected.get(sid)
            if previous is None or day < previous:
                affected[sid] = day

        for eid in normalized_deletes:
            old_event = events[eid]
            changed = True
            mark(old_event)

        for new_event in normalized_upserts:
            old_event = events.get(new_event["id"])
            if old_event != new_event:
                changed = True
                if old_event is not None:
                    mark(old_event)
                mark(new_event)

        if not changed:
            result = {"revision": current_revision, "changed": False}
            self._put_request(request_id, identity, result)
            self._commit(request_id)
            return copy.deepcopy(result)

        # Reset evidence is a hard constraint on source writes.  The check runs
        # on the full post-batch observation set before anything is mutated;
        # revise itself has no permission to add or withdraw resets.
        candidate_events = dict(events)
        for eid in normalized_deletes:
            del candidate_events[eid]
        for event in normalized_upserts:
            candidate_events[event["id"]] = dict(event)
        self._guard_resets(candidate_events)

        new_revision = current_revision + 1

        for eid in normalized_deletes:
            del events[eid]
        for event in normalized_upserts:
            events[event["id"]] = dict(event)

        for sid, earliest_day in affected.items():
            self._invalidate_from(sid, earliest_day, new_revision)

        self._state["revision"] = new_revision
        self._grouped = None
        result = {"revision": new_revision, "changed": True}
        self._put_request(request_id, identity, result)
        self._commit(request_id)
        return copy.deepcopy(result)

    def revise_with_resets(
        self,
        request_id: str,
        expected_revision: object,
        upserts: object,
        deletes: object,
        add_resets: object,
        withdraw_reset_ids: object,
    ) -> dict[str, Any]:
        """Atomically apply observation corrections and reset-record changes.

        Source observations and reset records are validated against the whole
        post-batch state.  Reset-only changes never increment the source
        revision and never withdraw original packages or take credentials.
        """
        if not _nonempty_str(request_id):
            raise ValueError("request_id must be a nonempty string")
        if not _is_real_int(expected_revision) or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(upserts, list) or len(upserts) > _MAX_ITEMS:
            raise ValueError("upserts must be a list of at most 100000 events")
        if not isinstance(deletes, list) or len(deletes) > _MAX_ITEMS:
            raise ValueError("deletes must be a list of at most 100000 event ids")
        if not isinstance(add_resets, list) or not 0 <= len(add_resets) <= _MAX_RESET_BATCH:
            raise ValueError(
                "add_resets must be a list of at most 1000 reset records"
            )
        if (
            not isinstance(withdraw_reset_ids, list)
            or not 0 <= len(withdraw_reset_ids) <= _MAX_RESET_BATCH
        ):
            raise ValueError(
                "withdraw_reset_ids must be a list of at most 1000 reset ids"
            )

        start_day = _parse_day(self._state["config"]["start_day"])
        end_day = _parse_day(self._state["config"]["end_day"])

        normalized_upserts: list[dict[str, Any]] = []
        upsert_ids: set[str] = set()
        for raw_event in upserts:
            event = _normalize_event(
                raw_event, self._checkpoint_owner, start_day, end_day
            )
            eid = event["id"]
            if eid in upsert_ids:
                raise ValueError("upsert event ids must be unique within a request")
            upsert_ids.add(eid)
            normalized_upserts.append(event)

        normalized_deletes: list[str] = []
        delete_ids: set[str] = set()
        for raw_id in deletes:
            if not _nonempty_str(raw_id):
                raise ValueError("delete ids must be nonempty strings")
            assert isinstance(raw_id, str)
            if raw_id in delete_ids:
                raise ValueError("delete ids must be unique within a request")
            delete_ids.add(raw_id)
            normalized_deletes.append(raw_id)

        if upsert_ids.intersection(delete_ids):
            raise ValueError("the same event id cannot be upserted and deleted")

        normalized_resets: list[dict[str, Any]] = []
        add_reset_ids: set[str] = set()
        for raw_reset in add_resets:
            reset = _normalize_reset(
                raw_reset,
                self._checkpoint_owner,
                self._sections_by_id,
                start_day,
                end_day,
            )
            reset_id = reset["id"]
            if reset_id in add_reset_ids:
                raise ValueError("reset ids must be unique within a request")
            add_reset_ids.add(reset_id)
            normalized_resets.append(reset)

        normalized_withdrawals: list[str] = []
        withdrawal_ids: set[str] = set()
        for raw_id in withdraw_reset_ids:
            if not _nonempty_str(raw_id):
                raise ValueError("withdraw reset ids must be nonempty strings")
            assert isinstance(raw_id, str)
            if raw_id in withdrawal_ids:
                raise ValueError(
                    "withdraw reset ids must be unique within a request"
                )
            withdrawal_ids.add(raw_id)
            normalized_withdrawals.append(raw_id)

        if add_reset_ids.intersection(withdrawal_ids):
            raise ValueError(
                "the same reset id cannot be added and withdrawn in one request"
            )

        identity = _identity_digest(
            {
                "op": "revise_with_resets",
                "request_id": request_id,
                "expected_revision": expected_revision,
                # Sorted arrays make input order irrelevant to request identity;
                # UTC-equivalent timestamp spelling is already normalized.
                "upserts": sorted(normalized_upserts, key=lambda event: event["id"]),
                "deletes": sorted(normalized_deletes),
                "add_resets": sorted(
                    normalized_resets, key=lambda record: record["id"]
                ),
                "withdraw_reset_ids": sorted(normalized_withdrawals),
            }
        )
        replayed = self._replay_saved(request_id, identity)
        if replayed is not None:
            return replayed

        current_revision = int(self._state["revision"])
        if expected_revision != current_revision:
            raise ValueError("revision conflict")

        events: dict[str, dict[str, Any]] = self._state["events"]
        reset_records: dict[str, dict[str, Any]] = self._state["resets"]
        reset_active: dict[str, bool] = self._state["reset_active"]

        # -- Current-reference checks --------------------------------------
        missing = [eid for eid in normalized_deletes if eid not in events]
        if missing:
            raise ValueError("deletes must identify currently existing events")

        for reset_id in normalized_withdrawals:
            if reset_id not in reset_records:
                raise ValueError("withdraw targets must be existing reset records")
            if not reset_active.get(reset_id, False):
                raise ValueError("withdraw targets must be currently active resets")

        for reset in normalized_resets:
            # Reset ids are permanently unique, including withdrawn records,
            # and live in a namespace separate from observation ids.
            if reset["id"] in reset_records:
                raise ValueError("reset ids are permanently unique within an archive")

        added_count = sum(
            1 for event in normalized_upserts if event["id"] not in events
        )
        candidate_count = len(events) - len(normalized_deletes) + added_count
        if candidate_count > _MAX_ITEMS:
            raise ValueError("the revised event set may contain at most 100000 events")

        # -- Final active reset set: limits and same-instant conflicts ------
        # Judged on the final effective collection, so an old record withdrawn
        # in this same batch may be replaced at the same instant by a new id.
        final_active: dict[str, dict[str, Any]] = {
            reset_id: copy.deepcopy(record)
            for reset_id, record in reset_records.items()
            if reset_active.get(reset_id, False)
            and reset_id not in withdrawal_ids
        }
        for reset in normalized_resets:
            final_active[reset["id"]] = copy.deepcopy(reset)

        if len(final_active) > _MAX_ACTIVE_RESETS:
            raise ValueError("an archive may hold at most 1000 active resets")

        instant_owner: dict[tuple[str, int], str] = {}
        for reset_id, record in final_active.items():
            key = (
                record["section_id"],
                timestamp_microseconds(record["time"]),
            )
            owner = instant_owner.get(key)
            if owner is not None:
                raise ValueError(
                    "a section may have at most one active reset at a given UTC instant"
                )
            instant_owner[key] = reset_id

        # -- Post-batch observations ----------------------------------------
        candidate_events = dict(events)
        for eid in normalized_deletes:
            del candidate_events[eid]
        for event in normalized_upserts:
            candidate_events[event["id"]] = dict(event)

        affected: dict[str, str] = {}
        changed = False

        def mark(event: dict[str, Any]) -> None:
            sid = self._checkpoint_owner[event["checkpoint"]]
            day = event["time"][:10]
            previous = affected.get(sid)
            if previous is None or day < previous:
                affected[sid] = day

        for eid in normalized_deletes:
            changed = True
            mark(events[eid])
        for new_event in normalized_upserts:
            old_event = events.get(new_event["id"])
            if old_event != new_event:
                changed = True
                if old_event is not None:
                    mark(old_event)
                mark(new_event)

        # -- Whole-batch reset evidence replay ------------------------------
        failures = self._reset_failures(
            candidate_events, list(final_active.values())
        )
        if failures:
            raise ValueError(
                "reset evidence no longer matches the unclosed entries: "
                + ", ".join(failures)
            )

        # -- Commit: source revision first, then reset history --------------
        new_revision = current_revision
        if changed:
            new_revision = current_revision + 1
            for eid in normalized_deletes:
                del events[eid]
            for event in normalized_upserts:
                events[event["id"]] = dict(event)
            for sid, earliest_day in affected.items():
                self._invalidate_from(sid, earliest_day, new_revision)
            self._state["revision"] = new_revision
            self._grouped = None

        for reset_id in normalized_withdrawals:
            # Withdrawal only flips the active flag; the canonical body stays.
            reset_active[reset_id] = False
        for reset in normalized_resets:
            reset_records[reset["id"]] = copy.deepcopy(reset)
            reset_active[reset["id"]] = True

        result = {
            "revision": new_revision,
            "changed": changed,
            "added_reset_ids": sorted(add_reset_ids),
            "withdrawn_reset_ids": sorted(withdrawal_ids),
        }
        self._put_request(request_id, identity, result)
        self._commit(request_id)
        return copy.deepcopy(result)

    def _invalidate_from(self, section_id: str, earliest_day: str, revision: int) -> None:
        day_index = self._state["day_index"].get(section_id, {})
        statuses = self._state["package_status"]
        takes = self._state["takes"]
        take_index = self._state["take_index"]

        for day, package_ids in day_index.items():
            if day < earliest_day:
                continue
            for package_id in package_ids:
                if statuses.get(package_id) is None:
                    # Keep the first revision that withdrew this package.
                    statuses[package_id] = revision
                    for receipt_id in take_index.get(package_id, []):
                        receipt_record = takes[receipt_id]
                        if receipt_record.get("invalidated_by_revision") is None:
                            receipt_record["invalidated_by_revision"] = revision

    # ------------------------------------------------------------------
    # Packages
    # ------------------------------------------------------------------

    def _current_package_id(self, section_id: str, day: str) -> str | None:
        package_ids = self._state["day_index"].get(section_id, {}).get(day, [])
        for package_id in reversed(package_ids):
            if self._state["package_status"].get(package_id) is None:
                return package_id
        return None

    def _group_events(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        revision = int(self._state["revision"])
        if self._grouped is not None and self._grouped_revision == revision:
            return self._grouped

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in self._state["events"].values():
            section_id = self._checkpoint_owner[event["checkpoint"]]
            day = event["time"][:10]
            groups.setdefault((section_id, day), []).append(event)

        for group in groups.values():
            group.sort(key=lambda event: (_parse_timestamp(event["time"]), event["id"]))

        self._grouped = groups
        self._grouped_revision = revision
        return groups

    # ------------------------------------------------------------------
    # Offline reset reconciliation: replay, verification, reviewed days
    # ------------------------------------------------------------------

    def _active_reset_records(self) -> list[dict[str, Any]]:
        active = self._state["reset_active"]
        records = self._state["resets"]
        return [
            copy.deepcopy(records[reset_id])
            for reset_id in active
            if active.get(reset_id)
        ]

    @staticmethod
    def _same_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
        """Full five-field observation equality, UTC time spelling tolerant."""
        if left.get("id") != right.get("id"):
            return False
        if left.get("train_id") != right.get("train_id"):
            return False
        if left.get("checkpoint") != right.get("checkpoint"):
            return False
        if left.get("axles") != right.get("axles"):
            return False
        try:
            return timestamp_microseconds(left["time"]) == timestamp_microseconds(
                right["time"]
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _reset_failures(
        self,
        candidate_events: dict[str, dict[str, Any]],
        active_records: list[dict[str, Any]],
    ) -> list[str]:
        """Replay every section with active resets and return failing reset ids.

        The check uses the whole candidate observation set (including days
        after any correction), the final active reset set, and never depends on
        the order of the submitted arrays.
        """
        if not active_records:
            return []

        section_ids = {record["section_id"] for record in active_records}
        events_by_section: dict[str, list[dict[str, Any]]] = {}
        for event in candidate_events.values():
            sid = self._checkpoint_owner[event["checkpoint"]]
            if sid in section_ids:
                events_by_section.setdefault(sid, []).append(dict(event))

        failures: list[str] = []
        for section_id in section_ids:
            section = self._sections_by_id[section_id]
            events = sorted(
                events_by_section.get(section_id, []),
                key=lambda event: (
                    timestamp_microseconds(event["time"]),
                    event["id"],
                ),
            )
            resets = sorted(
                (
                    record
                    for record in active_records
                    if record["section_id"] == section_id
                ),
                key=lambda record: (
                    timestamp_microseconds(record["time"]),
                    record["id"],
                ),
            )

            # One ordered stream; at the same UTC instant a reset precedes the
            # observations of that instant.
            timeline: list[tuple[int, int, str, Any]] = []
            for event in events:
                timeline.append(
                    (timestamp_microseconds(event["time"]), 1, event["id"], event)
                )
            for record in resets:
                timeline.append(
                    (timestamp_microseconds(record["time"]), 0, record["id"], record)
                )
            timeline.sort(key=lambda item: (item[0], item[1], item[2]))  # reset kind 0 before event kind 1

            active: dict[str, dict[str, Any]] = {}
            for _, kind, _, payload in timeline:
                if kind == 0:
                    record = payload
                    opening = record["opening"]
                    current = sorted(
                        active.values(),
                        key=lambda event: event["id"],
                    )
                    authorized = sorted(
                        (dict(entry) for entry in opening),
                        key=lambda event: event["id"],
                    )
                    matches = len(current) == len(authorized) and all(
                        self._same_evidence(current_entry, authorized_entry)
                        for current_entry, authorized_entry in zip(
                            current, authorized
                        )
                    )
                    if not matches:
                        failures.append(record["id"])
                        continue
                    # The opening is the complete current unclosed set, so a
                    # valid write-off releases every occupied entry.  It
                    # creates no movement and fabricates no observation.
                    active = {}
                    continue

                event = payload
                train_id = event["train_id"]
                checkpoint = event["checkpoint"]
                if checkpoint == section["entry"]:
                    if train_id not in active:
                        active[train_id] = dict(event)
                    # A later entry while the train is open is a duplicate and
                    # never replaces the still-open original entry.
                elif checkpoint == section["exit"]:
                    active.pop(train_id, None)

        return sorted(set(failures))

    def _guard_resets(
        self, candidate_events: dict[str, dict[str, Any]]
    ) -> None:
        """Reject a write whose final state breaks any still-active reset."""
        active_records = self._active_reset_records()
        failures = self._reset_failures(candidate_events, active_records)
        if failures:
            raise ValueError(
                "correction would invalidate registered reset(s): "
                + ", ".join(failures)
            )

    def _reviewed_replay(
        self,
        section_id: str,
        day_text: str,
        events: list[dict[str, Any]],
        resets: list[dict[str, Any]],
        opening: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run one UTC day of reset-aware review from a supplied opening set."""
        section = self._sections_by_id[section_id]
        entry_checkpoint = section["entry"]
        exit_checkpoint = section["exit"]

        timeline: list[tuple[int, int, str, Any]] = []
        for event in events:
            timeline.append(
                (timestamp_microseconds(event["time"]), 1, event["id"], event)
            )
        for record in resets:
            timeline.append(
                (timestamp_microseconds(record["time"]), 0, record["id"], record)
            )
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))  # reset kind 0 before event kind 1

        active: dict[str, dict[str, Any]] = {
            event["train_id"]: dict(event) for event in opening
        }
        movements: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        day_resets: list[dict[str, Any]] = []

        for _, kind, _, payload in timeline:
            if kind == 0:
                record = payload
                day_resets.append(copy.deepcopy(record))
                current = sorted(active.values(), key=lambda event: event["id"])
                authorized = sorted(
                    (dict(entry) for entry in record["opening"]),
                    key=lambda event: event["id"],
                )
                matches = len(current) == len(authorized) and all(
                    self._same_evidence(current_entry, authorized_entry)
                    for current_entry, authorized_entry in zip(
                        current, authorized
                    )
                )
                if not matches:
                    raise ValueError(
                        f"reset {record['id']} no longer matches its authorized "
                        "opening under current observations"
                    )
                # The opening is the complete current unclosed set; a valid
                # write-off releases every occupied entry without a movement.
                active = {}
                continue

            event = payload
            train_id = event["train_id"]
            checkpoint = event["checkpoint"]
            if checkpoint == entry_checkpoint:
                if train_id in active:
                    anomalies.append(
                        {
                            "event_id": event["id"],
                            "section_id": section_id,
                            "train_id": train_id,
                            "type": "duplicate_entry",
                        }
                    )
                else:
                    active[train_id] = dict(event)
            elif checkpoint == exit_checkpoint:
                opened = active.pop(train_id, None)
                if opened is None:
                    anomalies.append(
                        {
                            "event_id": event["id"],
                            "section_id": section_id,
                            "train_id": train_id,
                            "type": "exit_without_entry",
                        }
                    )
                else:
                    entry_time = _parse_timestamp(opened["time"])
                    exit_time = _parse_timestamp(event["time"])
                    movements.append(
                        {
                            "section_id": section_id,
                            "train_id": train_id,
                            "entry_event_id": opened["id"],
                            "exit_event_id": event["id"],
                            "entry_axles": opened["axles"],
                            "exit_axles": event["axles"],
                            "duration_seconds": int(
                                (exit_time - entry_time).total_seconds()
                            ),
                            "status": (
                                "balanced"
                                if opened["axles"] == event["axles"]
                                else "axle_mismatch"
                            ),
                        }
                    )
            else:
                raise ValueError(
                    "checkpoint is not an entry or exit for its section"
                )

        movements.sort(key=lambda item: (item["section_id"], item["entry_event_id"]))
        anomalies.sort(key=lambda item: item["event_id"])
        closing = sorted(
            (dict(event) for event in active.values()),
            key=lambda event: (event["train_id"], event["id"]),
        )
        day_resets.sort(
            key=lambda record: (
                timestamp_microseconds(record["time"]),
                record["id"],
            )
        )
        return {
            "movements": movements,
            "anomalies": anomalies,
            "closing": closing,
            "resets": day_resets,
        }

    def reviewed_day(self, section_id: object, day: object) -> dict[str, Any]:
        """Current reset-aware review result for one section and UTC day.

        This is a review view, not a handoff package: it creates no package, no
        take credential and requires no sealed day package.
        """
        if not _nonempty_str(section_id) or section_id not in self._sections_by_id:
            raise ValueError("unknown section")
        assert isinstance(section_id, str)
        parsed_day = _parse_day(day)
        config = self._state["config"]
        start_day = _parse_day(config["start_day"])
        end_day = _parse_day(config["end_day"])
        if not start_day <= parsed_day <= end_day:
            raise ValueError("day is outside the archive date range")
        day_text = parsed_day.isoformat()

        section_events = sorted(
            (
                dict(event)
                for event in self._state["events"].values()
                if self._checkpoint_owner[event["checkpoint"]] == section_id
            ),
            key=lambda event: (
                timestamp_microseconds(event["time"]),
                event["id"],
            ),
        )
        active_records = [
            record
            for record in self._active_reset_records()
            if record["section_id"] == section_id
        ]

        # Replay every day from the first range day so the opening set carries
        # real entries across empty days, including through reset write-offs.
        opening: list[dict[str, Any]] = []
        movements: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        day_resets: list[dict[str, Any]] = []
        closing: list[dict[str, Any]] = []
        midnight_opening: list[dict[str, Any]] = []
        for ordinal in range(start_day.toordinal(), parsed_day.toordinal() + 1):
            current_day = date.fromordinal(ordinal).isoformat()
            if current_day == day_text:
                midnight_opening = copy.deepcopy(opening)
            day_events = [
                event
                for event in section_events
                if event["time"][:10] == current_day
            ]
            day_reset_records = [
                record
                for record in active_records
                if record["time"][:10] == current_day
            ]
            result = self._reviewed_replay(
                section_id,
                current_day,
                day_events,
                day_reset_records,
                opening,
            )
            if current_day == day_text:
                movements = result["movements"]
                anomalies = result["anomalies"]
                day_resets = result["resets"]
            opening = result["closing"]
            closing = result["closing"]

        return {
            "section_id": section_id,
            "day": day_text,
            "opening": midnight_opening,
            "movements": movements,
            "anomalies": anomalies,
            "closing": closing,
            "resets": day_resets,
        }

    def read_reset(self, reset_id: object) -> dict[str, Any]:
        """Return the canonical reset body and its current active flag."""
        if not _nonempty_str(reset_id) or reset_id not in self._state["resets"]:
            raise ValueError("unknown reset")
        assert isinstance(reset_id, str)
        return {
            "record": copy.deepcopy(self._state["resets"][reset_id]),
            "active": bool(self._state["reset_active"].get(reset_id, False)),
        }

    def seal(self, section_id: str, day: object, request_id: str) -> dict[str, Any]:
        if not _nonempty_str(request_id):
            raise ValueError("request_id must be a nonempty string")
        if not _nonempty_str(section_id) or section_id not in self._sections_by_id:
            raise ValueError("unknown section")
        assert isinstance(section_id, str)
        parsed_day = _parse_day(day)
        config = self._state["config"]
        start_day = _parse_day(config["start_day"])
        end_day = _parse_day(config["end_day"])
        if not start_day <= parsed_day <= end_day:
            raise ValueError("day is outside the archive date range")
        day_text = parsed_day.isoformat()

        identity = _identity_digest(
            {
                "op": "seal",
                "request_id": request_id,
                "section_id": section_id,
                "day": day_text,
            }
        )
        replayed = self._replay_saved(request_id, identity)
        if replayed is not None:
            return replayed

        existing_package_id = self._current_package_id(section_id, day_text)
        if existing_package_id is not None:
            package = self._state["packages"][existing_package_id]
            result = copy.deepcopy(package)
            self._put_request(request_id, identity, result)
            self._commit(request_id)
            return result

        parent_package_id: str | None = None
        opening: list[dict[str, Any]] = []
        if parsed_day > start_day:
            previous_day = (parsed_day - timedelta(days=1)).isoformat()
            parent_package_id = self._current_package_id(section_id, previous_day)
            if parent_package_id is None:
                raise ValueError("previous calendar day has no currently valid parent package")
            parent = self._state["packages"][parent_package_id]
            opening = copy.deepcopy(parent["closing"])

        section = self._sections_by_id[section_id]
        entry_checkpoint = section["entry"]
        exit_checkpoint = section["exit"]
        events = self._group_events().get((section_id, day_text), [])

        active: dict[str, dict[str, Any]] = {
            event["train_id"]: copy.deepcopy(event) for event in opening
        }
        movements: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []

        for event in events:
            train_id = event["train_id"]
            checkpoint = event["checkpoint"]
            if checkpoint == entry_checkpoint:
                if train_id in active:
                    anomalies.append(
                        {
                            "event_id": event["id"],
                            "section_id": section_id,
                            "train_id": train_id,
                            "type": "duplicate_entry",
                        }
                    )
                else:
                    active[train_id] = copy.deepcopy(event)
            elif checkpoint == exit_checkpoint:
                opened = active.pop(train_id, None)
                if opened is None:
                    anomalies.append(
                        {
                            "event_id": event["id"],
                            "section_id": section_id,
                            "train_id": train_id,
                            "type": "exit_without_entry",
                        }
                    )
                else:
                    entry_time = _parse_timestamp(opened["time"])
                    exit_time = _parse_timestamp(event["time"])
                    movements.append(
                        {
                            "section_id": section_id,
                            "train_id": train_id,
                            "entry_event_id": opened["id"],
                            "exit_event_id": event["id"],
                            "entry_axles": opened["axles"],
                            "exit_axles": event["axles"],
                            "duration_seconds": int(
                                (exit_time - entry_time).total_seconds()
                            ),
                            "status": (
                                "balanced"
                                if opened["axles"] == event["axles"]
                                else "axle_mismatch"
                            ),
                        }
                    )
            else:
                # Defensive: normalized events always map to entry or exit.
                raise ValueError("checkpoint is not an entry or exit for its section")

        movements.sort(key=lambda item: (item["section_id"], item["entry_event_id"]))
        anomalies.sort(key=lambda item: item["event_id"])
        closing = sorted(
            (copy.deepcopy(event) for event in active.values()),
            key=lambda event: (event["train_id"], event["id"]),
        )

        self._state["package_seq"] = int(self._state["package_seq"]) + 1
        package_id = f"package-{self._state['package_seq']}"
        package: dict[str, Any] = {
            "package_id": package_id,
            "section_id": section_id,
            "day": day_text,
            "revision": int(self._state["revision"]),
            "parent_package_id": parent_package_id,
            "events": copy.deepcopy(events),
            "opening": opening,
            "movements": movements,
            "anomalies": anomalies,
            "closing": closing,
        }

        self._state["packages"][package_id] = package
        self._state["package_status"][package_id] = None
        self._state["day_index"].setdefault(section_id, {}).setdefault(
            day_text, []
        ).append(package_id)
        self._state["take_index"].setdefault(package_id, [])

        result = copy.deepcopy(package)
        self._put_request(request_id, identity, result)
        self._commit(request_id)
        return result

    def current(self, section_id: object, day: object) -> dict[str, Any]:
        if not _nonempty_str(section_id) or section_id not in self._sections_by_id:
            raise ValueError("unknown section")
        assert isinstance(section_id, str)
        parsed_day = _parse_day(day)
        start_day = _parse_day(self._state["config"]["start_day"])
        end_day = _parse_day(self._state["config"]["end_day"])
        if not start_day <= parsed_day <= end_day:
            raise ValueError("day is outside the archive date range")

        package_id = self._current_package_id(section_id, parsed_day.isoformat())
        if package_id is None:
            raise ValueError("no currently valid package for section and day")
        return copy.deepcopy(self._state["packages"][package_id])

    def read_package(self, package_id: object) -> dict[str, Any]:
        if not _nonempty_str(package_id) or package_id not in self._state["packages"]:
            raise ValueError("unknown package")
        assert isinstance(package_id, str)
        invalidated_by_revision = self._state["package_status"].get(package_id)
        return {
            "package": copy.deepcopy(self._state["packages"][package_id]),
            "valid": invalidated_by_revision is None,
            "invalidated_by_revision": invalidated_by_revision,
        }

    # ------------------------------------------------------------------
    # Takes
    # ------------------------------------------------------------------

    def take(self, package_id: object, request_id: str) -> dict[str, Any]:
        if not _nonempty_str(request_id):
            raise ValueError("request_id must be a nonempty string")
        if not _nonempty_str(package_id) or package_id not in self._state["packages"]:
            raise ValueError("unknown package")
        assert isinstance(package_id, str)

        identity = _identity_digest(
            {
                "op": "take",
                "request_id": request_id,
                "package_id": package_id,
            }
        )
        replayed = self._replay_saved(request_id, identity)
        if replayed is not None:
            return replayed

        if self._state["package_status"].get(package_id) is not None:
            raise ValueError("package is no longer valid")

        self._state["receipt_seq"] = int(self._state["receipt_seq"]) + 1
        receipt_id = f"receipt-{self._state['receipt_seq']}"
        receipt = {"receipt_id": receipt_id, "package_id": package_id}
        self._state["takes"][receipt_id] = {
            "receipt": receipt,
            "package_id": package_id,
            "invalidated_by_revision": None,
        }
        self._state["take_index"].setdefault(package_id, []).append(receipt_id)

        result = copy.deepcopy(receipt)
        self._put_request(request_id, identity, result)
        self._commit(request_id)
        return result

    def read_take(self, receipt_id: object) -> dict[str, Any]:
        if not _nonempty_str(receipt_id) or receipt_id not in self._state["takes"]:
            raise ValueError("unknown receipt")
        assert isinstance(receipt_id, str)
        record = self._state["takes"][receipt_id]
        invalidated_by_revision = record.get("invalidated_by_revision")
        if invalidated_by_revision is None:
            # Defensive consistency with the package's first invalidation.
            package_id = record.get("package_id")
            invalidated_by_revision = self._state["package_status"].get(package_id)
        return {
            "receipt": copy.deepcopy(record["receipt"]),
            "valid": invalidated_by_revision is None,
            "invalidated_by_revision": invalidated_by_revision,
        }

    # ------------------------------------------------------------------
    # Identity plans
    # ------------------------------------------------------------------

    def _section_events(self, section_id: str) -> list[dict[str, Any]]:
        """Return detached copies of every current observation of a section."""
        return [
            dict(event)
            for event in self._state["events"].values()
            if self._checkpoint_owner[event["checkpoint"]] == section_id
        ]

    @staticmethod
    def _normalize_identity_rules(
        min_seconds: object,
        max_seconds: object,
        target_seconds: object,
        max_axle_gap: object,
    ) -> dict[str, int]:
        values = (min_seconds, max_seconds, target_seconds, max_axle_gap)
        if not all(_is_real_int(value) for value in values):
            raise ValueError("identity rule parameters must be integers")
        assert _is_real_int(min_seconds)
        assert _is_real_int(max_seconds)
        assert _is_real_int(target_seconds)
        assert _is_real_int(max_axle_gap)
        if not 0 <= min_seconds <= target_seconds <= max_seconds <= 604800:
            raise ValueError(
                "identity seconds must satisfy 0 <= min <= target <= max <= 604800"
            )
        if not 0 <= max_axle_gap <= 999:
            raise ValueError("max_axle_gap must be an integer in 0..999")
        return {
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
            "target_seconds": target_seconds,
            "max_axle_gap": max_axle_gap,
        }

    @staticmethod
    def _normalize_exit_ids(exit_ids: object) -> list[str]:
        if not isinstance(exit_ids, list) or not 1 <= len(exit_ids) <= 32:
            raise ValueError(
                "unidentified_exit_ids must be a list of 1..32 event ids"
            )
        seen: set[str] = set()
        normalized: list[str] = []
        for exit_id in exit_ids:
            if not _nonempty_str(exit_id):
                raise ValueError("exit event ids must be nonempty strings")
            assert isinstance(exit_id, str)
            if exit_id in seen:
                raise ValueError("exit event ids must be unique")
            seen.add(exit_id)
            normalized.append(exit_id)
        return normalized

    def _identity_evidence(
        self,
        section_id: str,
        exit_ids: list[str],
        rules: dict[str, int],
        section_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        section = self._sections_by_id[section_id]
        events = (
            self._section_events(section_id)
            if section_events is None
            else section_events
        )
        by_id = {event["id"]: event for event in events}
        exits: list[dict[str, Any]] = []
        for exit_id in sorted(exit_ids):
            event = by_id.get(exit_id)
            if event is None:
                raise ValueError(
                    "unidentified exit is not a current observation of the section"
                )
            if event["checkpoint"] != section["exit"]:
                raise ValueError(
                    "unidentified exit is not an exit observation of the section"
                )
            exits.append(dict(event))
        return build_identity_evidence(
            events,
            section["entry"],
            section["exit"],
            exits,
            min_seconds=rules["min_seconds"],
            max_seconds=rules["max_seconds"],
            target_seconds=rules["target_seconds"],
            max_axle_gap=rules["max_axle_gap"],
        )

    def plan_identities(
        self,
        section_id: object,
        unidentified_exit_ids: object,
        min_seconds: object,
        max_seconds: object,
        target_seconds: object,
        max_axle_gap: object,
        request_id: object,
    ) -> dict[str, Any]:
        """Create and persist an immutable identity-disambiguation plan."""
        if not _nonempty_str(request_id):
            raise ValueError("request_id must be a nonempty string")
        if not _nonempty_str(section_id) or section_id not in self._sections_by_id:
            raise ValueError("unknown section")
        assert isinstance(section_id, str)
        exit_ids = self._normalize_exit_ids(unidentified_exit_ids)
        rules = self._normalize_identity_rules(
            min_seconds, max_seconds, target_seconds, max_axle_gap
        )

        identity = _identity_digest(
            {
                "op": "plan_identities",
                "request_id": request_id,
                "section_id": section_id,
                # Exit array order does not affect request identity.
                "unidentified_exit_ids": sorted(exit_ids),
                "rules": rules,
            }
        )
        replayed = self._replay_saved(request_id, identity)
        if replayed is not None:
            return replayed

        evidence = self._identity_evidence(section_id, exit_ids, rules)

        self._state["plan_seq"] = int(self._state["plan_seq"]) + 1
        plan_id = f"plan-{self._state['plan_seq']}"
        body: dict[str, Any] = {
            "plan_id": plan_id,
            "section_id": section_id,
            "source_revision": int(self._state["revision"]),
            "unidentified_exit_ids": list(exit_ids),
        }
        body.update(rules)
        body.update(evidence)
        self._state["plans"][plan_id] = copy.deepcopy(body)
        result = copy.deepcopy(body)
        self._put_request(request_id, identity, result)
        self._commit(request_id)
        return result

    def read_identity_plan(self, plan_id: object) -> dict[str, Any]:
        if not _nonempty_str(plan_id) or plan_id not in self._state["plans"]:
            raise ValueError("unknown identity plan")
        assert isinstance(plan_id, str)
        return copy.deepcopy(self._state["plans"][plan_id])

    @staticmethod
    def _same_observation(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if left.get("id") != right.get("id"):
            return False
        if left.get("train_id") != right.get("train_id"):
            return False
        if left.get("checkpoint") != right.get("checkpoint"):
            return False
        if left.get("axles") != right.get("axles"):
            return False
        try:
            return timestamp_microseconds(left["time"]) == timestamp_microseconds(
                right["time"]
            )
        except (KeyError, TypeError, ValueError):
            return False

    def apply_identities(
        self, plan_id: object, request_id: object
    ) -> dict[str, Any]:
        """Back-fill only the forced pairs of a saved plan, if still licensed."""
        if not _nonempty_str(request_id):
            raise ValueError("request_id must be a nonempty string")
        if not _nonempty_str(plan_id) or plan_id not in self._state["plans"]:
            raise ValueError("unknown identity plan")
        assert isinstance(plan_id, str)

        identity = _identity_digest(
            {
                "op": "apply_identities",
                "request_id": request_id,
                "plan_id": plan_id,
            }
        )
        replayed = self._replay_saved(request_id, identity)
        if replayed is not None:
            return replayed

        plan = self._state["plans"][plan_id]
        forced_pairs: list[dict[str, Any]] = plan["forced_pairs"]
        if not forced_pairs:
            raise ValueError("identity plan has no forced pairs to apply")

        section_id = plan["section_id"]
        if section_id not in self._sections_by_id:
            raise ValueError("unknown plan section")
        section = self._sections_by_id[section_id]
        rules = {
            key: plan[key]
            for key in (
                "min_seconds",
                "max_seconds",
                "target_seconds",
                "max_axle_gap",
            )
        }
        exit_ids = list(plan["unidentified_exit_ids"])

        # -- 1. Recompute the identity range from current observations. ----
        evidence = self._identity_evidence(section_id, exit_ids, rules)
        current_decisions = {
            decision["exit_event_id"]: decision
            for decision in evidence["decisions"]
        }
        for pair in forced_pairs:
            decision = current_decisions.get(pair["exit_event_id"])
            if decision is None:
                raise ValueError("forced exit is no longer a designated exit")
            if (
                decision["status"] != "forced"
                or decision["may_be_unmatched"]
                or decision["possible_entry_ids"] != [pair["entry_event_id"]]
            ):
                raise ValueError(
                    "a forced association is no longer forced under current evidence"
                )

        # -- 2. The original forced observations must be byte-for-byte the
        # same evidence, except for UTC-equivalent timestamp spelling. -------
        current_by_id = {
            event["id"]: event for event in self._section_events(section_id)
        }
        plan_entries = {
            event["id"]: event for event in plan["entry_candidates"]
        }
        plan_exits = {event["id"]: event for event in plan["exits"]}
        for pair in forced_pairs:
            entry_id = pair["entry_event_id"]
            exit_id = pair["exit_event_id"]
            original_entry = plan_entries.get(entry_id)
            original_exit = plan_exits.get(exit_id)
            if original_entry is None or original_exit is None:
                raise ValueError("plan evidence is missing a forced observation")
            current_entry = current_by_id.get(entry_id)
            current_exit = current_by_id.get(exit_id)
            if current_entry is None or current_exit is None:
                raise ValueError("a forced observation disappeared or moved section")
            if not self._same_observation(current_entry, original_entry):
                raise ValueError("a forced entry observation changed")
            if not self._same_observation(current_exit, original_exit):
                raise ValueError("a forced exit observation changed")
            if current_entry["checkpoint"] != section["entry"]:
                raise ValueError("a forced entry moved off the section entry")
            if current_exit["checkpoint"] != section["exit"]:
                raise ValueError("a forced exit moved off the section exit")

        # -- 3. Staged verification with the old real pairing semantics. ----
        section_events = self._section_events(section_id)
        designated = set(exit_ids)
        baseline = pair_events(
            [event for event in section_events if event["id"] not in designated],
            section["entry"],
            section["exit"],
        )

        new_train_by_exit: dict[str, str] = {}
        for pair in forced_pairs:
            new_train_by_exit[pair["exit_event_id"]] = pair["train_id"]
        staged_events = [
            dict(event, train_id=new_train_by_exit[event["id"]])
            if event["id"] in new_train_by_exit
            else dict(event)
            for event in section_events
        ]
        staged = pair_events(staged_events, section["entry"], section["exit"])
        staged_pairs = {
            (movement["entry_event_id"], movement["exit_event_id"])
            for movement in staged["movements"]
        }
        for pair in forced_pairs:
            if (pair["entry_event_id"], pair["exit_event_id"]) not in staged_pairs:
                raise ValueError(
                    "a forced pair would not produce the promised movement"
                )
        for movement in baseline["movements"]:
            if (
                movement["entry_event_id"],
                movement["exit_event_id"],
            ) not in staged_pairs:
                raise ValueError(
                    "back-fill would destroy an existing normal movement"
                )

        # -- 3b. A reset write-off is a hard constraint on source writes. ----
        # Back-filling a label may close an entry that a registered reset was
        # authorized to write off, so the whole post-apply observation set is
        # replayed before any label is changed.  apply_identities has no
        # permission to add or withdraw resets itself.
        candidate_events = dict(self._state["events"])
        for exit_id, new_train in new_train_by_exit.items():
            candidate_events[exit_id] = dict(
                candidate_events[exit_id], train_id=new_train
            )
        self._guard_resets(candidate_events)

        # -- 4. Commit one ordinary source revision with all corrections. ---
        events = self._state["events"]
        applied_pairs: list[dict[str, Any]] = []
        changed = False
        earliest_day: str | None = None
        for pair in sorted(forced_pairs, key=lambda item: item["exit_event_id"]):
            exit_id = pair["exit_event_id"]
            old_event = events[exit_id]
            old_train = old_event["train_id"]
            new_train = pair["train_id"]
            applied_pairs.append(
                {
                    "entry_event_id": pair["entry_event_id"],
                    "exit_event_id": exit_id,
                    "train_id": new_train,
                    "old_train_id": old_train,
                    "new_train_id": new_train,
                }
            )
            if old_train == new_train:
                continue
            new_event = dict(old_event, train_id=new_train)
            events[exit_id] = new_event
            changed = True
            day = old_event["time"][:10]
            if earliest_day is None or day < earliest_day:
                earliest_day = day

        if changed:
            new_revision = int(self._state["revision"]) + 1
            assert earliest_day is not None
            self._invalidate_from(section_id, earliest_day, new_revision)
            self._state["revision"] = new_revision
            self._grouped = None
        else:
            new_revision = int(self._state["revision"])

        result = {
            "plan_id": plan_id,
            "revision": new_revision,
            "changed": changed,
            "applied_pairs": applied_pairs,
        }
        self._put_request(request_id, identity, result)
        self._commit(request_id)
        return copy.deepcopy(result)

