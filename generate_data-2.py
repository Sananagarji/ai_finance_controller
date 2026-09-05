"""
Synthetic dataset generator for the AI Finance Controller.

Generates four related record sets that mirror what a real merchant has:
  - payments.csv          (what was charged)
  - bank_transactions.csv (what the bank actually settled)
  - ledger_entries.csv    (what was booked internally)
  - fees.csv              (processing fee records, sometimes linked to a payment)

Ground truth (which payment maps to which bank txn / ledger entry / fee,
and whether it SHOULD be auto-reconciled or SHOULD be escalated) is stored
separately in ground_truth.csv so we can objectively score the engine
afterwards. The engine itself never reads ground_truth.csv.

Deterministic given SEED, so results are reproducible for the pitch video.
"""

import csv
import random
from datetime import datetime, timedelta

SEED = 7
random.seed(SEED)

NUM_PAYMENTS = 70
START_DATE = datetime(2026, 8, 1)

MERCHANT_BASE_NAMES = [
    "Acme", "Bluepeak", "Nimbus Retail", "Cedar & Co", "Vertex Traders",
    "Orbit Foods", "Riverline", "Sunfield Textiles", "Northstar Logistics",
    "Clearwater Media"
]

def name_variant(base):
    """Return a randomly-styled variant of a merchant name, like real data."""
    forms = [
        f"{base} Pvt Ltd",
        f"{base.upper()} PRIVATE LIMITED",
        f"{base} Private Ltd.",
        f"UPI/{base.upper().replace(' ', '')}/{random.randint(10000,99999)}",
        f"{base} Pvt. Ltd",
    ]
    return random.choice(forms)

payments, bank_rows, ledger_rows, fee_rows, ground_truth = [], [], [], [], []

for i in range(1, NUM_PAYMENTS + 1):
    pay_id = f"PAY-{i:03d}"
    base_name = random.choice(MERCHANT_BASE_NAMES)
    amount = round(random.uniform(1000, 50000), 2)
    pay_date = START_DATE + timedelta(days=random.randint(0, 30))

    payments.append({
        "payment_id": pay_id,
        "merchant": name_variant(base_name),
        "amount": amount,
        "date": pay_date.strftime("%Y-%m-%d"),
    })

    case = random.random()
    expected_bank = ""
    expected_ledger = ""
    expected_fee = ""
    should_auto_reconcile = False

    # --- Ledger entry: usually present, name/date can drift slightly ---
    if case < 0.95:
        ledger_id = f"LDG-{i:03d}"
        ledger_rows.append({
            "ledger_id": ledger_id,
            "entity": name_variant(base_name),
            "amount": amount,
            "date": (pay_date + timedelta(days=random.choice([0, 0, 1]))).strftime("%Y-%m-%d"),
        })
        expected_ledger = ledger_id
    # else: 5% missing ledger entry entirely (untracked)

    # --- Bank transaction: several distinct difficulty cases ---
    if case < 0.55:
        # A. Clean exact match
        bank_id = f"BNK-{i:03d}"
        bank_rows.append({
            "bank_id": bank_id,
            "description": name_variant(base_name),
            "amount": amount,
            "date": (pay_date + timedelta(days=random.choice([0, 1]))).strftime("%Y-%m-%d"),
        })
        expected_bank = bank_id
        should_auto_reconcile = True

    elif case < 0.70:
        # E. Fee deducted - settled amount is lower, fee record exists and IS linked
        fee = round(amount * random.uniform(0.015, 0.025), 2)
        bank_id = f"BNK-{i:03d}"
        bank_rows.append({
            "bank_id": bank_id,
            "description": name_variant(base_name),
            "amount": round(amount - fee, 2),
            "date": (pay_date + timedelta(days=random.choice([0, 1]))).strftime("%Y-%m-%d"),
        })
        fee_id = f"FEE-{i:03d}"
        fee_rows.append({
            "fee_id": fee_id,
            "related_payment_id": pay_id,   # ground-truth link, engine must discover this via matching, not by reading this field blindly
            "amount": fee,
            "date": (pay_date + timedelta(days=random.choice([0, 1]))).strftime("%Y-%m-%d"),
        })
        expected_bank = bank_id
        expected_fee = fee_id
        should_auto_reconcile = True  # explainable via fee evidence

    elif case < 0.80:
        # H. Missing bank transaction entirely (payment stuck/pending)
        should_auto_reconcile = False

    elif case < 0.88:
        # D. Amount mismatch with NO fee record to explain it (should escalate)
        bad_amount = round(amount - random.uniform(300, 2000), 2)
        bank_id = f"BNK-{i:03d}"
        bank_rows.append({
            "bank_id": bank_id,
            "description": name_variant(base_name),
            "amount": bad_amount,
            "date": (pay_date + timedelta(days=random.choice([0, 1]))).strftime("%Y-%m-%d"),
        })
        expected_bank = bank_id
        should_auto_reconcile = False  # genuine discrepancy, no evidence to explain it

    elif case < 0.94:
        # K. Multiple plausible candidates (same amount, similar date, different txns)
        bank_id_1 = f"BNK-{i:03d}a"
        bank_id_2 = f"BNK-{i:03d}b"
        for bid in (bank_id_1, bank_id_2):
            bank_rows.append({
                "bank_id": bid,
                "description": name_variant(base_name),
                "amount": amount,
                "date": pay_date.strftime("%Y-%m-%d"),
            })
        expected_bank = bank_id_1  # true match is ambiguous by design
        should_auto_reconcile = False  # correct behavior: escalate, don't guess

    else:
        # J. Duplicate settlement (double-processed by the bank)
        bank_id = f"BNK-{i:03d}"
        bank_rows.append({
            "bank_id": bank_id,
            "description": name_variant(base_name),
            "amount": amount,
            "date": pay_date.strftime("%Y-%m-%d"),
        })
        bank_rows.append({
            "bank_id": f"BNK-{i:03d}-dup",
            "description": name_variant(base_name),
            "amount": amount,
            "date": pay_date.strftime("%Y-%m-%d"),
        })
        expected_bank = bank_id
        should_auto_reconcile = False  # duplicate needs a human to confirm which is real

    ground_truth.append({
        "payment_id": pay_id,
        "expected_bank_id": expected_bank,
        "expected_ledger_id": expected_ledger,
        "expected_fee_id": expected_fee,
        "should_auto_reconcile": should_auto_reconcile,
    })

random.shuffle(bank_rows)
random.shuffle(ledger_rows)
random.shuffle(fee_rows)


def write_csv(filename, rows, fieldnames):
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


write_csv("payments.csv", payments, ["payment_id", "merchant", "amount", "date"])
write_csv("bank_transactions.csv", bank_rows, ["bank_id", "description", "amount", "date"])
write_csv("ledger_entries.csv", ledger_rows, ["ledger_id", "entity", "amount", "date"])
write_csv("fees.csv", fee_rows, ["fee_id", "related_payment_id", "amount", "date"])
write_csv("ground_truth.csv", ground_truth,
          ["payment_id", "expected_bank_id", "expected_ledger_id", "expected_fee_id", "should_auto_reconcile"])

print(f"Generated {len(payments)} payments, {len(bank_rows)} bank txns, "
      f"{len(ledger_rows)} ledger entries, {len(fee_rows)} fee records.")
print("Ground truth written to ground_truth.csv (engine does not read this file).")
