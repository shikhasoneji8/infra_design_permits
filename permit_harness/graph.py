"""The harness as a LangGraph state machine, checkpointed in MongoDB.

    recall -> design -> measure -> review -> (passed | exhausted -> END, else -> recall)

Every node's output is persisted by LangGraph's MongoDBSaver (collection `checkpoints`
in the same Atlas database) before the next node starts, and our own `runs` collection
keeps the human-readable penalty history. Kill the process anywhere; invoking the graph
again with the same thread_id (= run id) continues from the last completed node.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from . import config as C
from .agents import designer, reviewer
from .llm import embed
from .plan import Plan
from .rules import Review, Violation, evaluate
from .site import Site, site_from_doc


class HarnessState(TypedDict, total=False):
    site_id: str
    run_id: str
    round: int
    max_rounds: int
    prev_plan: Optional[dict]
    prev_review: Optional[dict]
    parent_design_id: Optional[str]
    lessons: list[dict]
    plan: Optional[dict]
    review: Optional[dict]
    design_id: Optional[str]
    rejection: str
    source: str
    status: str
    precedents: list[str]
    round_started: float
    best_plan: Optional[dict]
    best_review: Optional[dict]
    best_design_id: Optional[str]
    regression: Optional[dict]
    stall: int


def review_from_doc(doc: dict) -> Review:
    return Review(penalty=doc["penalty"], passed=doc["passed"], measured=doc["measured"],
                  violations=[Violation(**v) for v in doc["violations"]])


def retrieval_query(site: Site, prev_review: Review | None) -> str:
    s = site.summary_for_llm()
    sectors = ", ".join(f"homes {k} {v['nearest_m']}m" for k, v in s["homes_by_sector"].items())
    wet = ", ".join(f"{w['label'].lower()} {w['sector']}" for w in s["wetlands"][:6])
    q = f"data center site plan; {sectors}; wetlands {wet}"
    if prev_review:
        q += "; rejected for " + "; ".join(f"{v.rule} {v.title}" for v in prev_review.violations)
    return q


def build_graph(store, use_llm: bool = True, crash_after_round: int | None = None,
                designer_model: str = C.DESIGNER_MODEL, reviewer_model: str = C.REVIEWER_MODEL,
                on_round=None, checkpointer=None, use_memory: bool = True):
    site_cache: dict[str, Site] = {}

    def site_of(state) -> Site:
        sid = state["site_id"]
        if sid not in site_cache:
            site_cache[sid] = site_from_doc(store.get_site(sid))
        return site_cache[sid]

    # ---- nodes ---------------------------------------------------------
    def recall(state: HarnessState) -> dict:
        site = site_of(state)
        prev = review_from_doc(state["prev_review"]) if state.get("prev_review") else None
        q = retrieval_query(site, prev)
        if not use_memory:  # A/B baseline: same harness, no recall from Atlas
            return {"lessons": [], "precedents": [], "round": state.get("round", 0) + 1, "round_started": time.time()}
        raw = store.similar_lessons_text(q, k=12) if hasattr(store, "similar_lessons_text") else \
            store.similar_lessons(embed([q])[0], k=12)
        lessons, seen = [], set()
        for l in raw:
            key = (l.get("text") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                lessons.append({"text": l.get("text"), "rule": l.get("rule"), "site_id": l.get("site_id")})
        precedents = store.search_rejections(q, k=3) if hasattr(store, "search_rejections") else []
        return {"lessons": lessons[:6], "precedents": precedents, "round": state.get("round", 0) + 1,
                "round_started": time.time()}

    def design(state: HarnessState) -> dict:
        site = site_of(state)
        prev_plan = Plan.model_validate(state["prev_plan"]) if state.get("prev_plan") else None
        prev = review_from_doc(state["prev_review"]) if state.get("prev_review") else None
        # Stall breaker: if the best penalty has not moved for STALL_ROUNDS rounds, hand the best plan to the
        # deterministic policy for one round (it changes equipment, e.g. low-noise fans, which the model
        # keeps refusing to do), then give control back to the model.
        if prev_plan is not None and state.get("stall", 0) >= C.STALL_ROUNDS:
            plan = designer.heuristic_design(site, prev_plan, prev)
            plan.rationale = "stall breaker: deterministic policy applied to the best plan. " + (plan.rationale or "")
            return {"plan": plan.compact(), "source": "stall-breaker"}
        plan, source = designer.design(site, prev_plan, prev, state.get("lessons", []), use_llm=use_llm,
                                       model=designer_model, regression=state.get("regression"))
        return {"plan": plan.compact(), "source": source}

    def measure(state: HarnessState) -> dict:
        site = site_of(state)
        plan = Plan.model_validate(state["plan"])
        rev = evaluate(plan, site)
        design_id = store.save_design(state["run_id"], state["site_id"], state["round"], state["plan"],
                                      state.get("parent_design_id"), [l["text"] for l in state.get("lessons", [])])
        return {"review": rev.to_doc(), "design_id": design_id}

    def review_node(state: HarnessState) -> dict:
        site = site_of(state)
        plan = Plan.model_validate(state["plan"])
        rev = review_from_doc(state["review"])
        rejection, new_lessons = reviewer.review(site, plan, rev, use_llm=use_llm, model=reviewer_model,
                                                 precedents=state.get("precedents", []))
        store.save_review(state["run_id"], state["site_id"], state["round"], state["design_id"], rev.to_doc(), rejection)
        if new_lessons:
            ctx = retrieval_query(site, None)
            if getattr(store, "atlas_auto_embed", False):
                docs = [{**l, "context": ctx} for l in new_lessons]
            else:
                embs = embed([l["text"] for l in new_lessons])
                docs = [{**l, "embedding": e, "context": ctx} for l, e in zip(new_lessons, embs)]
            store.save_lessons(state["run_id"], state["site_id"], state["round"], docs)
        status = "passed" if rev.passed else ("exhausted" if state["round"] >= state["max_rounds"] else "running")
        store.checkpoint(state["run_id"], state["round"], state["design_id"], rev.penalty,
                         rev.measured.get("capacity_fraction", 1.0), status)
        if on_round:
            on_round(state["round"], plan, rev, rejection, state.get("lessons", []), state.get("source", "?"),
                     time.time() - state.get("round_started", time.time()))
        # Hill-climb: the next round always starts from the best plan so far. A worse attempt is
        # recorded (designs/reviews/lessons) but never becomes the base for the next design.
        best_rev = state.get("best_review")
        if best_rev is None or rev.penalty < best_rev["penalty"]:
            best = {"best_plan": state["plan"], "best_review": rev.to_doc(), "best_design_id": state["design_id"],
                    "regression": None, "stall": 0}
        elif rev.penalty == best_rev["penalty"]:
            best = {"best_plan": state["plan"], "best_review": rev.to_doc(), "best_design_id": state["design_id"],
                    "regression": None, "stall": state.get("stall", 0) + 1}
        else:
            best = {"regression": {"penalty": rev.penalty, "rules": [v.rule for v in rev.violations],
                                   "best_penalty": best_rev["penalty"]}, "stall": state.get("stall", 0) + 1}
        return {"rejection": rejection, "status": status, "prev_plan": best.get("best_plan", state.get("best_plan")),
                "prev_review": best.get("best_review", state.get("best_review")),
                "parent_design_id": best.get("best_design_id", state.get("best_design_id")), **best}

    def crash_gate(state: HarnessState) -> dict:
        # Separate node so the review node's checkpoint is complete before we die.
        if crash_after_round and state["round"] >= crash_after_round and state["status"] == "running":
            print(f"\n*** simulated crash after round {state['round']}: checkpoint is in {store.name}; "
                  f"rerun with --resume and the same --run id ***")
            sys.stdout.flush()
            sys.exit(3)
        return {}

    def route(state: HarnessState) -> str:
        return END if state["status"] in {"passed", "exhausted"} else "recall"

    g = StateGraph(HarnessState)
    g.add_node("recall", recall)
    g.add_node("design", design)
    g.add_node("measure", measure)
    g.add_node("review", review_node)
    g.add_node("crash_gate", crash_gate)
    g.add_edge(START, "recall")
    g.add_edge("recall", "design")
    g.add_edge("design", "measure")
    g.add_edge("measure", "review")
    g.add_edge("review", "crash_gate")
    g.add_conditional_edges("crash_gate", route, {"recall": "recall", END: END})
    return g.compile(checkpointer=checkpointer)


def initial_state(site_id: str, run_id: str, max_rounds: int) -> HarnessState:
    return {"site_id": site_id, "run_id": run_id, "round": 0, "max_rounds": max_rounds, "prev_plan": None,
            "prev_review": None, "parent_design_id": None, "lessons": [], "plan": None, "review": None,
            "design_id": None, "rejection": "", "source": "", "status": "running", "precedents": [],
            "best_plan": None, "best_review": None, "best_design_id": None, "regression": None, "stall": 0}
