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
const routeLayer = L.layerGroup().addTo(map);
const stationMarkers = {};

function drawNetwork() {
  networkLayer.clearLayers();
  for (const line of state.network.lines) {
    L.polyline(line.coords, { color: line.color, weight: 5, opacity: 0.35 }).addTo(networkLayer);
  }
  for (const st of state.network.stations) {
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

function drawRoute(plan) {
  routeLayer.clearLayers();
  if (!plan) return;
  const bounds = [];
  for (const seg of plan.geometry) {
    L.polyline(seg.coords, { color: "#fff", weight: 11, opacity: 0.9 }).addTo(routeLayer);
    L.polyline(seg.coords, { color: seg.color, weight: 7, opacity: 1 }).addTo(routeLayer);
    for (const c of seg.coords) {
      L.circleMarker(c, { radius: 3.5, color: seg.color, weight: 2, fillColor: "#fff", fillOpacity: 1 }).addTo(routeLayer);
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
  })
);

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
  $$(".tab").find((b) => b.dataset.tab === "plan").click();
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
      .map((s, i) => `<li data-i="${i}" class="${i === active ? "active" : ""}"><span>${esc(s.name)}</span>${lineDots(s.lines)}</li>`)
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

async function planRoute() {
  showError("");
  const btn = $("#plan-form button[type=submit]");
  btn.disabled = true;
  try {
    const od = odParams();
    const plan = await api(`/api/route?${od}&mode=${state.mode}${timeParams()}`);
    const opts = await api(`/api/routes/options?${od}&departAt=${plan.summary.leaveAt}`).catch(() => ({ options: [] }));
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

function planCard(plan) {
  const s = plan.summary;
  const tiles = [
    [s.arriveAt, "arrive"],
    [`${Math.round(s.totalMinutes)} min`, "total"],
    [s.transfers, s.transfers === 1 ? "change" : "changes"],
    [`₹${s.fare}`, "fare"],
  ]
    .map(([v, l]) => `<div class="tile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`)
    .join("");
  const deadline =
    plan.request.arriveBy == null
      ? ""
      : `<div class="deadline ${plan.request.meetsDeadline ? "ok" : "late"}">${plan.request.meetsDeadline ? `✓ Arrives before ${esc(plan.request.arriveBy)}` : `✗ Can't make ${esc(plan.request.arriveBy)}`}</div>`;
  const steps = plan.steps.map(stepItem).join("");
  const notes = plan.notes.length ? `<ul class="notes">${plan.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
  const meta = `<div class="search-meta">${esc(plan.search.algorithm)} · ${plan.search.settled} nodes settled · ${plan.search.elapsedMs} ms · leave ${esc(s.leaveAt)} · ${s.stops} stops · ${s.distanceKm} km</div>`;
  return `<div class="card"><h3>${esc(plan.origin.name)} → ${esc(plan.destination.name)}${plan.label ? ` · ${esc(plan.label)}` : ""}</h3><div class="tiles">${tiles}</div>${deadline}<ul class="steps">${steps}</ul>${notes}${meta}</div>`;
}

function stepItem(step) {
  if (step.type === "ride") {
    const middle = step.stations.slice(1, -1).map((id) => esc(state.stations[id].name)).join(" · ");
    return `<li class="step ride"><div class="rail"><i style="background:${step.color}"></i></div><div><b>${esc(step.lineName)}</b> towards ${esc(step.towardsName)}<div class="meta">${esc(state.stations[step.from].name)} ${esc(step.departAt)} → ${esc(state.stations[step.to].name)} ${esc(step.arriveAt)} · ${step.stopCount} stops · ${Math.round(step.minutes)} min</div>${middle ? `<details><summary>${step.stations.length - 2} intermediate stations</summary><div>${middle}</div></details>` : ""}</div></li>`;
  }
  const icon = step.type === "transfer" ? "⇄" : step.type === "wait" ? "⏱" : "🚶";
  return `<li class="step"><div class="rail"><i></i></div><div>${icon} ${esc(step.text)}${step.minutes ? `<div class="meta">${step.minutes} min</div>` : ""}</div></li>`;
}

function optionsCard() {
  if (state.plans.length < 2) return "";
  const rows = state.plans
    .map((p, i) => {
      const s = p.summary;
      const lines = lineDots(s.lines);
      return `<div class="option ${i === state.activePlan ? "active" : ""}" data-i="${i}"><div><div class="lbl">${esc(i === 0 ? "Recommended" : p.label || `Option ${i}`)}</div><div class="sub">${s.transfers} change(s) · ${s.stops} stops</div></div><div style="text-align:right">${lines}<div class="sub">${esc(s.arriveAt)} · ${Math.round(s.totalMinutes)} min</div></div></div>`;
    })
    .join("");
  return `<div class="card options"><h3>Trade-offs (Pareto-optimal: time vs. changes)</h3>${rows}</div>`;
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
    $("#lab-out").innerHTML = `<div class="card"><h3>${esc(state.stations[r.origin].name)} → ${esc(state.stations[r.destination].name)} at ${esc(r.departAt)}</h3><div class="muted">Graph: ${r.graph.nodes} nodes · ${r.graph.edges} edges</div><table class="lab"><tr><th>Algorithm</th><th>Result</th><th class="num">Settled</th><th class="num">ms</th></tr>${rows}</table><ul class="insights">${r.insights.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`;
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
