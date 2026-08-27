# recon-engine

Standalone multi-source financial reconciliation engine. **No web-framework
imports** — it is a plain, importable Python library that is fully testable on
its own.

```python
from recon_engine import generate, reconcile, SimulatedLLMProvider

generate("data", seed=42, total=60)          # 3 linked CSVs + hidden ground truth
result = reconcile("data", provider=SimulatedLLMProvider())  # offline demo
print(result.metrics["match_rate"], result.metrics["matched_precision"])
for d in result.exceptions:
    print(d.txn_id, d.status.value, d.reason)
```

## Layers

1. **Deterministic matcher** (`matcher.py`) — joins settlements ↔ bank ↔ ledger
   on keys, applies amount/date tolerances, and classifies. It *abstains*
   (returns an `AmbiguousCase`) on genuinely gray records instead of guessing.
2. **Bounded LLM resolver** (`resolver.py` + `llm_provider.py`) — called only on
   ambiguous records. Guardrails: a `MATCHED` verdict is honoured only if
   `confidence >= CONFIDENCE_THRESHOLD` **and** `amount_delta <= AMOUNT_DELTA_CAP`;
   otherwise the record is escalated. Any provider failure is caught and escalated
   — the batch never crashes.
3. **Metrics** (`metrics.py`) — precision/recall of MATCHED vs ground truth,
   false-positive cost, throughput, per-status accuracy.
4. **Audit** (`audit.py`) — one JSON entry per record: inputs seen, rule fired,
   LLM reasoning, confidence, final decision.

All thresholds live in `config.py`.

## Install & test

```bash
pip install -e ".[llm,test]"
pytest
```

## LLM providers

- `GroqLLMProvider` — real Groq calls (OpenAI-compatible endpoint) via `requests`.
  Reads `GROQ_API_KEY` from env. Retries with backoff on 429; falls back to the
  documented `openai/gpt-oss-20b` model; escalates after exhausting retries.
- `SimulatedLLMProvider` — a deterministic, **clearly-labelled** offline heuristic
  (`model="simulated-heuristic"`) for demos/tests without a key. It is *not* a
  language model and never claims to be.
