/* FloodLens map UI (Tiers 1-4b). Plain JS, no build step. */
const COLORS = { open: "#1f9d55", blocked: "#d7263d", uncertain: "#f4a261", no_imagery: "#9aa0a6", pending: "#9aa0a6", outside: "#c3c8cc" };
const STATUS_WORD = { open: "Open", blocked: "Underwater", uncertain: "Unsure", no_imagery: "No photo", pending: "Not checked", outside: "Outside photos" };
const FAC_COLORS = { hospital: "#c0392b", clinic: "#e67e22", nursing_home: "#8e44ad", dialysis: "#8e44ad",
  fire_station: "#d35400", police: "#34495e", school: "#2980b9", shelter: "#16a085" };
const IMPACT_COLORS = { none: "#1f9d55", partial: "#f4d03f", surrounded: "#c0392b", hidden: "#9aa0a6", hidden_on_flooded_road: "#e67e22", unknown: "#9aa0a6",
  intact: "#1f9d55", debris_around: "#f4d03f", water_around: "#e67e22", damaged: "#c0392b", destroyed: "#641e16" };
const SITE_COLORS = { recommended: "#1f9d55", ok: "#7dcea0", uncertain: "#f4a261", rejected: "#9aa0a6" };
const MODE_NOTE = {
  permissive: "Roads we have no photo for count as passable but cost more. Use this to plan.",
  strict: "Only roads the model saw dry count. Use this before sending a car.",
};
const state = { area: null, areas: [], mode: "permissive", rank: "priority", cfg: null, segs: null, zones: {}, facilities: null,
  summary: null, routing: false, markers: [], showAllZones: false, legendFilter: null, rf: "blocked", review: [], rvIdx: 0,
  relief: null, impact: null, sites: null, drill: false };

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined || Number.isNaN(n) ? "–" : Number(n).toLocaleString("en-US"));
const usd = (n) => (n === null || n === undefined ? "–" : n >= 1e9 ? `$${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `$${(n / 1e6).toFixed(1)}M` : `$${fmt(Math.round(n))}`);
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const A = (p) => `/api/${state.area}${p}`;
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function busy(on) { $("#busy").classList.toggle("hidden", !on); }
const THEMES = ["auto", "light", "dark"];
function applyTheme(th) {
  if (th === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", th);
  const b = document.getElementById("themeBtn"); if (b) b.textContent = `Theme: ${th}`;
}
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
const empty = () => ({ type: "FeatureCollection", features: [] });

function baseStyle() {
  return { version: 8,
    sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19,
      attribution: "© OpenStreetMap contributors · Imagery NOAA NGS, USDA NAIP" } },
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#d5dde2" } },
             { id: "osm", type: "raster", source: "osm", paint: { "raster-opacity": 0.5, "raster-saturation": -0.35 } }] };
}
const map = new maplibregl.Map({ container: "map", style: baseStyle(), center: [-95.65, 29.8], zoom: 13, maxZoom: 20 });
const before = new maplibregl.Map({ container: "map-before", style: baseStyle(), center: [-95.65, 29.8], zoom: 13, maxZoom: 20, attributionControl: false });
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
// Street basemap is the only map layer that needs the internet. If it fails, hide it quietly:
// the aerial photo tiles and every data layer are served from this machine.
let basemapFailed = 0;
for (const m of [map, before]) m.on("error", (e) => {
  if (e.sourceId === "osm" && ++basemapFailed >= 3 && m.getLayer("osm")) { m.setLayoutProperty("osm", "visibility", "none"); }
});
let syncing = false;
function sync(a, b) { a.on("move", () => { if (syncing) return; syncing = true; b.jumpTo({ center: a.getCenter(), zoom: a.getZoom(), bearing: a.getBearing(), pitch: a.getPitch() }); syncing = false; }); }
sync(map, before); sync(before, map);

// panels float over the map, so frame things inside the visible gap between them
function PAD() {
  if (window.innerWidth <= 900) return { top: 150, left: 20, right: 20, bottom: Math.round(window.innerHeight * 0.45) };
  const cs = getComputedStyle(document.documentElement);
  const px = (v) => parseFloat(cs.getPropertyValue(v)) || 0;
  const gap = px("--gap");
  const left = document.body.classList.contains("left-closed") ? 0 : px("--left-w");
  return { top: px("--top-h") + 150, left: left + 3 * gap, right: px("--right-w") + 3 * gap + 50, bottom: 70 };
}

function bboxOf(geom) {
  let w = 180, s = 90, e = -180, n = -90;
  const walk = (c) => { if (typeof c[0] === "number") { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); } else c.forEach(walk); };
  walk(geom.coordinates);
  return [[w, s], [e, n]];
}

// ------------------------------------------------------------------ boot + areas
async function boot() {
  const a = await api("/api/areas");
  state.areas = a.areas;
  const ready = a.areas.filter((x) => x.ready);
  $("#areaSel").innerHTML = a.areas.map((x) => `<option value="${esc(x.id)}" ${x.ready ? "" : "disabled"}>${esc(x.name)}${!x.ready ? " (not started)" : x.partial ? " (processing, partial)" : ""}</option>`).join("");
  const fromHash = location.hash.slice(1);
  const pick = ready.find((x) => x.id === fromHash) || ready.find((x) => x.id === a.default) || ready[0];
  if (!pick) { $("#ev-title").textContent = "No area is processed yet. Run the pipeline first."; return; }
  addStaticLayers();
  await loadArea(pick.id);
  pills(); setInterval(pills, 15000);
  if (!store.get("floodlens.guide")) $("#guide").classList.remove("hidden");
}

async function loadArea(id) {
  busy(true);
  try {
    state.area = id; location.hash = id; $("#areaSel").value = id;
    state.cfg = await api(A("/config"));
    const info = state.cfg.area;
    $("#event").textContent = `${info.event}${info.after_dates.length ? ` · aerial photos ${info.after_dates.join(", ")}` : ""}`;
    $("#model").textContent = state.cfg.model;
    $("#modeNote").textContent = MODE_NOTE[state.mode];
    $("#compareBtn").classList.toggle("hidden", !state.cfg.paired);
    if (!state.cfg.paired) setCompare(false);
    setImagery();
    const [w, s, e, n] = info.bbox;
    map.fitBounds([[w, s], [e, n]], { padding: PAD(), duration: 0 });
    $("#evidence").classList.add("collapsed"); $("#ev-title").textContent = "Click a road, a neighbourhood, a site or a route to see the evidence";
    map.getSource("route").setData(empty()); map.getSource("naive").setData(empty());
    await refresh(true);
    sources();
  } finally { busy(false); }
}

function setImagery() {
  if (!before.isStyleLoaded()) before.once("load", () => setImagery1(before, "pre", null)); else setImagery1(before, "pre", null);
  setImagery1(map, "post", "zones-fill");
  const cb = document.querySelector('[data-layer="post"]');
  if (cb && !cb.checked && map.getLayer("img")) map.setLayoutProperty("img", "visibility", "none");
}
function setImagery1(m, epoch, below) {
  {
    if (m.getLayer("img")) m.removeLayer("img");
    if (m.getSource("img")) m.removeSource("img");
    const t = state.cfg.tiles?.[epoch];
    if (!t) return;
    m.addSource("img", { type: "raster", tiles: [t.url], tileSize: 256, minzoom: t.minzoom, maxzoom: t.maxzoom });
    m.addLayer({ id: "img", type: "raster", source: "img", paint: { "raster-fade-duration": 0 } }, below && m.getLayer(below) ? below : undefined);
  }
}

function addStaticLayers() {
  const add = (id, spec) => { map.addSource(id, { type: "geojson", data: empty(), ...(spec.promote ? { promoteId: "id" } : {}) }); };
  ["warnings", "buildings", "zones", "impact", "relief", "roads", "naive", "route", "facilities", "reports", "crossings", "sensors"].forEach((id) => add(id, { promote: ["zones", "roads", "relief"].includes(id) }));
  map.addLayer({ id: "warnings", type: "line", source: "warnings", layout: { visibility: "none" }, paint: { "line-color": "#7b2cbf", "line-width": 2, "line-dasharray": [3, 2] } });
  map.addLayer({ id: "buildings", type: "fill", source: "buildings", layout: { visibility: "none" },
    paint: { "fill-color": ["case", ["to-boolean", ["get", "zone_id"]], "#d7263d", "#34495e"], "fill-opacity": 0.35 } });
  map.addLayer({ id: "zones-fill", type: "fill", source: "zones",
    paint: { "fill-color": "#d7263d", "fill-opacity": ["case", ["boolean", ["feature-state", "hl"], false], 0.45, 0.22] } });
  map.addLayer({ id: "zones-line", type: "line", source: "zones", paint: { "line-color": "#d7263d", "line-width": ["case", ["boolean", ["feature-state", "hl"], false], 3.5, 1.8] } });
  map.addLayer({ id: "impact", type: "fill", source: "impact", layout: { visibility: "none" },
    paint: { "fill-color": ["match", ["get", "class"], ...Object.entries(IMPACT_COLORS).flat(), "#9aa0a6"], "fill-opacity": 0.85, "fill-outline-color": "#fff" } });
  map.addLayer({ id: "relief-fill", type: "fill", source: "relief",
    paint: { "fill-color": ["match", ["get", "status"], ...Object.entries(SITE_COLORS).flat(), "#9aa0a6"],
             "fill-opacity": ["match", ["get", "status"], "recommended", 0.55, "rejected", 0.15, 0.3] } });
  map.addLayer({ id: "relief-line", type: "line", source: "relief",
    paint: { "line-color": ["match", ["get", "status"], ...Object.entries(SITE_COLORS).flat(), "#9aa0a6"], "line-width": ["match", ["get", "status"], "recommended", 3, 1.2] } });
  map.addLayer({ id: "roads-casing", type: "line", source: "roads", layout: { "line-cap": "round" }, filter: ["!=", ["get", "status"], "outside"],
    paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 12, 2.5, 16, 7, 19, 13], "line-opacity": 0.85 } });
  map.addLayer({ id: "roads", type: "line", source: "roads", layout: { "line-cap": "round" },
    paint: { "line-color": ["match", ["get", "status"], "open", COLORS.open, "blocked", COLORS.blocked, "uncertain", COLORS.uncertain, "outside", COLORS.outside, COLORS.no_imagery],
             "line-width": ["interpolate", ["linear"], ["zoom"],
               12, ["case", ["boolean", ["feature-state", "hover"], false], 3, 1.5],
               16, ["case", ["boolean", ["feature-state", "hover"], false], 7.5, 4.5],
               19, ["case", ["boolean", ["feature-state", "hover"], false], 14, 9]] } });
  map.addLayer({ id: "roads-dash", type: "line", source: "roads", filter: ["==", ["get", "status"], "blocked"],
    paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.6, 16, 1.6, 19, 3], "line-dasharray": [2, 3] } });
  map.addLayer({ id: "naive", type: "line", source: "naive", paint: { "line-color": "#374151", "line-width": 4, "line-dasharray": [1.5, 1.5], "line-opacity": 0.9 } });
  map.addLayer({ id: "route-casing", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 10 } });
  map.addLayer({ id: "route", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#1e64c8", "line-width": 6 } });
  map.addLayer({ id: "reports", type: "circle", source: "reports", paint: { "circle-radius": 6, "circle-color": "#7b2cbf",
    "circle-stroke-width": 2, "circle-stroke-color": "#fff" } });
  map.addLayer({ id: "crossings", type: "circle", source: "crossings", paint: { "circle-radius": 7, "circle-color": "#0b6e99",
    "circle-stroke-width": 2, "circle-stroke-color": "#fff" } });
  map.addLayer({ id: "sensors", type: "circle", source: "sensors", paint: { "circle-radius": 6, "circle-color": "#f39c12",
    "circle-stroke-width": 2, "circle-stroke-color": "#fff" } });
  map.addLayer({ id: "facilities", type: "circle", source: "facilities", paint: {
    "circle-radius": 8, "circle-color": ["match", ["get", "kind"], ...Object.entries(FAC_COLORS).flat(), "#555"],
    "circle-stroke-width": 3, "circle-stroke-color": ["case", ["==", ["get", "reachable"], false], "#d7263d", "#ffffff"] } });
  wireMap();
}

async function refresh(first = false) {
  busy(true);
  try {
    const [segs, zs, zp, fac, sum] = await Promise.all([
      api(A("/layers/road_segments")), api(A("/layers/cutoff_zones_strict")), api(A("/layers/cutoff_zones_permissive")),
      api(A("/layers/facilities")), api(A("/summary"))]);
    Object.assign(state, { segs, zones: { strict: zs, permissive: zp }, facilities: fac, summary: sum });
    map.getSource("roads").setData(segs); map.getSource("facilities").setData(fac);
    map.getSource("zones").setData(state.zones[state.mode]);
    if (first) {
      const [blds, warn, imp, sites, impact, relief, reports, cross, sens, actx] = await Promise.all([
        api(A("/layers/buildings")), api(A("/layers/context_warnings")), api(A("/layers/impact_buildings")),
        api(A("/layers/relief_sites")), api(A("/impact")), api(A("/relief")), api(A("/layers/context_reports")),
        api(A("/layers/sa_crossings")), api(A("/layers/sa_sensors")), api(A("/area-context"))]);
      map.getSource("reports").setData(reports); map.getSource("crossings").setData(cross); map.getSource("sensors").setData(sens);
      state.actx = actx;
      document.querySelectorAll(".sa-only").forEach((el) => el.classList.toggle("hidden", !cross.features.length));
      map.getSource("buildings").setData(blds); map.getSource("warnings").setData(warn);
      map.getSource("impact").setData(imp); map.getSource("relief").setData(sites);
      Object.assign(state, { impact, relief, sites });
      renderImpact(); renderAreaContextOnly(); renderRelief();
    } else map.getSource("buildings").setData(await api(A("/layers/buildings")));
    renderKpis(); renderLegend(); renderZones(); renderMarkers(); renderFacilities(); await renderReview();
  } finally { busy(false); }
}

// ------------------------------------------------------------------ panels
function renderKpis() {
  const s = state.summary, m = state.mode, seg = s.segments || {};
  const seen = (seg.open || 0) + (seg.blocked || 0) + (seg.uncertain || 0);
  const nZ = (state.zones[m].features || []).length;
  const facs = s.facilities_cut_off || [];
  const dmg = state.impact?.homes?.est_building_damage_usd;
  $("#kpis").innerHTML = `
    ${s.partial ? `<button class="kpi"><div class="v">…</div><div class="l">still processing: photos shown, results soon</div></button>` : ""}
    <button class="kpi alert" data-kpi="zones"><div class="v">~${fmt(s.people_cut_off?.[m])}</div><div class="l">people with no dry road out · ${fmt(nZ)} neighbourhoods</div></button>
    <button class="kpi" data-kpi="blocked"><div class="v">${fmt(seg.blocked)} <span class="m">of ${fmt(seen)}</span></div><div class="l">road pieces underwater</div></button>
    <button class="kpi ${facs.length ? "alert" : ""}" data-kpi="facilities" title="${esc(facs.join(", "))}"><div class="v">${fmt(facs.length)}</div><div class="l">${facs.length ? `facilities cut off, incl. ${esc(facs[0])}` : "facilities cut off"}</div></button>
    <button class="kpi" data-kpi="impact"><div class="v">${dmg?.mid ? usd(dmg.mid) : "–"}</div><div class="l">${dmg?.mid ? `estimated home damage (${usd(dmg.low)} to ${usd(dmg.high)})` : "home damage not estimated yet"}</div></button>`;
}

function renderLegend() {
  const c = {};
  for (const f of state.segs.features) c[f.properties.status] = (c[f.properties.status] || 0) + 1;
  const rows = ["open", "blocked", "uncertain", "no_imagery", "pending"].filter((k) => c[k] || ["open", "blocked"].includes(k));
  $("#legend").innerHTML = rows.map((k) => `<button data-legend="${k}" aria-pressed="${state.legendFilter === k}" title="Show only these roads"
      style="${state.legendFilter && state.legendFilter !== k ? "opacity:.45" : ""}"><span class="sw ${k}"></span>${STATUS_WORD[k]}<b>${fmt(c[k] || 0)}</b></button>`).join("");
}

function sortedZones() {
  const z = [...state.zones[state.mode].features];
  return state.rank === "people" ? z.sort((a, b) => a.properties.people_rank - b.properties.people_rank) : z;
}
function rankOf(p) { return state.rank === "people" ? p.people_rank : p.priority_rank; }

function renderZones() {
  const z = sortedZones();
  const un = state.summary.unverified?.[state.mode];
  const extra = un && un.zones ? `<p class="note">${un.zones} more areas (${fmt(un.buildings)} buildings) are enclosed only by roads we have no photo for. They are not counted.</p>` : "";
  if (!z.length) { $("#zones").innerHTML = `<p class="empty">No neighbourhood is cut off in this view.</p>${extra}`; return; }
  const shown = state.showAllZones ? z : z.slice(0, 8);
  $("#zones").innerHTML = `<h4>Cut-off neighbourhoods, ${state.rank === "people" ? "most people" : "greatest need"} first</h4>` + shown.map((f) => {
    const p = f.properties, first = p.nearest_blocked_segment_ids?.[0], v = p.vulnerability;
    return `<div class="zone" data-zone="${esc(p.id)}" tabindex="0"><div class="rank">${rankOf(p)}</div><div>
      <div class="t">${esc(p.label)}</div>
      <div class="m">~${fmt(Math.round(p.est_pop))} people · ${fmt(p.building_count)} buildings${v ? ` · SVI ${Math.round(v.RPL_THEMES * 100)}th pct` : ""}${p.care_facilities?.length ? " · care home" : ""}</div>
      ${first ? `<div class="m">Fastest fix: <a data-seg="${esc(first)}">clear ${esc(segName(first))}</a></div>` : ""}</div></div>`;
  }).join("") + (z.length > 8 ? `<button class="more" data-allzones>${state.showAllZones ? "Show top 8" : `Show all ${z.length}`}</button>` : "") + extra;
}

function renderMarkers() {
  state.markers.forEach((m) => m.remove()); state.markers = [];
  for (const f of sortedZones().slice(0, 10)) {
    const p = f.properties; if (!p.label_point) continue;
    const el = document.createElement("div");
    el.className = "zmark"; el.textContent = rankOf(p); el.dataset.zone = p.id; el.title = `${p.label}: ~${Math.round(p.est_pop)} people`;
    el.addEventListener("mouseenter", () => highlightZone(p.id, true)); el.addEventListener("mouseleave", () => highlightZone(p.id, false));
    el.addEventListener("click", (e) => { e.stopPropagation(); showZone(p.id); });
    if (!document.querySelector('[data-layer="zones"]').checked) el.style.display = "none";
    state.markers.push(new maplibregl.Marker({ element: el }).setLngLat(p.label_point).addTo(map));
  }
}

function highlightZone(id, on) {
  map.setFeatureState({ source: "zones", id }, { hl: on });
  document.querySelectorAll(`[data-zone="${CSS.escape(id)}"]`).forEach((el) => el.classList.toggle("hl", on));
}

function renderFacilities() {
  const f = state.facilities.features;
  $("#facilities").innerHTML = f.length ? f.map((x) => {
    const p = x.properties, ok = p.reachable !== false;
    return `<div class="fac"><span class="dot" style="background:${FAC_COLORS[p.kind] || "#555"}"></span><div>${esc(p.name)}
      <div class="m">${esc(p.kind.replace("_", " "))}${p.source === "assumed_school" ? " (assumed shelter)" : ""} · ${ok ? "reachable" : "<b style='color:var(--blocked)'>cut off</b>"}</div></div></div>`;
  }).join("") : `<p class="empty">No hospitals, schools or care homes mapped in this area.</p>`;
}

const KIND = { parking: "Parking lot", pitch: "Sports field", park: "Park", school: "School grounds", college: "College grounds",
  university: "University grounds", community_centre: "Community centre", place_of_worship: "Place of worship",
  sports_centre: "Sports centre", recreation_ground: "Recreation ground", playground: "Playground" };
const kindName = (k) => KIND[k] || String(k || "Open area").replace("_", " ");
function renderRelief() {
  const r = state.relief, sites = state.sites?.features || [];
  if (!r) { $("#relief").innerHTML = `<p class="empty">Relief sites are not computed for this area yet.</p>`; return; }
  const rec = sites.filter((f) => f.properties.status === "recommended").sort((a, b) => a.properties.rank - b.properties.rank);
  const order = { ok: 0, uncertain: 1, rejected: 2 };
  const other = sites.filter((f) => f.properties.status !== "recommended")
    .sort((a, b) => (order[a.properties.status] - order[b.properties.status]) || (b.properties.need_within_walk - a.properties.need_within_walk));
  const card = (f) => { const p = f.properties; return `<div class="card site ${p.status}" data-site="${esc(p.id)}">
      <b>${p.rank ? `#${p.rank} · ` : ""}${esc(p.name || kindName(p.kind))}</b> <span class="m">${fmt(p.area_m2)} m²</span>
      <div class="m">${esc(p.use)}${p.status === "recommended" ? ` · serves ~${fmt(p.new_people_served)} more people` : ` · ${esc(p.status)}`}</div></div>`; };
  $("#relief").innerHTML = `<div class="card"><div class="big">${rec.length} site${rec.length === 1 ? "" : "s"}</div>
      <div class="m">cover ~${fmt(r.people_covered)} people within ${fmt(r.walk_m)} m walking (${Math.round((r.need_covered_share || 0) * 100)}% of weighted need). Checked ${r.candidates} open areas: ${r.status.uncertain} unsure, ${r.status.rejected} rejected.</div></div>
    <h4>Recommended</h4>${rec.map(card).join("") || `<p class="empty">No site passed every check.</p>`}
    <h4>Also passed or unsure</h4>${other.filter((f) => f.properties.status !== "rejected").map(card).join("") || `<p class="empty">None.</p>`}
    <details class="rej"><summary>Rejected (${other.filter((f) => f.properties.status === "rejected").length}), with reasons</summary>
      ${other.filter((f) => f.properties.status === "rejected").slice(0, 30).map(card).join("")}</details>`;
}

const IMPACT_WORD = { none: "no new water", partial: "some water nearby", surrounded: "surrounded by water", hidden: "hidden by trees",
  hidden_on_flooded_road: "hidden, on a flooded road", intact: "intact", debris_around: "debris around", water_around: "water around",
  damaged: "damaged", destroyed: "destroyed", unknown: "unknown" };
function renderImpact() {
  const im = state.impact;
  if (!im) { $("#impact").innerHTML = `<p class="empty">Impact is not estimated for this area yet.</p>`; return; }
  const h = im.homes, d = h.est_building_damage_usd, cl = h.classes || {}, ins = im.insured_claims, fm = im.farmland;
  const rg = h.damage_ratios?.class_ranges || {};
  const pct = (x) => (x === undefined ? "–" : `${Math.round(x * 100)}%`);
  const classRows = Object.entries(cl).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<tr><td><span class="dot" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${IMPACT_COLORS[k] || "#999"}"></span> ${esc(IMPACT_WORD[k] || k)}</td><td>${v}</td><td>${rg[k] ? `${pct(rg[k].low)}–${pct(rg[k].high)}` : "not counted"}</td></tr>`).join("");
  $("#impact").innerHTML = `
    <div class="card"><div class="big">${usd(d.mid)}</div><div class="m">estimated damage to homes, range ${usd(d.low)} to ${usd(d.high)}</div></div>
    <div class="card"><div class="big">~${fmt(h.est_homes_surrounded_or_worse)}</div><div class="m">of ${fmt(h.residential_buildings)} homes surrounded by floodwater${h.est_homes_hidden_on_flooded_road ? `, plus ~${fmt(h.est_homes_hidden_on_flooded_road)} hidden by trees on flooded roads` : ""}</div></div>
    <div class="card"><div class="big">${usd(h.market_discount_usd.low)} to ${usd(h.market_discount_usd.high)}</div><div class="m">possible drop in market value of the surrounded homes (3.1 to 5.5% floodplain discount after Harvey, Freddie Mac)</div></div>
    ${fm.fields_checked ? `<div class="card"><div class="big">${fmt(fm.acres_flooded)} acres</div><div class="m">of ${fmt(fm.acres_checked)} acres of farmland and pasture under water${fm.acres_with_deposit_or_debris ? `, ${fmt(fm.acres_with_deposit_or_debris)} acres with sand, mud or debris` : ""}</div></div>` : ""}
    <h4>How we got this</h4>
    <table class="kv"><tr><td>What the model saw</td><td>Homes</td><td>Damage ratio</td></tr>${classRows}</table>
    <p class="note">${esc(im.model)} checked a random sample of ${h.sampled} homes on before and after crops. Damage ratios: ${esc(h.damage_ratios?.source)}. Values: ${esc(h.home_value_source)}. Totals scale the sample to all ${fmt(h.residential_buildings)} homes in the photos.</p>
    ${ins.n_claims ? `<p class="note">Check: ${fmt(ins.n_claims)} NFIP flood insurance claims in these counties during the event reported ${usd(ins.building_damage_usd)} of building damage (${esc(ins.scope)}).${ins.tract_spearman_vs_estimate !== null ? ` Rank correlation with our estimate across ${ins.tracts_compared} tracts: ${ins.tract_spearman_vs_estimate}.` : ""}</p>` : ""}
    <p class="note"><b>Estimate, not an appraisal.</b> Use it to compare areas, not to settle claims.</p>${areaContextHtml()}`;
}
function areaContextHtml() {
  const c = state.actx || {}, g = c.gauges?.gauges || [], cty = c.county?.counties || [];
  let h = "";
  if (cty.length) h += `<h4>County flood insurance gap</h4>` + cty.map((r) => `<div class="card"><b>${esc(r.county)} County</b>
    <div class="m">${fmt(r.residential_structures_in_sfha)} homes in FEMA flood zones (${r.share_in_sfha_pct}%), only ${r.nfip_takeup_in_sfha_pct}% of them insured by NFIP</div></div>`).join("");
  if (g.length) h += `<h4>River gauges during the event</h4><table class="kv"><tr><td>Gauge</td><td>Peak vs flood stage</td></tr>` +
    g.map((x) => `<tr><td>${esc(x.name)}</td><td>${x.peak_stage} ${esc(x.unit || "")}${x.flood_stage ? ` vs ${x.flood_stage}` : ""}${x.above_flood_stage_on_photo_day ? " · <b>above flood stage on photo day</b>" : ""}</td></tr>`).join("") + `</table>`;
  return h;
}
function renderAreaContextOnly() { if (!state.impact) $("#impact").innerHTML = `<p class="empty">Impact is not estimated for this area.</p>${areaContextHtml()}`; }

async function renderReview() {
  state.review = await api(A(`/review?status=${state.rf}&limit=80`));
  const unsure = await api(A(`/review?status=uncertain&limit=500`));
  $("#nreview").textContent = unsure.length || "";
  state.rvIdx = Math.min(state.rvIdx, Math.max(0, state.review.length - 1));
  $("#review").innerHTML = state.review.length ? state.review.map((p, i) => `<div class="rv ${i === state.rvIdx ? "cur" : ""}" data-rv="${i}">
      <img src="${esc(p.chip_url)}" data-seg="${esc(p.id)}" alt="After photo of ${esc(p.name || "road")}">
      <div><b>${esc(p.name || "Unnamed road")}</b> <span class="st ${p.status}">${STATUS_WORD[p.status]}</span>
      <div class="m">${esc(p.label?.reason || "")}</div>
      <div class="btns"><button class="b-open" data-set="open" data-id="${esc(p.id)}">Open</button>
      <button class="b-blocked" data-set="blocked" data-id="${esc(p.id)}">Underwater</button></div></div></div>`).join("")
    : `<p class="empty">Nothing here. Try another filter.</p>`;
}

const segById = (id) => state.segs.features.find((f) => f.properties.id === id);
const segName = (id) => segById(id)?.properties.name || "unnamed road";

function compareWidget(pre, postUrl, preLabel, postLabel) {
  if (!pre) return `<div><img src="${postUrl}" style="width:230px;height:230px;border-radius:6px" alt="after photo"><div class="cap">${esc(postLabel)} (no before photo exists here)</div></div>`;
  return `<div><div class="compare" data-compare>
      <img src="${pre}" alt="before photo"><img class="after" src="${postUrl}" alt="after photo">
      <div class="bar"></div><span class="lab l">${esc(preLabel)}</span><span class="lab r">${esc(postLabel)}</span>
      <input type="range" min="0" max="100" value="50" aria-label="Swipe between before and after"></div>
    <div class="cap">Drag to compare. Magenta marks what is being judged.</div></div>`;
}
function wireCompare() {
  document.querySelectorAll("[data-compare]").forEach((box) => {
    const input = box.querySelector("input"), after = box.querySelector(".after"), bar = box.querySelector(".bar");
    const set = (v) => { after.style.clipPath = `inset(0 0 0 ${v}%)`; bar.style.left = `${v}%`; };
    input.addEventListener("input", () => set(input.value)); set(50);
  });
}
const badge = (meta) => `<span class="b ${esc(meta?.status || "unavailable")}">${esc(meta?.status || "–")}</span>`;

async function showSegment(id) {
  const f = segById(id); if (!f) return;
  const p = f.properties, l = p.label || {};
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.remove("one");
  $("#ev-title").innerHTML = `${esc(p.name || "Unnamed road")} <span class="st ${p.status}">${STATUS_WORD[p.status]}</span>`;
  const base = `/chips/${state.area}/roads/${p.id}`;
  const left = p.chip_url ? compareWidget(state.cfg.paired ? `${base}_pre.jpg` : null, `${base}_post.jpg`, "before", p.after_date || "after")
    : `<p class="empty">No photo covers this road, so the model could not judge it.</p>`;
  const pair = l.pair, surf = l.surface;
  const rows = [
    ...(pair ? [["Before vs after", `${pair.answer === "yes" ? "new water on road" : pair.answer === "no" ? "no new water" : "unsure"} (${pair.confidence})`]] : []),
    ...(surf ? [["After photo alone", `${(surf.surface || "").replace("_", " ")} (${surf.confidence})`]] : []),
    ["Decision rule", l.rule || "–"],
    ["Decided by", p.status_source === "human" ? `a person${p.reviewer ? ` (${p.reviewer})` : ""}` : p.status_source === "vlm" ? `model ${p.model || ""}` : "–"],
    ["Road", `${p.highway || ""} · ${p.length_m} m`]];
  $("#ev-body").innerHTML = `${left}<div>${l.reason ? `<p class="reason">“${esc(l.reason)}”</p>` : ""}
    <table class="kv">${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v ?? "–")}</td></tr>`).join("")}</table>
    <div class="ev-actions"><span class="m">Is this right?</span>
      <button class="b-open" data-set="open" data-id="${esc(p.id)}">It's open</button>
      <button class="b-blocked" data-set="blocked" data-id="${esc(p.id)}">It's underwater</button></div></div>
    <div id="ctx" class="ctx"><h5>Context</h5><span class="m">loading…</span></div>`;
  wireCompare();
  const [[w, s], [e, n]] = bboxOf(f.geometry), c = [(w + e) / 2, (s + n) / 2];
  if (!map.getBounds().contains(c)) map.easeTo({ center: c, zoom: Math.max(map.getZoom(), 16.5) });
  try {
    const { tile: t, public: pub } = await api(A(`/context/${encodeURIComponent(id)}`));
    let fema = "–"; try { fema = t?.fema_flood_zones ? JSON.parse(t.fema_flood_zones).join(", ") : "–"; } catch { fema = t?.fema_flood_zones; }
    const warn = pub?.nws_warnings, svi = pub?.svi, el = pub?.elevation;
    $("#ctx").innerHTML = `<h5>Context for this place</h5><table class="kv">
      ${t ? `<tr><td>FEMA zone</td><td>${esc(fema)}${t.fema_sfha_tf === "T" ? " · 100-year floodplain" : ""}</td></tr>
      <tr><td>Rain, 3 days before</td><td>${esc(t.preceding_3d_precipitation_mm ?? "–")} mm</td></tr>
      <tr><td>River gauge</td><td>${esc(t.usgs_gage_height_station_name ?? "–")}</td></tr>` : ""}
      <tr><td>NWS warning ${badge(warn?.meta)}</td><td>${warn?.data?.length ? esc(warn.data.map((x) => `${x.type} ${x.sig === "W" ? "Warning" : x.sig}`).join(", ")) : warn?.data ? "none active that day" : "–"}</td></tr>
      <tr><td>Tract SVI ${badge(svi?.meta)}</td><td>${svi?.data ? `${Math.round((svi.data.RPL_THEMES || 0) * 100)}th percentile · 65+ ${svi.data.EP_AGE65}% · no car ${svi.data.EP_NOVEH}%` : "–"}</td></tr>
      <tr><td>Elevation ${badge(el?.meta)}</td><td>${el?.data ? `${el.data.elevation_m} m (${el.data.relative_m >= 0 ? "+" : ""}${el.data.relative_m} m vs 300 m around)` : "–"}</td></tr></table>
      <p class="note">Station readings and tract figures are context near this road, not measurements on it.</p>`;
  } catch { $("#ctx").innerHTML = `<h5>Context</h5><span class="m">Not available.</span>`; }
}

async function setStatus(id, status) {
  busy(true);
  try { await post(A(`/review/${encodeURIComponent(id)}`), { status, reviewer: ($("#reviewer").value || store.get("floodlens.reviewer") || "").trim() || null }); }
  finally { busy(false); }
  await refresh();
  showSegment(id);
}

async function routeFrom(lngLat) {
  state.routing = false; $("#hint").classList.add("hidden"); map.getCanvas().style.cursor = "";
  const btn = $("#routeBtn"); btn.disabled = true; btn.textContent = "Finding the safest way out…";
  let r;
  try { r = await post(A("/route"), { lat: lngLat.lat, lon: lngLat.lng, mode: state.mode }); }
  finally { btn.disabled = false; btn.textContent = "Route me out"; }
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.add("one");
  const naive = r.naive && r.naive.geometry.coordinates.length > 1 ? r.naive : null;
  map.getSource("naive").setData(naive ? { type: "FeatureCollection", features: [naive] } : empty());
  if (!r.route) {
    map.getSource("route").setData(empty());
    $("#ev-title").textContent = "No dry road out from here";
    $("#ev-body").innerHTML = `<div><div class="callout"><b>Stay where you are and call for rescue.</b> No passable road reaches a shelter, a hospital or the edge of the photographed area${r.zone_id ? `. This point is in cut-off neighbourhood <b>${esc(r.zone_id)}</b>` : ""}. Turn around, don't drown.</div></div>`;
    return;
  }
  map.getSource("route").setData(r.route);
  map.fitBounds(bboxOf(r.route.geometry), { padding: PAD(), maxZoom: 17 });
  const nb = naive ? naive.properties.blocked_edge_ids.length : 0;
  $("#ev-title").textContent = `Safest way out: ${r.target.name} · ${fmt(r.length_m)} m`;
  $("#ev-body").innerHTML = `<div><div><span class="lg"><i></i>Safe route</span>${naive ? `<span class="lg"><i class="naive"></i>Usual shortest route</span>` : ""}</div>
    ${nb ? `<div class="callout">The usual shortest route (${fmt(naive.properties.length_m)} m) drives through <b>${nb} flooded road${nb > 1 ? "s" : ""}</b>. This route avoids them.</div>`
         : `<div class="callout ok">The usual shortest route is also dry here.</div>`}
    <ol class="steps">${r.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
    ${r.uncertain_edge_ids.length ? `<p class="m">Uses ${r.uncertain_edge_ids.length} road piece(s) we have no clear photo of. Drive slowly and turn around at any water.</p>` : ""}</div>`;
}

function showZone(id) {
  const f = state.zones[state.mode].features.find((z) => z.properties.id === id); if (!f) return;
  map.fitBounds(bboxOf(f.geometry), { padding: PAD(), maxZoom: 17 });
  const p = f.properties, v = p.vulnerability;
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.add("one");
  $("#ev-title").innerHTML = `<span class="zmark" style="display:inline-grid;width:22px;height:22px;font-size:12px">${rankOf(p)}</span> ${esc(p.label)}`;
  const blocked = (p.nearest_blocked_segment_ids || []).slice(0, 6);
  $("#ev-body").innerHTML = `<div><p>About <b>${fmt(Math.round(p.est_pop))} people</b> in ${fmt(p.building_count)} buildings have no ${state.mode === "strict" ? "confirmed-dry" : "passable"} road out.</p>
    ${v ? `<p class="m">Census tract vulnerability (SVI): ${Math.round(v.RPL_THEMES * 100)}th percentile. Age 65+: ${v.EP_AGE65}%. No vehicle: ${v.EP_NOVEH}%. Disability: ${v.EP_DISABL}%. Limited English: ${v.EP_LIMENG}%.${p.care_facilities?.length ? ` Care facility inside: ${esc(p.care_facilities.join(", "))}.` : ""}</p>` : ""}
    ${blocked.length ? `<p>Flooded roads on the edge, shortest first. Clearing the first one opens a way out fastest:</p>
    <ol class="steps">${blocked.map((s) => { const g = segById(s)?.properties; return `<li><a class="lnk" data-seg="${esc(s)}">${esc(g?.name || "unnamed road")}</a> · ${g?.length_m ?? "?"} m</li>`; }).join("")}</ol>` : ""}
    <div class="row"><b>Dispatch brief:</b><button data-brief="en" data-zid="${esc(p.id)}">Write in English</button><button data-brief="es" data-zid="${esc(p.id)}">Escribir en español</button></div>
    <div id="briefOut"></div></div>`;
}

async function brief(zid, lang) {
  $("#briefOut").innerHTML = `<p class="m">The model is writing a draft…</p>`;
  try {
    const b = await post(A("/brief"), { zone_id: zid, mode: state.mode, lang, prefer_cache: state.drill });
    $("#briefOut").innerHTML = `<div class="brief"><div class="m">${esc(b.label)} · ${esc(b.model)}${b.from_cache ? " · <b>saved copy</b> (model not reached)" : ""}</div><p>${b.brief_html}</p>
      <div class="alert">${b.alert_html}</div><div class="m">${b.alert_chars} characters${b.unverified_numbers.length ? ` · <span style="color:var(--blocked)">${b.unverified_numbers.length} number(s) not found in the data are struck through</span>` : " · every number checked against the data"}</div></div>`;
  } catch (e) { $("#briefOut").innerHTML = `<p class="m">Could not reach the model: ${esc(e.message)}</p>`; }
}

function showSite(id) {
  const f = state.sites?.features.find((x) => x.properties.id === id); if (!f) return;
  const p = f.properties, l = p.label || {};
  map.fitBounds(bboxOf(f.geometry), { padding: PAD(), maxZoom: 18 });
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.remove("one");
  $("#ev-title").innerHTML = `${esc(p.name || kindName(p.kind))} <span class="st ${p.status === "recommended" || p.status === "ok" ? "open" : p.status === "uncertain" ? "uncertain" : "pending"}">${esc(p.status)}</span>`;
  $("#ev-body").innerHTML = `${p.chip ? compareWidget(p.chip_pre, p.chip, "before", "after") : `<p class="empty">No photo.</p>`}
    <div><p class="reason">${esc(p.use)}</p>${l.reason ? `<p>“${esc(l.reason)}”</p>` : ""}
    <ul class="checks">${(p.checks || []).map((c) => `<li><span class="ck ${c.result}">${c.result === "pass" ? "✓" : c.result === "fail" ? "✕" : "?"}</span> ${esc(c.name)} <span class="m">${esc(c.detail || "")}</span></li>`).join("")}</ul></div>
    <div class="ctx"><h5>Need nearby</h5><table class="kv"><tr><td>Area</td><td>${fmt(p.area_m2)} m² · ${esc(p.surface)}</td></tr>
      <tr><td>People within 1.5 km walk</td><td>~${fmt(p.people_within_walk)}</td></tr>
      ${p.new_people_served ? `<tr><td>Not covered by a higher-ranked site</td><td>~${fmt(p.new_people_served)}</td></tr>` : ""}
      <tr><td>Changed since before</td><td>${esc(l.change || "–")}</td></tr></table></div>`;
  wireCompare();
}

function wireMap() {
  let hover = null;
  map.on("mousemove", "roads", (e) => {
    map.getCanvas().style.cursor = state.routing ? "crosshair" : "pointer";
    if (hover !== null) map.setFeatureState({ source: "roads", id: hover }, { hover: false });
    hover = e.features[0].id; map.setFeatureState({ source: "roads", id: hover }, { hover: true });
  });
  map.on("mouseleave", "roads", () => { if (hover !== null) map.setFeatureState({ source: "roads", id: hover }, { hover: false }); hover = null; map.getCanvas().style.cursor = state.routing ? "crosshair" : ""; });
  let zHover = null;
  map.on("mousemove", "zones-fill", (e) => { const id = e.features[0].id; if (zHover !== id) { if (zHover) highlightZone(zHover, false); zHover = id; highlightZone(id, true); } });
  map.on("mouseleave", "zones-fill", () => { if (zHover) highlightZone(zHover, false); zHover = null; });
  map.on("click", (e) => {
    if (state.routing) return routeFrom(e.lngLat);
    const layers = ["reports", "crossings", "sensors", "roads", "relief-fill", "impact", "facilities", "zones-fill"].filter((l) => map.getLayer(l) && map.getLayoutProperty(l, "visibility") !== "none");
    const hit = map.queryRenderedFeatures(e.point, { layers });
    const pt = hit.find((h) => ["reports", "crossings", "sensors"].includes(h.layer.id));
    if (pt) return showPoint(pt.layer.id, pt.properties);
    const road = hit.find((h) => h.layer.id === "roads");
    if (road && road.properties.status !== "outside") return showSegment(road.properties.id);
    const site = hit.find((h) => h.layer.id === "relief-fill");
    if (site) return showSite(site.properties.id);
    const home = hit.find((h) => h.layer.id === "impact");
    if (home) return showHome(home.properties);
    const z = hit.find((h) => h.layer.id === "zones-fill");
    if (z) showZone(z.properties.id);
  });
}

function showPoint(kind, p) {
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.add("one");
  if (kind === "reports") {
    $("#ev-title").textContent = `NOAA storm report: ${p.type} · ${p.date}`;
    $("#ev-body").innerHTML = `<div><p>${esc(p.place || "")}${p.county ? `, ${esc(p.county)} County` : ""}</p>
      <p class="reason">${esc(p.narrative || "No narrative.")}</p><p class="m">Deaths ${fmt(Number(p.deaths))} · reported property damage ${usd(Number(p.damage_usd))}. Source: NOAA NCEI Storm Events.</p></div>`;
  } else if (kind === "crossings") {
    $("#ev-title").textContent = `Low-water crossing: ${p.name || ""}`;
    $("#ev-body").innerHTML = `<div><p>In the heavy-rain scenario this crossing is closed. Neighbourhoods that can only leave across it show as cut off.</p><p class="m">City of San Antonio low-water crossing ${esc(p.id)}.</p></div>`;
  } else {
    $("#ev-title").textContent = `SmartSA flood sensor (${p.zone})`;
    $("#ev-body").innerHTML = `<div><table class="kv"><tr><td>Readings</td><td>${fmt(Number(p.readings))} from ${esc(String(p.first).slice(0, 10))} to ${esc(String(p.last).slice(0, 10))}</td></tr>
      <tr><td>Alerts triggered</td><td>${fmt(Number(p.alerts))}</td></tr><tr><td>Largest water rise</td><td>${p.max_rise_m ?? "–"} m</td></tr>
      <tr><td>Last status</td><td>${esc(p.status_last)}</td></tr></table></div>`;
  }
}

function showHome(p) {
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.remove("one");
  $("#ev-title").textContent = `Home check: ${IMPACT_WORD[p.class] || p.class}`;
  $("#ev-body").innerHTML = `${compareWidget(p.chip_pre && p.chip_pre !== "null" ? p.chip_pre : null, p.chip, "before", "after")}
    <div><p class="reason">“${esc(p.reason)}”</p><table class="kv"><tr><td>Tract median value (2016)</td><td>${usd(Number(p.value_usd))}</td></tr>
    <tr><td>Water in crop, before → after</td><td>${p.water_before ?? "–"}% → ${p.water_after ?? "–"}%</td></tr>
    <tr><td>Estimated damage</td><td>${p.damage_usd && p.damage_usd !== "null" ? `${usd(Number(p.damage_low))} to ${usd(Number(p.damage_high))}` : "not counted"}</td></tr></table><p class="note">Part of a random sample used to estimate the whole area.</p></div>`;
  wireCompare();
}

// ------------------------------------------------------------------ compare swipe
function setCompare(on) {
  $("#mapwrap").classList.toggle("comparing", on);
  $("#swipe").classList.toggle("hidden", !on);
  $("#compareBtn").setAttribute("aria-pressed", String(on));
  if (on) { before.resize(); placeSwipe($("#mapwrap").clientWidth / 2); } else $("#map").style.clipPath = "";
}
function placeSwipe(x) {
  const w = $("#mapwrap").clientWidth; x = Math.max(0, Math.min(w, x));
  $("#swipe").style.left = `${x}px`; $("#map").style.clipPath = `inset(0 0 0 ${x}px)`;
}
(() => {
  const handle = $("#swipe .handle"); let drag = false;
  handle.addEventListener("pointerdown", (e) => { drag = true; handle.setPointerCapture(e.pointerId); });
  handle.addEventListener("pointermove", (e) => { if (drag) placeSwipe(e.clientX - $("#mapwrap").getBoundingClientRect().left); });
  handle.addEventListener("pointerup", () => { drag = false; });
})();

// ------------------------------------------------------------------ events
document.addEventListener("click", (e) => {
  const t = e.target.closest("button, a, img, .zone, .rv, .site") || e.target;
  if (t.dataset.set) return setStatus(t.dataset.id, t.dataset.set);
  if (t.dataset.seg) return showSegment(t.dataset.seg);
  if (t.dataset.brief) return brief(t.dataset.zid, t.dataset.brief);
  if (t.dataset.site) return showSite(t.dataset.site);
  if (t.hasAttribute?.("data-allzones")) { state.showAllZones = !state.showAllZones; return renderZones(); }
  if (t.dataset.zone) return showZone(t.dataset.zone);
  if (t.dataset.rv) { state.rvIdx = Number(t.dataset.rv); renderReviewCursor(); return showSegment(state.review[state.rvIdx].id); }
  if (t.dataset.kpi) {
    if (t.dataset.kpi === "blocked") { state.legendFilter = state.legendFilter === "blocked" ? null : "blocked"; applyLegendFilter(); }
    else switchTab(t.dataset.kpi === "impact" ? "impact" : "zones");
    return;
  }
  if (t.dataset.legend) { state.legendFilter = state.legendFilter === t.dataset.legend ? null : t.dataset.legend; return applyLegendFilter(); }
  if (t.dataset.tab) return switchTab(t.dataset.tab);
  if (t.dataset.rank) {
    state.rank = t.dataset.rank; document.querySelectorAll("[data-rank]").forEach((b) => b.classList.toggle("on", b === t));
    renderZones(); renderMarkers(); return;
  }
  if (t.dataset.rf) { state.rf = t.dataset.rf; state.rvIdx = 0; document.querySelectorAll("[data-rf]").forEach((b) => b.classList.toggle("on", b === t)); return renderReview(); }
  if (t.dataset.mode) {
    state.mode = t.dataset.mode; $("#modeNote").textContent = MODE_NOTE[state.mode];
    document.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b === t));
    map.getSource("zones").setData(state.zones[state.mode]); renderKpis(); renderZones(); renderMarkers();
  }
});
document.addEventListener("mouseover", (e) => { const z = e.target.closest?.(".zone"); if (z) highlightZone(z.dataset.zone, true); });
document.addEventListener("mouseout", (e) => { const z = e.target.closest?.(".zone"); if (z) highlightZone(z.dataset.zone, false); });

function switchTab(tab) {
  document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  for (const t of ["zones", "relief", "impact", "review"]) $(`#tab-${t}`).classList.toggle("hidden", tab !== t);
  const imp = document.querySelector('[data-layer="impact"]');
  if (tab === "impact" && !imp.checked) { imp.checked = true; imp.dispatchEvent(new Event("change")); }
}
function applyLegendFilter() {
  const base = ["!=", ["get", "status"], "outside"];
  map.setFilter("roads", state.legendFilter ? ["==", ["get", "status"], state.legendFilter] : null);
  map.setFilter("roads-casing", state.legendFilter ? ["==", ["get", "status"], state.legendFilter] : base);
  renderLegend();
}
function renderReviewCursor() { document.querySelectorAll(".rv").forEach((el, i) => el.classList.toggle("cur", i === state.rvIdx)); document.querySelector(".rv.cur")?.scrollIntoView({ block: "nearest" }); }

document.addEventListener("keydown", (e) => {
  if ($("#tab-review").classList.contains("hidden") || !state.review.length || e.target.closest("input, textarea, select")) return;
  const cur = state.review[state.rvIdx];
  if (e.key === "o" || e.key === "O") setStatus(cur.id, "open");
  else if (e.key === "b" || e.key === "B") setStatus(cur.id, "blocked");
  else if (e.key === "ArrowRight" || e.key === "ArrowDown") { state.rvIdx = Math.min(state.rvIdx + 1, state.review.length - 1); renderReviewCursor(); showSegment(state.review[state.rvIdx].id); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { state.rvIdx = Math.max(state.rvIdx - 1, 0); renderReviewCursor(); showSegment(state.review[state.rvIdx].id); }
  else return;
  e.preventDefault();
});
document.querySelectorAll("[data-layer]").forEach((cb) => cb.addEventListener("change", () => {
  const vis = cb.checked ? "visible" : "none";
  const ids = { post: ["img"], roads: ["roads", "roads-casing", "roads-dash"], zones: ["zones-fill", "zones-line"], facilities: ["facilities"],
    buildings: ["buildings"], relief: ["relief-fill", "relief-line"], impact: ["impact"], warnings: ["warnings"],
    reports: ["reports"], crossings: ["crossings"], sensors: ["sensors"] }[cb.dataset.layer];
  ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", vis));
  if (cb.dataset.layer === "zones") state.markers.forEach((m) => (m.getElement().style.display = cb.checked ? "" : "none"));
}));
$("#areaSel").addEventListener("change", (e) => loadArea(e.target.value));
applyTheme(store.get("floodlens.theme") || "auto");
$("#themeBtn").addEventListener("click", () => {
  const cur = store.get("floodlens.theme") || "auto", next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  store.set("floodlens.theme", next); applyTheme(next);
});
$("#reviewer").value = store.get("floodlens.reviewer") || "";
$("#reviewer").addEventListener("change", (e) => store.set("floodlens.reviewer", e.target.value.trim()));
$("#routeBtn").addEventListener("click", () => {
  state.routing = true; map.getCanvas().style.cursor = "crosshair";
  $("#hint").textContent = "Click the map where you are"; $("#hint").classList.remove("hidden");
});
$("#compareBtn").addEventListener("click", () => setCompare(!$("#mapwrap").classList.contains("comparing")));
$("#ev-close").addEventListener("click", () => $("#evidence").classList.add("collapsed"));
document.querySelector('[data-collapse="left"]').addEventListener("click", () => {
  document.body.classList.add("left-closed"); document.querySelector('[data-reopen="left"]').classList.remove("hidden"); map.resize();
});
document.querySelector('[data-reopen="left"]').addEventListener("click", (e) => {
  document.body.classList.remove("left-closed"); e.currentTarget.classList.add("hidden"); map.resize();
});
document.querySelector(".ev-head").addEventListener("click", (e) => { if (e.target.id !== "ev-close") $("#evidence").classList.remove("collapsed"); });
$("#guideOk").addEventListener("click", () => { $("#guide").classList.add("hidden"); store.set("floodlens.guide", "1"); });
$("#drillBtn").addEventListener("click", async () => {
  state.drill = !state.drill;
  await post("/api/admin/fault", { source: "all", mode: state.drill ? "down" : "none" });
  $("#drillBtn").setAttribute("aria-pressed", String(state.drill));
  $("#drillBtn").textContent = state.drill ? "Offline drill on: using saved data" : "Offline drill";
  sources();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { state.routing = false; $("#hint").classList.add("hidden"); $("#guide").classList.add("hidden"); } });

async function sources() {
  if (!state.area) return;
  try {
    const list = await api(A("/sources"));
    const names = { svi: "Census vulnerability (SVI)", nws_history: "NWS warnings archive", elevation: "USGS elevation", nws_live: "NWS alerts, live" };
    $("#sources").innerHTML = list.map((x) => `<div title="${esc(x.error || "")}"><span>${esc(names[x.name] || x.name)}</span>${badge(x)}</div>`).join("");
  } catch { $("#sources").innerHTML = `<span class="m">unavailable</span>`; }
}
async function pills() {
  try {
    const h = await api("/api/health");
    $("#pills").innerHTML = `<span class="pill live">App running</span><span class="pill ${h.vlm_mock ? "cached" : "live"}">Model: ${h.vlm_mock ? "mock" : esc(h.model)}</span>`;
  } catch { $("#pills").innerHTML = `<span class="pill down">App unreachable</span>`; }
  sources();
}

map.on("load", () => boot().catch((err) => { console.error(err); $("#ev-title").textContent = "Could not load data: " + err.message; }));
