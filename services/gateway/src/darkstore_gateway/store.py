"""Redis-backed tracking state: one stream + one state hash per order, plus pub/sub fan-out.

ingest() is a SINGLE Lua script: dedupe, forward-only status check, XADD, state update and PUBLISH
happen atomically. That is what makes "snapshot then replay since last_event_id" gap-free: the
snapshot's last_event_id and the stream always agree, even with several gateway replicas.
"""
from __future__ import annotations

import json

from redis.asyncio import Redis

from .tracking import RANK, TERMINAL

INGEST = """
-- KEYS: stream, state, seen          ARGV: channel, order_id, kind, data_json, rank, status,
--                                          maxlen, ttl, meta_json, seen_ttl, terminal_rank, ts
if redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[10]) == false then return false end
-- The order's store and destination are static facts carried by order.created. Record them BEFORE any staleness check:
-- the topics are consumed independently, so order.created can arrive after a later status and would otherwise be
-- dropped with its facts. They are announced as their own 'meta' event (once per order) so a client that subscribed
-- before they were known still receives them; the status event itself stays subject to the forward-only rule.
if ARGV[9] ~= '' then
  local m = cjson.decode(ARGV[9])
  local fresh = false
  if m.store and redis.call('HSETNX', KEYS[2], 'store', cjson.encode(m.store)) == 1 then fresh = true end
  if m.dest and redis.call('HSETNX', KEYS[2], 'dest', cjson.encode(m.dest)) == 1 then fresh = true end
  if fresh then
    local body = cjson.encode({store = m.store, dest = m.dest})
    local mid = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[7], '*', 'kind', 'meta', 'data', body)
    redis.call('HSET', KEYS[2], 'last_id', mid)
    redis.call('EXPIRE', KEYS[2], ARGV[8])
    redis.call('EXPIRE', KEYS[1], ARGV[8])
    redis.call('PUBLISH', ARGV[1], '{"order_id":' .. cjson.encode(ARGV[2]) .. ',"id":"' .. mid ..
      '","kind":"meta","data":' .. body .. '}')
  end
end
local cur = redis.call('HGET', KEYS[2], 'rank')
local kind = ARGV[3]
if kind == 'status' then
  if cur and tonumber(cur) >= tonumber(ARGV[5]) then return false end   -- stale, duplicate, or after terminal
elseif cur and tonumber(cur) >= tonumber(ARGV[11]) then
  return false                                                           -- no positions/ETAs after the end
end
if kind ~= 'status' and ARGV[12] ~= '' then          -- positions/ETAs never go back in time
  local last = redis.call('HGET', KEYS[2], 'ts:' .. kind)
  if last and tonumber(last) > tonumber(ARGV[12]) then return false end
  redis.call('HSET', KEYS[2], 'ts:' .. kind, ARGV[12])
end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[7], '*', 'kind', kind, 'data', ARGV[4])
if kind == 'status' then
  redis.call('HSET', KEYS[2], 'status', ARGV[6], 'rank', ARGV[5], 'h:' .. ARGV[6], ARGV[4])
elseif kind == 'eta' then
  redis.call('HSET', KEYS[2], 'eta', ARGV[4])
else
  redis.call('HSET', KEYS[2], 'rider', ARGV[4])
end
redis.call('HSET', KEYS[2], 'last_id', id)
redis.call('EXPIRE', KEYS[2], ARGV[8])
redis.call('EXPIRE', KEYS[1], ARGV[8])
redis.call('PUBLISH', ARGV[1], '{"order_id":' .. cjson.encode(ARGV[2]) .. ',"id":"' .. id ..
  '","kind":"' .. kind .. '","data":' .. ARGV[4] .. '}')
return id
"""


class TrackStore:
    def __init__(self, redis: Redis, prefix: str = "ds:", maxlen: int = 300, ttl: int = 86400):
        self.r, self.p, self.maxlen, self.ttl = redis, prefix, maxlen, ttl
        self._ingest = redis.register_script(INGEST)

    def _stream(self, oid: str) -> str:
        return f"{self.p}track:stream:{oid}"

    def _state(self, oid: str) -> str:
        return f"{self.p}track:state:{oid}"

    def channel(self, oid: str) -> str:
        return f"{self.p}track:ch:{oid}"

    @property
    def pattern(self) -> str:
        return f"{self.p}track:ch:*"

    async def ingest(self, order_id: str, event_id: str, kind: str, data: dict, *,
                     status: str | None = None, meta: dict | None = None,
                     ts: float | None = None) -> str | None:
        """Returns the new stream id, or None if the event was a duplicate / stale / after the end."""
        rank = RANK[status] if status else ""
        res = await self._ingest(
            keys=[self._stream(order_id), self._state(order_id), f"{self.p}track:seen:{event_id}"],
            args=[self.channel(order_id), order_id, kind, json.dumps(data, separators=(",", ":")),
                  rank, status or "", self.maxlen, self.ttl, json.dumps(meta) if meta else "",
                  3600, TERMINAL, "" if ts is None else repr(ts)])
        return res or None

    async def snapshot(self, order_id: str) -> dict:
        h = await self.r.hgetall(self._state(order_id))
        j = lambda k: json.loads(h[k]) if k in h else None  # noqa: E731
        history = sorted(({"status": k[2:], "at": json.loads(v)["at"]} for k, v in h.items()
                          if k.startswith("h:")), key=lambda x: RANK[x["status"]])
        return {"type": "snapshot", "order_id": order_id, "last_event_id": h.get("last_id"),
                "status": h.get("status"), "store": j("store"), "dest": j("dest"), "eta": j("eta"),
                "rider": j("rider"), "history": history}

    async def replay(self, order_id: str, after_id: str | None) -> list[dict]:
        if not after_id:
            return []
        rows = await self.r.xrange(self._stream(order_id), min=f"({after_id}", max="+")
        return [{"id": i, "kind": f["kind"], "data": json.loads(f["data"])} for i, f in rows]
