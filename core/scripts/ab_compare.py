"""A/B harness: single-shot resolver vs investigative agent, same batch.

Runs the identical 60-record batch twice — once with the single-shot LLM
resolver, once with the tool-calling agent — and reports the honest lift: which
exceptions the agent resolved that the single-shot version could not, and any
regressions the other way.

    python core/scripts/ab_compare.py --simulate     # offline, no key
    python core/scripts/ab_compare.py                # real Groq (needs key)
"""

from __future__ import annotations

import argparse
import sys

from recon_engine import (
    generate,
    reconcile,
    SimulatedAgentModel,
    SimulatedLLMProvider,
)
from recon_engine.dataio import load_ground_truth


def _by_txn(result):
    return {d.txn_id: d for d in result.decisions}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data")
    ap.add_argument("--regen", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--total", type=int, default=60)
    ap.add_argument("--simulate", action="store_true",
                    help="use offline simulators for both arms (no API key)")
    args = ap.parse_args()

    import os
    if args.regen or not os.path.exists(os.path.join(args.out, "gateway_settlements.csv")):
        generate(args.out, seed=args.seed, total=args.total)

    single_provider = SimulatedLLMProvider() if args.simulate else None
    agent_model = SimulatedAgentModel() if args.simulate else None

    print("Running single-shot arm ...")
    single = reconcile(args.out, provider=single_provider, use_llm=True)
    print("Running agent arm ...")
    agent = reconcile(args.out, agent_model=agent_model, use_agent=True)

    truth = load_ground_truth(args.out)
    s_by, a_by = _by_txn(single), _by_txn(agent)

    lift, regressions, agree = [], [], 0
    for txn, gt in truth.items():
        s, a = s_by.get(txn), a_by.get(txn)
        if not s or not a:
            continue
        s_ok = s.status.value == gt.true_status
        a_ok = a.status.value == gt.true_status
        if a_ok and not s_ok:
            lift.append((txn, s.status.value, a.status.value, gt.true_status))
        elif s_ok and not a_ok:
            regressions.append((txn, s.status.value, a.status.value, gt.true_status))
        else:
            agree += 1

    def line(m, label):
        print(f"  {label:12s} match_rate={m['match_rate']*100:5.1f}%  "
              f"recall={m['matched_recall']:.3f}  FP={m['false_positive_count']}  "
              f"status_acc={m['status_accuracy']}")

    print("\n=== A/B RESULT ===")
    line(single.metrics, "single-shot")
    line(agent.metrics, "agent")
    ag = agent.metrics.get("agent", {})
    print(f"\n  agent: {ag.get('records_investigated')} investigated, "
          f"avg {ag.get('avg_steps')} steps, {ag.get('total_tool_calls')} tool calls, "
          f"{ag.get('verdicts_honored')} honored / {ag.get('verdicts_overridden_by_guardrail')} overridden")
    if ag.get("groq"):
        print(f"  groq: {ag['groq']['total_calls']} calls, peak {ag['groq']['peak_rpm']} RPM "
              f"(cap {ag['groq']['rpm_cap']})")

    print(f"\n  agreement: {agree} records")
    print(f"  LIFT (agent right where single-shot wrong): {len(lift)}")
    for t in lift:
        print(f"    + {t[0]}: single={t[1]} -> agent={t[2]} (truth {t[3]})")
    print(f"  REGRESSIONS (single-shot right where agent wrong): {len(regressions)}")
    for t in regressions:
        print(f"    - {t[0]}: single={t[1]} -> agent={t[2]} (truth {t[3]})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
