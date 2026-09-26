"""The rulebook. Code measures, the reviewer explains.

Ten checks: R0 geometry sanity, R1-R9 from the one-pager, plus the capacity rule.
Every violation carries the measured value, the limit, the rule citation and the
objects involved, so the reviewer agent can write a rejection a permit officer
would sign.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from . import config as C
from .plan import Plan, PlacedObject, required_counts
from .site import Site


@dataclass
class Violation:
    rule: str
    title: str
    severity: str  # hard | medium | soft | capacity
    penalty: int
    measured: str
    limit: str
    citation: str
    objects: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class Review:
    penalty: int
    violations: list[Violation]
    measured: dict
    passed: bool

    def to_doc(self) -> dict:
        return {"penalty": self.penalty, "passed": self.passed, "measured": self.measured,
                "violations": [asdict(v) for v in self.violations]}


# ---------------------------------------------------------------- noise ----
def _level_at(source_dba: float, ref_m: float, d_m: float) -> float:
    d = max(d_m, ref_m)
    return source_dba - 20 * math.log10(d / ref_m)


def _sum_db(levels: list[float]) -> float:
    if not levels:
        return 0.0
    return 10 * math.log10(sum(10 ** (l / 10) for l in levels))


def _shielded(src: PlacedObject, rx_pt, halls: list[Polygon]) -> bool:
    line = LineString([(src.x, src.y), (rx_pt.x, rx_pt.y)])
    return any(line.crosses(h) or line.within(h) for h in halls)


def noise_at_receivers(plan: Plan, site: Site, sources: list[tuple[PlacedObject, float, float]],
                       receivers) -> list[tuple[str, float, float]]:
    """For each receiver parcel return (pin, level_dba, distance_to_loudest_m)."""
    halls = [o.footprint() for o in plan.by_kind("data_hall")]
    out = []
    for r in receivers:
        levels = []
        nearest = 1e9
        for src, dba, ref in sources:
            from shapely.geometry import Point
            p = Point(src.x, src.y)
            d = p.distance(r.geom)
            rx = r.geom.exterior.interpolate(r.geom.exterior.project(p)) if isinstance(r.geom, Polygon) \
                else r.geom.centroid
            lvl = _level_at(dba, ref, d)
            if _shielded(src, rx, halls):
                lvl -= C.BUILDING_SHIELDING_DB
            levels.append(lvl)
            nearest = min(nearest, d)
        out.append((r.pin, round(_sum_db(levels), 1), round(nearest, 1)))
    return out


# ------------------------------------------------------------- the rules ----
def evaluate(plan: Plan, site: Site) -> Review:
    V: list[Violation] = []
    measured: dict = {}
    counts = required_counts(plan)
    gens = plan.by_kind("generator")
    cools = plan.by_kind("cooling")
    halls = plan.by_kind("data_hall")
    all_objs = plan.objects
    homes = site.homes
    # Only the 80 nearest homes matter for noise; keeps runs fast on dense parcels.
    homes_near = sorted(homes, key=lambda h: site.parcel.distance(h.geom))[:80]

    # R0: geometry sanity -- inside parcel, no overlaps, equipment list complete.
    outside = [o.id for o in all_objs if not site.parcel.buffer(0.5).contains(o.footprint())]
    overlaps = []
    for i, a in enumerate(all_objs):
        for b in all_objs[i + 1:]:
            if a.footprint().intersection(b.footprint()).area > 1.0:
                overlaps.append(f"{a.id}x{b.id}")
    missing = {k: (n, len(plan.by_kind(k))) for k, n in counts.items() if len(plan.by_kind(k)) < n}
    if outside or overlaps or missing:
        V.append(Violation("R0", "Plan is not buildable as drawn", "hard", C.PENALTY_HARD,
                           measured=f"outside parcel: {outside[:6]}; overlaps: {overlaps[:6]}; missing: {missing}",
                           limit="every object inside the parcel, no overlaps, full equipment list placed",
                           citation="Site plan completeness (municipal land use application)",
                           objects=outside + [o for pair in overlaps for o in pair.split('x')]))

    # R1 / R2: noise
    gen_dba = C.GENERATOR_DBA_BY_ENCLOSURE[plan.generator_enclosure] - (C.GENERATOR_SCREEN_DB if plan.generator_screen else 0.0)
    cool_dba = C.COOLING_DBA_BY_OPTION[plan.cooling_noise] - (C.COOLING_BARRIER_DB if plan.cooling_barrier else 0.0)
    night_sources = [(o, cool_dba, C.COOLING_REF_DISTANCE_M) for o in cools]
    if C.GENERATORS_RUN_AT_NIGHT:
        night_sources += [(o, gen_dba, C.GENERATOR_REF_DISTANCE_M) for o in gens]
    day_sources = [(o, cool_dba, C.COOLING_REF_DISTANCE_M) for o in cools] + \
                  [(o, gen_dba, C.GENERATOR_REF_DISTANCE_M) for o in gens]

    night = noise_at_receivers(plan, site, night_sources, homes_near) if night_sources else []
    worst_night = max(night, key=lambda t: t[1]) if night else None
    measured["night_dba_worst_home"] = worst_night
    if worst_night and worst_night[1] > C.NIGHT_LIMIT_RESIDENTIAL_DBA:
        V.append(Violation("R1", "Night-time noise at a residence", "hard", C.PENALTY_HARD,
                           measured=f"{worst_night[1]} dBA at property line of {worst_night[0]} ({worst_night[2]} m from nearest cooling unit)",
                           limit=f"{C.NIGHT_LIMIT_RESIDENTIAL_DBA} dBA, 10 pm to 7 am",
                           citation="N.J.A.C. 7:29-1.2 (Noise Control), residential receiving property",
                           objects=[o.id for o in cools],
                           detail="Cooling runs all night. Move it away from homes, put the data hall between them, specify low-noise fan packages (cooling_noise), and if still over, an acoustic screen wall (cooling_barrier)."))

    day = noise_at_receivers(plan, site, day_sources, homes_near) if day_sources else []
    worst_day = max(day, key=lambda t: t[1]) if day else None
    measured["day_dba_worst_home"] = worst_day
    comm = noise_at_receivers(plan, site, day_sources, site.commercial[:40]) if day_sources else []
    worst_comm = max(comm, key=lambda t: t[1]) if comm else None
    measured["dba_worst_commercial"] = worst_comm
    day_fail = worst_day and worst_day[1] > C.DAY_LIMIT_RESIDENTIAL_DBA
    comm_fail = worst_comm and worst_comm[1] > C.LIMIT_COMMERCIAL_DBA
    if day_fail or comm_fail:
        w = worst_day if day_fail else worst_comm
        V.append(Violation("R2", "Day-time noise at a neighbor (generator testing)", "hard", C.PENALTY_HARD,
                           measured=f"{w[1]} dBA at {w[0]} ({w[2]} m from nearest source)",
                           limit=f"{C.DAY_LIMIT_RESIDENTIAL_DBA} dBA residential 7 am-10 pm; {C.LIMIT_COMMERCIAL_DBA} dBA commercial any time",
                           citation="N.J.A.C. 7:29-1.2; CSG Law summary of NJ noise limits",
                           objects=[o.id for o in gens],
                           detail="Generators are tested by day. Cluster them on the far side of the hall from homes or use critically-silenced enclosures."))

    # R3: wetlands + transition areas
    hits = []
    for w in site.wetlands:
        zone = w.geom.buffer(w.buffer_m)
        for o in all_objs:
            if o.footprint().intersects(zone):
                hits.append((o.id, w.label, round(w.buffer_m, 1)))
    measured["wetland_buffer_hits"] = hits[:10]
    if hits:
        V.append(Violation("R3", "Structure or pavement inside a wetland transition area", "hard", C.PENALTY_HARD,
                           measured=f"{len(hits)} object(s) in buffer, e.g. {hits[0][0]} in {hits[0][2]} m buffer of {hits[0][1].lower()}",
                           limit="150 ft from exceptional-value wetlands, 50 ft from intermediate, 0 ft from ordinary",
                           citation="N.J.A.C. 7:7A-3.3 (Freshwater Wetlands Protection Act transition areas)",
                           objects=[h[0] for h in hits],
                           detail="Pull everything out of the buffer. The buildable footprint excludes wetlands and their buffers."))

    # R4: air general permit GP-005A
    n_gen = len(gens)
    heat = n_gen * C.GENERATOR_HEAT_INPUT_MMBTU_HR
    measured["generator_heat_input_mmbtu_hr"] = round(heat, 1)
    if heat > C.GP005A_MAX_COMBINED_HEAT_INPUT:
        V.append(Violation("R4", "Generators exceed the air general permit cap", "medium", C.PENALTY_MEDIUM,
                           measured=f"{n_gen} gensets x {C.GENERATOR_HEAT_INPUT_MMBTU_HR} = {heat:.0f} MMBtu/hr combined",
                           limit=f"{C.GP005A_MAX_COMBINED_HEAT_INPUT} MMBtu/hr combined to stay on GP-005A; above that is a slow custom preconstruction permit",
                           citation="NJDEP General Permit GP-005A (emergency generators), All4 Inc. summary",
                           objects=[o.id for o in gens],
                           detail="Fewer diesel units: cover part of the critical load with battery storage (bess_mw) so the genset count drops."))

    # R5: NOx potential to emit
    nox_tpy = n_gen * C.GENERATOR_KW * C.PTE_HOURS_PER_YEAR * C.GENERATOR_NOX_BY_TIER[plan.generator_tier] / 907_184.74
    measured["nox_pte_tpy"] = round(nox_tpy, 1)
    if nox_tpy > C.NOX_MAJOR_SOURCE_TPY:
        V.append(Violation("R5", "Facility NOx potential-to-emit makes it a Title V major source", "hard", C.PENALTY_HARD,
                           measured=f"{nox_tpy:.1f} tons/yr NOx ({n_gen} x {C.GENERATOR_KW:.0f} kW x {C.PTE_HOURS_PER_YEAR} hr x {C.GENERATOR_NOX_BY_TIER[plan.generator_tier]} g/kWh, {plan.generator_tier})",
                           limit=f"{C.NOX_MAJOR_SOURCE_TPY} tons/yr",
                           citation="NJDEP ACE Academy: major source thresholds in NJ (ozone non-attainment)",
                           objects=[o.id for o in gens],
                           detail="Specify Tier 4 Final gensets (SCR) or cut the diesel count with battery storage."))

    # R6: water allocation
    gpd = plan.it_mw * 1000 * 24 * C.WATER_GAL_PER_KWH[plan.cooling_type]
    measured["water_gpd"] = round(gpd)
    threshold = C.WATER_ALLOCATION_PERMIT_GPD_HIGHLANDS if site.highlands_preservation else C.WATER_ALLOCATION_PERMIT_GPD
    if plan.water_source == "well" and gpd > threshold:
        V.append(Violation("R6", "Water withdrawal needs a Water Allocation Permit", "medium", C.PENALTY_MEDIUM,
                           measured=f"{gpd:,.0f} gal/day ({plan.cooling_type} cooling at {plan.it_mw} MW IT, from {plan.water_source})",
                           limit=f"{threshold:,} gal/day from wells or streams",
                           citation="NJDEP Water Allocation Permit program (N.J.A.C. 7:19)",
                           detail="Switch to closed-loop air cooling, or buy municipal water (then the utility holds the allocation)."))

    # R7: stormwater
    impervious = sum(o.footprint().area for o in all_objs if o.kind != "stormwater_basin") * 1.3  # +30% roads/aprons
    imp_acres = impervious / C.ACRE_M2
    basins = plan.by_kind("stormwater_basin")
    basin_area = sum(o.footprint().area for o in basins)
    measured["new_impervious_acres"] = round(imp_acres, 2)
    measured["stormwater_basin_m2"] = round(basin_area)
    need = C.STORMWATER_BASIN_FRACTION * impervious
    if imp_acres >= C.MAJOR_DEV_NEW_IMPERVIOUS_ACRES and basin_area < need:
        V.append(Violation("R7", "Major development without a stormwater basin", "medium", C.PENALTY_MEDIUM,
                           measured=f"{imp_acres:.1f} acres new impervious, basin {basin_area:.0f} m2 (need {need:.0f} m2)",
                           limit="0.25 acre new impervious = major development; reserve basin area (10% of impervious, our sizing)",
                           citation="N.J.A.C. 7:8-1.2 (Stormwater Management Rules)",
                           detail="Add a stormwater_basin object of at least the required area, on the low side, outside wetland buffers."))

    # R8: residential setback for buildings
    setback_hits = []
    for o in halls + plan.by_kind("substation"):
        for h in homes_near:
            d = o.footprint().distance(h.geom)
            if d < C.RESIDENTIAL_SETBACK_M:
                setback_hits.append((o.id, h.pin, round(d, 1)))
    measured["setback_hits"] = setback_hits[:5]
    if setback_hits:
        worst = min(setback_hits, key=lambda t: t[2])
        V.append(Violation("R8", "Building inside the residential setback", "hard", C.PENALTY_HARD,
                           measured=f"{worst[0]} is {worst[2]} m from residential lot {worst[1]}",
                           limit=f"{C.RESIDENTIAL_SETBACK_M:.1f} m (200 ft) from any residential lot line (demo value; set per municipality)",
                           citation="Local zoning; cf. Loudoun County VA data center setback and noise ordinance (2024)",
                           objects=[worst[0]],
                           detail="Shift the hall and substation toward the side of the parcel with no homes."))

    # R9 (soft): generators visible from the nearest home
    los = []
    if gens and homes_near and halls and not plan.generator_screen:
        hall_polys = [h.footprint() for h in halls]
        for g in gens:
            from shapely.geometry import Point
            nearest_home = min(homes_near, key=lambda h: h.geom.distance(Point(g.x, g.y)))
            rx = nearest_home.geom.centroid
            if not _shielded(g, rx, hall_polys):
                los.append(g.id)
    measured["generators_with_line_of_sight_to_home"] = los
    if los:
        V.append(Violation("R9", "Generators in line of sight of homes", "soft", C.PENALTY_SOFT,
                           measured=f"{len(los)} of {len(gens)} generators visible from the nearest home",
                           limit="building should block the line of sight",
                           citation="Northern Virginia HOA / Prince William County design guidance",
                           objects=los,
                           detail="Line generators up along the hall wall that faces away from homes, or add a screening wall/berm around the generator yard (generator_screen)."))

    # Capacity
    frac = plan.it_mw / C.TARGET_IT_MW
    measured["capacity_fraction"] = round(frac, 2)
    if frac < C.MIN_CAPACITY_FRACTION:
        V.append(Violation("CAP", "Facility no longer meets the operator's capacity target", "capacity", C.PENALTY_CAPACITY,
                           measured=f"{plan.it_mw} MW IT ({frac:.0%} of {C.TARGET_IT_MW} MW target)",
                           limit=f"at least {C.MIN_CAPACITY_FRACTION:.0%} of target",
                           citation="Operator requirement",
                           detail="You cannot pass the permit by deleting the data center."))

    penalty = sum(v.penalty for v in V)
    return Review(penalty=penalty, violations=V, measured=measured, passed=penalty == 0)
