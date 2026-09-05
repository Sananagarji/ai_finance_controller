# AI Finance Controller
### "Reconcile what you can prove. Escalate what you can't."

Built for Razorpay Buildathon Track 04: AI Finance Controller.

## The idea

Financial reconciliation isn't about matching the most transactions —
it's about knowing which matches you can trust. This engine reconciles
a merchant's payments against bank settlements automatically when the
evidence is strong, investigates when it's ambiguous, and **refuses to
guess** when it isn't sure — creating an exception with full evidence
instead.

**Forced matches: 0.** No transaction is ever reconciled just to
improve the match rate.

## How it works

```
Payments + Bank txns + Ledger + Fees
              |
         Normalization        (merchant names, dates, amounts)
              |
      Candidate scoring         (amount + date + name similarity)
              |
        +-----+------+
        |            |
  Clean, high     Ambiguous /
  confidence      amount gap
        |            |
        v            v
  AUTO RECONCILE   INVESTIGATOR
                   (searches fee records,
                    checks for contradictions)
                        |
                +-------+-------+
                |               |
          Evidence found   No evidence
                |               |
                v               v
          RECONCILE        EXCEPTION
        (explained)      (with reasoning +
                          recommended action)
```

- **`generate_data.py`** — creates a synthetic dataset (70 payments, bank
  transactions, ledger entries, fee records) with deliberately difficult
  cases: name variations, date drift, fee deductions, missing records,
  ambiguous duplicates, unexplained mismatches. Ground truth is stored
  separately for evaluation and never seen by the engine while deciding.
- **`engine.py`** — normalization, candidate scoring, and the rule-based
  investigator that searches fee records for evidence before ever
  recommending a match.
- **`main.py`** — orchestrates the pipeline, logs a full audit trail per
  payment, computes match-rate metrics, and scores decisions against
  ground truth (correct decisions, false matches, missed reconciliations).
- **`ask_controller.py`** — a Q&A interface over the results ("why wasn't
  PAY-005 reconciled?", "match rate?", "unresolved above ₹10,000?"). Answers
  are computed directly from structured data and cite specific payment IDs —
  never invented.

## Why the investigator is rule-based, not an LLM API call

Every "evidence-gathering" step (searching fee records, checking date
proximity, detecting ambiguous duplicates) is implemented as a
deterministic function rather than an LLM call. This keeps every
decision 100% reproducible and auditable for a finance context where
that matters, and keeps the project runnable offline with no API key.
The architecture is written so an LLM call could later replace the
`investigate()` function without changing anything downstream — the
decision policy (what counts as sufficient evidence, when to refuse)
is the actual product, independent of how evidence-gathering is
implemented.

## Results on this run

```
Total payments processed     : 70
Reconciled                   : 46 (65.7%)
  - Rule-based auto-reconcile : 37
  - AI-investigated & resolved: 9
Exceptions (escalated)       : 24 (34.3%)
Forced matches                : 0

Evaluation against ground truth:
Correct decisions            : 69/70 (98.6%)
False matches                 : 0   <- the metric that matters most
Correctly escalated           : 23
Missed safe reconciliations    : 1 (overly cautious — acceptable trade-off)
```

## Running it

```bash
python3 generate_data.py     # generate the synthetic dataset + ground truth
python3 main.py              # run reconciliation, print metrics + evaluation
python3 ask_controller.py    # interactive Q&A over the results
```

## What broke, and how I got out

My first version scored candidates purely on amount + date + name
similarity and auto-reconciled anything above a single threshold. That
immediately produced false matches on the "multiple plausible
candidates" cases — two bank transactions with the same amount and
date, where the engine just picked whichever came first alphabetically.
I added an explicit ambiguity check: if the top two candidates score
within 5 points of each other, that's treated as insufficient evidence
by definition, and the payment is escalated instead of guessed. That
single change took false matches on the evaluation set from several
down to zero.

## Design choices

- **Why refuse instead of always picking the top-scored candidate:** in
  finance, a false match (auto-reconciling the wrong transaction) is far
  more costly than an exception a human reviews. The engine is tuned to
  prefer "I don't know" over a wrong answer.
- **Why check fee records before assuming a gap is a fee:** an unexplained
  amount difference could be a genuine problem. The investigator only
  reconciles a fee-driven gap when an actual fee record exists with a
  matching amount and date — it never assumes.
- **Why separate ground truth from what the engine sees:** this is the
  only way to honestly claim the 98.6% / 0 false matches numbers above —
  they're computed by comparing decisions to a held-out answer key, not
  self-reported.

## Next steps with more time

- Swap the rule-based investigator for a real LLM call with tool-calling
  (search_fees(), search_ledger(), etc.) — the decision policy already
  supports this without changes
- Add a lightweight web UI over the existing CSV outputs
- Fuzzy matching when transaction references themselves are corrupted
