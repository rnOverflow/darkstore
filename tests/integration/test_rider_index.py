"""Redis-backed H3 rider index: search, atomic claim (Lua), races. Real Redis."""
import asyncio

import pytest

from darkstore_common.city import CENTER
from darkstore_common.geo import cell_of

pytestmark = pytest.mark.integration
KM = 1 / 111.195  # degrees latitude per km
TASK = dict(store_lat=CENTER[0], store_lng=CENTER[1], dest_lat=CENTER[0] + 0.01, dest_lng=CENTER[1])


async def add(index, rid, km_north=0.0, **kw):
    await index.add_rider(rid, CENTER[0] + km_north * KM, CENTER[1], **kw)


async def test_search_finds_riders_by_expanding_rings(index):
    await add(index, "near", 0.1)
    await add(index, "mid", 2.0)
    await add(index, "far", 40.0)
    got = {c.rider_id for c in await index.search(*CENTER, want=2, max_k=6)}
    assert {"near", "mid"} <= got and "far" not in got


async def test_search_stops_expanding_once_enough_found(index):
    await add(index, "a", 0.05)
    await add(index, "b", 0.06)
    await add(index, "c", 3.0)
    got = {c.rider_id for c in await index.search(*CENTER, want=2, max_k=6)}
    assert got == {"a", "b"}


async def test_200_concurrent_claims_on_one_rider_have_one_winner(index):
    await add(index, "r1")
    wins = await asyncio.gather(*[index.claim("r1", f"order-{i}", **TASK) for i in range(200)])
    assert sum(wins) == 1
    assert (await index.get("r1"))["status"] == "assigned"


async def test_one_order_with_many_riders_gets_exactly_one_rider(index):
    for i in range(50):
        await add(index, f"r{i}", 0.01 * i)
    wins = await asyncio.gather(*[index.claim(f"r{i}", "the-order", **TASK) for i in range(50)])
    assert sum(wins) == 1
    winner = await index.order_rider("the-order")
    assert winner == f"r{wins.index(True)}"
    assert await index.idle_count() == 49  # the 49 losers were NOT taken out of the idle set


async def test_claim_is_idempotent_for_same_rider_and_order(index):
    await add(index, "r1")
    assert await index.claim("r1", "o1", **TASK)
    assert await index.claim("r1", "o1", **TASK)  # retry after a crash must not fail or double-claim
    assert not await index.claim("r1", "o2", **TASK)


async def test_claimed_rider_is_invisible_to_search(index):
    await add(index, "r1")
    await index.claim("r1", "o1", **TASK)
    assert await index.search(*CENTER) == []


async def test_release_returns_rider_to_idle_and_frees_the_order(index):
    await add(index, "r1")
    await index.claim("r1", "o1", **TASK)
    assert not await index.release("r1", "WRONG-ORDER")  # only the owner can release
    assert await index.release("r1", "o1")
    assert await index.order_rider("o1") is None
    assert [c.rider_id for c in await index.search(*CENTER)] == ["r1"]
    assert await index.claim("r1", "o2", **TASK)


async def test_move_keeps_the_idle_cell_index_consistent(index):
    await add(index, "r1")
    await index.move("r1", CENTER[0] + 3 * KM, CENTER[1])
    assert [c.rider_id for c in await index.search(CENTER[0] + 3 * KM, CENTER[1], max_k=1)] == ["r1"]
    assert await index.search(*CENTER, want=1, max_k=1) == []
    assert (await index.get("r1"))["cell"] == cell_of(CENTER[0] + 3 * KM, CENTER[1])


async def test_cooldown_rider_is_unavailable_until_recovered(index):
    await add(index, "r1")
    await index.claim("r1", "o1", **TASK)
    await index.release("r1", "o1", cooldown_s=60)
    assert await index.search(*CENTER) == []
    assert await index.recover_cooldowns(now=10**12) == 1
    assert [c.rider_id for c in await index.search(*CENTER)] == ["r1"]


async def test_300_orders_race_for_100_riders_with_no_double_assignment(index):
    for i in range(100):
        await add(index, f"r{i}", 0.02 * (i % 10))
    results = await asyncio.gather(*[
        index.match(f"o{i}", CENTER[0], CENTER[1], CENTER[0] + 0.01, CENTER[1]) for i in range(300)])
    won = [r for r in results if r is not None]
    riders = [r[0] for r in won]
    assert len(won) == 100 and len(set(riders)) == 100      # every rider used once, no sharing
    assert await index.idle_count() == 0
    for rid, oid in ((r[0], f"o{i}") for i, r in enumerate(results) if r):
        assert (await index.get(rid))["order_id"] == oid and await index.order_rider(oid) == rid


async def test_search_counts_only_idle_riders_and_expands_one_extra_ring(index):
    await add(index, "stale", 0.05)
    await add(index, "x", 0.06)
    await index.claim("stale", "o1", **TASK)          # now busy, but still in nobody's idle set
    await add(index, "across_border", 1.0)             # a ring or two away
    got = {c.rider_id for c in await index.search(*CENTER, want=2, max_k=6)}
    assert {"x", "across_border"} <= got               # extra ring: the nearest-ring-only trap is gone


async def test_ranking_uses_each_riders_own_speed(index):
    await add(index, "slow", 1.0, speed_kmh=10)
    await add(index, "fast", 1.0, speed_kmh=30)
    from darkstore_common.matching import rank
    ranked = rank(await index.search(*CENTER), *CENTER)
    assert [c.rider_id for _, _, c in ranked][0] == "fast"
