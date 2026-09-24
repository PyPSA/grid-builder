# SPDX-FileCopyrightText: Contributors to grid-builder <https://github.com/pypsa/grid-builder
#
# SPDX-License-Identifier: MIT

"""Build an interactive PyDeck map from generic network GeoJSON files.

Station/bus polygons and line geometries are simplified and their
coordinates rounded per the ``interactive_map`` config before being
embedded, keeping the standalone HTML small without discarding
perceptible detail; ``compress_html`` then strips whitespace from the
result. The page is otherwise self-contained: layer toggles,
voltage/text filtering, multi-circuit line offsetting, and click
tooltips with OSM links are all injected as plain HTML/CSS/JS by
``inject_controls``, adapted from PyPSA-Eur's
``prepare_osm_network_release.py``.
"""

import html
import json
import re
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import pandas as pd
import pydeck as pdk
from scripts._helpers import configure_logging, load_internal_yaml

if TYPE_CHECKING:
    snakemake: Any


def tooltip(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Create an HTML table from the fields actually present in a layer."""
    frame = frame.copy()
    columns = [column for column in frame if column != "geometry"]

    def value_text(column: str, value: Any) -> str:
        if isinstance(value, (list, tuple, dict, set)):
            value = ", ".join(map(str, value))
        if pd.isna(value):
            return ""
        value = str(value)
        if column == "osm_ids":
            return "<br>".join(
                f'<a href="https://www.openstreetmap.org/{item}" target="_blank">{html.escape(item)}</a>'
                for item in value.split(";")
            )
        return html.escape(value)

    frame["tooltip_html"] = frame.apply(
        lambda row: (
            "<table>"
            + "".join(
                f"<tr><th>{html.escape(column)}</th><td>{value_text(column, row[column])}</td></tr>"
                for column in columns
            )
            + "</table>"
        ),
        axis=1,
    )
    return frame


def _coord(point: Any, decimals: int) -> list[float]:
    """Round a 2D point to ``decimals`` places, as a plain ``[lon, lat]`` pair."""
    return [round(point[0], decimals), round(point[1], decimals)]


def line_colors(voltages: pd.Series) -> list[list[int]]:
    """Return the PyPSA-Eur voltage palette from internal configuration."""
    bands = load_internal_yaml("colors.yaml")["lines"]["voltage"]

    def rgba(color: str) -> list[int]:
        color = color.removeprefix("#")
        return [int(color[index : index + 2], 16) for index in range(0, 6, 2)] + [150]

    def color(voltage: float) -> str:
        for voltage_range, value in bands.items():
            minimum, maximum = voltage_range.split("-", maxsplit=1)
            if (
                float(minimum)
                <= voltage
                <= (float(maximum) if maximum != "inf" else float("inf"))
            ):
                return value
        raise ValueError(f"No line colour configured for {voltage} kV.")

    return [rgba(color(voltage)) for voltage in voltages]


def path_layer(
    frame: gpd.GeoDataFrame,
    name: str,
    color: list[int] | str,
    *,
    geo_crs: str,
    distance_crs: str,
    coord_decimals: int,
    simplify_m: float | None = None,
    auto_highlight: bool = True,
) -> pdk.Layer | None:
    """Render lines/transformers as a PathLayer, including MultiLineString geometries."""
    if frame.empty:
        return None
    if simplify_m is not None:
        frame = frame.copy()
        frame["geometry"] = (
            frame.geometry.to_crs(distance_crs).simplify(simplify_m).to_crs(geo_crs)
        )
    data = tooltip(frame)
    data["path"] = data.geometry.map(
        lambda line: (
            [
                [_coord(point, coord_decimals) for point in item.coords]
                for item in line.geoms
            ]
            if line.geom_type == "MultiLineString"
            else [_coord(point, coord_decimals) for point in line.coords]
        )
    )
    return pdk.Layer(
        "PathLayer",
        data=data.drop(columns="geometry"),
        get_path="path",
        get_color=color,
        width_min_pixels=2,
        pickable=True,
        auto_highlight=auto_highlight,
        parameters={"depthTest": False},
        id=name,
    )


def polygon_layer(
    frame: gpd.GeoDataFrame,
    name: str,
    color: list[int],
    *,
    geo_crs: str,
    distance_crs: str,
    coord_decimals: int,
    simplify_m: float | None = None,
    extruded: bool = False,
) -> pdk.Layer | None:
    """Render station and bus polygons, including MultiPolygon geometries."""
    if frame.empty:
        return None
    if simplify_m is not None:
        frame = frame.copy()
        frame["geometry"] = (
            frame.geometry.to_crs(distance_crs).simplify(simplify_m).to_crs(geo_crs)
        )
    data = tooltip(frame)
    data["polygon"] = data.geometry.map(
        lambda polygon: (
            [
                [_coord(point, coord_decimals) for point in item.exterior.coords]
                for item in polygon.geoms
            ]
            if polygon.geom_type == "MultiPolygon"
            else [_coord(point, coord_decimals) for point in polygon.exterior.coords]
        )
    )
    extrusion_kwargs: dict[str, Any] = {}
    if extruded:
        extrusion_kwargs.update(
            extruded=True,
            wireframe=True,
            get_elevation=200,
            get_line_color=[255, 255, 255],
        )
    return pdk.Layer(
        "PolygonLayer",
        data=data.drop(columns="geometry"),
        get_polygon="polygon",
        get_fill_color=color,
        pickable=True,
        auto_highlight=True,
        parameters={"depthTest": False},
        id=name,
        **extrusion_kwargs,
    )


def build_map(
    buses: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    transformers: gpd.GeoDataFrame,
    stations: gpd.GeoDataFrame,
    bus_polygons: gpd.GeoDataFrame,
    *,
    geo_crs: str,
    distance_crs: str,
    stations_simplify_m: float | None,
    buses_polygon_simplify_m: float | None,
    lines_simplify_m: float | None,
    coord_decimals: int,
) -> pdk.Deck:
    """Create the generic AC map without requiring DC links or converters."""
    lines = lines.copy()
    if not lines.empty:
        lines["color"] = line_colors(lines["voltage_kv"])
    layers = [
        layer
        for layer in (
            polygon_layer(
                stations,
                "Stations",
                [0, 80, 255, 60],
                geo_crs=geo_crs,
                distance_crs=distance_crs,
                coord_decimals=coord_decimals,
                simplify_m=stations_simplify_m,
            ),
            polygon_layer(
                bus_polygons,
                "Bus polygons",
                [255, 0, 155, 50],
                geo_crs=geo_crs,
                distance_crs=distance_crs,
                coord_decimals=coord_decimals,
                simplify_m=buses_polygon_simplify_m,
                extruded=True,
            ),
            path_layer(
                lines,
                "Lines",
                "color",
                geo_crs=geo_crs,
                distance_crs=distance_crs,
                coord_decimals=coord_decimals,
                simplify_m=lines_simplify_m,
                auto_highlight=False,
            ),
            path_layer(
                transformers,
                "Transformers",
                [255, 255, 0, 180],
                geo_crs=geo_crs,
                distance_crs=distance_crs,
                coord_decimals=coord_decimals,
            ),
        )
        if layer
    ]
    if not buses.empty:
        data = tooltip(buses)
        data["position"] = data.geometry.map(
            lambda point: _coord((point.x, point.y), coord_decimals)
        )
        layers.append(
            pdk.Layer(
                "ColumnLayer",
                data=data.drop(columns="geometry"),
                get_position="position",
                get_fill_color=[255, 0, 155, 180],
                radius=20,
                get_elevation=10,
                pickable=True,
                auto_highlight=True,
                parameters={"depthTest": False},
                id="Buses",
            )
        )
    geometry = pd.concat([buses.geometry, lines.geometry], ignore_index=True)
    center = geometry.union_all().centroid if not geometry.empty else None
    return pdk.Deck(
        layers=layers,
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
        initial_view_state=pdk.ViewState(
            longitude=center.x if center else 0,
            latitude=center.y if center else 0,
            zoom=5,
            pitch=25,
        ),
        tooltip={"html": "{tooltip_html}"},
    )


def inject_controls(deck: pdk.Deck) -> str:
    """Inject the release-map UI, adapted to grid-builder's available layers."""
    page = deck.to_html(as_string=True)
    # PyDeck 0.9 varies the indentation before createDeck's closing delimiter,
    # so inject at the final script closing tag instead of matching whitespace.
    script, closing_tag = page.rsplit("\n  </script>", maxsplit=1)
    page = script + "\nwindow.deck = deckInstance;\n  </script>" + closing_tag
    # This is deliberately self-contained: maps are distributed as standalone HTML.
    controls = r"""<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
:root{
  /* Colors: theme-invariant accents (same in light/dark, on purpose - this
     is what keeps the "selected"/"active" state readable in both themes). */
  --accent:#6366f1;
  --accent-hover:#4f46e5;
  --selected-chip-bg:rgba(99,102,241,.92);
  --selected-chip-bg-hover:rgba(79,70,229,.95);
  --osm-link-color:#4a9eff;

  /* Colors: redefined per theme under body.dark-theme below. Each is used
     by exactly one rule, so body.dark-theme only ever overrides variables
     here - never a whole selector - which is what avoids the specificity
     trap a "body.dark-theme .foo{...}" override rule would otherwise have
     (an extra element in the selector outweighs an extra class elsewhere,
     e.g. it used to silently beat ".voltage-tag.selected" in dark mode). */
  --text-color:#1f2328;
  --label-color:#6b7280;
  --hint-color:#9ca3af;
  --focus-border:#6366f1;
  --btn-bg:rgba(255,255,255,.2);
  --btn-bg-hover:rgba(255,255,255,.4);
  --panel-bg:rgba(255,255,255,.2);
  --divider-color:rgba(0,0,0,.07);
  --chip-bg:rgba(0,0,0,.02);
  --chip-bg-hover:rgba(0,0,0,.05);
  --input-border:rgba(0,0,0,.07);
  --input-bg:rgba(0,0,0,.01);
  --input-bg-focus:rgba(255,255,255,.35);

  /* Sizing: the button row and panel position/size all derive from these
     three, so resizing the buttons keeps the panel aligned automatically. */
  --ui-inset:10px;
  --btn-size:34px;
  --btn-gap:10px;
  --radius-pill:999px;
  --radius-panel:16px;
  --panel-min-width:225px;
  --panel-max-width:270px;
  --panel-padding:14px;
  --blur-btn:14px;
  --blur-panel:16px;
  --font-size-panel:12px;
  --font-size-label:10px;
  --font-size-chip:11px;
  --font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;

  /* Motion */
  --duration-fast:.1s;
  --duration-base:.15s;
  --duration-slow:.2s;
}
body.dark-theme{
  --text-color:#f4f4f5;
  --label-color:#9ca3af;
  --focus-border:#818cf8;
  --btn-bg:rgba(28,28,32,.18);
  --btn-bg-hover:rgba(40,40,46,.32);
  --panel-bg:rgba(22,22,26,.18);
  --divider-color:rgba(255,255,255,.1);
  --chip-bg:rgba(255,255,255,.03);
  --chip-bg-hover:rgba(255,255,255,.07);
  --input-border:rgba(255,255,255,.12);
  --input-bg:rgba(255,255,255,.02);
  --input-bg-focus:rgba(255,255,255,.06);
}
html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden}
#deck-container{position:fixed!important;inset:0!important;width:100%!important;height:100%!important;touch-action:none}
#deck-container canvas{width:100%!important;height:100%!important}
.map-btn{position:absolute;top:var(--ui-inset);width:var(--btn-size);height:var(--btn-size);border:0;border-radius:50%;z-index:10001;background:var(--btn-bg);backdrop-filter:blur(var(--blur-btn));-webkit-backdrop-filter:blur(var(--blur-btn));box-shadow:0 1px 3px rgba(0,0,0,.12),0 3px 8px rgba(0,0,0,.08);font-size:14px;color:var(--text-color);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background var(--duration-slow),color var(--duration-slow),box-shadow var(--duration-base),transform var(--duration-fast);-webkit-tap-highlight-color:transparent}
.map-btn:hover{background:var(--btn-bg-hover);box-shadow:0 2px 6px rgba(0,0,0,.15),0 6px 14px rgba(0,0,0,.1)}
.map-btn:active{transform:scale(.92)}
.hamburger{flex-direction:column;gap:5px}
.hamburger .bar{display:block;width:16px;height:2px;border-radius:1px;background:currentColor;transition:transform var(--duration-base) ease,opacity var(--duration-base) ease}
.hamburger.open .bar:nth-child(1){transform:translateY(7px) rotate(45deg)}
.hamburger.open .bar:nth-child(2){opacity:0}
.hamburger.open .bar:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
#layer-controls{display:none;position:absolute;top:var(--ui-inset);left:var(--ui-inset);margin-top:calc(var(--ui-inset) + var(--btn-size));min-width:var(--panel-min-width);max-width:var(--panel-max-width);max-height:calc(100vh - 3*var(--ui-inset) - var(--btn-size));overflow-y:auto;padding:var(--panel-padding);z-index:10001;border-radius:var(--radius-panel);background:var(--panel-bg);backdrop-filter:blur(var(--blur-panel));-webkit-backdrop-filter:blur(var(--blur-panel));box-shadow:0 2px 8px rgba(0,0,0,.08),0 12px 32px rgba(0,0,0,.16);color:var(--text-color);font:var(--font-size-panel) var(--font-family);transition:background var(--duration-slow),color var(--duration-slow)}
#layer-controls.open{display:block}
.panel-section{padding:12px 0;border-bottom:1px solid var(--divider-color);transition:border-color var(--duration-slow)}
.panel-section:first-of-type{padding-top:0}
.panel-section:last-of-type{border-bottom:0;padding-bottom:0}
.tag-row{margin-top:8px}
.hint-text{font-size:var(--font-size-label);color:var(--hint-color);margin-top:5px}
.ctrl-btn{width:100%;margin-top:8px;padding:6px 14px;background:var(--chip-bg);color:var(--text-color);border:0;border-radius:var(--radius-pill);cursor:pointer;font-size:var(--font-size-panel);font-weight:500;transition:background var(--duration-base),color var(--duration-base),transform var(--duration-fast);-webkit-tap-highlight-color:transparent}
.ctrl-btn:hover{background:var(--chip-bg-hover)}
.ctrl-btn:active{background:var(--accent);color:#fff;transform:scale(.97)}
.pill-input{width:100%;box-sizing:border-box;margin-top:6px;padding:7px 14px;border:1px solid var(--input-border);border-radius:var(--radius-pill);background:var(--input-bg);color:var(--text-color);font-size:var(--font-size-panel);outline:none;transition:border-color var(--duration-base),background var(--duration-base),color var(--duration-slow)}
.pill-input:focus{border-color:var(--focus-border);background:var(--input-bg-focus)}
.section-label{display:block;font-size:var(--font-size-label);font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--label-color);transition:color var(--duration-slow)}
.voltage-tag{display:inline-flex;align-items:center;background:var(--chip-bg);color:var(--text-color);padding:4px 11px;margin:3px 3px 0 0;border-radius:var(--radius-pill);font-size:var(--font-size-chip);font-weight:500;cursor:pointer;transition:background var(--duration-base),color var(--duration-base),transform var(--duration-fast);-webkit-tap-highlight-color:transparent}
.voltage-tag:hover{background:var(--chip-bg-hover)}
.voltage-tag:active{transform:scale(.95)}
.voltage-tag.selected{background:var(--selected-chip-bg);color:#fff}
.voltage-tag.selected:hover{background:var(--selected-chip-bg-hover)}
</style>
<button id="menu-toggle" class="map-btn hamburger" style="left:var(--ui-inset)" aria-label="Menu"><span class="bar"></span><span class="bar"></span><span class="bar"></span></button>
<button id="theme-toggle" class="map-btn" style="left:calc(var(--ui-inset) + var(--btn-size) + var(--btn-gap))">◐</button>
<button id="zoom-in" class="map-btn" style="left:calc(var(--ui-inset) + 2*(var(--btn-size) + var(--btn-gap)))">+</button>
<button id="zoom-out" class="map-btn" style="left:calc(var(--ui-inset) + 3*(var(--btn-size) + var(--btn-gap)))">−</button>
<div id="layer-controls">
<div class="panel-section">
<span class="section-label">Search all fields</span>
<input id="text-search-filter" placeholder="term1 &amp; term2 | term3" class="pill-input">
<div class="hint-text">Use &amp; for AND, | for OR, ( ) to group</div>
<button id="clear-text" class="ctrl-btn">Clear search</button>
</div>
<div class="panel-section">
<span class="section-label">Filter by voltage (kV)</span>
<div id="voltage-tags" class="tag-row"></div>
<button id="clear-voltage" class="ctrl-btn">Clear filter</button>
</div>
<div class="panel-section">
<span class="section-label">Show components</span>
<div id="component-tags" class="tag-row"></div>
<button id="reset-components" class="ctrl-btn">Reset</button>
</div>
</div>
<script>
let originalLayerData = {};
let availableVoltages = new Set();
let selectedVoltages = new Set();
let hiddenLayers = new Set();
let currentTextSearch = '';
let isDarkMode = true;
let hashUpdateTimeout = null;
let circuitsExpanded = false;
let hoveredLineId = null;
let currentTooltip = null;
let currentTooltipCoords = null;
let animationFrameId = null;
const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

// Every tunable number/color/duration the controls script uses lives here,
// so the map UI's visual and interaction tuning can be changed in one place
// without hunting through the functions below.
const CONFIG = {
  mapStyles: {
    light: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
    dark: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
  },
  excludedKeys: ['geometry', 'path', 'polygon', 'position', 'color', 'tooltip_html'],
  voltageKeys: ['voltage_kv', 'voltage_bus0_kv', 'voltage_bus1_kv'],
  hashDebounceMs: 150,
  zoomStep: 1,
  zoomMin: 1,
  zoomMax: 20,
  pickingRadiusDesktop: 10,
  pickingRadiusMobile: 20,
  tapMaxMoveMobile: 15,
  tapMaxDurationMobile: 300,
  // Multi-circuit line offset: metric-space miter join with tapered ends,
  // mirroring prepare_osm_network_release.py's offsetPath/miterOffset.
  circuitOffsetMeters: 0.0003,
  circuitZoomThreshold: 11.5,
  taperDegrees: 0.00025,
  hoverColorDark: [255, 255, 255, 255],
  hoverColorLight: [255, 20, 147, 255],
  tooltipFollowOffsetPx: 5,
  tooltipTransitionMs: 300,
  tooltipBlurPx: 16,
  // A fixed width, not max-width: an absolutely-positioned element with
  // width:auto/max-width shrink-to-fits against the remaining space to the
  // viewport edge, so it visibly narrows and re-wraps as it nears the edge.
  // A fixed width removes that dependency - the box stays one size and
  // simply extends past the edge (clipped, not reshaped).
  tooltipWidthPx: 300,
  osmLinkColor: '#4a9eff',
  tooltip: {
    dark: { bg: 'rgba(20,20,24,.32)', text: '#fff', label: 'rgba(255,255,255,.6)', close: 'rgba(255,255,255,.55)', closeHover: '#fff' },
    light: { bg: 'rgba(255,255,255,.32)', text: '#1f2328', label: 'rgba(31,35,40,.55)', close: 'rgba(31,35,40,.45)', closeHover: '#1f2328' },
  },
};

function parseHash() {
  const hash = location.hash.slice(1);
  if (!hash) return null;
  const parts = hash.split('/');
  if (parts.length < 4) return null;
  return { theme: parts[0], zoom: parseFloat(parts[1].replace(/^#/, '')), latitude: parseFloat(parts[2]), longitude: parseFloat(parts[3]) };
}

function updateHash(viewState, immediate) {
  if (hashUpdateTimeout) clearTimeout(hashUpdateTimeout);
  const write = () => {
    const theme = isDarkMode ? 'dark' : 'light';
    history.replaceState(null, '', '#' + theme + '/#' + viewState.zoom.toFixed(2) + '/' + viewState.latitude.toFixed(4) + '/' + viewState.longitude.toFixed(4));
  };
  if (immediate) write();
  else hashUpdateTimeout = setTimeout(write, CONFIG.hashDebounceMs);
}

// Drives every theme-aware CSS custom property (see body.dark-theme in the
// <style> block above), so static chrome repaints - with its own CSS
// transitions - the instant the theme flips. The tooltip is dynamic content
// built with inline styles, so it needs the separate recolorTooltip() below.
function applyBodyTheme() {
  document.body.classList.toggle('dark-theme', isDarkMode);
}

function applyHashToMap() {
  const hashParams = parseHash();
  const deck = window.deck;
  if (!hashParams || !deck) return;
  deck.setProps({ initialViewState: { latitude: hashParams.latitude, longitude: hashParams.longitude, zoom: hashParams.zoom, pitch: 30, transitionDuration: 500 } });
  if (hashParams.theme && hashParams.theme !== (isDarkMode ? 'dark' : 'light')) {
    isDarkMode = hashParams.theme === 'dark';
    document.getElementById('theme-toggle').textContent = isDarkMode ? '◐' : '○';
    deck.setProps({ mapStyle: isDarkMode ? CONFIG.mapStyles.dark : CONFIG.mapStyles.light });
    applyBodyTheme();
    recolorTooltip();
  }
}

function toggleTheme() {
  const deck = window.deck;
  if (!deck) return;
  isDarkMode = !isDarkMode;
  document.getElementById('theme-toggle').textContent = isDarkMode ? '◐' : '○';
  deck.setProps({ mapStyle: isDarkMode ? CONFIG.mapStyles.dark : CONFIG.mapStyles.light });
  applyBodyTheme();
  recolorTooltip();
  const vp = deck.viewManager.getViewports()[0];
  if (vp) updateHash(vp, true);
}

function zoomBy(delta) {
  const deck = window.deck;
  const vp = deck && deck.viewManager.getViewports()[0];
  if (!vp) return;
  deck.setProps({ initialViewState: { latitude: vp.latitude, longitude: vp.longitude, zoom: Math.max(CONFIG.zoomMin, Math.min(CONFIG.zoomMax, vp.zoom + delta)), pitch: vp.pitch || 30, transitionDuration: 300 } });
}

function pathLength(path) {
  let total = 0;
  for (let i = 1; i < path.length; i++) {
    const dx = path[i][0] - path[i - 1][0];
    const dy = path[i][1] - path[i - 1][1];
    total += Math.sqrt(dx * dx + dy * dy);
  }
  return total;
}

function interpCoord(a, b, t) {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

function insertTaperPoints(path, taper, total) {
  if (total <= taper * 2) return [path, total];
  const result = [];
  let cumDist = 0;
  const startT = taper, endT = total - taper;
  let startIns = false, endIns = false;
  for (let i = 0; i < path.length; i++) {
    if (i > 0) {
      const dx = path[i][0] - path[i - 1][0];
      const dy = path[i][1] - path[i - 1][1];
      const seg = Math.sqrt(dx * dx + dy * dy);
      if (!startIns && cumDist + seg >= startT) {
        result.push(interpCoord(path[i - 1], path[i], (startT - cumDist) / seg));
        startIns = true;
      }
      if (!endIns && cumDist + seg >= endT) {
        result.push(interpCoord(path[i - 1], path[i], (endT - cumDist) / seg));
        endIns = true;
      }
      cumDist += seg;
      if (startIns && endIns && cumDist >= endT) {
        result.push(path[i]);
        for (let j = i + 1; j < path.length; j++) result.push(path[j]);
        return [result, total];
      }
    }
    result.push(path[i]);
  }
  return [result, total];
}

function miterOffset(path, shiftDeg) {
  const n = path.length;
  if (n < 2) return path;
  const latRad = path[0][1] * Math.PI / 180;
  const Mlat = 111320.0;
  const Mlon = Mlat * Math.cos(latRad);
  const shiftM = shiftDeg * Mlat;
  const ox0 = path[0][0], oy0 = path[0][1];

  const ns = new Array(n - 1);
  let px = 0, py = 0;
  for (let i = 0; i < n - 1; i++) {
    const qx = (path[i + 1][0] - ox0) * Mlon;
    const qy = (path[i + 1][1] - oy0) * Mlat;
    const dx = qx - px, dy = qy - py;
    const ln = Math.sqrt(dx * dx + dy * dy) || 1;
    ns[i] = [-dy / ln, dx / ln];
    px = qx; py = qy;
  }

  const result = new Array(n);
  for (let i = 0; i < n; i++) {
    let nx, ny;
    if (i === 0) {
      [nx, ny] = ns[0];
    } else if (i === n - 1) {
      [nx, ny] = ns[n - 2];
    } else {
      const [n1x, n1y] = ns[i - 1];
      const [n2x, n2y] = ns[i];
      let bx = n1x + n2x, by = n1y + n2y;
      const bl = Math.sqrt(bx * bx + by * by);
      if (bl < 1e-10) {
        nx = n1x; ny = n1y;
      } else {
        bx /= bl; by /= bl;
        const dot = bx * n1x + by * n1y;
        const scale = Math.min(Math.abs(dot) > 1e-10 ? 1.0 / dot : 4.0, 4.0);
        nx = bx * scale; ny = by * scale;
      }
    }
    const pmx = (path[i][0] - ox0) * Mlon + nx * shiftM;
    const pmy = (path[i][1] - oy0) * Mlat + ny * shiftM;
    result[i] = [ox0 + pmx / Mlon, oy0 + pmy / Mlat];
  }
  return result;
}

function offsetSimplePath(path, offsetIndex, totalCircuits) {
  if (totalCircuits <= 1) return path;
  const center = (totalCircuits - 1) / 2;
  const shift = (offsetIndex - center) * CONFIG.circuitOffsetMeters;
  const total = pathLength(path);
  const taper = Math.min(CONFIG.taperDegrees, total * 0.45);
  const [dense, denseTotal] = insertTaperPoints(path, taper, total);
  const mitered = miterOffset(dense, shift);
  let cumDist = 0;
  return mitered.map((coord, i) => {
    if (i > 0) {
      const dx = dense[i][0] - dense[i - 1][0];
      const dy = dense[i][1] - dense[i - 1][1];
      cumDist += Math.sqrt(dx * dx + dy * dy);
    }
    const t = taper > 0 ? Math.min(cumDist, denseTotal - cumDist, taper) / taper : 1;
    const ox = coord[0] - dense[i][0];
    const oy = coord[1] - dense[i][1];
    return [dense[i][0] + ox * t, dense[i][1] + oy * t];
  });
}

function offsetPath(path, offsetIndex, totalCircuits) {
  if (totalCircuits <= 1 || !Array.isArray(path[0])) return path;
  if (Array.isArray(path[0][0])) return path.map((sub) => offsetSimplePath(sub, offsetIndex, totalCircuits));
  return offsetSimplePath(path, offsetIndex, totalCircuits);
}

function buildCircuitIndex(data) {
  if (!data || !data.length || data[0].circuits === undefined) return null;
  const index = [];
  data.forEach((item, dataIdx) => {
    const circuits = parseInt(item.circuits) || 1;
    for (let i = 0; i < circuits; i++) index.push({ dataIdx, circuitIdx: i, circuits });
  });
  return index;
}

function buildLinesView(data) {
  const index = buildCircuitIndex(data);
  if (!index) return data;
  return index.map(({ dataIdx, circuitIdx, circuits }) => {
    const item = data[dataIdx];
    return { ...item, path: offsetPath(item.path, circuitIdx, circuits) };
  });
}

function updateCircuitExpansion(zoom) {
  const deck = window.deck;
  if (!deck) return;
  const shouldExpand = zoom >= CONFIG.circuitZoomThreshold;
  if (shouldExpand === circuitsExpanded) return;
  circuitsExpanded = shouldExpand;
  const originalData = originalLayerData['Lines'];
  if (!originalData) return;
  deck.setProps({
    layers: deck.props.layers.map((l) => l.id === 'Lines' ? l.clone({ data: shouldExpand ? buildLinesView(originalData) : originalData }) : l),
  });
}

function updateHoveredLine(lineId) {
  if (lineId === hoveredLineId) return;
  hoveredLineId = lineId;
  const deck = window.deck;
  if (!deck) return;
  const hoverColor = isDarkMode ? [255, 255, 255, 255] : [255, 20, 147, 255];
  deck.setProps({
    layers: deck.props.layers.map((l) => l.id !== 'Lines' ? l : l.clone({
      getColor: (d) => d.line_id === hoveredLineId ? hoverColor : d.color,
      updateTriggers: { getColor: hoveredLineId },
    })),
  });
}

// --- Filtering ---
function initializeVoltages() {
  const deck = window.deck;
  if (!deck) return;
  deck.props.layers.forEach((layer) => {
    originalLayerData[layer.id] = layer.props.data || [];
    (layer.props.data || []).forEach((item) => CONFIG.voltageKeys.forEach((key) => {
      if (item[key] !== undefined) availableVoltages.add(item[key]);
    }));
  });
  availableVoltages = new Set([...availableVoltages].sort((a, b) => b - a));
  renderVoltageTags();
}

function toggleVoltage(voltage) {
  if (selectedVoltages.has(voltage)) selectedVoltages.delete(voltage);
  else selectedVoltages.add(voltage);
  renderVoltageTags();
  applyAllFilters();
}

function renderVoltageTags() {
  const container = document.getElementById('voltage-tags');
  container.innerHTML = '';
  [...availableVoltages].forEach((voltage) => {
    const tag = document.createElement('span');
    tag.className = selectedVoltages.has(voltage) ? 'voltage-tag selected' : 'voltage-tag';
    tag.textContent = voltage;
    tag.onclick = () => toggleVoltage(voltage);
    container.appendChild(tag);
  });
}

function toggleLayerVisibility(layerId) {
  if (hiddenLayers.has(layerId)) hiddenLayers.delete(layerId);
  else hiddenLayers.add(layerId);
  setLayerVisibility(layerId, !hiddenLayers.has(layerId));
  renderComponentTags();
}

function renderComponentTags() {
  const deck = window.deck;
  if (!deck) return;
  const container = document.getElementById('component-tags');
  container.innerHTML = '';
  deck.props.layers.forEach((layer) => {
    const tag = document.createElement('span');
    tag.className = hiddenLayers.has(layer.id) ? 'voltage-tag' : 'voltage-tag selected';
    tag.textContent = layer.id;
    tag.onclick = () => toggleLayerVisibility(layer.id);
    container.appendChild(tag);
  });
}

function resetComponents() {
  hiddenLayers.clear();
  const deck = window.deck;
  if (deck) {
    deck.setProps({ layers: deck.props.layers.map((l) => l.clone({ visible: true })) });
  }
  renderComponentTags();
}

// Small recursive-descent parser for the search syntax: '&' binds tighter
// than '|' (like * over +), and '(' ')' can override that grouping.
// Grammar: orExpr := andExpr ('|' andExpr)* ; andExpr := term ('&' term)* ;
// term := '(' orExpr ')' | TEXT
function tokenizeSearch(searchTerm) {
  return searchTerm
    .split(/([()&|])/)
    .map((token) => token.trim())
    .filter((token) => token.length > 0);
}

function parseSearchExpr(tokens) {
  let pos = 0;

  function parseTerm() {
    if (tokens[pos] === '(') {
      pos++;
      const node = parseOr();
      if (tokens[pos] === ')') pos++;
      return node;
    }
    // Out of tokens (e.g. a dangling trailing '&'/'|' while still typing) or
    // a stray ')': treat as an empty term, which matches everything below.
    if (pos >= tokens.length || tokens[pos] === ')') return { type: 'text', value: '' };
    return { type: 'text', value: tokens[pos++] };
  }

  function parseAnd() {
    let node = parseTerm();
    while (tokens[pos] === '&') {
      pos++;
      node = { type: 'and', left: node, right: parseTerm() };
    }
    return node;
  }

  function parseOr() {
    let node = parseAnd();
    while (tokens[pos] === '|') {
      pos++;
      node = { type: 'or', left: node, right: parseAnd() };
    }
    return node;
  }

  return parseOr();
}

function evalSearchNode(node, item) {
  if (node.type === 'or') return evalSearchNode(node.left, item) || evalSearchNode(node.right, item);
  if (node.type === 'and') return evalSearchNode(node.left, item) && evalSearchNode(node.right, item);
  const lowerTerm = node.value.toLowerCase();
  if (!lowerTerm) return true;
  for (const [key, value] of Object.entries(item)) {
    if (CONFIG.excludedKeys.includes(key)) continue;
    if (String(value).toLowerCase().includes(lowerTerm)) return true;
  }
  return false;
}

function matchesTextSearch(item, searchTerm) {
  if (!searchTerm) return true;
  const tokens = tokenizeSearch(searchTerm);
  if (!tokens.length) return true;
  return evalSearchNode(parseSearchExpr(tokens), item);
}

function applyAllFilters() {
  const deck = window.deck;
  if (!deck) return;
  const hasVoltageFilter = selectedVoltages.size > 0;
  const hasTextFilter = currentTextSearch.length > 0;
  const voltages = [...selectedVoltages];
  const layers = deck.props.layers.map((layer) => {
    const originalData = originalLayerData[layer.id] || [];
    const filteredData = (hasVoltageFilter || hasTextFilter) ? originalData.filter((item) => {
      let passesVoltage = !hasVoltageFilter;
      if (hasVoltageFilter) {
        const values = CONFIG.voltageKeys.map((key) => item[key]).filter((v) => v !== undefined);
        passesVoltage = !values.length || values.some((v) => voltages.includes(v));
      }
      return passesVoltage && matchesTextSearch(item, currentTextSearch);
    }) : originalData;
    const data = (layer.id === 'Lines' && circuitsExpanded) ? buildLinesView(filteredData) : filteredData;
    return layer.clone({ data });
  });
  deck.setProps({ layers });
}

function clearVoltageFilter() {
  selectedVoltages.clear();
  renderVoltageTags();
  applyAllFilters();
}

function clearTextSearchFilter() {
  currentTextSearch = '';
  document.getElementById('text-search-filter').value = '';
  applyAllFilters();
}

function setLayerVisibility(layerId, visible) {
  const deck = window.deck;
  if (!deck) return;
  deck.setProps({ layers: deck.props.layers.map((l) => l.id === layerId ? l.clone({ visible }) : l) });
}

// --- Click tooltip: mirrors prepare_osm_network_release.py's click handler
// (table formatting, close button, and map-follows positioning), extended
// with an osm_ids link-out matching what tooltip() already renders server-side.
function updateTooltipPosition() {
  if (!currentTooltip || !currentTooltipCoords) {
    if (animationFrameId) { cancelAnimationFrame(animationFrameId); animationFrameId = null; }
    return;
  }
  const deck = window.deck;
  if (deck && deck.viewManager) {
    try {
      const viewport = deck.viewManager.getViewports()[0];
      if (viewport && viewport.project) {
        const screen = viewport.project(currentTooltipCoords);
        if (screen && currentTooltip) {
          // Fixed pixel offsets, not percentages: this keeps the tooltip a
          // constant distance from the picked point regardless of position.
          currentTooltip.style.left = (screen[0] + CONFIG.tooltipFollowOffsetPx) + 'px';
          currentTooltip.style.top = (screen[1] + CONFIG.tooltipFollowOffsetPx) + 'px';
        }
      }
    } catch (e) {}
  }
  animationFrameId = requestAnimationFrame(updateTooltipPosition);
}

function closeTooltip() {
  if (currentTooltip) currentTooltip.remove();
  currentTooltip = null;
  currentTooltipCoords = null;
  if (animationFrameId) { cancelAnimationFrame(animationFrameId); animationFrameId = null; }
}

// Any "way/<id>" or "relation/<id>" reference, in any column, becomes an
// OSM link - not just in osm_ids. The id runs up to the first non-digit
// (e.g. the "-<suffix>"/":<part>" build_network.py appends), and a
// negative lookbehind (not \b, which treats "_" as a word char and would
// miss "virtual_way/...") only blocks a preceding LETTER, so it still
// matches after "_" or ":" but not inside "highway/...".
const OSM_REF_PATTERN = /(?<![a-zA-Z])(?:way|relation)\/\d+/g;

function linkifyOsmRefs(value) {
  if (value === undefined || value === null) return value;
  return String(value).replace(OSM_REF_PATTERN, (ref) =>
    '<a href="https://www.openstreetmap.org/' + ref + '" target="_blank" rel="noopener noreferrer" style="color:' + CONFIG.osmLinkColor + ';text-decoration:underline">' + ref + '</a>'
  );
}

function tooltipColors() {
  return isDarkMode ? CONFIG.tooltip.dark : CONFIG.tooltip.light;
}

// Re-applies tooltipColors() to whatever tooltip is currently open, so a
// theme toggle repaints it immediately instead of leaving it stuck with the
// colors it was created with. The CSS `transition` set below on each of
// these elements is what turns this into a fade rather than a hard cut.
function recolorTooltip() {
  if (!currentTooltip) return;
  const colors = tooltipColors();
  currentTooltip.style.backgroundColor = colors.bg;
  currentTooltip.style.color = colors.text;
  currentTooltip.querySelectorAll('.tt-key').forEach((el) => { el.style.color = colors.label; });
  currentTooltip.querySelectorAll('.tt-val').forEach((el) => { el.style.color = colors.text; });
  const closeBtn = currentTooltip.querySelector('.tt-close');
  if (closeBtn) closeBtn.style.color = colors.close;
}

function showTooltip(pickInfo) {
  const colors = tooltipColors();
  const colorTransition = 'color ' + CONFIG.tooltipTransitionMs + 'ms';
  let rows = '';
  let hasContent = false;
  for (const [key, value] of Object.entries(pickInfo.object)) {
    if (CONFIG.excludedKeys.includes(key)) continue;
    rows += '<tr>'
      + '<td class="tt-key" style="padding:2px 6px 2px 0;font-weight:600;vertical-align:top;color:' + colors.label + ';white-space:nowrap;transition:' + colorTransition + '">' + key + '</td>'
      + '<td class="tt-val" style="padding:2px 0;vertical-align:top;color:' + colors.text + ';word-break:break-all;max-width:260px;transition:' + colorTransition + '">' + linkifyOsmRefs(value) + '</td>'
      + '</tr>';
    hasContent = true;
  }
  if (!hasContent) return;

  currentTooltipCoords = pickInfo.coordinate;
  const tooltip = document.createElement('div');
  tooltip.innerHTML = '<table style="border-collapse:collapse;font-size:11px;line-height:1.35">' + rows + '</table>';
  // width (fixed), not max-width: see the CONFIG.tooltipWidthPx comment -
  // this is what keeps the box from narrowing as it nears the viewport edge.
  tooltip.style.cssText = 'position:absolute;background-color:' + colors.bg + ';backdrop-filter:blur(' + CONFIG.tooltipBlurPx + 'px);-webkit-backdrop-filter:blur(' + CONFIG.tooltipBlurPx + 'px);color:' + colors.text + ';padding:8px 24px 8px 12px;border-radius:12px;z-index:10000;pointer-events:auto;width:' + CONFIG.tooltipWidthPx + 'px;box-sizing:border-box;box-shadow:0 8px 24px rgba(0,0,0,.3);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;user-select:text;cursor:text;will-change:transform;transition:background-color ' + CONFIG.tooltipTransitionMs + 'ms,' + colorTransition;

  const closeBtn = document.createElement('div');
  closeBtn.className = 'tt-close';
  closeBtn.innerHTML = '×';
  closeBtn.style.cssText = 'position:absolute;top:3px;right:7px;cursor:pointer;font-size:15px;font-weight:bold;color:' + colors.close + ';line-height:1;-webkit-tap-highlight-color:transparent;transition:' + colorTransition;
  closeBtn.onclick = (e) => { e.stopPropagation(); closeTooltip(); };
  closeBtn.onmouseover = () => { closeBtn.style.color = tooltipColors().closeHover; };
  closeBtn.onmouseout = () => { closeBtn.style.color = tooltipColors().close; };
  tooltip.appendChild(closeBtn);

  currentTooltip = tooltip;
  document.body.appendChild(tooltip);
  updateTooltipPosition();
}

document.addEventListener('DOMContentLoaded', () => {
  const deck = window.deck;
  if (!deck) return;

  applyBodyTheme();
  renderComponentTags();
  initializeVoltages();
  applyHashToMap();
  addEventListener('hashchange', applyHashToMap);

  document.getElementById('text-search-filter').addEventListener('input', (e) => { currentTextSearch = e.target.value; applyAllFilters(); });
  document.getElementById('clear-voltage').addEventListener('click', clearVoltageFilter);
  document.getElementById('clear-text').addEventListener('click', clearTextSearchFilter);
  document.getElementById('reset-components').addEventListener('click', resetComponents);

  // The menu only ever opens/closes via this button (its hamburger icon
  // animates into an X while open, so the affordance to close it is
  // visible) - deliberately no "click outside/on the map closes it"
  // listener, unlike the tooltip, so panning the map can't dismiss it.
  const menuToggleBtn = document.getElementById('menu-toggle');
  const layerControls = document.getElementById('layer-controls');
  menuToggleBtn.addEventListener('click', () => {
    const isOpen = layerControls.classList.toggle('open');
    menuToggleBtn.classList.toggle('open', isOpen);
  });
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  document.getElementById('zoom-in').addEventListener('click', () => zoomBy(CONFIG.zoomStep));
  document.getElementById('zoom-out').addEventListener('click', () => zoomBy(-CONFIG.zoomStep));

  deck.setProps({
    getTooltip: null,
    pickingRadius: isMobile ? CONFIG.pickingRadiusMobile : CONFIG.pickingRadiusDesktop,
    onHover: ({ object }) => updateHoveredLine(object ? object.line_id : null),
    onViewStateChange: ({ viewState }) => {
      updateHash(viewState);
      updateCircuitExpansion(viewState.zoom);
      return viewState;
    },
  });

  const deckContainer = document.getElementById('deck-container');
  let touchStartInfo = null;

  if (isMobile) {
    deckContainer.addEventListener('touchstart', (event) => {
      touchStartInfo = event.touches.length > 1 ? null : { x: event.touches[0].clientX, y: event.touches[0].clientY, time: Date.now() };
    }, { passive: true });
    deckContainer.addEventListener('touchmove', (event) => { if (event.touches.length > 1) touchStartInfo = null; }, { passive: true });
  }

  deckContainer.addEventListener(isMobile ? 'touchend' : 'click', (event) => {
    let clientX, clientY;
    if (isMobile) {
      if (!touchStartInfo) return;
      const touch = event.changedTouches && event.changedTouches[0];
      if (!touch) return;
      const dist = Math.hypot(touch.clientX - touchStartInfo.x, touch.clientY - touchStartInfo.y);
      const duration = Date.now() - touchStartInfo.time;
      touchStartInfo = null;
      if (dist > CONFIG.tapMaxMoveMobile || duration > CONFIG.tapMaxDurationMobile) return;
      clientX = touch.clientX; clientY = touch.clientY;
    } else {
      clientX = event.clientX; clientY = event.clientY;
    }

    closeTooltip();
    const pickInfo = deck.pickObject({ x: clientX, y: clientY, radius: isMobile ? CONFIG.pickingRadiusMobile : CONFIG.pickingRadiusDesktop });
    if (pickInfo && pickInfo.object) showTooltip(pickInfo);
  });
});
</script>"""
    return page.replace(
        '<div id="deck-container">', controls + '<div id="deck-container">'
    ).replace("<title>pydeck</title>", "<title>grid-builder OSM network</title>")


def compress_html(page: str) -> str:
    """Strip whitespace and compact pydeck's embedded JSON to shrink the output."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.DOTALL)

    def minify_css(match: re.Match[str]) -> str:
        css = re.sub(r"\s+", " ", match.group(1))
        css = re.sub(r"\s*([{};:,])\s*", r"\1", css)
        return f"<style>{css.strip()}</style>"

    page = re.sub(r"<style>(.*?)</style>", minify_css, page, flags=re.DOTALL)

    def compress_json_in_script(match: re.Match[str]) -> str:
        script = match.group(0)
        json_match = re.search(r"const jsonInput = (\{.*?\});", script, re.DOTALL)
        if json_match:
            try:
                compact = json.dumps(
                    json.loads(json_match.group(1)), separators=(",", ":")
                )
                script = script.replace(json_match.group(1), compact)
            except json.JSONDecodeError:
                pass
        return script

    # This targets only pydeck's own auto-generated script (the one that
    # starts with its jsonInput blob), never our hand-written controls
    # script below it, which is left readable.
    page = re.sub(
        r"<script>\s*const container = document\.getElementById.*?</script>",
        compress_json_in_script,
        page,
        flags=re.DOTALL,
    )

    parts = re.split(r"(<script>.*?</script>)", page, flags=re.DOTALL)
    for index, part in enumerate(parts):
        if not part.startswith("<script>"):
            part = re.sub(r">\s+<", "><", part)
            part = re.sub(r"\n\s+", "\n", part)
            parts[index] = part
    return "".join(parts)


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("build_interactive_map")
    configure_logging(snakemake.log[0])
    geo_crs = snakemake.params.crs["geo"]
    simplify = snakemake.params.interactive_map["simplify_geometries"]
    deck = build_map(
        *(gpd.read_file(path).to_crs(geo_crs) for path in snakemake.input),
        geo_crs=geo_crs,
        distance_crs=snakemake.params.crs["distance"],
        stations_simplify_m=simplify["stations_m"] if simplify["enable"] else None,
        buses_polygon_simplify_m=simplify["buses_polygon_m"]
        if simplify["enable"]
        else None,
        lines_simplify_m=simplify["lines_m"] if simplify["enable"] else None,
        coord_decimals=snakemake.params.interactive_map["coordinate_decimals"],
    )
    map_html = inject_controls(deck)
    map_html = compress_html(map_html)
    with open(snakemake.output.map, "w", encoding="utf-8") as output:
        output.write(map_html)
