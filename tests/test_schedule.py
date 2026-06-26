"""Leader-window finder tests against a mocked RPC."""

import json

import httpx
import pytest

from solfleet.schedule import SLOT_TIME_S, leader_windows

RPC = "https://rpc.example"
IDENTITY = "Val11111111111111111111111111111111111111111"


def make_handler(epoch_info, schedule):
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        result = epoch_info if method == "getEpochInfo" else schedule
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return handler


async def run(epoch_info, schedule, **kw):
    transport = httpx.MockTransport(make_handler(epoch_info, schedule))
    async with httpx.AsyncClient(transport=transport) as client:
        return await leader_windows(client, RPC, IDENTITY, **kw)


# epoch starts at slot 1000 (absolute 1100, slotIndex 100), 10_000 slots long
EPOCH = {"absoluteSlot": 1100, "slotIndex": 100, "slotsInEpoch": 10000, "epoch": 5}


async def test_rpc_only_node_always_safe():
    # identity not in schedule -> no leader slots
    result = await run(EPOCH, {})
    assert result["leads_this_epoch"] == 0
    assert result["next_leader_slot"] is None
    assert result["safe_now"] is True
    # one big window covering the rest of the epoch
    assert len(result["windows"]) == 1
    assert result["windows"][0]["start_slot"] == 1100


async def test_next_leader_soon_is_unsafe():
    # leader at index 150 -> absolute 1150, 50 slots = 20s away, < 5 min
    result = await run(EPOCH, {IDENTITY: [150, 151, 152, 153]})
    assert result["next_leader_slot"] == 1150
    assert result["seconds_to_next_leader"] == pytest.approx(50 * SLOT_TIME_S)
    assert result["safe_now"] is False


async def test_far_leader_is_safe_now():
    # index 5000 -> absolute slot 1000+5000=6000, ~32 min away
    result = await run(EPOCH, {IDENTITY: [5000, 5001, 5002, 5003]})
    assert result["safe_now"] is True
    # a qualifying window exists from now up to the leader run
    first = result["windows"][0]
    assert first["start_slot"] == 1100
    assert first["end_slot"] == 6000


async def test_windows_filtered_by_min_length():
    # two leader runs close together (indices -> absolute 3000s) leave a
    # tiny middle gap that should be filtered out at a 5-min minimum
    sched = {IDENTITY: [2000, 2001, 2002, 2003, 2010, 2011, 2012, 2013]}
    result = await run(EPOCH, sched, min_window_minutes=5)
    middle = [w for w in result["windows"] if w["start_slot"] == 3004]
    assert middle == []  # 6-slot gap (~2.4s) is far below 5 min
    # the long pre-window (1100..3000) and tail (3014..11000) qualify
    starts = {w["start_slot"] for w in result["windows"]}
    assert 1100 in starts and 3014 in starts
