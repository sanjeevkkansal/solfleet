"""Leader-schedule math for safe validator restarts.

A validator that restarts during its own leader slots skips blocks (lost
rewards, hurts the cluster). This module reads getEpochInfo +
getLeaderSchedule and reports when the validator next leads and whether a
restart can finish before then. The actual blocking wait at execute time
is delegated to `agave-validator exit --min-idle-time` (see executor),
which talks to the validator's admin RPC; this is the planning/visibility
view over public RPC.

RPC-only nodes (no identity in the schedule) are always safe to restart.
"""

from __future__ import annotations

import httpx

from .probe import rpc_call

SLOT_TIME_S = 0.4  # Solana target slot time


def _runs(slots: list[int]) -> list[list[int]]:
    """Group sorted slots into runs of consecutive values (a leader
    produces several consecutive slots)."""
    runs: list[list[int]] = []
    for s in slots:
        if runs and s == runs[-1][-1] + 1:
            runs[-1].append(s)
        else:
            runs.append([s])
    return runs


def _free_windows(
    current: int, upcoming: list[int], epoch_end: int, min_window_s: float
) -> list[dict]:
    windows: list[tuple[int, int]] = []
    cursor = current
    for run in _runs(upcoming):
        if run[0] - cursor > 0:
            windows.append((cursor, run[0]))
        cursor = run[-1] + 1
    if epoch_end - cursor > 0:
        windows.append((cursor, epoch_end))
    return [
        {
            "start_slot": a,
            "end_slot": b,
            "slots": b - a,
            "duration_minutes": round((b - a) * SLOT_TIME_S / 60, 1),
        }
        for a, b in windows
        if (b - a) * SLOT_TIME_S >= min_window_s
    ]


async def leader_windows(
    client: httpx.AsyncClient,
    rpc_url: str,
    identity: str,
    *,
    min_window_minutes: int = 5,
) -> dict:
    epoch = await rpc_call(client, rpc_url, "getEpochInfo")
    absolute = epoch["absoluteSlot"]
    epoch_start = absolute - epoch["slotIndex"]
    epoch_end = epoch_start + epoch["slotsInEpoch"]

    sched = await rpc_call(
        client, rpc_url, "getLeaderSchedule", [absolute, {"identity": identity}]
    )
    indices = (sched or {}).get(identity, []) or []
    upcoming = sorted(epoch_start + i for i in indices if epoch_start + i > absolute)

    next_leader = upcoming[0] if upcoming else None
    seconds_to_next = (next_leader - absolute) * SLOT_TIME_S if next_leader is not None else None
    min_window_s = min_window_minutes * 60
    safe_now = next_leader is None or (seconds_to_next is not None and seconds_to_next >= min_window_s)

    return {
        "identity": identity,
        "epoch": epoch["epoch"],
        "current_slot": absolute,
        "leads_this_epoch": len(indices),
        "next_leader_slot": next_leader,
        "seconds_to_next_leader": round(seconds_to_next, 1) if seconds_to_next is not None else None,
        "safe_now": safe_now,
        "min_window_minutes": min_window_minutes,
        "windows": _free_windows(absolute, upcoming, epoch_end, min_window_s),
    }
