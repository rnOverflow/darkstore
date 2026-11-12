"""ETA service: re-predicts on every state change, with fallback and hot reload. Real infra."""
from __future__ import annotations

import json

import pytest

from darkstore_eta.service import serve as serve_eta
from darkstore_eta.simulate import generate_trips
from darkstore_eta.train import train

from .test_dispatch import S0, KM, _eq, fleet, place, until

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("eta_model")
    train(generate_trips(3000, seed=8), out_dir=d, seed=8, n_estimators=80)
    return d


RIDER = [("r1", S0[0] + 0.5 * KM, S0[1], False)]


async def etas(c, oid):
    rows = await c.q("select body from eta.outbox where key=:o order by id", o=oid)
    return [json.loads(r[0])["payload"] for r in rows]


async def test_eta_is_republished_at_every_stage_with_the_model(env, model_dir):
    async with fleet(env, seed=RIDER, extra=(serve_eta,), eta_model_dir=str(model_dir),
                     eta_model_enabled=True) as c:
        oid = await place(c)
        await until(lambda: _eq(c.status(oid), "delivered"), timeout=60)

        async def have_outcome():
            return (await c.q("select count(*) from eta.outcomes where order_id=:o", o=oid))[0][0] == 1
        await until(have_outcome, timeout=20)
        evs = await etas(c, oid)
        stages = [e["stage"] for e in evs]
        assert list(dict.fromkeys(stages)) == ["created", "picking", "assigned", "out_for_delivery"]
        assert all(e["source"] == "model" and e["model_version"] for e in evs)
        assert all(0 < e["p50_min"] < e["p90_min"] for e in evs)
        assert all(abs(e["p50_real_s"] - e["p50_min"] * 60 / c.env.sim_speed) < 1e-6 for e in evs)
        assert evs[-1]["p50_min"] < evs[0]["p50_min"]       # less time left once the rider has it
        out = (await c.q("select actual_min, first_p50, first_p90 from eta.outcomes where order_id=:o", o=oid))[0]
        assert out[0] > 0 and out[1] > 0 and out[2] > out[1]


async def test_flag_off_serves_the_heuristic(env, model_dir):
    async with fleet(env, seed=RIDER, extra=(serve_eta,), eta_model_dir=str(model_dir),
                     eta_model_enabled=False) as c:
        oid = await place(c)
        await until(lambda: _eq(c.status(oid), "delivered"), timeout=60)
        evs = await etas(c, oid)
        assert evs and all(e["source"] == "heuristic" and e["model_version"] is None for e in evs)


async def test_missing_model_falls_back_and_the_service_keeps_working(env, tmp_path):
    async with fleet(env, seed=RIDER, extra=(serve_eta,), eta_model_dir=str(tmp_path / "absent")) as c:
        oid = await place(c)
        await until(lambda: _eq(c.status(oid), "delivered"), timeout=60)
        evs = await etas(c, oid)
        assert {e["stage"] for e in evs} == {"created", "picking", "assigned", "out_for_delivery"}
        assert all(e["source"] == "heuristic" for e in evs)


async def test_newly_published_model_is_hot_reloaded_without_a_restart(env, tmp_path):
    live = tmp_path / "live"
    async with fleet(env, seed=[*RIDER, ("r2", S0[0] + 0.6 * KM, S0[1], False)], extra=(serve_eta,),
                     eta_model_dir=str(live), eta_reload_seconds=1.0) as c:
        first = await place(c)
        await until(lambda: _eq(c.status(first), "delivered"), timeout=60)
        assert all(e["source"] == "heuristic" for e in await etas(c, first))
        train(generate_trips(2500, seed=4), out_dir=live, seed=4, n_estimators=60)   # "retrain.sh"
        second = None

        async def served_by_model():
            nonlocal second
            second = await place(c)
            await until(lambda: _eq(c.status(second), "delivered"), timeout=60)
            return any(e["source"] == "model" for e in await etas(c, second))
        await until(served_by_model, timeout=60, interval=1.0)
