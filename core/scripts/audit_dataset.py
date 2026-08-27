"""Audit a dataset for INVESTIGABLE exceptions — cases that reward the agent.

Reports which exceptions the single-shot resolver gets wrong/escalates but whose
correct answer a read-only tool can reach AND that survives the guardrails. These
are the cases that produce non-zero A/B lift. Makes NO LLM calls; never mutates
data (unless you pass --inject).

    python core/scripts/audit_dataset.py --data data
    python core/scripts/audit_dataset.py --data data --inject   # top up if short
"""

from __future__ import annotations

import argparse
import sys

from recon_engine.investigability import MIN_LIFT_CASES, audit, inject


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data", help="dataset directory")
    ap.add_argument("--inject", action="store_true",
                    help="append investigable cases if lift-eligible < MIN_LIFT_CASES")
    ap.add_argument("--min", type=int, default=MIN_LIFT_CASES)
    args = ap.parse_args()

    rep = audit(args.data)
    print("=== DATASET AUDIT ===")
    print(f"total records:     {rep.total_records}")
    print(f"dirty records:     {rep.dirty_records}")
    print(f"reached the agent: {rep.reached_agent} (became ambiguous)")
    print(f"dirty by true status: {rep.by_true_status}")
    print(f"\nlift-eligible cases: {rep.lift_eligible_count}")
    for c in rep.lift_eligible:
        print(f"  · {c['txn_id']} [{c['class']}] truth={c['true_status']} "
              f"single-shot={c['single_shot_said']}  — {c['tool_evidence']}")

    if rep.can_show_lift():
        print("\n✅ This dataset CAN demonstrate non-zero A/B lift as-is.")
    else:
        print("\n⚠️  No lift-eligible cases: single-shot vs agent will TIE on this data.")
        print("    Re-run with --inject to append realistic investigable cases.")

    if args.inject:
        print("\n=== INJECTING ===")
        res = inject(args.data, min_cases=args.min)
        if res["injected"] == 0:
            print(f"nothing injected: {res.get('reason')}")
        else:
            print(f"injected {res['injected']} case(s); lift-eligible "
                  f"{res['lift_eligible_before']} -> {res['lift_eligible_after']}")
            for c in res["cases"]:
                print(f"  + {c['txn_id']} [{c['class']}] truth={c['true_status']} "
                      f"(correct: {c['correct_match']}, decoy ₹{c['decoy']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
