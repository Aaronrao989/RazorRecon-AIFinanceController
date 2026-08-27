# Sample metrics report (seed 42, 60 records, SimulatedLLMProvider)

- match_rate: 0.8667  (52/60)
- precision / recall / F1: 1.0 / 1.0 / 1.0
- status_accuracy: 1.0
- false_positives: 0  (₹0 at risk)
- llm_calls: 5   throughput: 5983.88/s
- by_status: {"MATCHED": 52, "DATE_SKEW": 1, "MISSING_IN_BANK": 1, "CURRENCY_MISMATCH": 1, "SPLIT_SETTLEMENT": 1, "MISMATCH_AMOUNT": 2, "MISSING_IN_LEDGER": 1, "DUPLICATE_UTR": 1}

## Exception list

- TXN10002 [DATE_SKEW] via deterministic-rule: bank credit dated 2026-07-31 is T+10 vs settlement, far outside the T+4 window
- TXN10006 [MISSING_IN_BANK] via deterministic-rule: no bank credit references this settlement
- TXN10007 [CURRENCY_MISMATCH] via deterministic-rule: order currency USD is not the base currency INR
- TXN10022 [SPLIT_SETTLEMENT] via deterministic-rule: net ₹2477.96 paid across 2 credits summing to ₹2477.96
- TXN10029 [MISMATCH_AMOUNT] via deterministic-rule: bank credit ₹2356.32 differs from net ₹2513.00 by ₹156.68, beyond gray tolerance
- TXN10039 [MISMATCH_AMOUNT] via deterministic-rule: bank credit ₹3765.89 differs from net ₹4115.49 by ₹349.60, beyond gray tolerance
- TXN10042 [MISSING_IN_LEDGER] via deterministic-rule: order_id not found in the order ledger (likely a wrong mapping)
- TXN10051 [DUPLICATE_UTR] via deterministic-rule: UTR PDZP6LFEWVZNVKSP appears 2 times for this settlement