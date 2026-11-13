/* FloodLens map UI (Tier 1-3b). Plain JS, no build step. */
const COLORS = { open: "#1f9d55", blocked: "#d7263d", uncertain: "#f4a261", no_imagery: "#9aa0a6", pending: "#9aa0a6" };
const STATUS_WORD = { open: "Open", blocked: "Underwater", uncertain: "Unsure", no_imagery: "No photo", pending: "Not checked" };
const FAC_COLORS = { hospital: "#c0392b", clinic: "#e67e22", nursing_home: "#8e44ad", dialysis: "#8e44ad",
  fire_station: "#d35400", police: "#34495e", school: "#2980b9", shelter: "#16a085" };
const MODE_NOTE = {
  permissive: "Roads we have no photo for count as passable but cost more. Use this to plan.",
  strict: "Only roads the model saw dry count. Use this before sending a car.",
};
const state = { mode: "permissive", cfg: null, segs: null, zones: {}, facilities: null, summary: null,
  routing: false, markers: [], showAllZones: false, legendFilter: null, rf: "blocked", review: [], rvIdx: 0 };

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function busy(on) { $("#busy").classList.toggle("hidden", !on); }
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };

function baseStyle() {
  return { version: 8,
    sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19,
      attribution: "© OpenStreetMap contributors · Imagery NOAA NGS, USDA NAIP" } },
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#dfe5e8" } },
             { id: "osm", type: "raster", source: "osm", paint: { "raster-opacity": 0.55 } }] };
}
const map = new maplibregl.Map({ container: "map", style: baseStyle(), center: [-95.655, 29.833], zoom: 14, maxZoom: 20 });
const before = new maplibregl.Map({ container: "map-before", style: baseStyle(), center: [-95.655, 29.833], zoom: 14, maxZoom: 20,
  interactive: true, attributionControl: false });
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

// keep both maps on the same camera
let syncing = false;
function sync(a, b) { a.on("move", () => { if (syncing) return; syncing = true; b.jumpTo({ center: a.getCenter(), zoom: a.getZoom(), bearing: a.getBearing(), pitch: a.getPitch() }); syncing = false; }); }
sync(map, before); sync(before, map);

function bboxOf(geom) {
  let w = 180, s = 90, e = -180, n = -90;
  const walk = (c) => { if (typeof c[0] === "number") { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); } else c.forEach(walk); };
  walk(geom.coordinates);
  return [[w, s], [e, n]];
}

async function loadAll() {
  state.cfg = await api("/api/config");
  $("#event").textContent = `${state.cfg.event.name} · aerial photos of 30 Aug 2017`;
  $("#model").textContent = state.cfg.model;
  $("#modeNote").textContent = MODE_NOTE[state.mode];
  const [w, s, e, n] = state.cfg.bbox;
  map.fitBounds([[w, s], [e, n]], { padding: 24, duration: 0 });
  addOverlays();
  await refresh(true);
  pills(); setInterval(pills, 10000);
  if (!store.get("floodlens.guide")) $("#guide").classList.remove("hidden");
}

function addOverlays() {
  const add = (m, epoch, vis) => (state.cfg.overlays[epoch] || []).forEach((o) => {
    const id = `${epoch}-${o.id}`;
    m.addSource(id, { type: "image", url: o.url, coordinates: o.coordinates });
    m.addLayer({ id, type: "raster", source: id, layout: { visibility: vis }, paint: { "raster-fade-duration": 0 } });
  });
  add(map, "post", "visible");
  const go = () => add(before, "pre", "visible");
  before.loaded() ? go() : before.on("load", go);
}

async function refresh(first = false) {
  busy(true);
  try {
    const [segs, zs, zp, fac, sum] = await Promise.all([
      api("/api/layers/road_segments"), api("/api/layers/cutoff_zones_strict"),
      api("/api/layers/cutoff_zones_permissive"), api("/api/layers/facilities"), api("/api/summary")]);
    Object.assign(state, { segs, zones: { strict: zs, permissive: zp }, facilities: fac, summary: sum });
    if (first) await addLayers(); else {
      map.getSource("roads").setData(segs); map.getSource("facilities").setData(fac);
      map.getSource("buildings").setData(await api("/api/layers/buildings"));
    }
    map.getSource("zones").setData(state.zones[state.mode]);
    renderKpis(); renderLegend(); renderZones(); renderMarkers(); renderFacilities(); await renderReview();
  } finally { busy(false); }
}

async function addLayers() {
  const blds = await api("/api/layers/buildings");
  map.addSource("buildings", { type: "geojson", data: blds });
  map.addLayer({ id: "buildings", type: "fill", source: "buildings", layout: { visibility: "none" },
    paint: { "fill-color": ["case", ["to-boolean", ["get", "zone_id"]], "#d7263d", "#34495e"], "fill-opacity": 0.35 } });
  map.addSource("zones", { type: "geojson", data: state.zones[state.mode], promoteId: "id" });
  map.addLayer({ id: "zones-fill", type: "fill", source: "zones",
    paint: { "fill-color": "#d7263d", "fill-opacity": ["case", ["boolean", ["feature-state", "hl"], false], 0.45, 0.22] } });
  map.addLayer({ id: "zones-line", type: "line", source: "zones", paint: { "line-color": "#d7263d", "line-width": ["case", ["boolean", ["feature-state", "hl"], false], 3.5, 1.8] } });
  map.addSource("roads", { type: "geojson", data: state.segs, promoteId: "id" });
  map.addLayer({ id: "roads-casing", type: "line", source: "roads", layout: { "line-cap": "round" },
    paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 13, 3.5, 16, 7, 19, 13], "line-opacity": 0.85 } });
  map.addLayer({ id: "roads", type: "line", source: "roads", layout: { "line-cap": "round" },
    paint: { "line-color": ["match", ["get", "status"], "open", COLORS.open, "blocked", COLORS.blocked, "uncertain", COLORS.uncertain, COLORS.no_imagery],
             "line-width": ["interpolate", ["linear"], ["zoom"],
               13, ["case", ["boolean", ["feature-state", "hover"], false], 3.5, 2],
               16, ["case", ["boolean", ["feature-state", "hover"], false], 7.5, 4.5],
               19, ["case", ["boolean", ["feature-state", "hover"], false], 14, 9]] } });
  // colour is not the only signal: underwater roads also get a white dash
  map.addLayer({ id: "roads-dash", type: "line", source: "roads", filter: ["==", ["get", "status"], "blocked"],
    paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.8, 16, 1.6, 19, 3], "line-dasharray": [2, 3] } });
  map.addSource("naive", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "naive", type: "line", source: "naive", paint: { "line-color": "#374151", "line-width": 4, "line-dasharray": [1.5, 1.5], "line-opacity": 0.9 } });
  map.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "route-casing", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 10 } });
  map.addLayer({ id: "route", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#1e64c8", "line-width": 6 } });
  map.addSource("facilities", { type: "geojson", data: state.facilities });
  map.addLayer({ id: "facilities", type: "circle", source: "facilities", paint: {
    "circle-radius": 8, "circle-color": ["match", ["get", "kind"], ...Object.entries(FAC_COLORS).flat(), "#555"],
    "circle-stroke-width": 3, "circle-stroke-color": ["case", ["==", ["get", "reachable"], false], "#d7263d", "#ffffff"] } });
  wireMap();
}

function renderKpis() {
  const s = state.summary, m = state.mode, seg = s.segments || {};
  const seen = (seg.open || 0) + (seg.blocked || 0) + (seg.uncertain || 0);
  const nZ = (state.zones[m].features || []).length;
  const facs = s.facilities_cut_off || [];
  $("#kpis").innerHTML = `
    <button class="kpi alert" data-kpi="zones"><div class="v">~${fmt(s.people_cut_off?.[m])}</div><div class="l">people with no dry road out</div></button>
    <button class="kpi alert" data-kpi="zones"><div class="v">${fmt(nZ)}</div><div class="l">neighbourhoods cut off</div></button>
    <button class="kpi" data-kpi="blocked"><div class="v">${fmt(seg.blocked)} <span class="m">of ${fmt(seen)}</span></div><div class="l">road pieces underwater</div></button>
    <button class="kpi ${facs.length ? "alert" : ""}" data-kpi="facilities"><div class="v">${fmt(facs.length)}</div><div class="l">${facs.length ? esc(facs.slice(0, 2).join(", ")) : "facilities cut off"}</div></button>`;
}

function renderLegend() {
  const c = {};
  for (const f of state.segs.features) c[f.properties.status] = (c[f.properties.status] || 0) + 1;
  const rows = ["open", "blocked", "uncertain", "no_imagery", "pending"].filter((k) => c[k] || ["open", "blocked"].includes(k));
  $("#legend").innerHTML = rows.map((k) => `<button data-legend="${k}" aria-pressed="${state.legendFilter === k}" title="Show only these roads"
      style="${state.legendFilter && state.legendFilter !== k ? "opacity:.45" : ""}"><span class="sw ${k}"></span>${STATUS_WORD[k]}<b>${fmt(c[k] || 0)}</b></button>`).join("");
}

function renderZones() {
  const z = state.zones[state.mode].features;
  const un = state.summary.unverified?.[state.mode];
  const extra = un && un.zones ? `<p class="note">${un.zones} more areas (${fmt(un.buildings)} buildings) are enclosed only by roads we have no photo for. They are not counted.</p>` : "";
  if (!z.length) { $("#zones").innerHTML = `<p class="empty">No neighbourhood is cut off in this view.</p>${extra}`; return; }
  const shown = state.showAllZones ? z : z.slice(0, 8);
  $("#zones").innerHTML = `<h4>Cut-off neighbourhoods, most people first</h4>` + shown.map((f) => {
    const p = f.properties, first = p.nearest_blocked_segment_ids?.[0];
    return `<div class="zone" data-zone="${esc(p.id)}" tabindex="0"><div class="rank">${p.priority_rank}</div><div>
      <div class="t">${esc(p.label)}</div>
      <div class="m">~${fmt(Math.round(p.est_pop))} people · ${fmt(p.building_count)} buildings</div>
      ${first ? `<div class="m">Fastest fix: <a data-seg="${esc(first)}">clear ${esc(segName(first))}</a></div>` : ""}</div></div>`;
  }).join("") + (z.length > 8 ? `<button class="more" data-allzones>${state.showAllZones ? "Show top 8" : `Show all ${z.length}`}</button>` : "") + extra;
}

function renderMarkers() {
  state.markers.forEach((m) => m.remove()); state.markers = [];
  for (const f of state.zones[state.mode].features.slice(0, 20)) {
    const p = f.properties; if (!p.label_point) continue;
    const el = document.createElement("div");
    el.className = "zmark"; el.textContent = p.priority_rank; el.dataset.zone = p.id; el.title = `${p.label}: ~${Math.round(p.est_pop)} people`;
    el.addEventListener("mouseenter", () => highlightZone(p.id, true)); el.addEventListener("mouseleave", () => highlightZone(p.id, false));
    el.addEventListener("click", (e) => { e.stopPropagation(); showZone(p.id); });
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

async function renderReview() {
  state.review = await api(`/api/review?status=${state.rf}&limit=80`);
  const unsure = await api(`/api/review?status=uncertain&limit=500`);
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

function compareWidget(base, p) {
  return `<div><div class="compare" data-compare>
      <img src="${base}_pre.jpg" alt="2016 photo"><img class="after" src="${base}_post.jpg" alt="2017 photo">
      <div class="bar"></div><span class="lab l">2016</span><span class="lab r">30 Aug 2017</span>
      <input type="range" min="0" max="100" value="50" aria-label="Swipe between 2016 and 2017"></div>
    <div class="cap">Drag to compare. The magenta line is the road being judged.</div></div>`;
}

async function showSegment(id) {
  const f = segById(id); if (!f) return;
  const p = f.properties, l = p.label || {};
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.remove("one");
  $("#ev-title").innerHTML = `${esc(p.name || "Unnamed road")} <span class="st ${p.status}">${STATUS_WORD[p.status]}</span>`;
  const left = p.chip_url ? compareWidget(`/chips/roads/${p.id}`, p) : `<p class="empty">No photo covers this road, so the model could not judge it.</p>`;
  const rows = [["Model says", l.answer ? `${l.answer === "yes" ? "new water on road" : l.answer === "no" ? "no new water" : "unsure"}` : "–"],
    ["Water on road", l.water_on_road], ["Visibility", l.visibility], ["Confidence", p.confidence ?? "–"],
    ["Decided by", p.status_source === "human" ? "a person (review)" : p.status_source === "vlm" ? `model ${p.model || ""}` : "–"],
    ["Prompt", p.prompt_version || "–"], ["Road", `${p.highway || ""} · ${p.length_m} m`]];
  $("#ev-body").innerHTML = `${left}<div>${l.reason ? `<p class="reason">“${esc(l.reason)}”</p>` : ""}
    <table class="kv">${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v ?? "–")}</td></tr>`).join("")}</table>
    <div class="ev-actions"><span class="m">Is this right?</span>
      <button class="b-open" data-set="open" data-id="${esc(p.id)}">It's open</button>
      <button class="b-blocked" data-set="blocked" data-id="${esc(p.id)}">It's underwater</button></div></div>
    <div id="ctx" class="ctx"><h5>Context for this photo tile</h5><span class="m">loading…</span></div>`;
  wireCompare();
  const [[w, s], [e, n]] = bboxOf(f.geometry), c = [(w + e) / 2, (s + n) / 2];
  if (!map.getBounds().contains(c)) map.easeTo({ center: c, zoom: Math.max(map.getZoom(), 16.5) });
  try {
    const { tile: t } = await api(`/api/context/${encodeURIComponent(id)}`);
    const fema = t?.fema_flood_zones ? JSON.parse(t.fema_flood_zones).join(", ") : "–";
    $("#ctx").innerHTML = t ? `<h5>Context for this photo tile (organizer data)</h5><table class="kv">
      <tr><td>FEMA zone</td><td>${esc(fema)}${t.fema_sfha_tf === "T" ? " · 100-year floodplain" : ""}</td></tr>
      <tr><td>Rain on photo day</td><td>${esc(t.precipitation_mm ?? "–")} mm</td></tr>
      <tr><td>Rain, 3 days before</td><td>${esc(t.preceding_3d_precipitation_mm ?? "–")} mm</td></tr>
      <tr><td>Rain station</td><td>${esc(t.precipitation_station_name ?? "–")}</td></tr>
      <tr><td>River gauge</td><td>${esc(t.usgs_gage_height_station_name ?? "–")}${t.usgs_daily_mean_gage_height_si ? ` · ${esc(t.usgs_daily_mean_gage_height_si)} ${esc(t.usgs_gage_height_si_unit || "")}` : ""}</td></tr>
      <tr><td>Ground elevation</td><td>${esc(t.elevation_m ?? "–")} m</td></tr></table>
      <p class="note">Station readings are nearby context, not measurements on this road.</p>` : `<h5>Context</h5><span class="m">No tile metadata.</span>`;
  } catch { $("#ctx").innerHTML = `<h5>Context</h5><span class="m">Not available.</span>`; }
}

function wireCompare() {
  document.querySelectorAll("[data-compare]").forEach((box) => {
    const input = box.querySelector("input"), after = box.querySelector(".after"), bar = box.querySelector(".bar");
    const set = (v) => { after.style.clipPath = `inset(0 0 0 ${v}%)`; bar.style.left = `${v}%`; };
    input.addEventListener("input", () => set(input.value)); set(50);
  });
}

async function setStatus(id, status) {
  busy(true);
  try { await post(`/api/review/${encodeURIComponent(id)}`, { status }); } finally { busy(false); }
  await refresh();
  if (!$("#tab-review").classList.contains("hidden")) { state.rvIdx = Math.min(state.rvIdx, state.review.length - 1); }
  showSegment(id);
}

async function routeFrom(lngLat) {
  state.routing = false; $("#hint").classList.add("hidden"); map.getCanvas().style.cursor = "";
  const btn = $("#routeBtn"); btn.disabled = true; btn.textContent = "Finding the safest way out…";
  let r;
  try { r = await post("/api/route", { lat: lngLat.lat, lon: lngLat.lng, mode: state.mode }); }
  finally { btn.disabled = false; btn.textContent = "Route me out"; }
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.add("one");
  const naive = r.naive && r.naive.geometry.coordinates.length > 1 ? r.naive : null;
  map.getSource("naive").setData(naive ? { type: "FeatureCollection", features: [naive] } : { type: "FeatureCollection", features: [] });
  if (!r.route) {
    map.getSource("route").setData({ type: "FeatureCollection", features: [] });
    $("#ev-title").textContent = "No dry road out from here";
    $("#ev-body").innerHTML = `<div><div class="callout"><b>Stay where you are and call for rescue.</b> No passable road reaches a shelter, a hospital or the edge of the mapped area${r.zone_id ? `. This point is in cut-off neighbourhood <b>${esc(r.zone_id.replace("Z", "#"))}</b>` : ""}. Turn around, don't drown.</div></div>`;
    return;
  }
  map.getSource("route").setData(r.route);
  map.fitBounds(bboxOf(r.route.geometry), { padding: 80, maxZoom: 17 });
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
  map.fitBounds(bboxOf(f.geometry), { padding: 80, maxZoom: 17 });
  const p = f.properties;
  $("#evidence").classList.remove("collapsed"); $("#ev-body").classList.add("one");
  $("#ev-title").innerHTML = `<span class="zmark" style="display:inline-grid;width:22px;height:22px;font-size:12px">${p.priority_rank}</span> ${esc(p.label)}`;
  const blocked = (p.nearest_blocked_segment_ids || []).slice(0, 6);
  $("#ev-body").innerHTML = `<div><p>About <b>${fmt(Math.round(p.est_pop))} people</b> in ${fmt(p.building_count)} buildings have no ${state.mode === "strict" ? "confirmed-dry" : "passable"} road out.
    Estimate uses ${state.cfg ? "2.5 people per home" : ""} until census data is joined.</p>
    ${blocked.length ? `<p>Flooded roads on the edge of this neighbourhood. Clearing the shortest one first opens a way out fastest:</p>
    <ol class="steps">${blocked.map((s) => { const g = segById(s)?.properties; return `<li><a class="lnk" data-seg="${esc(s)}">${esc(g?.name || "unnamed road")}</a> · ${g?.length_m ?? "?"} m</li>`; }).join("")}</ol>` : ""}</div>`;
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
    const hit = map.queryRenderedFeatures(e.point, { layers: ["roads", "facilities", "zones-fill"] });
    const road = hit.find((h) => h.layer.id === "roads");
    if (road) return showSegment(road.properties.id);
    const z = hit.find((h) => h.layer.id === "zones-fill");
    if (z) showZone(z.properties.id);
  });
}

// before/after swipe on the map
function setCompare(on) {
  $("#mapwrap").classList.toggle("comparing", on);
  $("#swipe").classList.toggle("hidden", !on);
  $("#compareBtn").setAttribute("aria-pressed", String(on));
  if (on) { before.resize(); placeSwipe($("#mapwrap").clientWidth / 2); }
  else $("#map").style.clipPath = "";
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

document.addEventListener("click", (e) => {
  const t = e.target.closest("button, a, img, .zone, .rv") || e.target;
  if (t.dataset.set) return setStatus(t.dataset.id, t.dataset.set);
  if (t.dataset.seg) return showSegment(t.dataset.seg);
  if (t.hasAttribute?.("data-allzones")) { state.showAllZones = !state.showAllZones; return renderZones(); }
  if (t.dataset.zone) return showZone(t.dataset.zone);
  if (t.dataset.rv) { state.rvIdx = Number(t.dataset.rv); renderReviewCursor(); return showSegment(state.review[state.rvIdx].id); }
  if (t.dataset.kpi) {
    if (t.dataset.kpi === "blocked") { state.legendFilter = state.legendFilter === "blocked" ? null : "blocked"; applyLegendFilter(); }
    else switchTab("zones");
    return;
  }
  if (t.dataset.legend) { state.legendFilter = state.legendFilter === t.dataset.legend ? null : t.dataset.legend; return applyLegendFilter(); }
  if (t.dataset.tab) return switchTab(t.dataset.tab);
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
  $("#tab-zones").classList.toggle("hidden", tab !== "zones");
  $("#tab-review").classList.toggle("hidden", tab !== "review");
}
function applyLegendFilter() {
  const f = state.legendFilter ? ["==", ["get", "status"], state.legendFilter] : null;
  ["roads", "roads-casing"].forEach((id) => map.setFilter(id, f));
  renderLegend();
}
function renderReviewCursor() { document.querySelectorAll(".rv").forEach((el, i) => el.classList.toggle("cur", i === state.rvIdx)); document.querySelector(".rv.cur")?.scrollIntoView({ block: "nearest" }); }

document.addEventListener("keydown", (e) => {
  if ($("#tab-review").classList.contains("hidden") || !state.review.length || e.target.closest("input, textarea")) return;
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
  const ids = { post: (state.cfg.overlays.post || []).map((o) => `post-${o.id}`), roads: ["roads", "roads-casing", "roads-dash"],
    zones: ["zones-fill", "zones-line"], facilities: ["facilities"], buildings: ["buildings"] }[cb.dataset.layer];
  ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", vis));
  if (cb.dataset.layer === "zones") state.markers.forEach((m) => (m.getElement().style.display = cb.checked ? "" : "none"));
}));
$("#routeBtn").addEventListener("click", () => {
  state.routing = true; map.getCanvas().style.cursor = "crosshair";
  $("#hint").textContent = "Click the map where you are"; $("#hint").classList.remove("hidden");
});
$("#compareBtn").addEventListener("click", () => setCompare(!$("#mapwrap").classList.contains("comparing")));
$("#ev-close").addEventListener("click", () => $("#evidence").classList.add("collapsed"));
$("#guideOk").addEventListener("click", () => { $("#guide").classList.add("hidden"); store.set("floodlens.guide", "1"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { state.routing = false; $("#hint").classList.add("hidden"); $("#guide").classList.add("hidden"); } });

async function pills() {
  try {
    const h = await api("/api/health");
    $("#pills").innerHTML = `<span class="pill live">App running</span><span class="pill ${h.vlm_mock ? "cached" : "live"}">Model: ${h.vlm_mock ? "mock" : esc(h.model)}</span>`;
  } catch { $("#pills").innerHTML = `<span class="pill down">App unreachable</span>`; }
}

map.on("load", () => loadAll().catch((err) => { console.error(err); $("#ev-title").textContent = "Could not load data: " + err.message; }));
