import math

import pytest

from permit_harness import config as C
from permit_harness.agents import designer, reviewer
from permit_harness.loop import run
from permit_harness.plan import Plan, PlacedObject
from permit_harness.rules import evaluate, _level_at, _sum_db
from permit_harness.site import site_doc_from_geojson_bundle, site_from_doc
from permit_harness.store import MemoryStore
from permit_harness.synthetic import bundle


@pytest.fixture
def site():
    return site_from_doc(site_doc_from_geojson_bundle(bundle()))


def test_site_geometry(site):
    assert 30 < site.acres < 40
    assert len(site.homes) == 8
    assert len(site.commercial) == 1
    assert len(site.wetlands) == 1
    # homes are to the east
    assert all(h.geom.centroid.x > 150 for h in site.homes)
    s = site.summary_for_llm()
    assert "E" in s["homes_by_sector"]
    assert s["buildable_area_acres"] < site.acres


def test_noise_math():
    assert _level_at(85, 7, 7) == 85
    assert math.isclose(_level_at(85, 7, 14), 78.98, abs_tol=0.01)  # -6 dB per doubling
    assert math.isclose(_sum_db([80, 80]), 83.01, abs_tol=0.01)  # two equal sources +3 dB


def test_naive_plan_fails_hard(site):
    plan = designer.heuristic_design(site, None, None)
    rev = evaluate(plan, site)
    rules = {v.rule for v in rev.violations}
    assert rev.penalty >= 40
    assert {"R1", "R2", "R4", "R5", "R6"} <= rules, rules


def test_good_plan_passes(site):
    # Everything on the west side, shielded by the hall, quiet gear, air cooled, Tier 4, BESS.
    objs = [PlacedObject(id="hall", kind="data_hall", x=-40, y=-20, w=60, l=100, h=15)]
    for i in range(5):
        objs.append(PlacedObject(id=f"g{i}", kind="generator", x=-120, y=-70 + i * 16, w=4, l=12, h=5))
    for i in range(6):
        objs.append(PlacedObject(id=f"c{i}", kind="cooling", x=-150, y=-80 + i * 16, w=6, l=12, h=5))
    objs.append(PlacedObject(id="sub", kind="substation", x=-140, y=60, w=40, l=40, h=8))
    objs.append(PlacedObject(id="park", kind="parking", x=60, y=-120, w=40, l=60, h=0))
    objs.append(PlacedObject(id="basin", kind="stormwater_basin", x=60, y=-30, w=60, l=60, h=0))
    plan = Plan(objects=objs, it_mw=40, cooling_type="air_cooled", cooling_noise="low_noise",
                generator_tier="tier4f", generator_enclosure="critical_silenced", bess_mw=10, water_source="well")
    rev = evaluate(plan, site)
    assert rev.passed, [(v.rule, v.measured) for v in rev.violations]


def test_capacity_rule_bites(site):
    plan = designer.heuristic_design(site, None, None)
    plan.it_mw = 10
    rev = evaluate(plan, site)
    assert any(v.rule == "CAP" for v in rev.violations)


def test_wetland_buffer(site):
    plan = Plan(objects=[PlacedObject(id="hall", kind="data_hall", x=0, y=100, w=60, l=100, h=15)])
    rev = evaluate(plan, site)
    assert any(v.rule == "R3" for v in rev.violations)


def test_offline_loop_converges_and_resumes(site):
    store = MemoryStore()
    store.upsert_site(site_doc_from_geojson_bundle(bundle()))
    # crash after round 2
    with pytest.raises(SystemExit):
        run("synthetic_wayne", "t1", store=store, max_rounds=15, use_llm=False, crash_after_round=2)
    r = store.get_run("t1")
    assert r["round"] == 2 and r["status"] == "running"
    # resume, must continue at round 3 and eventually pass
    r = run("synthetic_wayne", "t1", store=store, max_rounds=15, resume=True, use_llm=False)
    assert r["status"] == "passed", r["history"]
    pens = [h["penalty"] for h in r["history"]]
    assert pens[0] > pens[-1] == 0
    assert r["history"][2]["round"] == 3
    assert store.lesson_count() > 0


def test_second_site_learns_faster(site):
    store = MemoryStore()
    store.upsert_site(site_doc_from_geojson_bundle(bundle()))
    store.upsert_site(site_doc_from_geojson_bundle(bundle(site_id="syn2", homes_side="W", wetland_side="S")))
    r1 = run("synthetic_wayne", "a", store=store, max_rounds=15, use_llm=False)
    r2 = run("syn2", "b", store=store, max_rounds=15, use_llm=False)
    assert r1["status"] == "passed" and r2["status"] == "passed"
    # lessons were retrieved for site 2 round 1
    d = store.get_design("b:r1")
    assert d["lessons_used"]
