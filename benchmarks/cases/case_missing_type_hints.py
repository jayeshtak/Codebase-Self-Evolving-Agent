# issue_type: missing_type_hints
# severity: low
# description: Public functions missing PEP 484 type annotations on arguments and return type
# validate: '->' in fixed_code and fixed_code.count('->') >= 2

def calculate_discount(price, discount_percent):
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("Discount must be between 0 and 100")
    return price * (1 - discount_percent / 100)


def format_currency(amount, currency_code, decimals):
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency_code, currency_code)
    return f"{symbol}{amount:.{decimals}f}"


def is_valid_email(email):
    return "@" in email and "." in email.split("@")[-1]
