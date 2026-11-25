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
