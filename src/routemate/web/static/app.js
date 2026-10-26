const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  network: null,
  stations: {},
  lines: {},
  selected: { from: null, to: null },
  mode: "fastest",
  context: null,
  plans: [],
  activePlan: 0,
};

const MODE_LABELS = {
  fastest: "Fastest",
  fewest_transfers: "Fewest changes",
  fewest_stops: "Fewest stops",
  shortest_distance: "Shortest",
  cheapest: "Cheapest",
  least_crowded: "Least crowded",
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.status === "error") {
    const err = body.error || { message: `Request failed (${res.status})` };
    throw new Error(err.message);
  }
  return body;
}

function showError(message) {
  const el = $("#error");
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

const map = L.map("map", { zoomControl: true }).setView([17.415, 78.47], 12);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 19,
}).addTo(map);
const networkLayer = L.layerGroup().addTo(map);
const busStopLayer = L.layerGroup();
const routeLayer = L.layerGroup().addTo(map);
const stationMarkers = {};
const BUS_STOP_MIN_ZOOM = 15;

function drawNetwork() {
  networkLayer.clearLayers();
  busStopLayer.clearLayers();
  for (const line of state.network.lines) {
    L.polyline(line.coords, { color: line.color, weight: 5, opacity: 0.35 }).addTo(networkLayer);
  }
  for (const st of state.network.stations) {
    if (st.kind === "bus") {
      L.circleMarker([st.lat, st.lon], { radius: 3, color: "#0F766E", weight: 1, fillColor: "#fff", fillOpacity: 1 })
        .bindTooltip(`🚌 ${st.name}`, { className: "station-label", direction: "top", offset: [0, -3] })
        .on("click", () => pickFromMap(st.id))
        .addTo(busStopLayer);
      continue;
    }
    const color = state.lines[st.lines[0]].color;
    const m = L.circleMarker([st.lat, st.lon], {
      radius: st.interchange ? 6 : 4,
      color: st.interchange ? "#111827" : color,
      weight: st.interchange ? 2.5 : 1.5,
      fillColor: "#fff",
      fillOpacity: 1,
    })
      .bindTooltip(st.name, { className: "station-label", direction: "top", offset: [0, -4] })
      .on("click", () => pickFromMap(st.id))
      .addTo(networkLayer);
    stationMarkers[st.id] = m;
  }
}

map.on("zoomend", () => {
  const show = map.getZoom() >= BUS_STOP_MIN_ZOOM;
  if (show && !map.hasLayer(busStopLayer)) busStopLayer.addTo(map);
  if (!show && map.hasLayer(busStopLayer)) map.removeLayer(busStopLayer);
});

const SEGMENT_STYLE = {
  metro: { weight: 7, dashArray: null },
  bus: { weight: 6, dashArray: "10 7" },
  walk: { weight: 4, dashArray: "2 8" },
};

function drawRoute(plan) {
  routeLayer.clearLayers();
  if (!plan) return;
  const bounds = [];
  for (const seg of plan.geometry) {
    const style = SEGMENT_STYLE[seg.type] || SEGMENT_STYLE.metro;
    if (seg.type !== "walk") L.polyline(seg.coords, { color: "#fff", weight: style.weight + 4, opacity: 0.9 }).addTo(routeLayer);
    L.polyline(seg.coords, { color: seg.color, weight: style.weight, opacity: 1, dashArray: style.dashArray, lineCap: "round" }).addTo(routeLayer);
    for (const c of seg.coords) {
      if (seg.type !== "walk") L.circleMarker(c, { radius: 3.5, color: seg.color, weight: 2, fillColor: "#fff", fillOpacity: 1 }).addTo(routeLayer);
      bounds.push(c);
    }
  }
  const pin = (id, label, bg) => {
    const st = state.stations[id];
    L.marker([st.lat, st.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div style="background:${bg};color:#fff;font:600 11px Inter,sans-serif;padding:4px 8px;border-radius:999px;box-shadow:0 2px 6px rgba(0,0,0,.25);white-space:nowrap;transform:translate(-50%,-130%)">${esc(label)}</div>`,
      }),
    }).addTo(routeLayer);
  };
  pin(plan.origin.id, plan.origin.name, "#111827");
  pin(plan.destination.id, plan.destination.name, "#4f46e5");
  for (const step of plan.steps.filter((s) => s.type === "transfer")) {
    pin(step.station, `Change · ${state.stations[step.station].name}`, "#b45309");
  }
  if (bounds.length) map.fitBounds(bounds, { padding: [60, 60] });
}

$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".tab-body").forEach((b) => b.classList.toggle("active", b.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "history") loadHistory();
  })
);

function openTab(name) {
  $$(".tab").find((b) => b.dataset.tab === name).click();
}

function renderModes() {
  const box = $("#mode");
  box.innerHTML = "";
  for (const m of state.network.modes) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = MODE_LABELS[m.id] || m.id;
    b.title = m.description;
    b.classList.toggle("active", m.id === state.mode);
    b.onclick = () => {
      state.mode = m.id;
      renderModes();
    };
    box.appendChild(b);
  }
}

$("#when").addEventListener("change", (e) => {
  const t = $("#time");
  t.disabled = e.target.value === "now";
  if (!t.disabled && !t.value) {
    const d = new Date();
    t.value = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
});

function setStation(role, id) {
  state.selected[role] = id;
  $(`#${role}`).value = id ? state.stations[id].name : "";
}

function pickFromMap(id) {
  openTab("plan");
  if (!state.selected.from || (state.selected.from && state.selected.to)) {
    setStation("from", id);
    setStation("to", null);
  } else {
    setStation("to", id);
    planRoute();
  }
}

$("#swap").onclick = () => {
  const { from, to } = state.selected;
  setStation("from", to);
  setStation("to", from);
};

function lineDots(lines) {
  return `<span class="dots">${lines.map((l) => `<i class="dot" style="background:${state.lines[l].color}"></i>`).join("")}</span>`;
}

function setupAutocomplete(role) {
  const input = $(`#${role}`);
  const list = $(`.suggest[data-for="${role}"]`);
  let items = [];
  let active = -1;
  let timer;

  const close = () => {
    list.innerHTML = "";
    items = [];
    active = -1;
  };
  const choose = (i) => {
    if (!items[i]) return;
    setStation(role, items[i].id);
    close();
  };
  const render = () => {
    list.innerHTML = items
      .map((s, i) => `<li data-i="${i}" class="${i === active ? "active" : ""}"><span>${s.kind === "bus" ? "🚌 " : ""}${esc(s.name)}</span>${s.kind === "bus" ? '<small class="muted">bus stop</small>' : lineDots(s.lines)}</li>`)
      .join("");
  };

  input.addEventListener("input", () => {
    state.selected[role] = null;
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) return close();
    timer = setTimeout(async () => {
      const res = await api(`/api/stations/suggest?q=${encodeURIComponent(q)}&limit=6`);
      items = res.results;
      active = items.length ? 0 : -1;
      render();
    }, 90);
  });
  input.addEventListener("keydown", (e) => {
    if (!items.length) return;
    if (e.key === "ArrowDown") { active = (active + 1) % items.length; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = (active - 1 + items.length) % items.length; render(); e.preventDefault(); }
    else if (e.key === "Enter") { choose(active); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (li) choose(Number(li.dataset.i));
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
}

function timeParams() {
  const when = $("#when").value;
  const t = $("#time").value;
  if (when === "now" || !t) return "";
  return `&${when}=${t}`;
}

function odParams() {
  const from = state.selected.from || $("#from").value.trim();
  const to = state.selected.to || $("#to").value.trim();
  if (!from || !to) throw new Error("Choose both a start and a destination station.");
  return `from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
}

function busParam() {
  return $("#buses").checked ? "&buses=1" : "";
}

async function planRoute(avoid = []) {
  showError("");
  const btn = $("#plan-form button[type=submit]");
  btn.disabled = true;
  try {
    const od = odParams();
    const avoidParam = avoid.length ? `&avoid=${encodeURIComponent(avoid.join(","))}` : "";
    const plan = await api(`/api/route?${od}&mode=${state.mode}${timeParams()}${busParam()}${avoidParam}`);
    const opts = await api(`/api/routes/options?${od}&departAt=${plan.summary.leaveAt}${plan.buses ? "&buses=1" : ""}${avoidParam}`).catch(() => ({ options: [] }));
    renderResult({ plan, options: opts.options, explanation: null, query: null });
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

$("#plan-form").addEventListener("submit", (e) => {
  e.preventDefault();
  planRoute();
});

async function* sseEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const event = block.match(/^event: (.*)$/m)?.[1];
      const data = block.match(/^data: (.*)$/m)?.[1];
      if (event && data) yield [event, JSON.parse(data)];
    }
  }
}

const run = { steps: [], timer: null, target: "", shown: 0, raf: null, lastFrame: 0, streamDone: true };

const STEP_LABELS = {
  understand: (ai) => (ai ? "Understanding your request with Gemini" : "Understanding your request"),
  route: () => "Finding the best route",
  explain: (ai) => (ai ? "Gemini is writing the explanation" : "Writing the explanation"),
};
const STEP_DONE = { understand: "understood", route: "routed", explain: "explained" };

const secs = (ms) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`);

function startRun(ai) {
  stopTimer();
  cancelAnimationFrame(run.raf);
  Object.assign(run, { target: "", shown: 0, raf: null, streamDone: false });
  run.steps = ["understand", "route", "explain"].map((key) => ({ key, label: STEP_LABELS[key](ai), status: "todo", started: 0, ms: 0 }));
  run.timer = setInterval(renderProgress, 200);
}

function relabel(ai) {
  run.steps.forEach((step) => (step.label = STEP_LABELS[step.key](ai)));
}

function stopTimer() {
  clearInterval(run.timer);
  run.timer = null;
}

function stepTo(key) {
  const now = performance.now();
  for (const step of run.steps) {
    if (step.status === "active") {
      step.status = "done";
      step.ms = now - step.started;
    }
  }
  const next = run.steps.find((step) => step.key === key && step.status === "todo");
  if (next) {
    next.status = "active";
    next.started = now;
  }
  renderProgress();
}

function finishRun() {
  stepTo(null);
  stopTimer();
  renderProgress();
}

function progressHtml() {
  if (!run.steps.length) return "";
  if (!run.steps.some((step) => step.status === "active")) {
    const parts = run.steps.filter((step) => step.status === "done").map((step) => `${STEP_DONE[step.key]} in ${secs(step.ms)}`);
    return parts.length ? `<div class="progress done" data-shape="${run.steps.map((step) => step.status).join()}">${parts.join(" · ")}</div>` : "";
  }
  const rows = run.steps.map((step) => {
    const t = step.status === "active" ? secs(performance.now() - step.started) : step.status === "done" ? secs(step.ms) : "";
    const icon = step.status === "done" ? "✓" : step.status === "active" ? '<span class="spin"></span>' : "";
    return `<li class="${step.status}"><span class="ic">${icon}</span><span class="lbl">${esc(step.label)}</span><span class="t">${t}</span></li>`;
  });
  return `<ul class="progress" data-shape="${run.steps.map((step) => step.status).join()}">${rows.join("")}</ul>`;
}

function renderProgress() {
  const el = $("#result .progress");
  if (!el) return;
  const shape = run.steps.map((step) => step.status).join();
  if (el.dataset.shape === shape) {
    const active = run.steps.find((step) => step.status === "active");
    const cell = active && el.querySelector("li.active .t");
    if (cell) cell.textContent = secs(performance.now() - active.started);
    return;
  }
  el.outerHTML = progressHtml() || '<div class="progress"></div>';
}

function typeText(text, reset = false) {
  if (reset) {
    run.target = "";
    run.shown = 0;
  }
  run.target += text;
  if (!run.raf) {
    run.lastFrame = performance.now();
    run.raf = requestAnimationFrame(typeFrame);
  }
}

function typeFrame(now) {
  const dt = Math.min(100, now - run.lastFrame);
  run.lastFrame = now;
  const backlog = run.target.length - run.shown;
  const charsPerSecond = 60 + backlog * 1.5;
  run.shown = Math.min(run.target.length, run.shown + Math.max(1, Math.round((charsPerSecond * dt) / 1000)));
  renderAnswer();
  run.raf = run.shown < run.target.length ? requestAnimationFrame(typeFrame) : null;
}

function renderAnswer() {
  const el = $("#result .answer");
  if (!el) return;
  el.textContent = run.target.slice(0, run.shown);
  el.classList.toggle("typing", !run.streamDone || run.shown < run.target.length);
}

function renderSource() {
  const el = $("#result .explain .who small");
  if (el) el.textContent = sourceLabel(state.lastExplanation);
}

function renderAskCard() {
  $("#result").innerHTML = explainCard(state.lastExplanation, state.lastQuery);
}

async function ask(text) {
  showError("");
  const btn = $("#ask-form button");
  btn.disabled = true;
  btn.textContent = "…";
  state.plans = [];
  state.activePlan = 0;
  state.lastQuery = null;
  state.lastExplanation = { source: "pending" };
  startRun(state.aiEnabled);
  renderAskCard();
  try {
    const res = await fetch("/api/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, context: state.context }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error?.message || `Request failed (${res.status})`);
    }
    for await (const [event, data] of sseEvents(res)) {
      if (event === "stage") {
        relabel(data.ai);
        stepTo(data.stage === "understanding" ? "understand" : "explain");
      } else if (event === "clarify") {
        finishRun();
        state.context = data.query;
        renderContext();
        state.lastExplanation = { source: "clarify" };
        typeText(data.message, true);
        run.streamDone = true;
        renderAskCard();
      } else if (event === "query") {
        state.lastQuery = data;
        state.context = data;
        renderContext();
        if (data.origin) setStation("from", data.origin);
        if (data.destination) setStation("to", data.destination);
        stepTo("route");
        renderAskCard();
      } else if (event === "plan") {
        renderResult({ plan: data.plan, options: data.options, explanation: state.lastExplanation, query: state.lastQuery });
      } else if (event === "explanation_delta") {
        state.lastExplanation = { source: "streaming" };
        renderSource();
        typeText(data.text);
      } else if (event === "explanation") {
        const streamed = run.target.trim() === data.text.trim();
        state.lastExplanation = data;
        run.streamDone = true;
        if (!streamed) typeText(data.text, true);
        renderSource();
        renderAnswer();
      } else if (event === "done") {
        finishRun();
      } else if (event === "error") {
        throw new Error(data.message);
      }
    }
  } catch (e) {
    showError(e.message);
  } finally {
    stopTimer();
    run.streamDone = true;
    renderAnswer();
    btn.disabled = false;
    btn.textContent = "Ask";
  }
}

function renderContext() {
  const c = state.context;
  const box = $("#context");
  if (!c || (!c.originName && !c.destinationName)) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  $("#context-text").textContent = `Follow-ups use: ${c.originName || "?"} → ${c.destinationName || "?"}`;
}

$("#context-clear").onclick = () => {
  state.context = null;
  renderContext();
};

$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#ask-text").value.trim();
  if (text) ask(text);
});
$("#ask-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#ask-form").requestSubmit();
  }
});
$$("#examples .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $("#ask-text").value = chip.textContent;
    ask(chip.textContent);
  })
);

function renderResult({ plan, options, explanation, query }) {
  state.plans = [plan, ...(options || []).filter((o) => o.summary.lines.join() !== plan.summary.lines.join() || o.summary.arriveAt !== plan.summary.arriveAt)];
  state.activePlan = 0;
  state.lastExplanation = explanation;
  state.lastQuery = query;
  paint();
}

function paint() {
  const plan = state.plans[state.activePlan];
  const explanation = state.activePlan === 0 ? state.lastExplanation : null;
  const q = state.activePlan === 0 ? state.lastQuery : null;
  $("#result").innerHTML = [explanation ? explainCard(explanation, q) : "", planCard(plan), optionsCard()].join("");
  $$("#result .option").forEach((el) =>
    el.addEventListener("click", () => {
      state.activePlan = Number(el.dataset.i);
      paint();
    })
  );
  $("#result .save-trip")?.addEventListener("click", (e) => saveTrip(plan, e.currentTarget));
  drawRoute(plan);
}

function sourceLabel(ex) {
  if (!ex) return "";
  if (ex.source === "gemini") return `Gemini · ${ex.model || ""}`;
  if (ex.source === "streaming") return "Gemini · typing";
  if (ex.source === "template") return ex.aiError ? "offline explainer (Gemini timed out)" : "offline explainer";
  return "";
}

function explainCard(ex, q) {
  const understood = q
    ? `<div class="understood">Understood: <b>${esc(q.originName)}</b> → <b>${esc(q.destinationName)}</b> · ${esc(MODE_LABELS[q.mode] || "Fastest")}${q.arriveBy ? ` · arrive by ${esc(q.arriveBy)}` : ""}${q.departAt ? ` · depart ${esc(q.departAt)}` : ""}${q.avoidNames?.length ? ` · avoid ${esc(q.avoidNames.join(", "))}` : ""} <small>(${esc(q.parser)} parser)</small></div>`
    : "";
  const typing = !run.streamDone || run.shown < run.target.length;
  const answer = `<p class="answer${typing && run.target ? " typing" : ""}">${esc(run.target.slice(0, run.shown))}</p>`;
  return `<div class="card explain"><div class="who">RouteMate <small>${esc(sourceLabel(ex))}</small></div>${progressHtml() || '<div class="progress"></div>'}${answer}${understood}</div>`;
}

function crowdBadge(crowd) {
  if (!crowd) return "";
  return `<span class="crowd crowd-${crowd.label.toLowerCase()}" title="Modelled from published HMRL ridership and peak hours, not live data">${esc(crowd.label)}</span>`;
}

function ticketsHtml(s) {
  if (!s.tickets || s.tickets.length < 2) return "";
  const rows = s.tickets.map((t) => `<li>${t.mode === "bus" ? "🚌" : "🚇"} ${esc(t.lineName)} · ${esc(t.fromName)} → ${esc(t.toName)} <b>₹${t.fare}</b></li>`).join("");
  return `<ul class="tickets">${rows}</ul>`;
}

function planCard(plan) {
  const s = plan.summary;
  const tiles = [
    [s.arriveAt, "arrive"],
    [`${Math.round(s.totalMinutes)} min`, "total"],
    [s.transfers, s.transfers === 1 ? "change" : "changes"],
    [`₹${s.fare}`, s.tickets && s.tickets.length > 1 ? `fare · ${s.tickets.length} tickets` : "fare"],
    ...(s.crowd ? [[s.crowd.label, "crowding*"]] : []),
  ]
    .map(([v, l]) => `<div class="tile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`)
    .join("");
  const deadline =
    plan.request.arriveBy == null
      ? ""
      : `<div class="deadline ${plan.request.meetsDeadline ? "ok" : "late"}">${plan.request.meetsDeadline ? `✓ Arrives before ${esc(plan.request.arriveBy)}` : `✗ Can't make ${esc(plan.request.arriveBy)}`}</div>`;
  const steps = plan.steps.map(stepItem).join("");
  const notes = plan.notes.length ? `<ul class="notes">${plan.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
  const walking = s.walkingKm ? ` · ${s.walkingKm} km on foot` : "";
  const meta = `<div class="search-meta">${esc(plan.search.algorithm)} · ${plan.search.settled} nodes settled · ${plan.search.elapsedMs} ms · leave ${esc(s.leaveAt)} · ${s.stops} stops · ${s.distanceKm} km${walking}${s.crowd ? " · *crowding is modelled, not live" : ""}</div>`;
  const save = `<button class="link save-trip" title="Save this trip">☆ Save</button>`;
  return `<div class="card"><div class="card-head"><h3>${esc(plan.origin.name)} → ${esc(plan.destination.name)}${plan.label ? ` · ${esc(plan.label)}` : ""}</h3>${save}</div><div class="tiles">${tiles}</div>${deadline}<ul class="steps">${steps}</ul>${ticketsHtml(s)}${notes}${meta}</div>`;
}

function stepItem(step) {
  if (step.type === "ride") {
    const middle = step.stations.slice(1, -1).map((id) => esc(state.stations[id].name)).join(" · ");
    const bus = step.mode === "bus";
    const rail = bus ? `background:repeating-linear-gradient(${step.color} 0 6px, transparent 6px 10px)` : `background:${step.color}`;
    return `<li class="step ride"><div class="rail"><i style="${rail}"></i></div><div><b>${bus ? "🚌 " : ""}${esc(step.lineName)}</b> towards ${esc(step.towardsName)} ${crowdBadge(step.crowd)}<div class="meta">${esc(state.stations[step.from].name)} ${esc(step.departAt)} → ${esc(state.stations[step.to].name)} ${esc(step.arriveAt)} · ${step.stopCount} stops · ${Math.round(step.minutes)} min</div>${middle ? `<details><summary>${step.stations.length - 2} intermediate ${bus ? "stops" : "stations"}</summary><div>${middle}</div></details>` : ""}</div></li>`;
  }
  const icon = step.type === "transfer" ? "⇄" : step.type === "wait" ? "⏱" : step.type === "exit" ? "↗" : "🚶";
  return `<li class="step"><div class="rail"><i></i></div><div>${icon} ${esc(step.text)}${step.minutes ? `<div class="meta">${step.minutes} min</div>` : ""}</div></li>`;
}

function optionsCard() {
  if (state.plans.length < 2) return "";
  const rows = state.plans
    .map((p, i) => {
      const s = p.summary;
      const lines = lineDots(s.lines);
      const extra = p.buses ? ` · ₹${s.fare}` : s.crowd ? ` · ${esc(s.crowd.label)}` : "";
      return `<div class="option ${i === state.activePlan ? "active" : ""}" data-i="${i}"><div><div class="lbl">${esc(i === 0 ? "Recommended" : p.label || `Option ${i}`)}</div><div class="sub">${s.transfers} change(s) · ${s.stops} stops${extra}</div></div><div style="text-align:right">${lines}<div class="sub">${esc(s.arriveAt)} · ${Math.round(s.totalMinutes)} min</div></div></div>`;
    })
    .join("");
  const axes = state.plans.some((p) => p.buses) ? "time vs. changes vs. fare" : "time vs. changes vs. crowding";
  return `<div class="card options"><h3>Trade-offs (Pareto-optimal: ${axes})</h3>${rows}</div>`;
}

$("#run-lab").addEventListener("click", async () => {
  showError("");
  try {
    const od = odParams();
    const t = $("#time").value && $("#when").value !== "now" ? `&departAt=${$("#time").value}` : "";
    const r = await api(`/api/algorithms/compare?${od}${t}`);
    const max = Math.max(...r.algorithms.map((a) => a.stats.settled));
    const rows = r.algorithms
      .map((a) => {
        const res = a.result ? Object.entries(a.result).map(([k, v]) => `${k}: ${v}`).join(", ") : "—";
        return `<tr><td>${esc(a.title)}<div class="bar" style="width:${(100 * a.stats.settled) / max}%"></div></td><td>${esc(res)}</td><td class="num">${a.stats.settled}</td><td class="num">${a.stats.elapsedMs}</td></tr>`;
      })
      .join("");
    $("#lab-out").innerHTML = `<div class="card"><h3>${esc(state.stations[r.origin].name)} → ${esc(state.stations[r.destination].name)} at ${esc(r.departAt)}</h3><div class="muted">Graph: ${r.graph.nodes} nodes · ${r.graph.edges} edges</div><button id="open-viz" class="primary wide">▶ Watch Dijkstra, A* and BFS search side by side</button><table class="lab"><tr><th>Algorithm</th><th>Result</th><th class="num">Settled</th><th class="num">ms</th></tr>${rows}</table><ul class="insights">${r.insights.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`;
    $("#open-viz").addEventListener("click", () => openViz(r));
  } catch (e) {
    showError(e.message);
  }
});

(async function boot() {
  try {
    const [network, health] = await Promise.all([api("/api/network"), api("/api/health")]);
    state.network = network;
    network.stations.forEach((s) => (state.stations[s.id] = s));
    network.lines.forEach((l) => (state.lines[l.id] = l));
    (network.busRoutes || []).forEach((r) => (state.lines[r.id] = r));
    drawNetwork();
    renderModes();
    setupAutocomplete("from");
    setupAutocomplete("to");
    const badge = $("#ai-badge");
    state.aiEnabled = health.ai.enabled;
    badge.textContent = health.ai.enabled ? `Gemini on` : "AI offline";
    badge.classList.toggle("on", health.ai.enabled);
    badge.title = health.ai.enabled ? health.ai.model : health.ai.reason || "";
  } catch (e) {
    showError(`Could not load the network: ${e.message}`);
  }
})();

function setWhen(kind, time) {
  $("#when").value = kind;
  $("#time").disabled = kind === "now";
  $("#time").value = kind === "now" ? "" : time || "";
}

function timeAgo(iso) {
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
  return `${Math.floor(sec / 86400)} d ago`;
}

const placeName = (id) => state.stations[id]?.name || id;

async function saveTrip(plan, btn) {
  showError("");
  const query = state.activePlan === 0 ? state.lastQuery : null;
  const formDepart = $("#when").value === "departAt" ? $("#time").value : null;
  const body = {
    origin: plan.origin.id,
    destination: plan.destination.id,
    mode: plan.mode === "pareto" ? state.mode : plan.mode,
    departAt: plan.request.arriveBy ? null : query ? query.departAt : formDepart,
    arriveBy: plan.request.arriveBy,
    avoid: plan.request.avoid.map((a) => a.id),
    buses: plan.buses,
  };
  try {
    await api("/api/trips", { method: "POST", body: JSON.stringify(body) });
    btn.textContent = "★ Saved";
    btn.disabled = true;
  } catch (e) {
    showError(e.message);
  }
}

function tripMeta(t) {
  const when = t.arriveBy ? `arrive by ${t.arriveBy}` : t.departAt ? `depart ${t.departAt}` : "leave now";
  const avoid = t.avoid.length ? ` · avoid ${t.avoid.map(placeName).join(", ")}` : "";
  return `${MODE_LABELS[t.mode] || t.mode} · ${when}${t.buses ? " · buses on" : ""}${avoid}`;
}

function savedHtml(trips) {
  const rows = trips.length
    ? trips
        .map(
          (t) => `<div class="trip" data-id="${t.id}"><div><b>${esc(t.name)}</b><div class="sub">${esc(tripMeta(t))}</div></div><div class="trip-actions"><button class="small go">Plan</button><button class="link del" title="Delete saved trip">Delete</button></div></div>`
        )
        .join("")
    : '<p class="muted">No saved trips yet. Plan a route and press ☆ Save.</p>';
  return `<div class="card"><h3>Saved trips</h3>${rows}</div>`;
}

function recentHtml(searches) {
  const rows = searches.length
    ? searches
        .map((s, i) => {
          const fromAsk = s.channel.startsWith("ask") && s.query;
          const what = fromAsk ? `“${esc(s.query)}”` : `${esc(placeName(s.origin))} → ${esc(placeName(s.destination))}`;
          const result = s.found ? `${Math.round(s.totalMin)} min` : "no route";
          const runnable = fromAsk || (s.origin && s.destination);
          return `<div class="recent ${runnable ? "" : "dead"}" data-i="${i}"><div>${fromAsk ? "💬 " : "🗺 "}${what}<div class="sub">${esc(MODE_LABELS[s.mode] || s.mode || "fastest")} · ${result} · ${esc(timeAgo(s.createdAt))}</div></div>${runnable ? '<span class="sub">run again ↻</span>' : ""}</div>`;
        })
        .join("")
    : '<p class="muted">Your searches will appear here.</p>';
  return `<div class="card"><h3>Recent searches</h3>${rows}</div>`;
}

function runSaved(t) {
  setStation("from", t.origin);
  setStation("to", t.destination);
  state.mode = t.mode;
  renderModes();
  setWhen(t.arriveBy ? "arriveBy" : t.departAt ? "departAt" : "now", t.arriveBy || t.departAt);
  $("#buses").checked = t.buses;
  openTab("plan");
  planRoute(t.avoid);
}

function rerunSearch(s) {
  if (s.channel.startsWith("ask") && s.query) {
    openTab("ask");
    $("#ask-text").value = s.query;
    ask(s.query);
    return;
  }
  setStation("from", s.origin);
  setStation("to", s.destination);
  if (s.mode && MODE_LABELS[s.mode]) state.mode = s.mode;
  renderModes();
  setWhen("now");
  openTab("plan");
  planRoute();
}

async function loadHistory() {
  const box = $("#history-out");
  box.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const [trips, hist] = await Promise.all([api("/api/trips"), api("/api/history?limit=25")]);
    box.innerHTML = savedHtml(trips.trips) + recentHtml(hist.searches);
    $$("#history-out .trip").forEach((el) => {
      const trip = trips.trips.find((t) => t.id === Number(el.dataset.id));
      $(".go", el).addEventListener("click", () => runSaved(trip));
      $(".del", el).addEventListener("click", async () => {
        try {
          await api(`/api/trips/${trip.id}`, { method: "DELETE" });
          loadHistory();
        } catch (e) {
          showError(e.message);
        }
      });
    });
    $$("#history-out .recent:not(.dead)").forEach((el) => el.addEventListener("click", () => rerunSearch(hist.searches[Number(el.dataset.i)])));
  } catch (e) {
    box.innerHTML = "";
    showError(e.message);
  }
}

const VIZ_ALGOS = [
  { key: "dijkstra_td", label: "Dijkstra", color: "#dc2626" },
  { key: "astar_td", label: "A*", color: "#2563eb" },
  { key: "bfs", label: "BFS", color: "#16a34a" },
];
const viz = { runs: [], timer: null, step: 0, speed: 12, syncing: false };

function openViz(report) {
  closeViz();
  const runs = VIZ_ALGOS.map((a) => ({ ...a, entry: report.algorithms.find((x) => x.key === a.key) })).filter((r) => r.entry?.exploration?.length);
  if (!runs.length) return;
  const el = document.createElement("div");
  el.id = "viz";
  el.innerHTML = `<div class="viz-bar">
      <b>${esc(placeName(report.origin))} → ${esc(placeName(report.destination))} · ${esc(report.departAt)}</b>
      <button id="viz-play" class="primary">Pause</button>
      <button id="viz-restart" class="link">Restart</button>
      <label class="muted">Speed <input id="viz-speed" type="range" min="2" max="60" value="${viz.speed}"></label>
      <span class="muted viz-help">One step = one station the search settles. The search that needs fewer steps did less work.</span>
      <button id="viz-close" class="link">Close ✕</button>
    </div>
    <div class="viz-grid">${runs
      .map(
        (r, i) => `<section class="viz-panel">
          <header><b style="color:${r.color}">${esc(r.label)}</b> <span class="viz-count" id="viz-count-${i}">0 / ${r.entry.exploration.length}</span>
            <div class="sub">${esc(r.entry.title)} · ${r.entry.stats.settled} graph nodes settled</div>
            <div class="viz-progress"><i id="viz-bar-${i}" style="background:${r.color}"></i></div></header>
          <div class="viz-map" id="viz-map-${i}"></div>
        </section>`
      )
      .join("")}</div>`;
  document.body.appendChild(el);

  const bounds = L.latLngBounds(state.network.lines.flatMap((l) => l.coords));
  viz.runs = runs.map((r, i) => {
    const m = L.map(`viz-map-${i}`, { zoomControl: i === 0, attributionControl: i === runs.length - 1 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(m);
    for (const line of state.network.lines) L.polyline(line.coords, { color: line.color, weight: 3, opacity: 0.25 }).addTo(m);
    const markers = {};
    for (const st of state.network.stations) {
      if (st.kind !== "metro") continue;
      markers[st.id] = L.circleMarker([st.lat, st.lon], { radius: 3, color: "#9ca3af", weight: 1, fillColor: "#fff", fillOpacity: 1 })
        .bindTooltip(st.name, { className: "station-label", direction: "top" })
        .addTo(m);
    }
    for (const [id, label, bg] of [[report.origin, "A", "#111827"], [report.destination, "B", "#4f46e5"]]) {
      const st = state.stations[id];
      L.marker([st.lat, st.lon], {
        icon: L.divIcon({ className: "", html: `<div class="viz-pin" style="background:${bg}">${label}</div>` }),
        zIndexOffset: 1000,
      }).addTo(m);
    }
    m.fitBounds(bounds, { padding: [10, 10] });
    m.on("move", () => {
      if (viz.syncing) return;
      viz.syncing = true;
      viz.runs.forEach((o) => o.map !== m && o.map.setView(m.getCenter(), m.getZoom(), { animate: false }));
      viz.syncing = false;
    });
    const cursor = L.circleMarker([0, 0], { radius: 10, color: r.color, weight: 3, fill: false, opacity: 0 }).addTo(m);
    return { ...r, map: m, markers, cursor, routeLayer: L.layerGroup().addTo(m), visits: {} };
  });

  $("#viz-close").addEventListener("click", closeViz);
  $("#viz-restart").addEventListener("click", () => {
    resetViz();
    playViz();
  });
  $("#viz-play").addEventListener("click", () => (viz.timer ? pauseViz() : (viz.step >= vizLength() && resetViz(), playViz())));
  $("#viz-speed").addEventListener("input", (e) => {
    viz.speed = Number(e.target.value);
    if (viz.timer) playViz();
  });
  resetViz();
  playViz();
}

const vizLength = () => Math.max(...viz.runs.map((r) => r.entry.exploration.length));

function resetViz() {
  viz.step = 0;
  viz.runs.forEach((r, i) => {
    r.visits = {};
    r.routeLayer.clearLayers();
    r.cursor.setStyle({ opacity: 0 });
    Object.values(r.markers).forEach((m) => m.setStyle({ radius: 3, color: "#9ca3af", fillColor: "#fff", fillOpacity: 1 }));
    $(`#viz-count-${i}`).textContent = `0 / ${r.entry.exploration.length}`;
    $(`#viz-bar-${i}`).style.width = "0%";
  });
}

function stepViz() {
  viz.step += 1;
  const longest = vizLength();
  viz.runs.forEach((r, i) => {
    const order = r.entry.exploration;
    if (viz.step > order.length) return;
    const sid = order[viz.step - 1];
    const seen = (r.visits[sid] = (r.visits[sid] || 0) + 1);
    const marker = r.markers[sid];
    if (marker) {
      marker.setStyle({ radius: 5 + Math.min(seen, 4), color: r.color, fillColor: r.color, fillOpacity: Math.min(0.35 + 0.15 * seen, 0.95) });
      r.cursor.setLatLng(marker.getLatLng()).setStyle({ opacity: 1 });
    }
    $(`#viz-count-${i}`).textContent = `${viz.step} / ${order.length}`;
    $(`#viz-bar-${i}`).style.width = `${(100 * viz.step) / longest}%`;
    if (viz.step === order.length) {
      const coords = r.entry.route.map((id) => [state.stations[id].lat, state.stations[id].lon]);
      L.polyline(coords, { color: "#fff", weight: 9, opacity: 0.9 }).addTo(r.routeLayer);
      L.polyline(coords, { color: r.color, weight: 5 }).addTo(r.routeLayer);
      r.cursor.setStyle({ opacity: 0 });
      $(`#viz-count-${i}`).textContent = `✓ found in ${order.length} steps`;
    }
  });
  if (viz.step >= longest) pauseViz(true);
}

function playViz() {
  clearInterval(viz.timer);
  viz.timer = setInterval(stepViz, 1000 / viz.speed);
  $("#viz-play").textContent = "Pause";
}

function pauseViz(finished = false) {
  clearInterval(viz.timer);
  viz.timer = null;
  const btn = $("#viz-play");
  if (btn) btn.textContent = finished ? "Replay" : "Play";
}

function closeViz() {
  pauseViz();
  viz.runs.forEach((r) => r.map.remove());
  viz.runs = [];
  $("#viz")?.remove();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#viz")) closeViz();
});
