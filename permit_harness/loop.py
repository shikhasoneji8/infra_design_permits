"""The harness loop. One round = design -> measure -> review -> remember -> checkpoint.

Every round is durable before the next one starts, so the process can be killed at any
point and resumed with the same run id. Memory lives in Atlas, not in the model."""
from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.table import Table

from . import config as C
from .agents import designer, reviewer
from .llm import embed
from .plan import Plan
from .rules import Review, Violation, evaluate
from .site import Site, site_from_doc
from .store import MongoStore, MemoryStore

console = Console()


def _review_from_doc(doc: dict) -> Review:
    return Review(penalty=doc["penalty"], passed=doc["passed"], measured=doc["measured"],
                  violations=[Violation(**v) for v in doc["violations"]])


def _retrieval_query(site: Site, prev_review: Review | None) -> str:
    s = site.summary_for_llm()
    sectors = ", ".join(f"homes {k} {v['nearest_m']}m" for k, v in s["homes_by_sector"].items())
    wet = ", ".join(f"{w['label'].lower()} {w['sector']}" for w in s["wetlands"][:6])
    q = f"data center site plan; {sectors}; wetlands {wet}"
    if prev_review:
        q += "; rejected for " + "; ".join(f"{v.rule} {v.title}" for v in prev_review.violations)
    return q


def run(site_id: str, run_id: str, store=None, max_rounds: int = 12, resume: bool = False,
        use_llm: bool = True, crash_after_round: int | None = None, sleep_s: float = 0.0,
        designer_model: str = C.DESIGNER_MODEL, reviewer_model: str = C.REVIEWER_MODEL) -> dict:
    store = store or MongoStore()
    site_doc = store.get_site(site_id)
    if not site_doc:
        raise SystemExit(f"site '{site_id}' not found in {store.name}; run scripts/load_sites.py first")
    site = site_from_doc(site_doc)

    run_doc = store.get_run(run_id) if resume else None
    if run_doc and run_doc.get("status") == "passed":
        console.print(f"[green]run {run_id} already passed at round {run_doc['round']}[/green]")
        return run_doc
    if run_doc is None:
        run_doc = store.create_run(run_id, site_id, max_rounds)
        round_no, prev_plan, prev_review, parent_design_id = 0, None, None, None
        console.rule(f"[bold]NEW RUN {run_id} on {site.name} ({site.acres:.1f} ac, {len(site.homes)} homes, {len(site.wetlands)} wetlands) store={store.name}")
    else:
        round_no = run_doc["round"]
        parent_design_id = run_doc["last_design_id"]
        d = store.get_design(parent_design_id)
        r = store.get_review(run_id, round_no)
        prev_plan = Plan.model_validate(d["plan"])
        prev_review = _review_from_doc(r)
        console.rule(f"[bold yellow]RESUMING {run_id} at round {round_no} (last penalty {run_doc['last_penalty']}) from {store.name}")

    while round_no < max_rounds:
        round_no += 1
        t0 = time.time()
        # 1. remember: pull lessons relevant to this site and the last rejection
        q_emb = embed([_retrieval_query(site, prev_review)])[0]
        lessons, seen = [], set()
        for l in store.similar_lessons(q_emb, k=12):
            key = (l.get("text") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                lessons.append(l)
        lessons = lessons[:6]
        # 2. design
        plan, source = designer.design(site, prev_plan, prev_review, lessons, use_llm=use_llm, model=designer_model)
        # 3. measure (code, not AI)
        rev = evaluate(plan, site)
        design_id = store.save_design(run_id, site_id, round_no, plan.compact(), parent_design_id,
                                      [l.get("text") for l in lessons])
        # 4. review + distil lessons
        rejection, new_lessons = reviewer.review(site, plan, rev, use_llm=use_llm, model=reviewer_model)
        store.save_review(run_id, site_id, round_no, design_id, rev.to_doc(), rejection)
        if new_lessons:
            embs = embed([l["text"] for l in new_lessons])
            store.save_lessons(run_id, site_id, round_no, [
                {**l, "embedding": e, "context": _retrieval_query(site, None)} for l, e in zip(new_lessons, embs)])
        # 5. checkpoint (durable before anything else happens)
        status = "passed" if rev.passed else "running"
        store.checkpoint(run_id, round_no, design_id, rev.penalty, rev.measured.get("capacity_fraction", 1.0), status)
        _print_round(round_no, plan, rev, rejection, lessons, source, time.time() - t0)
        if rev.passed:
            console.print(f"[bold green]PERMIT PASSED at round {round_no} with {plan.it_mw} MW IT retained[/bold green]")
            break
        if crash_after_round and round_no >= crash_after_round:
            console.print(f"[bold red]simulated crash after round {round_no} (checkpoint is in {store.name}; rerun with --resume)[/bold red]")
            sys.exit(3)
        prev_plan, prev_review, parent_design_id = plan, rev, design_id
        if sleep_s:
            time.sleep(sleep_s)
    else:
        store.checkpoint(run_id, round_no, parent_design_id, prev_review.penalty if prev_review else -1,
                         prev_review.measured.get("capacity_fraction", 1.0) if prev_review else 1.0, "exhausted")
    return store.get_run(run_id)


def _print_round(n: int, plan: Plan, rev: Review, rejection: str, lessons: list[dict], source: str, secs: float):
    t = Table(title=f"round {n}  penalty {rev.penalty}  ({source} designer, {secs:.1f}s)", show_lines=False)
    t.add_column("rule")
    t.add_column("measured", overflow="fold")
    t.add_column("limit", overflow="fold")
    for v in rev.violations:
        t.add_row(f"{v.rule} ({v.penalty})", v.measured, v.limit)
    if not rev.violations:
        t.add_row("-", "no violations", "-")
    console.print(t)
    m = rev.measured
    console.print(f"  night {m.get('night_dba_worst_home')}  day {m.get('day_dba_worst_home')}  NOx {m.get('nox_pte_tpy')} tpy  "
                  f"water {m.get('water_gpd'):,} gpd  impervious {m.get('new_impervious_acres')} ac  IT {plan.it_mw} MW  "
                  f"gens {len(plan.by_kind('generator'))} {plan.generator_tier}/{plan.generator_enclosure}  cooling {plan.cooling_type}/{plan.cooling_noise}")
    if lessons:
        console.print(f"  [dim]lessons retrieved: {len(lessons)} e.g. \"{lessons[0].get('text', '')[:110]}\"[/dim]")
    if rejection:
        console.print(f"  [italic]{rejection[:400]}{'...' if len(rejection) > 400 else ''}[/italic]")
