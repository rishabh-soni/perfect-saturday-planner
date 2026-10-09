"""OSM venue markers; schematic links are NOT Google route geometry."""
import html
import os
from urllib.parse import urlparse

import folium
import streamlit as st
import streamlit.components.v1 as components


OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'


def marker_text(value):
    # Folium inserts Popup/Tooltip text into JavaScript template literals.
    # HTML escaping alone does not stop ${...} interpolation or backticks.
    return html.escape(value).replace("$", "&#36;").replace("`", "&#96;").replace("\\", "&#92;")


def map_html(itinerary):
    stops = [s for s in itinerary.get("stops", []) if (s.get("venue") or {}).get("latitude") is not None
             and (s.get("venue") or {}).get("longitude") is not None]
    if not stops:
        return None
    # Google-sourced venue content is not displayed on the OSM map either.
    stops = [s for s in stops if s["venue"].get("source") in {"openstreetmap", "mock"}]
    if not stops:
        return None
    coords = [[s["venue"]["latitude"], s["venue"]["longitude"]] for s in stops]
    result = folium.Map(location=coords[0], zoom_start=14, tiles=None, control_scale=True)
    tile_url = os.getenv("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    parsed = urlparse(tile_url)
    if parsed.scheme != "https" or any(word in parsed.netloc.lower() for word in ("google", "gstatic", "googleapis")):
        raise ValueError("Choose an HTTPS OSM-compatible tile provider")
    attribution = os.getenv("MAP_TILE_ATTRIBUTION", OSM_ATTRIBUTION)
    folium.TileLayer(tiles=tile_url, attr=attribution, name="OpenStreetMap",
                     max_zoom=19, keep_buffer=0, update_when_idle=True).add_to(result)
    for number, stop in enumerate(stops, 1):
        venue = stop["venue"]
        text = f"{number}. {venue['name']} · {stop['start_time']}–{stop['end_time']}"
        if venue.get("source") == "mock":
            text += " · approximate demo location"
        folium.Marker([venue["latitude"], venue["longitude"]], tooltip=marker_text(text),
                      popup=folium.Popup(marker_text(text), max_width=260)).add_to(result)
    if len(coords) > 1:
        folium.PolyLine(coords, color="#718464", dash_array="5,8", weight=2,
                        tooltip="Schematic stop order from venue coordinates; not a road or walking route").add_to(result)
        result.fit_bounds(coords, padding=(25, 25), max_zoom=15)
    # Streamlit's iframe can have an opaque origin. Set an explicit page base so
    # browser tile requests carry a meaningful HTTP Referer, without proxying tiles.
    base = os.getenv("APP_PUBLIC_URL", "http://127.0.0.1:8501/")
    if urlparse(base).scheme not in {"http", "https"}:
        raise ValueError("APP_PUBLIC_URL must be a web URL")
    page = result.get_root().render()
    page = page.replace("<head>", '<head><meta name="referrer" content="strict-origin-when-cross-origin">' +
                        '<base href="' + html.escape(base, quote=True) + '">', 1)
    return page


def itinerary_map(itinerary):
    with st.expander("Your stops on the map", expanded=True):
        try:
            page = map_html(itinerary)
        except (ValueError, TypeError, KeyError):
            page = None
        if page is None:
            st.caption("Map unavailable: venue coordinates or tile configuration are missing.")
            return
        if hasattr(st, "iframe"):
            st.iframe(page, height=330, alt="Interactive OpenStreetMap itinerary markers")
        else:
            components.html(page, height=330, scrolling=False)
        st.caption("© OpenStreetMap contributors · ODbL. Dotted links show stop order, not navigable routes. Route times and sources are listed separately below.")
        st.markdown("[Report an OSM map issue](https://www.openstreetmap.org/fixthemap)")
