"""Saturday, Sorted — Streamlit UI for the existing Phase 1 agent."""
import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from pydantic import ValidationError

from agent.planner import sanitize
from agent.schemas import next_saturday
from ui.components import empty_state, hero, result_view, duration, money
from ui.controller import (CONSTRAINTS, DEFAULTS, INTERESTS, MOODS, PRESETS, availability,
                           build_preferences, event_label, generate, public_result, selected_result)
from ui.styles import CSS


ROOT = Path(__file__).resolve().parent


def configure():
    load_dotenv(ROOT / ".env")
    try:
        secrets = st.secrets.to_dict()
    except FileNotFoundError:
        secrets = {}
    for name in ("OPENAI_API_KEY", "GOOGLE_MAPS_API_KEY", "OPENAI_MODEL", "OPENAI_DIAGNOSTICS", "AGENT_MAX_ITERATIONS", "SATURDAY_DEMO_ONLY", "OSM_USER_AGENT", "NOMINATIM_URL", "OVERPASS_URL", "OPEN_METEO_URL", "OSM_SEARCH_RADIUS_METERS", "PROVIDER_CACHE_PATH", "ROUTING_FALLBACK", "APP_PUBLIC_URL", "MAP_TILE_URL", "MAP_TILE_ATTRIBUTION"):
        if not os.getenv(name) and secrets.get(name):
            os.environ[name] = str(secrets[name])


def apply_preset():
    for name, value in PRESETS[st.session_state.preset].items():
        st.session_state[name] = value.copy() if isinstance(value, list) else value


def run():
    st.set_page_config(page_title="Saturday, Sorted", page_icon="☼", layout="wide", initial_sidebar_state="collapsed")
    configure()
    st.markdown(CSS, unsafe_allow_html=True)
    for name, value in DEFAULTS.items():
        if name not in st.session_state:
            st.session_state[name] = value.copy() if isinstance(value, list) else value
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("last_preferences", None)
    st.session_state.setdefault("last_demo", False)
    hero(next_saturday())
    left, right = st.columns([.93, 1.3], gap="large")
    submitted = False
    with left:
        with st.container(border=False, key="preferences_panel"):
            st.markdown('<div class="panel-title">What sounds like a good day?</div><p class="panel-note">A few details. A day that feels like you.</p>', unsafe_allow_html=True)
            st.selectbox("Start with an idea", list(PRESETS), key="preset", on_change=apply_preset)
            settings = availability()
            forced_demo = settings["demo_only"] or not settings["openai"]
            demo = st.toggle("Try the Bengaluru demo", value=forced_demo, disabled=forced_demo, key="demo")
            if settings["demo_only"]:
                st.caption("You're exploring the demo. Places, prices and travel are illustrative.")
            elif forced_demo:
                st.caption("Demo data is available now. Live recommendations need a server-side OpenAI key.")
            elif demo:
                st.caption("Illustrative places and estimates. No live AI or API calls.")
            elif not settings["google"]:
                st.caption("AI planning with OpenStreetMap places and weather; short-distance routing uses labeled estimates when Google Routes is unavailable.")
            else:
                st.caption("AI planning with OpenStreetMap places, Open-Meteo weather and Google Routes.")
            with st.form("saturday_preferences", border=False):
                st.markdown('<div class="section-label">THE PRACTICAL BITS</div>', unsafe_allow_html=True)
                st.text_input("City", key="city", max_chars=120, placeholder="Bangalore")
                st.text_input("Starting neighborhood (optional)", key="neighborhood", max_chars=120, placeholder="e.g. Indiranagar")
                budget_col, duration_col = st.columns(2)
                with budget_col:
                    st.number_input("Budget · INR", min_value=0, max_value=100000, step=250, key="budget")
                with duration_col:
                    st.selectbox("Time available", [2, 3, 4, 6, 8], format_func=lambda h: f"{h} hours", key="hours")
                time_col, mode_col = st.columns(2)
                with time_col:
                    st.time_input("Start time", key="start", step=900)
                with mode_col:
                    st.selectbox("Getting around", ["Walking", "Driving"], key="travel_mode")
                st.markdown('<div class="section-label">MAKE IT YOURS</div>', unsafe_allow_html=True)
                st.text_input("What's your mood?", key="mood", max_chars=500, help="Try " + ", ".join(MOODS))
                st.multiselect("What are you into?", INTERESTS, key="interests")
                st.multiselect("Anything to keep in mind?", CONSTRAINTS, key="constraints")
                st.text_input("Other requirements (optional)", key="extra_constraints", max_chars=500, placeholder="Separate requirements with commas")
                submitted = st.form_submit_button("Plan My Saturday  ↗", type="primary", use_container_width=True)
            st.caption("Your budget and time are checked. Uncertain details are clearly labeled.")
    with right:
        regenerate = False
        shown_result = st.session_state.result
        if shown_result is not None:
            options = shown_result.get("alternatives", [])
            if len(options) > 1:
                st.markdown("### Choose your Saturday")
                st.caption("Each option was checked against your submitted time, budget and constraints. Confirmation warnings still apply.")
                ids = [o["id"] for o in options]
                if st.session_state.get("selected_option") not in ids:
                    st.session_state.selected_option = ids[0]
                by_id = {o["id"]: o for o in options}
                with st.expander("Compare what each option includes"):
                    for option in options:
                        st.markdown(f"**{option['label']}**")
                        st.text(option["message"])
                        for trade_off in option["itinerary"].get("trade_offs", []):
                            st.caption(trade_off)
                chosen = st.radio("Pick an option", ids, key="selected_option", format_func=lambda i:
                    f"{by_id[i]['label']} · {duration(by_id[i]['validation']['total_minutes'])} · upper {money(by_id[i]['validation']['cost'].get('total_estimated_cost'))}")
                shown_result = selected_result(shown_result, chosen)
            elif options:
                st.caption("One option passed the available checks; more alternatives could not be confirmed within this search.")
            toolbar_left, toolbar_right = st.columns(2)
            with toolbar_left:
                regenerate = st.button("Plan again", key="regenerate", use_container_width=True,
                                       help="Reuses your last submitted preferences. Submit the form to apply edits.")
            with toolbar_right:
                st.download_button("Save itinerary · JSON", data=json.dumps(shown_result, indent=2, ensure_ascii=False),
                                   file_name="saturday-sorted.json", mime="application/json", use_container_width=True)
        if submitted or regenerate:
            preferences = None
            try:
                preferences = build_preferences(**{name: st.session_state[name] for name in DEFAULTS}) if submitted else build_preferences(**st.session_state.last_preferences)
            except (ValidationError, ValueError):
                st.error("Check your details: enter a city and mood, choose at least one interest, and make sure the time window finishes before midnight.")
                if st.session_state.result:
                    st.caption("Your previous plan is still shown below; this request was not generated.")
            if preferences:
                if submitted:
                    st.session_state.last_preferences = {name: st.session_state[name] for name in DEFAULTS}
                    st.session_state.last_demo = demo
                status = st.status("Finding your kind of Saturday…", expanded=True)
                def on_event(event):
                    status.text(f"{event_label(event.name)} · {sanitize(event.summary)}")
                    status.update(label=event_label(event.name) + "…")
                result = generate(preferences, demo=st.session_state.last_demo, on_event=on_event)
                st.session_state.result = public_result(result)
                st.session_state.pop("selected_option", None)
                status.update(label="Your plan is ready to explore" if result.status in {"success", "conditional"} else "This request needs a little adjustment",
                              state="complete" if result.status in {"success", "conditional"} else "error", expanded=False)
                # Refreshes controls (including download) with the just-generated
                # result; persistent real trace remains available after rerender.
                st.rerun()
        if st.session_state.result is not None:
            result_view(shown_result)
        else:
            empty_state()
    st.markdown('<div class="footer"><span>Saturday, Sorted · A little intention goes a long way.</span><span>Built around your time, your budget, your mood.</span></div>', unsafe_allow_html=True)


if __name__ == "__main__":
    run()
