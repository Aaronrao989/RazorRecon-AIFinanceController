"""Inject realistic INVESTIGABLE exceptions into a dataset (append-only).

Tops the dataset up to at least MIN_LIFT_CASES lift-eligible cases so a real-Groq
(or offline) A/B run can demonstrate genuine resolution lift. Idempotent and
seeded: re-running does not double-inject. Writes consistent ground truth so
metrics stay honest. Never mutates existing rows.

    python core/scripts/inject_investigable.py --data data
    python core/scripts/inject_investigable.py --data data --min 3 --seed 4242
"""

from __future__ import annotations

import argparse
import sys

from recon_engine.investigability import INJECT_SEED, MIN_LIFT_CASES, audit, inject


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data")
    ap.add_argument("--min", type=int, default=MIN_LIFT_CASES)
    ap.add_argument("--seed", type=int, default=INJECT_SEED)
    args = ap.parse_args()

    before = audit(args.data)
    print(f"lift-eligible before: {before.lift_eligible_count}")

    res = inject(args.data, min_cases=args.min, seed=args.seed)
    if res["injected"] == 0:
        print(f"nothing injected: {res.get('reason')}")
    else:
        print(f"injected {res['injected']} investigable case(s):")
        for c in res["cases"]:
            print(f"  + {c['txn_id']} [{c['class']}] truth={c['true_status']}")
            print(f"      correct match: {c['correct_match']}  |  decoy credit: ₹{c['decoy']}")

    after = audit(args.data)
    print(f"lift-eligible after:  {after.lift_eligible_count} "
          f"(target {args.min}) — {'OK' if after.lift_eligible_count >= args.min else 'SHORT'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
