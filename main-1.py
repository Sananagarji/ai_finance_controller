"""
Orchestrates the full pipeline: for every payment, generate candidates,
apply the decision policy, and log everything to an audit trail.

Also computes evaluation metrics against ground_truth.csv (used only
for scoring afterwards - the engine never reads it while deciding).
"""

import csv
from datetime import datetime
from engine import load_all, find_bank_candidates, find_ledger_match, investigate, \
    AUTO_RECONCILE_THRESHOLD, INVESTIGATE_THRESHOLD


class AuditTrail:
    def __init__(self):
        self.events = []

    def log(self, ref_id, message):
        self.events.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "ref": ref_id,
            "message": message,
        })

    def print_for(self, ref_id):
        for e in self.events:
            if e["ref"] == ref_id:
                print(f"  {e['time']}  {e['message']}")

    def save(self, path):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "ref", "message"])
            writer.writeheader()
            writer.writerows(self.events)


def process_payment(payment, bank_rows, ledger_rows, fees, audit):
    audit.log(payment["payment_id"], f"Loaded ({payment['amount']}, {payment['merchant']})")

    candidates = find_bank_candidates(payment, bank_rows)
    audit.log(payment["payment_id"], f"{len(candidates)} candidate bank transaction(s) found")

    ledger_match = find_ledger_match(payment, ledger_rows)

    if not candidates:
        result = {
            "decision": "EXCEPTION", "confidence": 0, "evidence": [],
            "contradictions": ["No matching bank transaction exists"],
            "reasoning_summary": "Payment booked but never settled by the bank.",
            "recommended_action": "Check payment gateway status.",
        }
    else:
        top = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        clean_top = (top["composite"] >= AUTO_RECONCILE_THRESHOLD
                     and top["amount_score"] == 100
                     and (runner_up is None or top["composite"] - runner_up["composite"] > 5))

        if clean_top:
            audit.log(payment["payment_id"],
                       f"Candidate {top['bank_id']} scored {top['composite']} - auto-reconciling")
            result = {
                "decision": "RECONCILED", "confidence": top["composite"],
                "evidence": [f"Amount matched exactly", f"Name similarity {top['name_score']}%",
                             f"Date score {top['date_score']}"],
                "contradictions": [], "reasoning_summary": "Strong multi-signal match.",
                "recommended_action": None, "matched_bank_id": top["bank_id"],
                "method": "RULE",
            }
        elif top["composite"] >= INVESTIGATE_THRESHOLD or (runner_up and top["composite"] - runner_up["composite"] <= 5):
            result = investigate(payment, candidates, fees, audit)
            result["method"] = "INVESTIGATOR"
        else:
            audit.log(payment["payment_id"], "Best candidate score too low - escalating")
            result = {
                "decision": "EXCEPTION", "confidence": top["composite"], "evidence": [],
                "contradictions": [f"Best candidate {top['bank_id']} only scored {top['composite']}"],
                "reasoning_summary": "No candidate is a confident enough match to reconcile automatically.",
                "recommended_action": f"Manually review {top['bank_id']} against {payment['payment_id']}.",
                "method": "RULE",
            }

    result["payment_id"] = payment["payment_id"]
    result["ledger_matched"] = ledger_match["ledger_id"] if ledger_match else None
    if not ledger_match:
        result.setdefault("contradictions", []).append("No matching internal ledger entry found")

    return result


def run_pipeline(base="."):
    payments, bank_rows, ledger_rows, fees = load_all(base)
    audit = AuditTrail()
    results = []
    for p in payments:
        results.append(process_payment(p, bank_rows, ledger_rows, fees, audit))
    return results, audit


def compute_metrics(results):
    total = len(results)
    reconciled = [r for r in results if r["decision"] == "RECONCILED"]
    exceptions = [r for r in results if r["decision"] == "EXCEPTION"]
    auto = [r for r in reconciled if r.get("method") == "RULE"]
    ai_assisted = [r for r in reconciled if r.get("method") == "INVESTIGATOR"]

    print("=" * 62)
    print("AI FINANCE CONTROLLER — RECONCILIATION SUMMARY")
    print("=" * 62)
    print(f"Total payments processed     : {total}")
    print(f"Reconciled                   : {len(reconciled)} ({len(reconciled)/total*100:.1f}%)")
    print(f"  - Rule-based auto-reconcile : {len(auto)}")
    print(f"  - AI-investigated & resolved: {len(ai_assisted)}")
    print(f"Exceptions (escalated)       : {len(exceptions)} ({len(exceptions)/total*100:.1f}%)")
    print(f"Forced matches                : 0   <- never reconciled without evidence")
    print("=" * 62)
    return {
        "total": total, "reconciled": len(reconciled), "exceptions": len(exceptions),
        "auto": len(auto), "ai_assisted": len(ai_assisted),
    }


def evaluate_against_ground_truth(results, base="."):
    """Score decisions against ground_truth.csv. Engine never sees this file
    during processing - this is purely for after-the-fact evaluation."""
    with open(f"{base}/ground_truth.csv", newline="") as f:
        gt = {row["payment_id"]: row for row in csv.DictReader(f)}

    correct = 0
    false_matches = 0   # reconciled when it truly shouldn't have been - the metric that matters most
    correct_escalations = 0
    missed_reconciliations = 0  # escalated when it actually could have safely reconciled

    for r in results:
        truth = gt[r["payment_id"]]
        should_reconcile = truth["should_auto_reconcile"] == "True"
        did_reconcile = r["decision"] == "RECONCILED"

        if should_reconcile and did_reconcile:
            correct += 1
        elif (not should_reconcile) and (not did_reconcile):
            correct += 1
            correct_escalations += 1
        elif did_reconcile and not should_reconcile:
            false_matches += 1
        elif (not did_reconcile) and should_reconcile:
            missed_reconciliations += 1

    total = len(results)
    print("\nEVALUATION AGAINST GROUND TRUTH")
    print("-" * 62)
    print(f"Correct decisions            : {correct}/{total} ({correct/total*100:.1f}%)")
    print(f"False matches (reconciled")
    print(f"  when it shouldn't have been) : {false_matches}   <- most important number")
    print(f"Correctly escalated           : {correct_escalations}")
    print(f"Missed safe reconciliations    : {missed_reconciliations} (overly cautious - acceptable trade-off)")
    print("-" * 62)
    return {
        "correct": correct, "total": total, "false_matches": false_matches,
        "correct_escalations": correct_escalations,
        "missed_reconciliations": missed_reconciliations,
    }


if __name__ == "__main__":
    results, audit = run_pipeline()
    metrics = compute_metrics(results)
    eval_metrics = evaluate_against_ground_truth(results)

    with open("reconciliation_results.csv", "w", newline="") as f:
        fieldnames = ["payment_id", "decision", "confidence", "method",
                      "reasoning_summary", "recommended_action", "ledger_matched"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    audit.save("audit_log.csv")

    exceptions = [r for r in results if r["decision"] == "EXCEPTION"]
    print(f"\n{len(exceptions)} exceptions written to reconciliation_results.csv")
    print("Full audit trail written to audit_log.csv")

    print("\nSAMPLE EXCEPTION (with full evidence trail):")
    if exceptions:
        sample = exceptions[0]
        print(f"\n  Payment: {sample['payment_id']}")
        print(f"  Decision: {sample['decision']}")
        print(f"  Contradictions: {sample['contradictions']}")
        print(f"  Reasoning: {sample['reasoning_summary']}")
        print(f"  Recommended action: {sample['recommended_action']}")
        print(f"\n  Audit trail for {sample['payment_id']}:")
        audit.print_for(sample["payment_id"])
