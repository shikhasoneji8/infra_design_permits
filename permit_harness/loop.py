"""Run the LangGraph harness. One round = recall -> design -> measure -> review -> checkpoint.

Every round is durable (LangGraph MongoDBSaver + our `runs` collection) before the next
starts, so the process can be killed at any point and resumed with the same run id.
Memory lives in Atlas, not in the model."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from . import config as C
from .graph import build_graph, initial_state
from .plan import Plan
from .rules import Review
from .site import site_from_doc
from .store import MongoStore

console = Console()


def run(site_id: str, run_id: str, store=None, max_rounds: int = 12, resume: bool = False,
        use_llm: bool = True, crash_after_round: int | None = None, sleep_s: float = 0.0,
        designer_model: str = C.DESIGNER_MODEL, reviewer_model: str = C.REVIEWER_MODEL,
        use_memory: bool = True, use_llm_reviewer: bool | None = None) -> dict:
    store = store or MongoStore()
    site_doc = store.get_site(site_id)
    if not site_doc:
        raise SystemExit(f"site '{site_id}' not found in {store.name}; run scripts/load_sites.py first")
    site = site_from_doc(site_doc)

    existing = store.get_run(run_id)
    if existing and existing.get("status") == "passed" and resume:
        console.print(f"[green]run {run_id} already passed at round {existing['round']}[/green]")
        return existing

    graph = build_graph(store, use_llm=use_llm, crash_after_round=None if resume else crash_after_round,
                        designer_model=designer_model, reviewer_model=reviewer_model,
                        on_round=_print_round, checkpointer=store.checkpointer(), use_memory=use_memory,
                        use_llm_reviewer=use_llm_reviewer)
    cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": max_rounds * 6 + 10}

    snapshot = graph.get_state(cfg)
    if resume and snapshot and snapshot.next:
        console.rule(f"[bold yellow]RESUMING {run_id} at round {snapshot.values.get('round')} "
                     f"(next node: {snapshot.next[0]}, last penalty {existing['last_penalty'] if existing else '?'}) from {store.name}")
        final = graph.invoke(None, cfg)
    else:
        if resume:
            console.print("[yellow]no checkpoint to resume; starting fresh[/yellow]")
        store.create_run(run_id, site_id, max_rounds)
        console.rule(f"[bold]NEW RUN {run_id}{'' if use_memory else ' (MEMORY OFF)'} on {site.name} ({site.acres:.1f} ac, {len(site.homes)} homes, "
                     f"{len(site.wetlands)} wetlands) store={store.name} checkpointer={type(store.checkpointer()).__name__}")
        final = graph.invoke(initial_state(site_id, run_id, max_rounds), cfg)

    if final.get("status") == "passed":
        console.print(f"[bold green]PERMIT PASSED at round {final['round']} with {final['plan']['it_mw']} MW IT retained[/bold green]")
    elif final.get("status") == "exhausted":
        console.print(f"[bold red]max rounds reached without passing (penalty {final['review']['penalty']})[/bold red]")
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
        console.print(f"  [dim]lessons retrieved from memory: {len(lessons)} e.g. \"{lessons[0].get('text', '')[:110]}\"[/dim]")
    if rejection:
        console.print(f"  [italic]{rejection[:400]}{'...' if len(rejection) > 400 else ''}[/italic]")
