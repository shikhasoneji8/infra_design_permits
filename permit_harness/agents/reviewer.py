"""Reviewer agent. The code already measured everything; this agent writes the
rejection letter a permit officer would send, and distils each failure into a short,
transferable lesson for memory."""
from __future__ import annotations

import json

from .. import config as C
from ..llm import chat, extract_json, have_llm
from ..plan import Plan
from ..rules import Review
from ..site import Site

SYSTEM = """You are a senior New Jersey permit reviewer (NJDEP plus municipal zoning).
You are given a site plan and the code-measured violations. Do not re-measure anything;
the numbers are authoritative. Write like a permit officer: specific, cites the rule, no hedging.
Then write LESSONS: short, general design rules another designer could apply on a different
parcel (mention compass sectors, distances, equipment choices; never mention this parcel's id).
Return ONLY JSON: {"rejection": "<letter, <= 180 words>", "lessons": [{"rule": "R1", "text": "..."}]}
One lesson per violated rule, each under 30 words."""


def review(site: Site, plan: Plan, rev: Review, use_llm: bool = True, model: str = C.REVIEWER_MODEL,
           precedents: list[str] | None = None) -> tuple[str, list[dict]]:
    """Returns (rejection_text, lessons[{rule, text}])."""
    if rev.passed:
        return ("PERMIT APPROVED. All measured values are within limits. "
                f"Capacity retained: {rev.measured.get('capacity_fraction', 1):.0%} of target."), []
    if use_llm and have_llm():
        try:
            return _review_llm(site, plan, rev, model, precedents or [])
        except Exception as e:  # noqa: BLE001
            print(f"[reviewer] LLM review failed ({type(e).__name__}: {e}); using templates")
    return _review_template(site, plan, rev)


def _review_llm(site, plan, rev, model, precedents):
    summary = site.summary_for_llm()
    user = (f"SITE CONTEXT (homes by compass sector, wetlands): {json.dumps({k: summary[k] for k in ['homes_by_sector', 'wetlands', 'acres']})}\n\n"
            f"PLAN CHOICES: it_mw={plan.it_mw}, cooling={plan.cooling_type}/{plan.cooling_noise}{'/barrier' if plan.cooling_barrier else ''}, "
            f"generators={plan.generators_required} {plan.generator_tier}/{plan.generator_enclosure}, bess_mw={plan.bess_mw}, water={plan.water_source}\n\n"
            f"VIOLATIONS (penalty {rev.penalty}):\n" +
            "\n".join(f"- {v.rule} {v.title}. Measured: {v.measured}. Limit: {v.limit}. Rule: {v.citation}. Objects: {v.objects[:6]}"
                      for v in rev.violations))
    if precedents:
        user += "\n\nPRECEDENT (earlier rejection letters found by Atlas Search; keep your letter consistent with them):\n" + \
                "\n".join(f"- {p[:300]}" for p in precedents)
    data = extract_json(chat(model, SYSTEM, user, json_mode=True, temperature=0.3, max_tokens=1200))
    lessons = [{"rule": l.get("rule", "?"), "text": l["text"].strip()} for l in data.get("lessons", []) if l.get("text")]
    return data.get("rejection", "").strip(), lessons


def _review_template(site: Site, plan: Plan, rev: Review) -> tuple[str, list[dict]]:
    summary = site.summary_for_llm()
    sectors = summary["homes_by_sector"]
    home_sector = max(sectors, key=lambda s: sectors[s]["count"]) if sectors else "E"
    opposite = {"E": "W", "W": "E", "N": "S", "S": "N", "NE": "SW", "SW": "NE", "NW": "SE", "SE": "NW"}[home_sector]
    lines = [f"PERMIT DENIED. Penalty score {rev.penalty}. {len(rev.violations)} deficiencies:"]
    lessons = []
    templ = {
        "R0": f"Every object must sit inside the parcel and outside other footprints; place the full equipment list before anything else.",
        "R1": f"When homes lie to the {home_sector}, put cooling on the {opposite} side of the hall and use the hall as a noise shield; low-noise fans, then an acoustic screen wall, if still over 50 dBA.",
        "R2": f"Line generators along the {opposite} wall of the hall so the building blocks them from homes to the {home_sector}; critically-silenced enclosures if still over 65 dBA.",
        "R3": "Design inside the buildable footprint (parcel minus wetlands and 50/150 ft transition areas); never let pavement touch a wetland buffer.",
        "R4": "Keep combined genset heat input under 100 MMBtu/hr (about 5 x 2 MW units) by covering part of the critical load with battery storage.",
        "R5": "Specify Tier 4 Final gensets with SCR so NOx potential-to-emit stays under 25 t/yr and the plant avoids Title V.",
        "R6": "Use closed-loop air cooling (or municipal supply) so withdrawals stay under 100,000 gal/day and no Water Allocation Permit is needed.",
        "R7": "Any plan with more than 0.25 acre of new pavement needs a stormwater basin sized to about 10% of impervious area.",
        "R8": f"Keep the hall and substation at least 200 ft from residential lot lines; on this kind of parcel that means shifting them {opposite}.",
        "R9": f"Hide generators behind the hall relative to the nearest homes ({home_sector} side of the parcel), or screen the generator yard with a wall or berm.",
        "CAP": "Do not shrink the facility to pass; solve rules with placement and equipment choices.",
    }
    for v in rev.violations:
        lines.append(f"{v.rule} {v.title}: measured {v.measured}; limit {v.limit} ({v.citation}).")
        lessons.append({"rule": v.rule, "text": templ.get(v.rule, v.detail)})
    return "\n".join(lines), lessons
