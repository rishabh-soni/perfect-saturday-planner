from decimal import Decimal

from agent.schemas import Price, Schema


class CostItem(Schema):
    label: str
    cost: Price


class BudgetArguments(Schema):
    activities: list[CostItem]
    transport_cost: Price


def estimate_cost(activities, transport_cost, *, budget):
    """Upper bounds are used for feasibility; unknown != free."""
    items = [CostItem.model_validate(a) for a in activities]
    items.append(CostItem(label="Transport", cost=Price.model_validate(transport_cost)))
    lower = upper = Decimal("0")
    unknown, warnings = [], []
    for item in items:
        if item.cost.confidence == "unknown":
            unknown.append(item.label)
            warnings.append(f"Cost unknown: {item.label}")
        else:
            lower += Decimal(str(item.cost.minimum))
            upper += Decimal(str(item.cost.maximum))
            if item.cost.confidence == "estimated":
                warnings.append(f"Estimated price range: {item.label}; {item.cost.basis}")
    return {"currency": "INR", "items": [item.model_dump(mode="json") for item in items],
            "known_subtotal_range": {"minimum": float(lower), "maximum": float(upper)},
            "total_estimated_cost": None if unknown else float(upper),
            "remaining_budget": None if unknown else float(Decimal(str(budget)) - upper),
            "unknown_cost_items": unknown,
            "confidence": "unknown" if unknown else ("estimated" if warnings else "verified"),
            "warnings": warnings}
