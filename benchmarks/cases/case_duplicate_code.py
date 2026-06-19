# issue_type: duplicate
# severity: low
# description: Duplicate validation logic repeated in multiple functions — extract into shared helper
# validate: fixed_code.count('if not isinstance') <= 1 or len([l for l in fixed_code.splitlines() if l.strip().startswith('def ')]) > 2

def process_order(order_id, amount, currency):
    if not isinstance(order_id, int) or order_id <= 0:
        raise ValueError("order_id must be a positive integer")
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValueError("amount must be positive")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ValueError("currency must be a 3-letter code")
    return {"order_id": order_id, "amount": amount, "currency": currency, "status": "pending"}


def process_refund(order_id, amount, currency):
    if not isinstance(order_id, int) or order_id <= 0:
        raise ValueError("order_id must be a positive integer")
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValueError("amount must be positive")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ValueError("currency must be a 3-letter code")
    return {"order_id": order_id, "amount": amount, "currency": currency, "status": "refunded"}
