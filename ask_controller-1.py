"""
"Ask the Controller" - a structured Q&A interface over the reconciliation
results. Answers are computed directly from the results/audit data, never
invented - each answer cites the specific payment IDs it's based on.

This deliberately avoids sending raw data to an LLM and hoping for a
correct answer: every question type below maps to a specific, testable
calculation over reconciliation_results.csv and payments.csv.

Run: python3 ask_controller.py
Then try things like:
  why PAY-005
  rate
  unresolved above 10000
  exceptions
"""

import csv
import sys
from main import run_pipeline, compute_metrics


def load_context():
    results, audit = run_pipeline()
    with open("payments.csv", newline="") as f:
        payments = {row["payment_id"]: row for row in csv.DictReader(f)}
    results_by_id = {r["payment_id"]: r for r in results}
    return results, results_by_id, payments, audit


def answer(query, results, results_by_id, payments, audit):
    q = query.strip().lower()

    if q.startswith("why "):
        pay_id = query.strip().split()[-1].upper()
        r = results_by_id.get(pay_id)
        if not r:
            return f"No payment with ID {pay_id} found."
        lines = [
            f"{pay_id} -> {r['decision']} (confidence: {r.get('confidence', 'n/a')})",
            f"Reasoning: {r['reasoning_summary']}",
        ]
        if r.get("contradictions"):
            lines.append(f"Contradictions: {'; '.join(r['contradictions'])}")
        if r.get("recommended_action"):
            lines.append(f"Recommended action: {r['recommended_action']}")
        lines.append("\nFull audit trail:")
        return "\n".join(lines)

    if "match rate" in q or q == "rate":
        total = len(results)
        reconciled = len([r for r in results if r["decision"] == "RECONCILED"])
        return f"Match rate: {reconciled}/{total} ({reconciled/total*100:.1f}%) reconciled."

    if "unresolved above" in q:
        try:
            threshold = float(q.split("above")[-1].strip())
        except ValueError:
            return "Couldn't parse the amount. Try: unresolved above 10000"
        matches = []
        for r in results:
            if r["decision"] == "EXCEPTION":
                amt = float(payments[r["payment_id"]]["amount"])
                if amt > threshold:
                    matches.append(f"{r['payment_id']} (₹{amt:,.2f})")
        if not matches:
            return f"No unresolved payments above ₹{threshold:,.0f}."
        return f"{len(matches)} unresolved payments above ₹{threshold:,.0f}: " + ", ".join(matches)

    if "unreconciled" in q and ("how much" in q or "total" in q):
        total_amt = sum(float(payments[r["payment_id"]]["amount"])
                         for r in results if r["decision"] == "EXCEPTION")
        return f"Total unreconciled amount: ₹{total_amt:,.2f}"

    if q == "exceptions" or "list exceptions" in q:
        exc = [r["payment_id"] for r in results if r["decision"] == "EXCEPTION"]
        return f"{len(exc)} exceptions: " + ", ".join(exc)

    if "fee" in q and "exception" in q:
        matches = [r["payment_id"] for r in results
                   if r["decision"] == "EXCEPTION"
                   and any("fee" in c.lower() or "gap" in c.lower() for c in r.get("contradictions", []))]
        return f"{len(matches)} exceptions caused by unexplained amount gaps: " + ", ".join(matches)

    return ("I can answer: 'why <PAY-ID>', 'match rate', 'unresolved above <amount>', "
            "'how much unreconciled', 'exceptions', 'fee exceptions'.")


if __name__ == "__main__":
    print("Loading and processing dataset...")
    results, results_by_id, payments, audit = load_context()
    compute_metrics(results)

    print("\nAsk the Controller — type a question, or 'quit' to exit.")
    print("Examples: 'why PAY-005', 'match rate', 'unresolved above 10000', 'exceptions'\n")

    while True:
        try:
            q = input("> ")
        except EOFError:
            break
        if q.strip().lower() in ("quit", "exit"):
            break
        result_text = answer(q, results, results_by_id, payments, audit)
        print(result_text)
        if q.strip().lower().startswith("why "):
            audit.print_for(q.strip().split()[-1].upper())
        print()
