"use strict";
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const SERIES = [["down", "var(--down)"], ["up", "var(--up)"]];
const WINDOW = 300; // seconds of history

let data = [];
let lastT = 0;
let hoverIdx = null;
let busy = false;
let engineBusy = false;
let lastNowKey = "";
let lastNow = { url: "" };

// -- helpers ---------------------------------------------------------------------

const fmt = v => (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2));
const clock = t => new Date(t * 1000).toLocaleTimeString([], { hour12: false });

function el(name, attrs, parent) {
  const n = document.createElementNS(NS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}

function setStatus(msg, type) {
  const s = $("status");
  s.textContent = msg;
  s.className = "status " + (type || "");
}

function log(msg, type) {
  const box = $("log");
  const line = document.createElement("div");
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = new Date().toTimeString().slice(0, 8);
  if (type) line.className = type;
  line.append(ts, document.createTextNode(msg));
  box.appendChild(line);
  while (box.childElementCount > 200) box.firstChild.remove();
  box.scrollTop = box.scrollHeight;
}

async function api(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok && !d.error) d.error = "HTTP " + r.status;
    return d;
  } catch (e) {
    return { ok: false, error: "Request failed: " + e.message };
  }
}

// -- actions ---------------------------------------------------------------------

function setBusy(on) {
  busy = on;
  $("play").disabled = on;
  $("play").textContent = on ? "Starting…" : "▶ Play";
}

async function play() {
  const source = $("src").value.trim();
  if (!source) return setStatus("Paste an acestream:// link, a content ID or a stream URL first.", "err");
  setBusy(true);
  setStatus("Starting the engine and connecting… (the very first run downloads the Docker image and can take a few minutes)");
  log("Play: " + source);
  const d = await api("/api/play", { source });
  setBusy(false);
  if (d.ok) {
    log("Engine ready: " + d.url, "ok");
    setStatus("Connecting to peers — VLC opens as soon as the stream is ready…");
  } else {
    log(d.error, "err");
    setStatus(d.error, "err");
  }
}

async function stop() {
  log("Stop requested");
  $("stop").disabled = true;
  const d = await api("/api/stop");
  if (d.ok) {
    setStatus("Stopped.");
    log("Stopped", "ok");
  } else {
    setStatus(d.error, "err");
    log(d.error, "err");
  }
}

async function toggleEngine() {
  const action = $("engine-btn").dataset.action;
  engineBusy = true;
  $("engine-btn").disabled = true;
  setStatus(action === "start"
    ? "Starting the engine… (first run pulls the Docker image, this can take a few minutes)"
    : "Stopping the engine…");
  log((action === "start" ? "Starting" : "Stopping") + " engine");
  const d = await api("/api/engine", { action });
  engineBusy = false;
  if (d.ok) {
    setStatus(action === "start" ? "Engine started." : "Engine stopped.", "ok");
    log("Engine " + (action === "start" ? "started" : "stopped"), "ok");
  } else {
    setStatus(d.error, "err");
    log(d.error, "err");
  }
}

$("play").onclick = play;
$("stop").onclick = stop;
$("engine-btn").onclick = toggleEngine;
$("src").addEventListener("keydown", e => { if (e.key === "Enter" && !busy) play(); });
$("copy").onclick = async () => {
  try {
    await navigator.clipboard.writeText(lastNow.url);
    $("copy").textContent = "Copied";
    setTimeout(() => ($("copy").textContent = "Copy"), 1200);
  } catch (_) { /* clipboard unavailable: the URL is user-select:all */ }
};

// -- library ---------------------------------------------------------------------

let histRev = -1;

function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  if (s < 7 * 86400) return Math.floor(s / 86400) + " d ago";
  return new Date(t * 1000).toLocaleDateString();
}

const shorten = s => (s.length > 46 ? s.slice(0, 24) + "…" + s.slice(-10) : s);

async function loadHistory() {
  try {
    const r = await fetch("/api/history", { cache: "no-store" });
    renderLibrary((await r.json()).items);
  } catch (_) { /* next poll retries */ }
}

async function histApi(action, key, value) {
  const d = await api("/api/history", { action, key, value });
  if (!d.ok) setStatus(d.error, "err");
  loadHistory();
}

function iconButton(text, label, onclick, cls) {
  const b = document.createElement("button");
  b.className = "icon " + (cls || "");
  b.textContent = text;
  b.title = label;
  b.setAttribute("aria-label", label);
  b.onclick = onclick;
  return b;
}

function startRename(e, titleEl) {
  const input = document.createElement("input");
  input.className = "title";
  input.value = e.name;
  input.placeholder = "Name this stream";
  input.maxLength = 80;
  let done = false;
  const finish = save => {
    if (done) return;
    done = true;
    if (save && input.value.trim() !== e.name) histApi("rename", e.key, input.value);
    else loadHistory();
  };
  input.addEventListener("keydown", ev => {
    if (ev.key === "Enter") finish(true);
    if (ev.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  titleEl.replaceWith(input);
  input.focus();
  input.select();
}

function libRow(e) {
  const li = document.createElement("li");
  li.className = "item";
  li.appendChild(iconButton(e.pinned ? "★" : "☆", e.pinned ? "Unpin" : "Pin (keep forever)",
    () => histApi("pin", e.key, !e.pinned), "pin"));
  li.lastChild.setAttribute("aria-pressed", String(e.pinned));

  const body = document.createElement("div");
  body.className = "body";
  const title = document.createElement("button");
  title.className = "title";
  title.textContent = e.name || shorten(e.source);
  title.title = "Put into the input";
  title.onclick = () => { $("src").value = e.source; $("src").focus(); };
  const sub = document.createElement("span");
  sub.className = "sub";
  sub.textContent = (e.name ? shorten(e.source) + " · " : "") + ago(e.last_played) + " · " + e.plays + "×";
  body.append(title, sub);
  li.appendChild(body);

  li.appendChild(iconButton("▶", "Play", () => { $("src").value = e.source; play(); }));
  li.appendChild(iconButton("✎", "Rename", () => startRename(e, title)));
  li.appendChild(iconButton("×", "Remove from library", () => histApi("delete", e.key)));
  return li;
}

function renderLibrary(items) {
  const ul = $("lib");
  ul.replaceChildren();
  $("lib-empty").hidden = items.length > 0;
  $("clear-recent").hidden = !items.some(e => !e.pinned);
  let group = null;
  for (const e of items) {
    const g = e.pinned ? "Pinned" : "Recent";
    if (g !== group) {
      group = g;
      const h = document.createElement("li");
      h.className = "grp";
      h.textContent = g;
      ul.appendChild(h);
    }
    ul.appendChild(libRow(e));
  }
}

$("clear-recent").onclick = () => histApi("clear", "");

// -- state -----------------------------------------------------------------------

function applyEngine(e) {
  const dot = $("dot"), btn = $("engine-btn");
  if (e.running === null) {
    dot.className = "dot";
    $("engine-label").textContent = "Engine: checking…";
    btn.disabled = true;
    return;
  }
  if (e.image) {
    const link = $("image-link");
    link.textContent = e.image;
    link.href = e.image_url;
    $("image-meta").textContent = e.version ? "\u00b7 AceStream " + e.version : "";
    $("image").hidden = false;
  }
  dot.className = "dot " + (e.running ? "on" : "off");
  $("engine-label").textContent = "Engine: " + (e.running ? "running" : "stopped");
  btn.textContent = e.running ? "Stop" : "Start";
  btn.dataset.action = e.running ? "stop" : "start";
  btn.disabled = engineBusy;
}

function applyNow(n, stream) {
  lastNow = n;
  const active = n.phase === "connecting" || n.phase === "playing";
  $("stop").disabled = !active;
  $("now").hidden = !n.url;
  $("now").classList.toggle("idle", !(active && n.vlc) && n.phase !== "connecting");
  $("now-url").textContent = n.url;

  let label = "Last opened";
  if (n.phase === "connecting") label = "Connecting";
  else if (n.phase === "playing") label = n.vlc ? "Now playing" : "Last opened (VLC is not running)";
  const peers = stream && stream.peers != null ? " · " + stream.peers + " peers" : "";
  $("now-label").textContent = label + (active ? peers : "");

  const key = n.phase + "|" + n.vlc + "|" + n.error;
  if (key !== lastNowKey) {
    if (n.phase === "playing" && lastNowKey.startsWith("connecting")) {
      setStatus("Playing in VLC. Buffering can take 10–30 s on the first load.", "ok");
      log("VLC launched", "ok");
    } else if (n.phase === "error") {
      setStatus(n.error, "err");
      log(n.error, "err");
    } else if (n.phase === "playing" && !n.vlc && lastNowKey.includes("true")) {
      log("VLC was closed");
    }
    lastNowKey = key;
  }
}

async function poll() {
  try {
    const r = await fetch("/api/state?since=" + lastT, { cache: "no-store" });
    const s = await r.json();
    applyEngine(s.engine);
    applyNow(s.now, s.stream);
    if (s.history_rev !== histRev) {
      histRev = s.history_rev;
      loadHistory();
    }
    if (s.net.samples.length) {
      data = data.concat(s.net.samples).slice(-WINDOW);
      lastT = data[data.length - 1].t;
    }
    $("iface").textContent = "Mbit/s · " + (s.net.iface ? s.net.iface + " · " : "") + "last 5 minutes";
    updateTiles();
    draw();
    renderTable();
  } catch (_) {
    $("engine-label").textContent = "Server unreachable";
    $("dot").className = "dot off";
  }
}

// -- charts ----------------------------------------------------------------------

function niceStep(max) {
  const raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (raw <= m * p) return m * p;
  return 10 * p;
}

function updateTiles() {
  const last = data[data.length - 1];
  if (!last) return;
  $("vd").textContent = fmt(last.down);
  $("vu").textContent = fmt(last.up);
  for (const [key, id] of [["down", "ad"], ["up", "au"]]) {
    const v = data.map(d => d[key]);
    $(id).textContent = "avg " + fmt(v.reduce((a, b) => a + b, 0) / v.length) + " · peak " + fmt(Math.max(...v));
  }
}

function spark(id, key, color) {
  const sv = $(id);
  sv.replaceChildren();
  const w = sv.clientWidth, h = sv.clientHeight, pts = data.slice(-60);
  if (!w || pts.length < 2) return;
  const mx = Math.max(0.5, ...pts.map(d => d[key]));
  const xy = pts.map((d, i) => [4 + i / (pts.length - 1) * (w - 8), h - 5 - d[key] / mx * (h - 10)]);
  el("path", {
    d: xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(""),
    fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
  }, sv);
  const l = xy[xy.length - 1];
  el("circle", { cx: l[0], cy: l[1], r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 }, sv);
}

function draw() {
  spark("sd", "down", "var(--down)");
  spark("su", "up", "var(--up)");
  const svg = $("svg");
  svg.replaceChildren();
  const W = svg.clientWidth, H = svg.clientHeight;
  if (!W || data.length < 2) return;
  const m = { l: 40, r: 64, t: 14, b: 24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const mid = m.t + ih / 2, half = ih / 2;
  const tmax = data[data.length - 1].t, tmin = tmax - WINDOW;
  const peak = Math.max(1, ...data.map(d => Math.max(d.down, d.up)));
  const step = niceStep(peak * 2), M = step * 2;
  const X = t => m.l + (t - tmin) / WINDOW * iw;
  const Y = { down: v => mid - v / M * half, up: v => mid + v / M * half };

  for (let k = -2; k <= 2; k++) {
    const y = mid - k * step / M * half;
    el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: k ? "var(--grid)" : "var(--axis)", "stroke-width": 1 }, svg);
    el("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, svg).textContent = Math.abs(k) * step;
  }
  el("text", { x: m.l + 6, y: m.t + 12 }, svg).textContent = "Download";
  el("text", { x: m.l + 6, y: H - m.b - 6 }, svg).textContent = "Upload";
  for (let s = 0; s <= WINDOW; s += 60) {
    el("text", { x: X(tmin + s), y: H - 6, "text-anchor": s === 0 ? "start" : s === WINDOW ? "end" : "middle" }, svg)
      .textContent = s === WINDOW ? "now" : "-" + (WINDOW - s) / 60 + " min";
  }

  for (const [key, color] of SERIES) {
    const f = Y[key], pts = data.map(d => [X(d.t), f(d[key])]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
    el("path", { d: line + `L${pts[pts.length - 1][0]} ${mid}L${pts[0][0]} ${mid}Z`, fill: color, "fill-opacity": 0.1 }, svg);
    el("path", { d: line, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);

    const avg = data.reduce((a, d) => a + d[key], 0) / data.length, ay = f(avg);
    el("line", { x1: m.l, x2: W - m.r, y1: ay, y2: ay, stroke: color, "stroke-opacity": 0.5, "stroke-width": 1 }, svg);
    const ly = key === "down" ? Math.min(ay + 4, mid - 4) : Math.max(ay + 4, mid + 14);
    el("text", { x: W - m.r + 6, y: ly }, svg).textContent = "avg " + fmt(avg);

    let pi = 0;
    data.forEach((d, i) => { if (d[key] > data[pi][key]) pi = i; });
    if (data[pi][key] > 0.05) {
      const px = pts[pi][0], py = pts[pi][1];
      el("circle", { cx: px, cy: py, r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
      const tx = Math.min(Math.max(px, m.l + 36), W - m.r - 36);
      const ty = key === "down" ? Math.max(py - 9, m.t + 22) : Math.min(py + 19, H - m.b - 4);
      el("text", { x: tx, y: ty, "text-anchor": "middle" }, svg).textContent = "peak " + fmt(data[pi][key]);
    }
    const last = pts[pts.length - 1];
    el("circle", { cx: last[0], cy: last[1], r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
  }

  if (hoverIdx !== null && data[hoverIdx]) {
    const d = data[hoverIdx], x = X(d.t);
    el("line", { x1: x, x2: x, y1: m.t, y2: m.t + ih, stroke: "var(--axis)", "stroke-width": 1 }, svg);
    for (const [key, color] of SERIES)
      el("circle", { cx: x, cy: Y[key](d[key]), r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
    showTip(d, x);
  } else {
    $("tip").style.display = "none";
  }
  svg._geom = { m, iw, tmin };
}

function showTip(d, x) {
  const tip = $("tip");
  tip.replaceChildren();
  const head = document.createElement("div");
  head.className = "t";
  head.textContent = clock(d.t);
  tip.appendChild(head);
  for (const [name, key, color] of [["Download", "down", "var(--down)"], ["Upload", "up", "var(--up)"]]) {
    const r = document.createElement("div");
    r.className = "r";
    const k = document.createElement("i");
    k.style.background = color;
    const v = document.createElement("b");
    v.textContent = fmt(d[key]) + " Mbit/s";
    const n = document.createElement("span");
    n.textContent = name;
    r.append(k, v, n);
    tip.appendChild(r);
  }
  tip.style.display = "block";
  const box = $("chartbox").clientWidth, w = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(x + 12, 0), box - w) + "px";
  tip.style.top = "8px";
}

$("svg").addEventListener("pointermove", e => {
  const g = $("svg")._geom;
  if (!g || !data.length) return;
  const rect = $("svg").getBoundingClientRect();
  const t = g.tmin + (e.clientX - rect.left - g.m.l) / g.iw * WINDOW;
  let best = 0, bd = Infinity;
  data.forEach((d, i) => { const dd = Math.abs(d.t - t); if (dd < bd) { bd = dd; best = i; } });
  hoverIdx = best;
  draw();
});
$("svg").addEventListener("pointerleave", () => { hoverIdx = null; draw(); });
$("svg").addEventListener("blur", () => { hoverIdx = null; draw(); });
$("svg").addEventListener("keydown", e => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  e.preventDefault();
  const d = e.key === "ArrowLeft" ? -1 : 1;
  hoverIdx = Math.min(Math.max((hoverIdx === null ? data.length - 1 : hoverIdx) + d, 0), data.length - 1);
  draw();
});

function renderTable() {
  const box = $("tablebox");
  if (box.hidden) return;
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const h of ["Time", "Download, Mbit/s", "Upload, Mbit/s"]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  const body = table.createTBody();
  for (const d of data.slice(-30).reverse()) {
    const r = body.insertRow();
    r.insertCell().textContent = clock(d.t);
    r.insertCell().textContent = fmt(d.down);
    r.insertCell().textContent = fmt(d.up);
  }
  box.replaceChildren(table);
}

$("toggle").onclick = () => {
  const showTable = $("tablebox").hidden;
  $("tablebox").hidden = !showTable;
  $("chartbox").hidden = showTable;
  $("toggle").textContent = showTable ? "Chart view" : "Table view";
  $("toggle").setAttribute("aria-pressed", String(showTable));
  renderTable();
  draw();
};

new ResizeObserver(draw).observe($("chartbox"));
poll();
setInterval(poll, 1000);
