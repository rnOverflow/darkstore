"""Redis-backed live rider index (H3 res 8) with atomic claim.

Keys (all under ``prefix``):
  rider:{id}         hash  lat,lng,rating,speed,status,cell,order_id,phase,idle_since,task coords...
  idle:{cell}        set   idle rider ids in that H3 cell
  order_rider:{oid}  string rider currently holding the order (fences one rider per order)
  riders:all         set   every known rider

Every state change that must be atomic is ONE Lua script: no read-then-write in Python.
Scripts compute the idle-set key from the rider hash's own ``cell`` field, so a stale cell read by
the caller can never cause a rider to be claimed out of the wrong set. (Dynamic key names mean this
needs a single Redis node, not Redis Cluster: noted in the README trade-offs.)
"""
from __future__ import annotations

import time

from redis.asyncio import Redis

from .geo import MATCH_RES, cell_of, ring
from .matching import Candidate, Weights, rank

# KEYS[1]=rider hash  KEYS[2]=order_rider key
# ARGV: prefix, rider_id, order_id, now, store_lat, store_lng, dest_lat, dest_lng
CLAIM = """
local holder = redis.call('GET', KEYS[2])
if holder then
  if holder == ARGV[2] then return 1 end   -- retry of the same claim: idempotent success
  return 0                                  -- order already has another rider: do not touch this one
end
if redis.call('HGET', KEYS[1], 'status') ~= 'idle' then return 0 end
local cell = redis.call('HGET', KEYS[1], 'cell')
if redis.call('SREM', ARGV[1] .. 'idle:' .. cell, ARGV[2]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'status', 'assigned', 'order_id', ARGV[3], 'phase', 'to_store',
  'assigned_at', ARGV[4], 'store_lat', ARGV[5], 'store_lng', ARGV[6],
  'dest_lat', ARGV[7], 'dest_lng', ARGV[8])
redis.call('SET', KEYS[2], ARGV[2])
return 1
"""

# KEYS[1]=rider hash  KEYS[2]=order_rider key
# ARGV: prefix, rider_id, order_id, now, cooldown_until (0 = back to idle)
RELEASE = """
if redis.call('HGET', KEYS[1], 'order_id') ~= ARGV[3] then return 0 end
if redis.call('GET', KEYS[2]) == ARGV[2] then redis.call('DEL', KEYS[2]) end
redis.call('HDEL', KEYS[1], 'order_id', 'phase', 'assigned_at',
  'store_lat', 'store_lng', 'dest_lat', 'dest_lng')
if tonumber(ARGV[5]) > 0 then
  redis.call('HSET', KEYS[1], 'status', 'cooldown', 'cooldown_until', ARGV[5])
else
  redis.call('HSET', KEYS[1], 'status', 'idle', 'idle_since', ARGV[4])
  redis.call('SADD', ARGV[1] .. 'idle:' .. redis.call('HGET', KEYS[1], 'cell'), ARGV[2])
end
return 1
"""

# KEYS[1]=rider hash   ARGV: prefix, rider_id, lat, lng, new_cell
MOVE = """
local status = redis.call('HGET', KEYS[1], 'status')
local old = redis.call('HGET', KEYS[1], 'cell')
if not status then return 0 end
redis.call('HSET', KEYS[1], 'lat', ARGV[3], 'lng', ARGV[4], 'cell', ARGV[5])
if status == 'idle' and old ~= ARGV[5] then
  redis.call('SREM', ARGV[1] .. 'idle:' .. old, ARGV[2])
  redis.call('SADD', ARGV[1] .. 'idle:' .. ARGV[5], ARGV[2])
end
return 1
"""

# KEYS[1]=rider hash   ARGV: order_id, from_phase, to_phase
PHASE_CAS = """
if redis.call('HGET', KEYS[1], 'order_id') == ARGV[1] and redis.call('HGET', KEYS[1], 'phase') == ARGV[2] then
  redis.call('HSET', KEYS[1], 'phase', ARGV[3])
  return 1
end
return 0
"""

# KEYS[1]=rider hash   ARGV: prefix, rider_id, now
RECOVER = """
if redis.call('HGET', KEYS[1], 'status') ~= 'cooldown' then return 0 end
if tonumber(redis.call('HGET', KEYS[1], 'cooldown_until')) > tonumber(ARGV[3]) then return 0 end
redis.call('HSET', KEYS[1], 'status', 'idle', 'idle_since', ARGV[3])
redis.call('HDEL', KEYS[1], 'cooldown_until')
redis.call('SADD', ARGV[1] .. 'idle:' .. redis.call('HGET', KEYS[1], 'cell'), ARGV[2])
return 1
"""


def make_redis(url: str, max_connections: int = 64) -> Redis:
    return Redis.from_url(url, decode_responses=True, max_connections=max_connections)


class RiderIndex:
    def __init__(self, redis: Redis, prefix: str = "ds:"):
        self.r, self.p = redis, prefix
        self._claim = redis.register_script(CLAIM)
        self._release = redis.register_script(RELEASE)
        self._move = redis.register_script(MOVE)
        self._cas = redis.register_script(PHASE_CAS)
        self._recover = redis.register_script(RECOVER)

    def _rk(self, rider_id: str) -> str:
        return f"{self.p}rider:{rider_id}"

    def _ok(self, order_id: str) -> str:
        return f"{self.p}order_rider:{order_id}"

    def _idle(self, cell: str) -> str:
        return f"{self.p}idle:{cell}"

    # ---- lifecycle
    async def add_rider(self, rider_id: str, lat: float, lng: float, rating: float = 4.5,
                        speed_kmh: float = 20.0, flaky: bool = False) -> None:
        cell = cell_of(lat, lng, MATCH_RES)
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(self._rk(rider_id), mapping={
                "lat": lat, "lng": lng, "rating": rating, "speed": speed_kmh, "status": "idle",
                "cell": cell, "idle_since": time.time(), "flaky": int(flaky)})
            pipe.sadd(self._idle(cell), rider_id)
            pipe.sadd(f"{self.p}riders:all", rider_id)
            await pipe.execute()

    async def get(self, rider_id: str) -> dict[str, str]:
        return await self.r.hgetall(self._rk(rider_id))

    async def all_ids(self) -> list[str]:
        return sorted(await self.r.smembers(f"{self.p}riders:all"))

    async def get_many(self, ids: list[str]) -> list[dict[str, str]]:
        async with self.r.pipeline(transaction=False) as pipe:
            for i in ids:
                pipe.hgetall(self._rk(i))
            return await pipe.execute()

    async def order_rider(self, order_id: str) -> str | None:
        return await self.r.get(self._ok(order_id))

    async def idle_count(self) -> int:
        n = 0
        async for key in self.r.scan_iter(match=f"{self.p}idle:*"):
            n += await self.r.scard(key)
        return n

    # ---- search / claim
    async def search(self, lat: float, lng: float, want: int = 8, max_k: int = 6) -> list[Candidate]:
        """Expand H3 rings outward from (lat,lng). Stops one ring AFTER having `want` *idle* riders
        (a closer rider just across a cell boundary is in the next ring), or at max_k."""
        centre, out, reached = cell_of(lat, lng, MATCH_RES), [], None
        now = time.time()
        for k in range(max_k + 1):
            async with self.r.pipeline(transaction=False) as pipe:
                for c in ring(centre, k):
                    pipe.smembers(self._idle(c))
                ids = [rid for members in await pipe.execute() for rid in members][:64]
            if ids:
                for rid, h in zip(ids, await self.get_many(ids)):
                    if h.get("status") == "idle":  # idle sets can briefly hold stale members
                        out.append(Candidate(rid, float(h["lat"]), float(h["lng"]),
                                             float(h["rating"]), max(0.0, now - float(h["idle_since"])),
                                             speed_kmh=float(h.get("speed", 20.0))))
            if reached is None and len(out) >= want:
                reached = k
            if reached is not None and k > reached:
                break
        return out

    async def claim(self, rider_id: str, order_id: str, *, store_lat: float, store_lng: float,
                    dest_lat: float, dest_lng: float, now: float | None = None) -> bool:
        return bool(await self._claim(
            keys=[self._rk(rider_id), self._ok(order_id)],
            args=[self.p, rider_id, order_id, now or time.time(), store_lat, store_lng,
                  dest_lat, dest_lng]))

    async def match(self, order_id: str, store_lat: float, store_lng: float, dest_lat: float,
                    dest_lng: float, *, want: int = 8, max_k: int = 8,
                    weights: Weights = Weights()) -> tuple[str, float] | None:
        """Find the best claimable rider. Returns (rider_id, eta_minutes) or None if nobody is free."""
        held = await self.order_rider(order_id)  # crash-retry: reuse the rider already holding it
        if held:
            return held, 0.0
        for attempt in range(4):
            # fresh view each time: riders claimed by competing matchers have left the idle sets,
            # and a wider net finds the next ones. None only when a fresh search finds nobody.
            found = await self.search(store_lat, store_lng, want * (attempt + 1), max_k)
            if not found:
                return None
            for _, eta, cand in rank(found, store_lat, store_lng, weights):
                if await self.claim(cand.rider_id, order_id, store_lat=store_lat,
                                    store_lng=store_lng, dest_lat=dest_lat, dest_lng=dest_lng):
                    return cand.rider_id, eta
                # lost the race for this rider: try the next-best one
        return None

    async def release(self, rider_id: str, order_id: str, *, cooldown_s: float = 0.0) -> bool:
        now = time.time()
        return bool(await self._release(
            keys=[self._rk(rider_id), self._ok(order_id)],
            args=[self.p, rider_id, order_id, now, now + cooldown_s if cooldown_s > 0 else 0]))

    # ---- movement (rider-sim)
    async def move(self, rider_id: str, lat: float, lng: float) -> None:
        await self._move(keys=[self._rk(rider_id)],
                         args=[self.p, rider_id, lat, lng, cell_of(lat, lng, MATCH_RES)])

    async def mark_noshow(self, rider_id: str, order_id: str) -> None:
        """Simulation only: this rider will ignore this one assignment."""
        await self.r.hset(self._rk(rider_id), mapping={"flaky": 0, "noshow": order_id})

    async def clear_noshow(self, rider_id: str) -> None:
        await self.r.hdel(self._rk(rider_id), "noshow")

    async def phase_cas(self, rider_id: str, order_id: str, frm: str, to: str) -> bool:
        return bool(await self._cas(keys=[self._rk(rider_id)], args=[order_id, frm, to]))

    async def recover_one(self, rider_id: str, now: float | None = None) -> bool:
        return bool(await self._recover(keys=[self._rk(rider_id)],
                                        args=[self.p, rider_id, now or time.time()]))

    async def recover_cooldowns(self, now: float | None = None) -> int:
        now, n = now or time.time(), 0
        for rid in await self.all_ids():
            n += await self._recover(keys=[self._rk(rid)], args=[self.p, rid, now])
        return n
