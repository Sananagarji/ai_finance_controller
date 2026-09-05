"""
AI Finance Controller — core engine.

Philosophy (this is the whole point of the project):

    RULES handle certainty.
    THE INVESTIGATOR handles ambiguity.
    HUMANS handle uncertainty.

Every payment goes through:

  1. Normalization      -> comparable representations of names/dates/amounts
  2. Candidate matching  -> score every plausible bank txn / ledger entry
  3. Decision policy:
       HIGH score, single clean candidate         -> AUTO RECONCILE
       MEDIUM score / amount gap / multiple cands  -> INVESTIGATOR
       Investigator finds sufficient evidence       -> RECONCILE (explained)
       Investigator finds insufficient evidence      -> EXCEPTION (never guesses)

The investigator here is a deterministic, rule-based evidence-gatherer
(no external LLM call — this keeps the project runnable offline and
every decision fully auditable). It searches fee records the same way
an LLM-based version would via tool calls; the difference is only that
the "tool calls" here are plain function calls instead of API round
trips. The decision policy and refusal-to-guess logic are the actual
product, not the specific implementation of the search step.
"""

import csv
import re
from difflib import SequenceMatcher
from datetime import datetime

AMOUNT_EXACT_TOLERANCE = 1.0     # rupees
DATE_WINDOW_DAYS = 2
AUTO_RECONCILE_THRESHOLD = 90    # score >= this AND single candidate -> auto
INVESTIGATE_THRESHOLD = 55       # score >= this -> worth investigating
FEE_MATCH_TOLERANCE = 5.0        # rupees, when checking if a fee explains a gap


# ---------- Normalization ----------

LEGAL_SUFFIXES = re.compile(r"\b(pvt\.?|private|ltd\.?|limited|co\.?)\b", re.IGNORECASE)
NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize_name(raw):
    """Strip legal suffixes, UPI noise, punctuation, and casing so
    'Acme Pvt Ltd' and 'UPI/ACME PRIVATE LIMITED/8213' compare sensibly."""
    s = raw.lower()
    s = re.sub(r"^upi/", "", s)
    s = re.sub(r"/\d+$", "", s)  # trailing UPI reference numbers
    s = LEGAL_SUFFIXES.sub("", s)
    s = NON_ALNUM.sub("", s)
    return s.strip()


def name_similarity(a, b):
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d")


def date_score(d1, d2):
    delta = abs((parse_date(d1) - parse_date(d2)).days)
    if delta == 0:
        return 100
    if delta <= DATE_WINDOW_DAYS:
        return 80
    return max(0, 50 - delta * 10)


def amount_score(a1, a2):
    diff = abs(a1 - a2)
    if diff <= AMOUNT_EXACT_TOLERANCE:
        return 100
    # graceful falloff, fully wrong past ~5% gap
    pct = diff / max(a1, a2)
    return max(0, round(100 - pct * 1500))


# ---------- Data loading ----------

def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_all(base="."):
    payments = load_csv(f"{base}/payments.csv")
    bank = load_csv(f"{base}/bank_transactions.csv")
    ledger = load_csv(f"{base}/ledger_entries.csv")
    fees = load_csv(f"{base}/fees.csv")
    for p in payments:
        p["amount"] = float(p["amount"])
    for b in bank:
        b["amount"] = float(b["amount"])
    for l in ledger:
        l["amount"] = float(l["amount"])
    for fe in fees:
        fe["amount"] = float(fe["amount"])
    return payments, bank, ledger, fees


# ---------- Candidate generation ----------

def find_bank_candidates(payment, bank_rows):
    """Score every bank row against this payment; return sorted candidates."""
    candidates = []
    for b in bank_rows:
        a_score = amount_score(payment["amount"], b["amount"])
        d_score = date_score(payment["date"], b["date"])
        n_score = name_similarity(payment["merchant"], b["description"]) * 100
        # weighted composite - amount matters most for financial safety
        composite = round(a_score * 0.5 + d_score * 0.2 + n_score * 0.3)
        if composite >= 40:  # don't even consider wildly irrelevant rows
            candidates.append({
                "bank_id": b["bank_id"],
                "amount": b["amount"],
                "date": b["date"],
                "description": b["description"],
                "amount_score": a_score,
                "date_score": d_score,
                "name_score": round(n_score),
                "composite": composite,
            })
    candidates.sort(key=lambda c: -c["composite"])
    return candidates


def find_ledger_match(payment, ledger_rows):
    best, best_score = None, 0
    for l in ledger_rows:
        a_score = amount_score(payment["amount"], l["amount"])
        d_score = date_score(payment["date"], l["date"])
        n_score = name_similarity(payment["merchant"], l["entity"]) * 100
        composite = round(a_score * 0.5 + d_score * 0.2 + n_score * 0.3)
        if composite > best_score:
            best_score = composite
            best = {**l, "composite": composite}
    return best if best_score >= 60 else None


# ---------- Investigator (rule-based evidence gathering) ----------

def investigate(payment, candidates, fees, audit):
    """
    Called when a payment can't be safely auto-reconciled by score alone.
    Searches fee records to see if an amount gap is explainable, and checks
    whether multiple candidates are genuinely ambiguous.

    Returns a structured decision dict - never silently forces a match.
    """
    audit.log(payment["payment_id"], "Investigator activated")

    if not candidates:
        audit.log(payment["payment_id"], "No candidate bank transactions found")
        return {
            "decision": "EXCEPTION",
            "confidence": 0,
            "evidence": [],
            "contradictions": ["No matching bank transaction exists"],
            "reasoning_summary": "Payment was booked but no corresponding bank "
                                  "settlement was found. Likely pending or failed.",
            "recommended_action": "Check payment gateway status for this transaction.",
        }

    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None

    # Ambiguity check: two candidates close enough in score that picking
    # one over the other would be a guess, not a decision.
    if runner_up and abs(top["composite"] - runner_up["composite"]) <= 5:
        audit.log(payment["payment_id"],
                   f"Ambiguous: {top['bank_id']} and {runner_up['bank_id']} "
                   f"score within 5 points of each other")
        return {
            "decision": "EXCEPTION",
            "confidence": top["composite"],
            "evidence": [f"{top['bank_id']} scored {top['composite']}",
                         f"{runner_up['bank_id']} scored {runner_up['composite']}"],
            "contradictions": ["Multiple equally plausible bank transactions - "
                                "cannot safely pick one without more evidence"],
            "reasoning_summary": "Two or more bank transactions are equally strong "
                                  "candidates. Auto-selecting one risks reconciling "
                                  "against the wrong transaction.",
            "recommended_action": f"Manually confirm whether {top['bank_id']} or "
                                   f"{runner_up['bank_id']} is the correct match.",
        }

    # Amount gap check: does a fee record explain the difference?
    gap = round(payment["amount"] - top["amount"], 2)
    if abs(gap) > AMOUNT_EXACT_TOLERANCE:
        audit.log(payment["payment_id"], f"Amount gap of {gap} detected, searching fee records")
        matching_fee = None
        for fe in fees:
            if abs(fe["amount"] - gap) <= FEE_MATCH_TOLERANCE:
                # also require date proximity to the payment as corroborating evidence
                if abs((parse_date(fe["date"]) - parse_date(payment["date"])).days) <= DATE_WINDOW_DAYS:
                    matching_fee = fe
                    break

        if matching_fee:
            audit.log(payment["payment_id"], f"Fee record {matching_fee['fee_id']} found, explains gap")
            return {
                "decision": "RECONCILED",
                "confidence": 85,
                "evidence": [
                    f"Amount gap of ₹{gap} matches fee record {matching_fee['fee_id']} "
                    f"(₹{matching_fee['amount']})",
                    f"Fee record date ({matching_fee['date']}) is within "
                    f"{DATE_WINDOW_DAYS} days of the payment",
                    f"Merchant name matches bank description "
                    f"({top['name_score']}% similarity)",
                ],
                "contradictions": [],
                "reasoning_summary": f"The ₹{gap} gap between payment and settlement "
                                      f"is fully explained by fee record {matching_fee['fee_id']}.",
                "recommended_action": None,
                "matched_bank_id": top["bank_id"],
                "matched_fee_id": matching_fee["fee_id"],
            }
        else:
            audit.log(payment["payment_id"], "No fee record explains the gap - escalating")
            return {
                "decision": "EXCEPTION",
                "confidence": top["composite"],
                "evidence": [f"Bank transaction {top['bank_id']} otherwise matches "
                             f"on name ({top['name_score']}%) and date"],
                "contradictions": [f"Amount differs by ₹{gap} with no fee record "
                                    f"to explain it"],
                "reasoning_summary": "An unexplained amount discrepancy exists. "
                                      "The system will not assume this is a fee "
                                      "without a corroborating fee record.",
                "recommended_action": f"Manually verify the ₹{gap} difference on {top['bank_id']}.",
            }

    # If we got here via investigation but nothing was actually wrong
    # (e.g. borderline composite score), reconcile with a lower confidence label.
    audit.log(payment["payment_id"], "Composite score borderline but evidence is consistent")
    return {
        "decision": "RECONCILED",
        "confidence": top["composite"],
        "evidence": [f"Amount matches exactly", f"Name similarity {top['name_score']}%",
                     f"Date within window"],
        "contradictions": [],
        "reasoning_summary": "Evidence is consistent across amount, name and date; "
                              "score was borderline only due to minor name formatting.",
        "recommended_action": None,
        "matched_bank_id": top["bank_id"],
    }
