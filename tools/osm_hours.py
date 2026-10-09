"""Conservative subset of OSM opening_hours; unsupported syntax stays unknown.

Supports weekly day lists/ranges, time ranges, overnight hours, off and 24/7.
Public-holiday, seasonal, solar, commented and other complex rules are not guessed.
"""
import re

DAY = {name: i for i, name in enumerate(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"])}


def osm_opening_check(expression, start, end):
    if expression == "24/7":
        return True
    if expression in {"off", "closed"}:
        return False
    weekly = {i: [] for i in range(7)}
    closed_days = set()
    try:
        for rule in expression.split(";"):
            match = re.fullmatch(r"\s*(?:((?:Mo|Tu|We|Th|Fr|Sa|Su)(?:\s*[-,]\s*(?:Mo|Tu|We|Th|Fr|Sa|Su))*)\s+)?(off|closed|\d{2}:\d{2}-\d{2}:\d{2}(?:\s*,\s*\d{2}:\d{2}-\d{2}:\d{2})*)\s*", rule)
            if not match:
                return None
            day_spec, time_spec = match.groups()
            selected = set(range(7)) if day_spec is None else set()
            if day_spec:
                for group in day_spec.replace(" ", "").split(","):
                    if "-" in group:
                        a, b = [DAY[d] for d in group.split("-")]
                        selected.update((a + offset) % 7 for offset in range((b-a) % 7 + 1))
                    else:
                        selected.add(DAY[group])
            if time_spec in {"off", "closed"}:
                for day in selected:
                    weekly[day] = []
                    closed_days.add(day)
                continue
            # Later weekday rules override earlier weekly ranges on those days.
            for day in selected:
                weekly[day] = []
                closed_days.discard(day)
            for interval in time_spec.split(","):
                bounds = []
                for value in interval.strip().split("-"):
                    h, m = map(int, value.split(":"))
                    if h > 24 or m > 59 or (h == 24 and m != 0):
                        return None
                    bounds.append(h*60 + m)
                a, b = bounds
                if b <= a:
                    b += 1440
                for day in selected:
                    weekly[day].append((a, b))
        # Saturday plus Friday intervals continuing after midnight.
        if 5 in closed_days:
            return False
        # A Saturday override following a Friday overnight rule can be ambiguous
        # in this deliberately small parser; do not certify it as open/closed.
        if weekly[5] and any(b > 1440 for a, b in weekly[4]):
            return None
        intervals = weekly[5] + [(a-1440, b-1440) for a, b in weekly[4] if b > 1440]
        # Adjacent ranges can jointly cover an activity.
        merged = []
        for a, b in sorted(intervals):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        return any(a <= start and end <= b for a, b in merged)
    except (AttributeError, KeyError, ValueError, TypeError):
        return None
