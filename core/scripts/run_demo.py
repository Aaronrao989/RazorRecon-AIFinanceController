"""CLI demo: generate data, reconcile, and print the metrics + exception list.

    python core/scripts/run_demo.py --out data --simulate-llm

Flags:
  --out DIR         where to write/read the datasets (default: data)
  --regen           regenerate the synthetic datasets first
  --seed N          generator seed (default 42)
  --total N         number of settlements to generate (default 60)
  --simulate-llm    use the offline SimulatedLLMProvider (clearly labelled, not
                    a real model) so ambiguous cases are resolved without a key
  --no-llm          do not use any resolver; ambiguous cases -> exception list
  (default)         use Groq if GROQ_API_KEY is set, else escalate ambiguous
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from recon_engine import generate, reconcile, SimulatedLLMProvider


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data")
    ap.add_argument("--regen", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--total", type=int, default=60)
    ap.add_argument("--simulate-llm", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    if args.regen or not os.path.exists(os.path.join(args.out, "gateway_settlements.csv")):
        print(f"Generating {args.total} records into {args.out}/ ...")
        generate(args.out, seed=args.seed, total=args.total)

    provider = SimulatedLLMProvider() if args.simulate_llm else None
    use_llm = not args.no_llm

    last = {"n": 0}

    def progress(ev):
        if ev.get("phase") == "reconciling":
            last["n"] = ev["processed"]

    result = reconcile(args.out, provider=provider, use_llm=use_llm, progress_cb=progress)

    m = result.metrics
    print("\n=== METRICS ===")
    print(f"records:            {m['total_records']}")
    print(f"auto-matched:       {m['auto_matched']}  (match rate {m['match_rate']*100:.1f}%)")
    print(f"exceptions:         {m['exceptions']}")
    print(f"MATCHED precision:  {m['matched_precision']}")
    print(f"MATCHED recall:     {m['matched_recall']}")
    print(f"MATCHED F1:         {m['matched_f1']}")
    print(f"status accuracy:    {m.get('status_accuracy')}")
    print(f"false positives:    {m['false_positive_count']}  (₹{m['false_positive_cost']} at risk)")
    print(f"LLM calls:          {m.get('llm_calls')}")
    print(f"throughput:         {m.get('throughput_rps')} rec/s over {m.get('wall_clock_seconds')}s")
    print(f"by status:          {json.dumps(m['by_status'])}")

    if m["false_positives"]:
        print("\n=== FALSE POSITIVES (called MATCHED but weren't) ===")
        for fp in m["false_positives"]:
            print(f"  {fp['txn_id']}: said {fp['said']} / truth {fp['truth']} "
                  f"(₹{fp['money_at_risk']}) — {fp['reason']}")
    else:
        print("\nNo false positives. ✅")

    print("\n=== EXCEPTION LIST (unresolved / not auto-matched) ===")
    for d in result.exceptions:
        print(f"  {d.txn_id} [{d.status.value}] via {d.source.value}: {d.reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
