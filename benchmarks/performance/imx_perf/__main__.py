"""``python -m imx_perf <command>`` — offline performance harness entry point.

Commands (all bounded and offline):

  fixtures   write the fictional fixture files under fixtures/
  funnel     closed-form capacity requirements per target tier
  selection  Jev call-strategy simulation and cache/invalidation scenarios
  chaos      durable-queue chaos test (kill workers mid-item)
  pipeline   discrete-event pipeline simulation across supply scenarios
  measure    time the real packages when importable (temporary databases)
  all        everything except measure (add --measure to include it)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import chaos, funnel, measure, pipeline_sim, selection_sim
from .config import Assumptions, provenance_table
from .fixtures import (
    candidate_evidence,
    fixture_json,
    generate,
    question_catalog_json,
)
from .report import md_table, run_metadata, write_json

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent


def _load(args: argparse.Namespace) -> Assumptions:
    return Assumptions.load(Path(args.assumptions) if args.assumptions else None)


def cmd_fixtures(args: argparse.Namespace) -> dict[str, Any]:
    a = _load(args)
    out = HERE / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    (out / "assumptions.default.json").write_text(a.to_json() + "\n", "utf-8")
    fixtures = generate(a, args.population, seed=args.seed)
    (out / f"population.seed{args.seed}.json").write_text(fixture_json(fixtures) + "\n", "utf-8")
    (out / "question_catalog.json").write_text(question_catalog_json() + "\n", "utf-8")
    (out / "candidate_evidence.fictional.json").write_text(
        json.dumps(candidate_evidence(), indent=1, sort_keys=True) + "\n", "utf-8")
    write_json(out / "assumption_provenance.json", provenance_table())
    return {"written": sorted(p.name for p in out.iterdir()), "listings": len(fixtures)}


def cmd_funnel(args: argparse.Namespace) -> dict[str, Any]:
    a = _load(args)
    result = funnel.model(a)
    result["metadata"] = run_metadata(REPO)
    write_json(Path(args.out) / "funnel.json", result)
    rows = []
    for t in result["tiers"]:
        rows.append([t["tier_v5_per_day"], t["submission_attempts_per_day"], t["enriched_listings_per_day"],
                     t["enrich_page_loads_per_day"], t["observations_per_day"], t["jev_calls_per_day_two_call"],
                     t["jev_usd_per_day_two_call"], t["browser_hours_per_day"], t["avg_browser_sessions_little"],
                     t["human_hours_per_day"]])
    md = ["## Funnel requirements per tier (modeled)", "",
          md_table(["V5/day", "attempts", "enriched", "enrich pages", "observations", "Jev calls",
                    "Jev USD", "browser h", "avg sessions", "human h"], rows), ""]
    lane_rows = []
    for t in result["tiers"]:
        for lane in t["lanes"]:
            lane_rows.append([t["tier_v5_per_day"], lane["window_h"], lane["service_s"], lane["per_minute"],
                              lane["completion_budget_s"], lane["lanes_at_util"], lane["lanes_ceiling"]])
    md += ["## Submission-only lanes at configured utilization (modeled; yield-adjusted; excludes resume/reconciliation)", "",
           md_table(["V5/day", "window h", "service s", "per minute", "budget s", "lanes", "ceil"], lane_rows), ""]
    plan = result["run_plan"]
    md += ["## Search plan (code-derived bounds, modeled timing and detail success)", "",
           md_table(["source", "search pages", "detail pages", "seconds", "max listings", "assumed FULL descriptions"],
                    [[s["source"], s["search_pages"], s["detail_pages"], s["seconds"], s["max_listings"],
                      s["full_description_listings"]] for s in plan["per_source"]]),
           "", f"Total: {plan['page_ops']} page operations, ≈ {plan['run_minutes']} min, ≤ {plan['max_observations']} "
               f"observations; {plan['full_description_listings']} FULL descriptions assumed, not observed.", ""]
    (Path(args.out) / "funnel.md").write_text("\n".join(md), "utf-8")
    return {"tiers": [(t["tier_v5_per_day"], t["avg_browser_sessions_little"]) for t in result["tiers"]]}


def cmd_selection(args: argparse.Namespace) -> dict[str, Any]:
    a = _load(args)
    fixtures = generate(a, args.population, seed=args.seed)
    strategies = selection_sim.run_strategies(fixtures, a, seed=args.seed)
    cache = selection_sim.cache_scenarios(fixtures, a, seed=args.seed)
    result = {"strategies": strategies, "cache": cache, "population": len(fixtures),
              "metadata": run_metadata(REPO)}
    write_json(Path(args.out) / "selection.json", result)
    rows = [[s["strategy"], s["reached_jev"], s["calls"], s["attempted_calls"], s["input_tokens"], s["usd"],
             s["mean_latency_s"], s["p95_latency_s"], s["deferred_to_enrichment"],
             s.get("apply_agreement_with_baseline")] for s in strategies["strategies"]]
    md = [f"## Call strategies over {len(fixtures)} fictional listings (stub judge; mechanics only)", "",
          md_table(["strategy", "reached Jev", "calls", "attempted", "input tokens", "USD", "mean s", "p95 s",
                    "deferred", "APPLY agreement"], rows), "",
          "## Cache and invalidation scenarios", "", "```json", json.dumps(cache, indent=2), "```", ""]
    (Path(args.out) / "selection.md").write_text("\n".join(md), "utf-8")
    return {"strategies": [(s["strategy"], s["calls"]) for s in strategies["strategies"]]}


def cmd_chaos(args: argparse.Namespace) -> dict[str, Any]:
    variants = {
        "default_heartbeat": chaos.ChaosConfig(seed=args.seed),
        "long_steps_no_heartbeat": chaos.ChaosConfig(seed=args.seed, step_ms=60.0, heartbeat=False,
                                                     p_kill_per_step=0.05),
        "long_steps_heartbeat": chaos.ChaosConfig(seed=args.seed, step_ms=60.0, heartbeat=True,
                                                  p_kill_per_step=0.05),
    }
    results = {}
    for name, cfg in variants.items():
        results[name] = chaos.as_dict(chaos.run(cfg))
    out = {"variants": results, "metadata": run_metadata(REPO)}
    write_json(Path(args.out) / "chaos.json", out)
    rows = [[name, r["config"]["items"], r["config"]["workers"], r["kills"], r["takeovers"], r["reclaims"],
             r["reconcile_routes"], r["reconciled_without_effect"], r["effects"], r["double_effects"], r["lane_violations"],
             r["live_leases_at_end"], r["wall_s"], "PASS" if r["passed"] else "FAIL"]
            for name, r in results.items()]
    md = ["## Local queue abandonment exercise (observed; simulated effects, no OS process kills)", "",
          md_table(["variant", "items", "workers", "kills", "takeovers", "reclaims", "routed to reconcile",
                    "no effect after intent", "effects", "double effects", "lane violations", "live leases", "wall s", "result"], rows), ""]
    (Path(args.out) / "chaos.md").write_text("\n".join(md), "utf-8")
    return {name: ("PASS" if r["passed"] else "FAIL") for name, r in results.items()}


def cmd_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    a = _load(args)
    scenarios = pipeline_sim.default_scenarios()
    for scenario in scenarios:
        scenario.seed = args.seed
    if args.quick:
        scenarios = scenarios[:3]
    started = time.perf_counter()
    results = pipeline_sim.sweep(a, scenarios)
    out = {"results": results, "wall_s": round(time.perf_counter() - started, 2), "metadata": run_metadata(REPO)}
    write_json(Path(args.out) / "pipeline.json", out)
    rows = []
    for r in results:
        d = r["per_day"]
        rows.append([r["scenario"]["label"], d["V1_observations"], d["V2_unique_listings"], d["enrich_page_loads"],
                     d["jev_calls"], d["jev_usd_est"], d["V3_effective_apply"], d["review"],
                     d["V5_submitted_site_observed"], d["submission_unknown"], d["needs_input_stops"],
                     d["human_hours"], r["binding_resource"], r["utilization"][r["binding_resource"]]])
    md = ["## Pipeline simulation, per simulated day (modeled; no site acceptance measured)", "",
          md_table(["scenario", "V1 obs", "V2 unique", "enrich pages", "Jev calls", "Jev USD", "V3 APPLY", "REVIEW",
                    "simulated accepted", "unknown", "input stops", "human h", "binding", "util"], rows), ""]
    (Path(args.out) / "pipeline.md").write_text("\n".join(md), "utf-8")
    return {"scenarios": len(results), "wall_s": out["wall_s"]}


def cmd_measure(args: argparse.Namespace) -> dict[str, Any]:
    a = _load(args)
    result = measure.run(a)
    result["metadata"] = run_metadata(REPO)
    tag = args.tag or "default"
    write_json(Path(args.out) / f"measure.{tag}.json", result)
    return {"environment": result["environment"], "skipped": result.get("skipped"),
            "sections": [k for k in result if k not in ("environment", "metadata", "skipped")]}


def cmd_all(args: argparse.Namespace) -> dict[str, Any]:
    out = {
        "fixtures": cmd_fixtures(args),
        "funnel": cmd_funnel(args),
        "selection": cmd_selection(args),
        "chaos": cmd_chaos(args),
        "pipeline": cmd_pipeline(args),
    }
    if args.measure:
        out["measure"] = cmd_measure(args)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imx_perf", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["fixtures", "funnel", "selection", "chaos", "pipeline", "measure", "all"])
    parser.add_argument("--out", default=str(HERE / "results"))
    parser.add_argument("--assumptions", default=None, help="JSON file overriding Assumptions")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--population", type=int, default=2000)
    parser.add_argument("--quick", action="store_true", help="pipeline: first three scenarios only")
    parser.add_argument("--measure", action="store_true", help="all: also run measure")
    parser.add_argument("--tag", default=None, help="measure: result file tag")
    args = parser.parse_args(argv)
    if not 1 <= args.population <= 10000:
        parser.error("population must be 1..10000 (offline bounded harness)")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    handler = {
        "fixtures": cmd_fixtures, "funnel": cmd_funnel, "selection": cmd_selection, "chaos": cmd_chaos,
        "pipeline": cmd_pipeline, "measure": cmd_measure, "all": cmd_all,
    }[args.command]
    started = time.perf_counter()
    summary = handler(args)
    summary["elapsed_s"] = round(time.perf_counter() - started, 2)
    print(json.dumps(summary, indent=2, default=str))
    if args.command in ("chaos", "all"):
        checks = summary if args.command == "chaos" else summary["chaos"]
        if any(v == "FAIL" for v in checks.values()):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
