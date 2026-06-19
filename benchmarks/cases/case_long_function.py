# BENCHMARK CASE: long_function
# issue_type: long_function
# severity: medium
# description: Function 'process_order' is 70 lines — exceeds 60-line limit
# expect_fixed: function split into smaller helpers
# validate: len([l for l in fixed_code.splitlines() if l.strip().startswith("def ")]) > 1

def process_order(order, user, inventory, pricing, discount_code, shipping_zone, tax_rate, notify):
    # Validate order
    if not order:
        return {"error": "empty order"}
    if not order.get("items"):
        return {"error": "no items"}
    if not user:
        return {"error": "no user"}
    if not user.get("id"):
        return {"error": "invalid user"}

    # Check inventory
    total_weight = 0
    items_cost = 0
    for item in order["items"]:
        sku = item.get("sku")
        qty = item.get("qty", 0)
        if sku not in inventory:
            return {"error": f"sku {sku} not found"}
        if inventory[sku]["stock"] < qty:
            return {"error": f"insufficient stock for {sku}"}
        price = pricing.get(sku, inventory[sku].get("price", 0))
        items_cost += price * qty
        total_weight += inventory[sku].get("weight", 0) * qty

    # Apply discount
    discount = 0
    if discount_code:
        code_data = pricing.get("discounts", {}).get(discount_code)
        if code_data:
            if code_data["type"] == "percent":
                discount = items_cost * code_data["value"] / 100
            elif code_data["type"] == "fixed":
                discount = code_data["value"]
        else:
            return {"error": "invalid discount code"}

    subtotal = items_cost - discount

    # Calculate shipping
    if shipping_zone == "local":
        shipping = 0 if subtotal > 50 else 5.99
    elif shipping_zone == "national":
        shipping = max(9.99, total_weight * 0.5)
    elif shipping_zone == "international":
        shipping = max(29.99, total_weight * 2.5)
    else:
        return {"error": "unknown shipping zone"}

    # Calculate tax
    taxable = subtotal + shipping
    tax = taxable * tax_rate

    total = subtotal + shipping + tax

    # Build receipt
    receipt = {
        "user_id": user["id"],
        "items": order["items"],
        "items_cost": round(items_cost, 2),
        "discount": round(discount, 2),
        "subtotal": round(subtotal, 2),
        "shipping": round(shipping, 2),
        "tax": round(tax, 2),
        "total": round(total, 2),
    }

    # Notify
    if notify and user.get("email"):
        receipt["notification_sent"] = True

    return receipt
