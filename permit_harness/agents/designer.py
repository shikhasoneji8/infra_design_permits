"""Designer agent. Reads the site, the last rejection and the retrieved lessons, and
returns a new Plan. An LLM does the judgment; a deterministic policy is the fallback
(no key, bad JSON, or --no-llm for a guaranteed demo)."""
from __future__ import annotations

import json
import math

from shapely.geometry import Point

from .. import config as C
from ..llm import chat, extract_json, have_llm
from ..plan import Plan, PlacedObject, required_counts
from ..rules import Review
from ..site import Site

SYSTEM = """You are the site designer for a data center developer in New Jersey.
You place equipment on a real parcel. Coordinates are metres; origin (0,0) is the parcel centroid,
x is east, y is north. Every object is an axis-aligned rectangle centred at (x, y).
A strict permit reviewer will measure your plan with code (noise propagation, wetland buffers,
setbacks, air and water thresholds). You cannot argue with the reviewer; you can only redesign.
Keep the facility useful: do not cut it_mw below 80% of the target to dodge rules.
Return ONLY a JSON object matching the schema. No prose outside the JSON."""


def _equipment_text(plan_hint: Plan | None) -> str:
    counts = required_counts(plan_hint) if plan_hint else {e.kind: e.count for e in C.EQUIPMENT}
    lines = []
    for e in C.EQUIPMENT:
        n = counts.get(e.kind, e.count)
        lines.append(f"- {e.kind}: {n} x ({e.width_m} m x {e.length_m} m x {e.height_m} m high). {e.note}")
    lines.append("- stormwater_basin: 0 or more, any size you choose (needed once new impervious area >= 0.25 acre).")
    lines.append(f"Generator count = ceil((it_mw*0.5 - bess_mw)/2). Adding bess_mw removes diesel units.")
    return "\n".join(lines)


SCHEMA = {
    "it_mw": "number, target %.0f" % C.TARGET_IT_MW,
    "cooling_type": "air_cooled | evaporative",
    "cooling_noise": "standard | low_noise",
    "cooling_barrier": "true | false (acoustic screen wall around the cooling yard, -8 dB)",
    "generator_tier": "tier2 | tier4f",
    "generator_enclosure": "standard | critical_silenced",
    "bess_mw": "number >= 0",
    "water_source": "municipal | well",
    "objects": [{"id": "hall", "kind": "data_hall", "x": 0, "y": 0, "w": 60, "l": 100, "h": 15, "rotation_deg": 0}],
    "rationale": "2-3 sentences: what you changed and why",
}


def design(site: Site, prev_plan: Plan | None, prev_review: Review | None, lessons: list[dict],
           use_llm: bool = True, model: str = C.DESIGNER_MODEL, regression: dict | None = None) -> tuple[Plan, str]:
    """Returns (plan, source) where source is 'llm' or 'heuristic'.
    prev_plan/prev_review are the BEST plan so far (hill-climb); `regression` describes the last
    attempt if it scored worse than the best, so the model does not repeat it."""
    if use_llm and have_llm():
        try:
            plan = _design_llm(site, prev_plan, prev_review, lessons, model, regression)
            return plan, "llm"
        except Exception as e:  # noqa: BLE001
            print(f"[designer] LLM design failed ({type(e).__name__}: {e}); using heuristic policy")
    return heuristic_design(site, prev_plan, prev_review), "heuristic"


def _design_llm(site, prev_plan, prev_review, lessons, model, regression=None) -> Plan:
    parts = [f"SITE:\n{json.dumps(site.summary_for_llm(), indent=1)}",
             f"EQUIPMENT TO PLACE:\n{_equipment_text(prev_plan)}",
             "RULES THE REVIEWER APPLIES (limits): night noise 50 dBA and day noise 65 dBA at any home's lot line; "
             "150 ft / 50 ft / 0 ft wetland buffers (exceptional/intermediate/ordinary); GP-005A air permit cap 100 MMBtu/hr "
             "combined genset heat input (19 MMBtu/hr each); NOx potential-to-emit under 25 t/yr; well water under 100,000 gal/day; "
             "stormwater basin once impervious >= 0.25 acre; 200 ft building setback from residential lots; "
             "generators should be hidden behind the hall from homes.",
             ]
    if lessons:
        parts.append("LESSONS FROM PREVIOUS PERMIT REJECTIONS (retrieved from memory, apply the relevant ones):\n" +
                     "\n".join(f"- [{l.get('rule')}] {l.get('text')}" for l in lessons))
    if prev_plan is not None and prev_review is not None:
        parts.append(f"YOUR PREVIOUS PLAN (round rejected, penalty {prev_review.penalty}):\n{json.dumps(prev_plan.compact())}")
        parts.append("REVIEWER'S REJECTIONS:\n" + "\n".join(
            f"- {v.rule} {v.title}: measured {v.measured}; limit {v.limit}. Objects: {v.objects[:8]}. Hint: {v.detail}"
            for v in prev_review.violations))
        if regression:
            parts.append(f"WARNING: your most recent attempt scored WORSE (penalty {regression.get('penalty')}) than the plan above, "
                         f"so it was discarded. It failed on: {regression.get('rules')}. Start from the plan above, not from that attempt.")
        parts.append("Fix every rejection. Move things decisively (tens of metres, not two), keep everything inside the parcel "
                     "polygon and out of wetland buffers, and do not reintroduce problems you already solved. "
                     "Noise falls 6 dB per doubling of distance and the hall blocks 10 dB; if geometry cannot get you under the "
                     "limit, change equipment (low_noise cooling, then cooling_barrier, critical_silenced generators) instead of shrinking it_mw. "
                     "Night noise is cooling only; a home 1 dB over the limit still fails, so escalate equipment rather than nudging.")
    else:
        parts.append("This is round 1. Produce a complete, buildable first plan.")
    parts.append("OUTPUT JSON SCHEMA:\n" + json.dumps(SCHEMA))
    raw = chat(model, SYSTEM, "\n\n".join(parts), json_mode=True)
    data = extract_json(raw)
    plan = Plan.model_validate(data)
    plan = _repair(plan, site)
    return plan


def _repair(plan: Plan, site: Site) -> Plan:
    """Code fixes geometry the model got wrong, without touching its choices:
    objects outside the parcel, inside a wetland buffer, or overlapping are re-placed by grid
    search; missing equipment is added; surplus generators are dropped."""
    region = site.buildable().buffer(0.5)
    anchor = _anchor(plan, site)
    # data hall first so everything else can anchor to it
    ordered = sorted(plan.objects, key=lambda o: 0 if o.kind == "data_hall" else 1)
    fixed: list[PlacedObject] = []
    moved = 0
    for o in ordered:
        fp = o.footprint()
        ok = region.contains(fp) and not any(fp.intersection(p.footprint()).area > 1.0 for p in fixed)
        if ok:
            fixed.append(o)
            continue
        r = _place(site, Plan(objects=fixed, it_mw=plan.it_mw), o.kind, o.w, o.l, o.h,
                   (fixed[0].x, fixed[0].y) if fixed and fixed[0].kind == "data_hall" else anchor, o.id)
        if r is not None:
            fixed.append(r)
            moved += 1
    plan.objects = fixed
    if moved:
        plan.rationale = (plan.rationale or "") + f" [harness re-placed {moved} object(s) that were outside the buildable area or overlapping]"
    # drop surplus generators, add missing equipment
    need = required_counts(plan)["generator"]
    gens = plan.by_kind("generator")
    if len(gens) > need:
        drop = {g.id for g in gens[need:]}
        plan.objects = [o for o in plan.objects if o.id not in drop]
    counts = required_counts(plan)
    for kind, n in counts.items():
        have = plan.by_kind(kind)
        spec = next(e for e in C.EQUIPMENT if e.kind == kind)
        anchor = _anchor(plan, site)
        while len(have) < n:
            obj = _place(site, plan, kind, spec.width_m, spec.length_m, spec.height_m, anchor,
                         f"{kind}_{len(have) + 1}")
            if obj is None:
                break
            plan.objects.append(obj)
            have = plan.by_kind(kind)
    return plan


# ------------------------------------------------------------- heuristic ----
def heuristic_design(site: Site, prev_plan: Plan | None, prev_review: Review | None) -> Plan:
    if prev_plan is None or prev_review is None:
        return _naive_first_plan(site)
    plan = prev_plan.model_copy(deep=True)
    rules = {v.rule for v in prev_review.violations}
    away = _away_from_homes_unit(site)
    if "R4" in rules:
        plan.bess_mw = min(plan.it_mw * 0.5, plan.bess_mw + 4.0)
    if "R5" in rules:
        plan.generator_tier = "tier4f"
    if "R6" in rules:
        plan.cooling_type = "air_cooled" if plan.cooling_type == "evaporative" else plan.cooling_type
        if plan.cooling_type == "air_cooled" and "R6" in rules and prev_plan.cooling_type == "air_cooled":
            plan.water_source = "municipal"
    if "CAP" in rules:
        plan.it_mw = C.TARGET_IT_MW
    # Geometry: nudge offenders away from homes, or re-place them if they are in a buffer/outside.
    offenders: set[str] = set()
    for v in prev_review.violations:
        if v.rule in {"R0", "R3", "R8"}:
            offenders |= set(v.objects)
    moved_kinds: set[str] = set()
    if "R1" in rules:
        moved_kinds.add("cooling")
    if "R2" in rules or "R9" in rules:
        moved_kinds.add("generator")
    step = 40.0
    for kind in moved_kinds:
        # move away from the receiver that is actually complaining, not a generic direction
        key = "night_dba_worst_home" if kind == "cooling" else "day_dba_worst_home"
        worst = prev_review.measured.get(key)
        comm = prev_review.measured.get("dba_worst_commercial")
        if kind == "generator" and comm and (not worst or comm[1] > worst[1]) and comm[1] > C.LIMIT_COMMERCIAL_DBA:
            worst = comm
        direction = away
        if worst:
            rx = next((n for n in site.neighbors if n.pin == worst[0]), None)
            if rx is not None:
                objs = plan.by_kind(kind)
                cx = sum(o.x for o in objs) / len(objs)
                cy = sum(o.y for o in objs) / len(objs)
                vx, vy = cx - rx.geom.centroid.x, cy - rx.geom.centroid.y
                L = math.hypot(vx, vy) or 1.0
                direction = (vx / L, vy / L)
        for o in plan.objects:
            if o.kind == kind:
                o.x += direction[0] * step
                o.y += direction[1] * step
    # Knob escalation once geometry alone has been tried
    if "R1" in rules and _rounds_hint(prev_review) >= 2:
        if prev_plan.cooling_noise == "standard":
            plan.cooling_noise = "low_noise"
        elif not prev_plan.cooling_barrier:
            plan.cooling_barrier = True
    if "R2" in rules and prev_plan.generator_enclosure == "standard" and _rounds_hint(prev_review) >= 2:
        plan.generator_enclosure = "critical_silenced"
    # Re-place anything invalid
    keep = [o for o in plan.objects if o.id not in offenders]
    plan.objects = keep
    plan = _repair(plan, site)
    # Regenerate generator list if count changed
    need = required_counts(plan)["generator"]
    gens = plan.by_kind("generator")
    if len(gens) > need:
        drop = {g.id for g in gens[need:]}
        plan.objects = [o for o in plan.objects if o.id not in drop]
    if "R7" in rules and not plan.by_kind("stormwater_basin"):
        imp = sum(o.footprint().area for o in plan.objects) * 1.3
        side = math.sqrt(C.STORMWATER_BASIN_FRACTION * imp * 1.2)
        b = _place(site, plan, "stormwater_basin", side, side, 0.0, _anchor(plan, site), "basin")
        if b:
            plan.objects.append(b)
    # Snap everything back inside the buildable area if the nudge pushed it out
    fixed = []
    for o in plan.objects:
        if site.buildable().buffer(0.5).contains(o.footprint()) and not any(
                o.footprint().intersection(p.footprint()).area > 1 for p in fixed):
            fixed.append(o)
        else:
            spec = next((e for e in C.EQUIPMENT if e.kind == o.kind), None)
            r = _place(site, Plan(objects=fixed, it_mw=plan.it_mw), o.kind, o.w, o.l, o.h, _anchor(plan, site), o.id)
            if r:
                fixed.append(r)
    plan.objects = fixed
    plan.rationale = f"Heuristic policy: responded to {sorted(rules)}"
    return plan


def _rounds_hint(review: Review) -> int:
    # The heuristic has no memory of its own. As a stall breaker it is called on a plan the model already
    # moved as far as it could, so it goes straight to the equipment change.
    return 2


def _naive_first_plan(site: Site) -> Plan:
    """What a developer draws before talking to a permit officer: everything in the middle,
    generators and cooling on whichever side is convenient (the homes side), evaporative
    cooling on well water, Tier 2 diesel, no basin."""
    toward = _away_from_homes_unit(site)
    toward = (-toward[0], -toward[1])
    objs = [PlacedObject(id="hall", kind="data_hall", x=0, y=0, w=60, l=100, h=15)]
    for i in range(10):
        objs.append(PlacedObject(id=f"gen_{i + 1}", kind="generator", x=toward[0] * 45 + (i - 4.5) * 6 * abs(toward[1]) ,
                                 y=toward[1] * 45 + (i - 4.5) * 6 * abs(toward[0]), w=4, l=12, h=5))
    for i in range(6):
        objs.append(PlacedObject(id=f"cool_{i + 1}", kind="cooling", x=toward[0] * 70 + (i - 2.5) * 9 * abs(toward[1]),
                                 y=toward[1] * 70 + (i - 2.5) * 9 * abs(toward[0]), w=6, l=12, h=5))
    objs.append(PlacedObject(id="sub", kind="substation", x=-toward[0] * 60, y=-toward[1] * 60, w=40, l=40, h=8))
    objs.append(PlacedObject(id="parking", kind="parking", x=-toward[1] * 70, y=toward[0] * 70, w=40, l=60, h=0))
    plan = Plan(objects=objs, cooling_type="evaporative", generator_tier="tier2", water_source="well")
    # make sure the first plan is at least inside the parcel (not necessarily out of buffers)
    fixed = []
    for o in plan.objects:
        if site.parcel.buffer(0.5).contains(o.footprint()) and not any(
                o.footprint().intersection(p.footprint()).area > 1 for p in fixed):
            fixed.append(o)
        else:
            r = _place(site, Plan(objects=fixed), o.kind, o.w, o.l, o.h, (0.0, 0.0), o.id, region=site.parcel)
            if r:
                fixed.append(r)
    plan.objects = fixed
    plan.rationale = "Round 1: developer's first sketch, no permit input."
    return plan


def _away_from_homes_unit(site: Site) -> tuple[float, float]:
    homes = site.homes
    if not homes:
        return (1.0, 0.0)
    # weight nearby homes more
    sx = sy = 0.0
    for h in homes:
        c = h.geom.centroid
        d = max(site.parcel.distance(h.geom), 1.0)
        w = 1.0 / d
        sx += c.x * w
        sy += c.y * w
    n = sum(1.0 / max(site.parcel.distance(h.geom), 1.0) for h in homes)
    cx, cy = sx / n, sy / n
    L = math.hypot(cx, cy) or 1.0
    return (-cx / L, -cy / L)


def _anchor(plan: Plan, site: Site) -> tuple[float, float]:
    halls = plan.by_kind("data_hall")
    if halls:
        return (halls[0].x, halls[0].y)
    return (0.0, 0.0)


def _place(site: Site, plan: Plan, kind: str, w: float, l: float, h: float, anchor: tuple[float, float],
           obj_id: str, region=None) -> PlacedObject | None:
    """Grid search for a spot inside the buildable area: far from homes, near the anchor, no overlaps."""
    region = region if region is not None else site.buildable()
    minx, miny, maxx, maxy = region.bounds
    homes = sorted(site.homes, key=lambda hh: site.parcel.distance(hh.geom))[:40]
    comm = sorted(site.commercial, key=lambda hh: site.parcel.distance(hh.geom))[:20]
    existing = [o.footprint() for o in plan.objects]
    best, best_score = None, -1e18
    step = 12.0
    x = minx + w / 2
    while x <= maxx - w / 2:
        y = miny + l / 2
        while y <= maxy - l / 2:
            fp = PlacedObject(id=obj_id, kind=kind, x=x, y=y, w=w, l=l, h=h).footprint()
            if region.buffer(0.5).contains(fp) and not any(fp.intersection(e).area > 1 for e in existing):
                p = Point(x, y)
                d_home = min((hh.geom.distance(p) for hh in homes), default=500.0)
                if kind in {"generator", "cooling"}:
                    # commercial neighbours have a 65 dBA limit too; count them at a discount
                    d_home = min(d_home, min((hh.geom.distance(p) * 1.8 for hh in comm), default=500.0))
                d_anchor = math.hypot(x - anchor[0], y - anchor[1])
                weight = {"generator": 1.0, "cooling": 1.2, "data_hall": 1.5, "substation": 1.0,
                          "parking": 0.2, "stormwater_basin": 0.1}[kind]
                score = weight * d_home - 0.35 * d_anchor
                if score > best_score:
                    best, best_score = (x, y), score
            y += step
        x += step
    if best is None:
        return None
    return PlacedObject(id=obj_id, kind=kind, x=round(best[0], 1), y=round(best[1], 1), w=w, l=l, h=h)
