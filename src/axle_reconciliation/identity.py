"""Bounded global identity-disambiguation for railway exit observations.

This module is deliberately independent of the persistent archive.  It works
on plain event dictionaries and uses only the Python standard library.

The problem is a small bipartite assignment.  Candidate entry/exit pairs are
restricted to real, currently occupied entries after the analyst-indicated
exit observations have been removed from the baseline pairing.  An explanation
is a one-to-one set of such pairs.  Explanations are ordered lexicographically
by:

1. largest matched pair count;
2. smallest sum of absolute axle-count differences;
3. smallest sum of absolute differences between the exact duration and the
   target duration.

The union of all globally optimal explanations is computed from the residual
graph of a min-cost max-flow solution.  No explanation-count cutoff and no
lexicographic event-ID tie-break is applied.  Only pairs that occur in *every*
optimal explanation are reported as forced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# An explanation has at most 32 pairs.  A single duration error is at most
# 604800 s = 6.048e14 us, so the total duration-error component is below
# 2e16.  This scale therefore keeps the three-level objective exact: a one-unit
# change in axle-gap sum outranks every possible duration-error difference.
_COST_SCALE = 10**20
_MAX_PAIR_COUNT = 32


def timestamp_microseconds(value: str) -> int:
    """Return the exact UTC microsecond offset for an event time string."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid event timestamp") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def pair_events(
    events: list[dict[str, Any]],
    entry_checkpoint: str,
    exit_checkpoint: str,
) -> dict[str, Any]:
    """Apply the baseline real pairing semantics to one section's events.

    The caller may pass an empty list.  Unlike the public ``process`` entry
    point this internal helper simply returns an empty result; it never changes
    the public contract that rejects empty event arrays.
    """
    ordered = sorted(
        events,
        key=lambda event: (timestamp_microseconds(event["time"]), event["id"]),
    )
    active: dict[str, dict[str, Any]] = {}
    movements: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for event in ordered:
        train_id = event["train_id"]
        checkpoint = event["checkpoint"]
        if checkpoint == entry_checkpoint:
            if train_id in active:
                anomalies.append(
                    {
                        "event_id": event["id"],
                        "train_id": train_id,
                        "type": "duplicate_entry",
                    }
                )
            else:
                active[train_id] = event
        elif checkpoint == exit_checkpoint:
            opened = active.pop(train_id, None)
            if opened is None:
                anomalies.append(
                    {
                        "event_id": event["id"],
                        "train_id": train_id,
                        "type": "exit_without_entry",
                    }
                )
            else:
                entry_us = timestamp_microseconds(opened["time"])
                exit_us = timestamp_microseconds(event["time"])
                movements.append(
                    {
                        "train_id": train_id,
                        "entry_event_id": opened["id"],
                        "exit_event_id": event["id"],
                        "entry_axles": opened["axles"],
                        "exit_axles": event["axles"],
                        "duration_seconds": (exit_us - entry_us) // 1_000_000,
                        "status": (
                            "balanced"
                            if opened["axles"] == event["axles"]
                            else "axle_mismatch"
                        ),
                    }
                )
        else:
            raise ValueError("checkpoint is not an entry or exit for its section")

    occupied = sorted(
        (dict(event) for event in active.values()),
        key=lambda event: (event["train_id"], event["id"]),
    )
    movements.sort(key=lambda item: item["entry_event_id"])
    anomalies.sort(key=lambda item: item["event_id"])
    return {
        "movements": movements,
        "anomalies": anomalies,
        "occupied": occupied,
    }


def _add_edge(
    graph: list[list[list[Any]]],
    source: int,
    target: int,
    capacity: int,
    cost: int,
) -> tuple[int, int]:
    forward = [target, len(graph[target]), capacity, cost, capacity]
    backward = [source, len(graph[source]), 0, -cost, 0]
    graph[source].append(forward)
    graph[target].append(backward)
    return source, len(graph[source]) - 1


def _min_cost_max_flow(
    graph: list[list[list[Any]]],
    node_count: int,
    source: int,
    sink: int,
    flow_limit: int,
) -> tuple[int, int]:
    """Successive shortest augmenting paths; returns ``(flow, real_cost)``.

    Residual back edges carry negative costs, so each augmentation uses a
    bounded Bellman-Ford relaxation.  The window is tiny (at most 66 nodes), and
    this avoids relying on potential bookkeeping for nodes that are not
    reachable from the source in every round.
    """
    flow = 0
    total_cost = 0
    infinity: int | None = None

    while flow < flow_limit:
        distance: list[int | None] = [infinity] * node_count
        prev_node = [-1] * node_count
        prev_edge = [-1] * node_count
        distance[source] = 0

        for _ in range(node_count):
            changed = False
            for node in range(node_count):
                node_distance = distance[node]
                if node_distance is None:
                    continue
                for index, edge in enumerate(graph[node]):
                    if edge[2] <= 0:
                        continue
                    candidate = node_distance + edge[3]
                    target = edge[0]
                    if distance[target] is None or candidate < distance[target]:
                        distance[target] = candidate
                        prev_node[target] = node
                        prev_edge[target] = index
                        changed = True
            if not changed:
                break

        if distance[sink] is None:
            break

        addition = flow_limit - flow
        path_cost = 0
        node = sink
        while node != source:
            parent = prev_node[node]
            edge = graph[parent][prev_edge[node]]
            addition = min(addition, edge[2])
            path_cost += edge[3]
            node = parent

        node = sink
        while node != source:
            parent = prev_node[node]
            edge = graph[parent][prev_edge[node]]
            edge[2] -= addition
            graph[node][edge[1]][2] += addition
            node = parent

        flow += addition
        total_cost += addition * path_cost

    return flow, total_cost


def _residual_potentials(
    graph: list[list[list[Any]]], node_count: int
) -> list[int]:
    """Shortest distances from a virtual zero-cost super source.

    The residual graph of a min-cost flow has no negative cycle.  These
    distances therefore make every residual edge's reduced cost nonnegative,
    including edges incident to nodes that were unreachable from the flow
    source during the last augmentation.
    """
    dist = [0] * node_count
    for _ in range(node_count):
        changed = False
        for node in range(node_count):
            node_dist = dist[node]
            for edge in graph[node]:
                if edge[2] > 0:
                    candidate = node_dist + edge[3]
                    if candidate < dist[edge[0]]:
                        dist[edge[0]] = candidate
                        changed = True
        if not changed:
            break
    return dist


def _zero_residual_reachability(
    graph: list[list[list[Any]]],
    node_count: int,
    potentials: list[int],
    starts: list[int],
) -> list[set[int]]:
    zero_adjacency: list[list[int]] = [[] for _ in range(node_count)]
    for node in range(node_count):
        for edge in graph[node]:
            if edge[2] <= 0:
                continue
            if edge[3] + potentials[node] - potentials[edge[0]] == 0:
                zero_adjacency[node].append(edge[0])

    reachable: list[set[int]] = []
    for start in starts:
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for target in zero_adjacency[node]:
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
        reachable.append(seen)
    return reachable


def build_identity_evidence(
    section_events: list[dict[str, Any]],
    entry_checkpoint: str,
    exit_checkpoint: str,
    exits: list[dict[str, Any]],
    *,
    min_seconds: int,
    max_seconds: int,
    target_seconds: int,
    max_axle_gap: int,
) -> dict[str, Any]:
    """Compute candidate evidence, optimum values and the identity range.

    ``exits`` must contain the already validated, id-sorted designated exit
    observations.  The return value is JSON-serialisable plain data.
    """
    exit_ids = {event["id"] for event in exits}
    remaining = [
        event for event in section_events if event["id"] not in exit_ids
    ]
    baseline = pair_events(remaining, entry_checkpoint, exit_checkpoint)
    candidates = sorted(baseline["occupied"], key=lambda event: event["id"])
    if len(candidates) > _MAX_PAIR_COUNT:
        raise ValueError("more than 32 candidate entries in this plan window")

    candidate_ids = [event["id"] for event in candidates]
    exit_id_list = [event["id"] for event in exits]

    min_us = min_seconds * 1_000_000
    max_us = max_seconds * 1_000_000
    target_us = target_seconds * 1_000_000

    entry_times = {
        event["id"]: timestamp_microseconds(event["time"])
        for event in candidates
    }
    exit_times = {
        event["id"]: timestamp_microseconds(event["time"]) for event in exits
    }

    edges: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(candidates):
        entry_us = entry_times[entry["id"]]
        for exit_index, exit_event in enumerate(exits):
            exit_us = exit_times[exit_event["id"]]
            if (entry_us, entry["id"]) >= (exit_us, exit_event["id"]):
                continue
            interval = exit_us - entry_us
            if interval < min_us or interval > max_us:
                continue
            axle_gap = abs(entry["axles"] - exit_event["axles"])
            if axle_gap > max_axle_gap:
                continue
            duration_error = abs(interval - target_us)
            edges.append(
                {
                    "entry_index": entry_index,
                    "exit_index": exit_index,
                    "cost": axle_gap * _COST_SCALE + duration_error,
                    "axle_gap": axle_gap,
                    "duration_error": duration_error,
                }
            )

    entry_count = len(candidates)
    exit_count = len(exits)
    matched_count = 0
    axle_gap_sum = 0
    duration_error_sum = 0
    possible: list[set[str]] = [set() for _ in range(exit_count)]
    may_be_unmatched = [True] * exit_count

    if entry_count > 0 and exit_count > 0:
        source = 0
        entry_base = 1
        exit_base = 1 + entry_count
        sink = exit_base + exit_count
        node_count = sink + 1
        graph: list[list[list[Any]]] = [[] for _ in range(node_count)]

        source_refs: list[tuple[int, int]] = []
        for entry_index in range(entry_count):
            source_refs.append(
                _add_edge(graph, source, entry_base + entry_index, 1, 0)
            )

        pair_refs: dict[tuple[int, int], tuple[int, int]] = {}
        for edge in edges:
            reference = _add_edge(
                graph,
                entry_base + edge["entry_index"],
                exit_base + edge["exit_index"],
                1,
                edge["cost"],
            )
            pair_refs[(edge["entry_index"], edge["exit_index"])] = reference

        sink_refs: list[tuple[int, int]] = []
        for exit_index in range(exit_count):
            sink_refs.append(
                _add_edge(graph, exit_base + exit_index, sink, 1, 0)
            )

        matched_count, total_cost = _min_cost_max_flow(
            graph, node_count, source, sink, min(entry_count, exit_count)
        )
        axle_gap_sum = total_cost // _COST_SCALE
        duration_error_sum = total_cost % _COST_SCALE

        potentials = _residual_potentials(graph, node_count)
        exit_nodes = [exit_base + index for index in range(exit_count)]
        reachable = _zero_residual_reachability(
            graph, node_count, potentials, exit_nodes
        )

        def edge_flow(reference: tuple[int, int]) -> int:
            node, index = reference
            edge = graph[node][index]
            return edge[4] - edge[2]

        # In the zero-reduced-cost residual graph, an alternative optimal
        # matching is reached by pushing along zero-cost cycles.  Every
        # residual edge has a nonnegative reduced cost under these potentials,
        # so a cycle is optimal-preserving exactly when all its edges have
        # zero reduced cost.
        for exit_index in range(exit_count):
            reachable_set = reachable[exit_index]
            if edge_flow(sink_refs[exit_index]) == 0:
                # Already unmatched in this optimum.
                may_be_unmatched[exit_index] = True
            else:
                # A zero cycle through the reverse of this exit's sink edge
                # also needs that reverse edge (cost 0) to be tight, i.e. the
                # exit and the sink share the same potential.  The zero path
                # from the exit back to the sink supplies the replacement
                # matched exit.
                exit_node = exit_base + exit_index
                may_be_unmatched[exit_index] = (
                    potentials[exit_node] == potentials[sink]
                    and sink in reachable_set
                )

        for edge in edges:
            entry_index = edge["entry_index"]
            exit_index = edge["exit_index"]
            reference = pair_refs[(entry_index, exit_index)]
            if edge_flow(reference) == 1:
                possible[exit_index].add(candidate_ids[entry_index])
                continue
            entry_node = entry_base + entry_index
            if entry_node not in reachable[exit_index]:
                continue
            node, index = reference
            forward = graph[node][index]
            reduced = forward[3] + potentials[node] - potentials[forward[0]]
            if reduced == 0:
                possible[exit_index].add(candidate_ids[entry_index])

    candidate_by_id = {event["id"]: event for event in candidates}
    decisions: list[dict[str, Any]] = []
    forced_pairs: list[dict[str, Any]] = []
    for exit_index, exit_event in enumerate(exits):
        possible_ids = sorted(possible[exit_index])
        unmatched = bool(may_be_unmatched[exit_index])
        if not possible_ids:
            status = "unmatched"
        elif len(possible_ids) == 1 and not unmatched:
            status = "forced"
        else:
            status = "ambiguous"
        decisions.append(
            {
                "exit_event_id": exit_id_list[exit_index],
                "possible_entry_ids": possible_ids,
                "may_be_unmatched": unmatched,
                "status": status,
            }
        )
        if status == "forced":
            entry_id = possible_ids[0]
            entry_event = candidate_by_id[entry_id]
            forced_pairs.append(
                {
                    "entry_event_id": entry_id,
                    "exit_event_id": exit_event["id"],
                    "train_id": entry_event["train_id"],
                }
            )

    decisions.sort(key=lambda item: item["exit_event_id"])
    forced_pairs.sort(key=lambda item: item["exit_event_id"])

    return {
        "entry_candidates": [dict(event) for event in candidates],
        "exits": [dict(event) for event in exits],
        "optimum": {
            "matched_count": matched_count,
            "axle_gap_sum": axle_gap_sum,
            "duration_error_microseconds": duration_error_sum,
        },
        "decisions": decisions,
        "forced_pairs": forced_pairs,
    }
