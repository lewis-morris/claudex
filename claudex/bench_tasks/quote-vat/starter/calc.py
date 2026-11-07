def subtotal(lines):
    """lines: list of (qty, unit_price_pence)"""
    return sum(q * p for q, p in lines)
