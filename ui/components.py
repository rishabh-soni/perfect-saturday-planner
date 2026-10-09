"""Presentation only. Dynamic HTML is escaped, and every value comes from results."""
import html
from datetime import date, datetime

import streamlit as st

from ui.controller import budget_categories, event_label, maps_url


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def money(value) -> str:
    if value is None:
        return "Unknown"
    return f"₹{value:,.0f}" if float(value).is_integer() else f"₹{value:,.2f}"


def duration(value) -> str:
    if value is None:
        return "Unknown duration"
    minutes = int(round(value))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"


def time_label(value):
    if not value:
        return "Time unknown"
    try:
        return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return value


def price_label(price):
    if not price or price.get("minimum") is None or price.get("maximum") is None:
        return "Price unknown"
    bounds = money(price["maximum"]) if price["minimum"] == price["maximum"] else f"{money(price['minimum'])}–{money(price['maximum'])}"
    return f"{bounds} · {price.get('confidence', 'unknown')}"


def hero(saturday: date):
    st.markdown(f'''<div class="nav"><div class="wordmark"><span class="sun-mark">☼</span>Saturday, Sorted<span style="color:#b78d60">.</span></div>
    <div class="nav-note">A DAY THAT FEELS LIKE YOU</div><div class="weekend">SAT · {saturday.strftime('%d %b').upper()}</div></div>
    <div class="hero"><div><div class="eyebrow">LESS PLANNING. MORE POSSIBILITY.</div>
    <h1>Make room for a<br><em>better Saturday.</em></h1><p>Your perfect Saturday, planned in seconds.</p></div>
    <div class="hero-art" aria-hidden="true"><div class="sun"></div><div class="hill back"></div><div class="hill"></div></div></div>''', unsafe_allow_html=True)


def empty_state():
    st.markdown('''<div class="empty"><div class="eyebrow">YOUR DAY, YOUR WAY</div><div class="orbit" aria-hidden="true">☼</div>
    <h2>A little less planning.<br>A little more living.</h2><p>A slow coffee, a new corner of the city, or something a little unexpected. Start with what feels good to you.</p>
    <div class="steps"><div class="step"><div class="step-num">01</div><div><strong>Tell us your kind of Saturday</strong><span>Your mood, interests, and a few practical details.</span></div></div>
    <div class="step"><div class="step-num">02</div><div><strong>We connect the dots</strong><span>Places, journeys, and a budget that makes sense.</span></div></div>
    <div class="step"><div class="step-num">03</div><div><strong>Make the day yours</strong><span>A personal itinerary, with the details to check.</span></div></div></div></div>''', unsafe_allow_html=True)


def route_segment(route: dict):
    mode = "Walk" if route.get("travel_mode") == "walking" else "Drive"
    distance = route.get("distance_meters")
    distance_text = "Distance unknown" if distance is None else (f"{distance / 1000:.1f} km" if distance >= 1000 else f"{distance:.0f} m")
    label = "Illustrative demo estimate" if route.get("source") == "mock" else "Provider estimate" if route.get("status") == "available" else "Route unavailable"
    st.markdown(f'<div class="route">{esc(mode)} &nbsp; · &nbsp; {esc(duration(route.get("duration_minutes")))} &nbsp; · &nbsp; {esc(distance_text)}<br>{esc(label)}</div>', unsafe_allow_html=True)


def activity_card(stop: dict, number: int):
    venue = stop.get("venue") or {}
    is_mock = venue.get("source") == "mock"
    label = "Demo venue · check details" if is_mock else "Place found · check hours" if venue else "Venue unverified"
    url = maps_url(venue)
    link = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">Find on Maps ↗</a>' if url else ""
    rating = f'<span>Rating {esc(venue["rating"])} / 5</span>' if venue.get("rating") is not None else ""
    st.markdown(f'''<article class="activity"><div class="activity-top"><div class="time">{number:02d} &nbsp; {esc(time_label(stop.get('start_time')))} — {esc(time_label(stop.get('end_time')))}</div>
    <span class="tag uncertain">{esc(label)}</span></div><h3>{esc(venue.get('name') or 'Venue information unavailable')}</h3>
    <div class="address">{esc(venue.get('address') or 'Address unknown')}</div><div class="description">{esc(stop.get('activity', ''))}</div>
    <div class="why"><b>WHY THIS FITS YOU</b>{esc(stop.get('rationale', 'Rationale unavailable'))}</div>
    <div class="activity-bottom"><span>{esc(duration(stop.get('duration_minutes')))}</span><span>{esc(price_label(stop.get('cost')))}</span>{rating}{link}</div></article>''', unsafe_allow_html=True)
    with st.expander(f"Details & things to check · stop {number}"):
        st.caption(f"Allow {stop.get('buffer_minutes', 0)} minutes to arrive and settle in before this activity.")
        if stop.get("cost"):
            st.text(stop["cost"].get("basis", "Price basis unavailable"))
        hours = venue.get("opening_hours")
        descriptions = hours.get("weekdayDescriptions", []) if hours else []
        if descriptions:
            st.caption("Regular opening hours from the venue provider; confirm Saturday exceptions.")
            for line in descriptions:
                st.text(line)
        else:
            st.warning("Opening hours are unknown. Confirm before you go.")
        if venue.get("evidence", {}).get("vegetarian") is True:
            st.caption("Vegetarian options are indicated by " + ("the demo fixture; confirm the current menu." if is_mock else "the place provider; confirm the current menu."))
        for note in venue.get("notes", []):
            st.text(note)
        if is_mock and url:
            st.caption("Maps uses the fixture's name and address; the demo's approximate coordinates are not treated as verified.")


def spending(itinerary: dict, validation: dict, preferences: dict | None = None):
    with st.expander("A little budget clarity", expanded=True):
        costs = validation.get("cost", {})
        budget = (preferences or {}).get("budget")
        st.caption(f"Your budget: {money(budget)} · INR · per person")
        items = costs.get("items", [])
        if not items:
            items = [{"label": s.get("activity", "Activity"), "cost": s.get("cost")} for s in itinerary.get("stops", [])]
            items.append({"label": "Transport", "cost": itinerary.get("transport_cost")})
        rows = []
        for item in items:
            price = item.get("cost") or {}
            rows.append({"Item": item.get("label", "Activity"), "Cost range (INR)": price_label(price),
                         "Basis": price.get("basis", "Price unavailable")})
        st.table(rows)
        known = costs.get("known_subtotal_range", {})
        unknown = costs.get("unknown_cost_items", [])
        if unknown:
            st.warning("Complete total and budget left are unknown. Confirm these prices: " + ", ".join(unknown))
            st.text(f"Known costs only: {money(known.get('minimum'))}–{money(known.get('maximum'))}. This excludes unknown items.")
        else:
            st.text(f"Estimated total range: {money(known.get('minimum'))}–{money(known.get('maximum'))}")
            st.text(f"Budget left after the upper estimate: {money(costs.get('remaining_budget'))}")
        st.caption("Estimates are planning allowances, not confirmed menu prices or fares. Travel from home and return travel are excluded unless explicitly included in the plan.")


def trace_section(result):
    with st.expander("How your Saturday was planned"):
        st.caption("Actual backend actions. A check requiring confirmation is not a full validation pass.")
        for event in result.get("trace", []):
            outcome = "Completed" if event["success"] else "Needs attention"
            st.text(f"{event_label(event['name'])} · {outcome} · {event['duration_ms']:.1f} ms")
            st.caption(event["summary"])
            st.caption(f"{event['name']} · model iteration {event['iteration']}")
        if not result.get("trace"):
            st.caption("No backend tool actions were recorded for this request.")
        if st.checkbox("Show sanitized tool arguments and model diagnostics", key="trace_arguments"):
            st.json(result.get("trace", []))


def result_view(result):
    itinerary, validation = result.get("itinerary"), result.get("validation") or {}
    if result["status"] in {"failure", "infeasible"}:
        st.error(result["message"])
        for error in validation.get("errors", []):
            st.text(error)
        st.info("Try fewer stops, more time, or a different budget. Keep dietary and accessibility requirements; relax only optional preferences.")
        if itinerary:
            with st.expander("Unapproved draft · do not follow"):
                st.warning("This draft did not pass the backend's feasibility checks.")
                st.json(itinerary)
        trace_section(result)
        return
    if not itinerary:
        st.warning("No itinerary was returned. Try again or choose the demo.")
        trace_section(result)
        return
    prefs = result.get("preferences") or {}
    st.markdown(f'<div class="eyebrow">YOUR SATURDAY IN {esc(prefs.get("city", "THE CITY"))}</div><h2 class="result-title">{esc(itinerary.get("title", "Your Saturday"))}</h2><p class="result-summary">{esc(result["message"])}</p>', unsafe_allow_html=True)
    if result["mode"] == "offline_demo":
        st.info("Demo plan · illustrative Bengaluru places, prices and travel. No live AI or external API calls were made.")
    if result["status"] == "conditional":
        st.warning("A promising plan, with details to confirm. Check hours, prices and any unverified constraints before heading out.")
    else:
        st.success("The plan passed the available time, budget and constraint checks. Estimates can still change.")
    costs = validation.get("cost", {})
    values = [(duration(validation.get("total_minutes")), "PLANNED TIME"),
              (money(costs.get("total_estimated_cost")), "UPPER ESTIMATE"),
              (money(costs.get("remaining_budget")), "BUDGET LEFT"),
              (str(len(itinerary.get("stops", []))), "STOPS")]
    st.markdown('<div class="stats">' + ''.join(f'<div class="stat"><b>{esc(v)}</b><span>{esc(label)}</span></div>' for v, label in values) + '</div>', unsafe_allow_html=True)
    st.caption(f"{prefs.get('saturday', '')} · {prefs.get('mood', '')} · Saved for your submitted preferences")
    routes = {(r["origin_id"], r["destination_id"]): r for r in itinerary.get("routes", [])}
    previous = itinerary.get("starting_place_id")
    for number, stop in enumerate(itinerary.get("stops", []), 1):
        if previous and previous != stop["place_id"]:
            route = routes.get((previous, stop["place_id"]))
            if route:
                route_segment(route)
            else:
                st.warning("Travel details unavailable for this segment.")
        activity_card(stop, number)
        previous = stop["place_id"]
    spending(itinerary, validation, prefs)
    warnings = list(dict.fromkeys(itinerary.get("warnings", []) + validation.get("warnings", [])))
    if warnings:
        with st.expander(f"Before you go · {len(warnings)} things to know"):
            for warning in warnings:
                st.text(warning)
    if itinerary.get("trade_offs"):
        with st.expander("The choices behind your day"):
            for trade_off in itinerary["trade_offs"]:
                st.text(trade_off)
    trace_section(result)
