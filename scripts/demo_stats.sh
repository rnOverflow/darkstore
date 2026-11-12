#!/usr/bin/env bash
# Snapshot of the running city: order funnel, rider fleet and assignment health.
set -u
q() { docker compose exec -T postgres psql -U "${POSTGRES_USER:-darkstore}" -d "${POSTGRES_DB:-darkstore}" -tA -F' ' -c "$1"; }
echo "== order funnel =="
q "select status, count(*) from orders.orders group by 1 order by 2 desc"
echo "== dispatch =="
q "select action, count(*) from dispatch.assignment_log group by 1 order by 2 desc"
echo "== riders (redis) =="
docker compose exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning --raw eval "$(cat)" 0' <<'LUA'
local ids = redis.call('SMEMBERS','ds:riders:all'); local c = {}
for _, id in ipairs(ids) do local s = redis.call('HGET','ds:rider:'..id,'status'); c[s] = (c[s] or 0) + 1 end
local out = {} for k, v in pairs(c) do table.insert(out, k..' '..v) end return out
LUA
