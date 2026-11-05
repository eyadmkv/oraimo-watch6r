/* Oraimo Watch 6R new-tab dashboard.
 * Polls the local Python bridge (127.0.0.1) and renders live and historical data.
 * Data-integrity rules:
 *  - an unavailable measurement renders as "—" and is never shown as zero;
 *  - readings older than STALE_S are dimmed;
 *  - provisional interpretations are labelled as provisional;
 *  - unknown bytes appear verbatim in the raw feed only.
 */

"use strict";

const DEFAULT_PORT = 8765;
const POLL_MS = 5000;
const HISTORY_POLL_MS = 5 * 60 * 1000;
const STALE_S = 120;
const RECENT_HR_LIMIT = 4;
const HR_FLOOR = 40;
const HR_CEIL = 180;
const SLOTS = 288;      // five-minute slots in a day
const SLOT_MIN = 5;
const BAD_TOKEN = "unauthorized — check the bridge token in settings";
const SVG_NS = "http://www.w3.org/2000/svg";
const FEE1 = "0000fee1-0000-1000-8000-00805f9b34fb";
const FEA1 = "0000fea1-0000-1000-8000-00805f9b34fb";

const state = {
  token: "",
  port: DEFAULT_PORT,
  online: false,
  lastError: null,
  snapshot: null,
  history: null,
  recentHr: [],          // {bpm, epoch}, newest first
  dayKey: null,
  diagTimer: null,
};

/* ---------- small helpers ---------- */

const $ = (id) => document.getElementById(id);
const text = (id, value) => { const node = $(id); if (node) node.textContent = value; };
const pad = (n) => String(n).padStart(2, "0");

function make(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function fmtInt(value) {
  return value === null || value === undefined ? "—" : Number(value).toLocaleString("en-US");
}
function fmtKm(metres) {
  return metres === null || metres === undefined ? "—" : `${(metres / 1000).toFixed(2)} km`;
}
function fmtKcal(value) {
  return value === null || value === undefined ? "—" : `${fmtInt(value)} kcal`;
}
function fmtClock(epoch) {
  const d = new Date(epoch * 1000);
  return Number.isNaN(d.getTime()) ? "—" : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtTime(isoOrEpoch) {
  if (!isoOrEpoch) return "—";
  const d = typeof isoOrEpoch === "number" ? new Date(isoOrEpoch * 1000) : new Date(isoOrEpoch);
  if (Number.isNaN(d.getTime())) return "—";
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function ageSeconds(stamp) {
  if (!stamp || !stamp.epoch) return null;
  return Math.max(0, Date.now() / 1000 - stamp.epoch);
}
function slotLabel(slot) {
  const minutes = slot * SLOT_MIN;
  return `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
}
function isoWeek(date) {
  const t = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t - yearStart) / 86400000 + 1) / 7);
}

/* A changed numeral gets a short highlighter wash: motion that reports state. */
function setNumber(id, value) {
  const node = $(id);
  if (!node) return;
  const previous = node.textContent;
  node.textContent = value;
  node.classList.toggle("is-empty", value === "—");
  if (previous !== value && previous !== "—" && value !== "—") {
    node.classList.remove("mark");
    void node.offsetWidth;
    node.classList.add("mark");
  }
}

/* ---------- bridge API ---------- */

async function api(path, options = {}) {
  const headers = { "X-Oraimo-Token": state.token, ...(options.headers || {}) };
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${state.port}${path}`, { ...options, headers });
  } catch (err) {
    state.online = false;
    state.lastError = "bridge unreachable";
    throw err;
  }
  state.online = true;
  state.lastError = null;
  const payload = await response.json().catch(() => null);
  if (response.status === 401) {
    state.lastError = BAD_TOKEN;
    throw new Error(state.lastError);
  }
  if (!response.ok || !payload || payload.ok !== true) {
    state.lastError = payload && payload.error ? payload.error : `HTTP ${response.status}`;
    throw new Error(state.lastError);
  }
  return payload;
}

/* ---------- connection and status ---------- */

const STATUS_LABELS = {
  connected: ["on", "Connected"],
  connecting: ["warn", "Connecting…"],
  scanning: ["warn", "Scanning…"],
  "no-watch-found": ["off", "Watch not found"],
  reconnecting: ["warn", "Reconnecting…"],
  error: ["off", "Connection error"],
  stopped: ["off", "Bridge stopped"],
};

function setConnection(kind, label) {
  $("connection-dot").className = `dot dot-${kind}`;
  text("connection-label", label);
}

function renderStatus(status) {
  const [kind, label] = STATUS_LABELS[status.state] || ["off", String(status.state)];
  setConnection(kind, label);
  text("dv-name", status.watch_name || "—");
  text("dv-address", status.watch_address || "—");
  const detail = status.state === "error" || status.state === "no-watch-found"
    ? (status.detail || status.last_error)
    : "";
  text("dv-state", detail ? `${label}: ${detail}` : label);
  text("dv-since", status.state === "connected" && status.connected_since ? fmtClock(status.connected_since.epoch) : "—");
  text("bridge-version", status.bridge_version ? `bridge ${status.bridge_version}` : "");
  $("reconnect-btn").disabled = false;
}

/* ---------- live snapshot ---------- */

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  const notifyAge = ageSeconds(snapshot.last_notification_at);
  const stale = notifyAge !== null && notifyAge > STALE_S;

  setNumber("ov-steps", fmtInt(snapshot.steps));
  $("ov-steps").classList.toggle("stale", snapshot.steps !== null && stale);
  text("ov-steps-src", snapshot.steps_source
    ? `${stale ? "last seen" : "live"} · ${snapshot.steps_source}`
    : "awaiting the watch");
  setNumber("ov-distance", fmtKm(snapshot.distance_m));
  setNumber("ov-calories", fmtKcal(snapshot.calories_kcal));
  $("ov-distance").classList.toggle("stale", snapshot.distance_m !== null && stale);
  $("ov-calories").classList.toggle("stale", snapshot.calories_kcal !== null && stale);

  text("dv-packet", snapshot.last_notification_at ? fmtTime(snapshot.last_notification_at.epoch) : "none yet");
  renderSteps();
  renderBattery();
  renderHeart();
  text("last-sync", `synced ${fmtClock(Date.now() / 1000)}`);
}

/* Steps read against the goal on a measured ruler. */
function niceTick(max) {
  return max <= 4000 ? 500 : max <= 12000 ? 1000 : 2000;
}
function tickText(value) {
  if (value === 0) return "0";
  return value % 1000 === 0 ? `${value / 1000}k` : `${(value / 1000).toFixed(1)}k`;
}

function renderSteps() {
  const snap = state.snapshot || {};
  const hist = state.history || {};
  const steps = snap.steps === undefined ? null : snap.steps;
  const goal = snap.step_goal ?? hist.step_goal ?? null;
  const pastActivity = hist.past_activity || {};
  const past = [["Yesterday", pastActivity["1"]], ["2 days ago", pastActivity["2"]]]
    .filter(([, entry]) => entry && entry.steps !== null && entry.steps !== undefined);

  const values = [goal, steps, ...past.map(([, entry]) => entry.steps)]
    .filter((v) => v !== null && v !== undefined);
  const highest = values.length ? Math.max(...values) : 8000;
  const tick = niceTick(highest);
  const scale = Math.max(tick * 2, Math.ceil(highest / tick) * tick);
  const at = (v) => (Math.min(v, scale) / scale) * 100;

  $("goal-bar").style.width = steps === null ? "0%" : `${at(steps)}%`;

  const marks = $("ruler-marks");
  marks.textContent = "";
  for (let v = 0; v <= scale; v += tick) {
    const t = make("div", "tick");
    t.style.left = `${at(v)}%`;
    const label = make("div", "tick-label", tickText(v));
    label.style.left = `${at(v)}%`;
    marks.append(t, label);
  }
  if (goal) {
    const g = make("div", "goal-mark");
    g.style.left = `${at(goal)}%`;
    marks.appendChild(g);
  }
  for (const [label, entry] of past) {
    const ghost = make("div", "ghost");
    ghost.style.left = `${at(entry.steps)}%`;
    ghost.title = `${label}: ${fmtInt(entry.steps)} steps`;
    marks.appendChild(ghost);
  }
  if (steps !== null) {
    const f = at(steps) / 100;
    const flag = make("div", `flag${f < 0.06 ? " flag-start" : f > 0.94 ? " flag-end" : ""}`, "Today");
    flag.style.left = `${at(steps)}%`;
    marks.appendChild(flag);
  }

  const legend = $("ruler-legend");
  legend.textContent = "";
  if (goal) {
    const li = make("li");
    li.append(make("i", "sw sw-goal"), document.createTextNode(`Goal ${fmtInt(goal)}`));
    legend.appendChild(li);
  }
  for (const [label, entry] of past) {
    const li = make("li");
    li.append(make("i", "sw sw-ghost"), document.createTextNode(`${label} ${fmtInt(entry.steps)}`));
    legend.appendChild(li);
  }

  if (steps !== null && goal) text("ov-goal-text", `of ${fmtInt(goal)} · ${Math.round((steps / goal) * 100)}%`);
  else if (goal) text("ov-goal-text", `goal ${fmtInt(goal)} · waiting for today’s count`);
  else text("ov-goal-text", "goal unavailable");

  const parts = [steps === null ? "No step count yet today" : `${fmtInt(steps)} steps today`];
  if (goal) parts.push(`goal ${fmtInt(goal)}`);
  for (const [label, entry] of past) parts.push(`${label} ${fmtInt(entry.steps)}`);
  $("steps-ruler").setAttribute("aria-label", parts.join(", "));
}

function renderBattery() {
  const snap = state.snapshot || {};
  const info = state.history && state.history.device_info;
  let percent = snap.battery_percent === undefined ? null : snap.battery_percent;
  let note = "";
  if (percent !== null) {
    note = snap.battery_received_at ? `read ${fmtClock(snap.battery_received_at.epoch)}` : "";
  } else if (info && info.battery_percent !== null && info.battery_percent !== undefined) {
    percent = info.battery_percent;
    note = "read when the watch connected";
  }
  setNumber("ov-battery", percent === null ? "—" : `${percent}%`);
  text("ov-battery-age", percent === null ? "never read" : note);

  const cells = $("battery-cells");
  const filled = percent === null ? 0 : Math.max(percent > 0 ? 1 : 0, Math.round(percent / 10));
  [...cells.children].forEach((cell, i) => cell.classList.toggle("on", i < filled));
  cells.classList.toggle("low", percent !== null && percent <= 20);
  cells.setAttribute("aria-label", percent === null ? "Battery level unknown" : `Battery ${percent} percent`);
}

/* ---------- heart rate ---------- */

const hrPct = (bpm) => ((Math.min(HR_CEIL, Math.max(HR_FLOOR, bpm)) - HR_FLOOR) / (HR_CEIL - HR_FLOOR)) * 100;

/* Five-minute slots for one day, from the watch's 72-sample history blocks. */
function daySamples(daysAgo) {
  const blocks = ((state.history && state.history.heart_rate_history) || []).filter((b) => b.days_ago === daysAgo);
  if (!blocks.length) return null;
  const slots = new Array(SLOTS).fill(0);
  for (const block of blocks) {
    (block.samples_bpm || []).forEach((bpm, i) => {
      const slot = block.start_hour * 12 + i;
      if (slot >= 0 && slot < SLOTS) slots[slot] = bpm;
    });
  }
  return slots;
}

function sampleStats(slots) {
  let min = Infinity, max = -Infinity, sum = 0, count = 0, last = -1;
  slots.forEach((bpm, i) => {
    if (bpm > 0) { count += 1; sum += bpm; min = Math.min(min, bpm); max = Math.max(max, bpm); last = i; }
  });
  return count ? { min, max, count, avg: sum / count, last } : null;
}

function renderHeart() {
  const snap = state.snapshot || {};
  const hr = snap.heart_rate_bpm === undefined ? null : snap.heart_rate_bpm;
  const age = ageSeconds(snap.heart_rate_received_at);
  const live = hr !== null && age !== null && age <= STALE_S;

  setNumber("hr-current", live ? String(hr) : "—");
  text("hr-status", live ? `live · ${fmtTime(snap.heart_rate_received_at.epoch)}` : "not measuring");
  if (live) {
    const epoch = snap.heart_rate_received_at.epoch;
    if (!state.recentHr.length || state.recentHr[0].epoch !== epoch) {
      state.recentHr.unshift({ bpm: hr, epoch });
      state.recentHr.length = Math.min(state.recentHr.length, RECENT_HR_LIMIT);
    }
  }

  const today = daySamples(0);
  const stats = today && sampleStats(today);
  if (live) text("hr-note", "Reading from the watch now.");
  else if (stats) text("hr-note", `Last stored reading ${today[stats.last]} bpm at ${slotLabel(stats.last)}. Start a measurement on the watch to see live values.`);
  else text("hr-note", "No stored readings today yet. Start a measurement on the watch to see live values.");

  const span = $("hr-span");
  if (stats) {
    const lo = hrPct(stats.min);
    const hi = hrPct(stats.max);
    span.style.left = `${lo}%`;
    span.style.width = `${Math.max(hi - lo, 1.5)}%`;
    span.classList.add("on");
    const line = $("hr-today");
    line.textContent = "";
    line.append(
      make("b", "", `Today ${stats.min}–${stats.max} bpm`),
      make("span", "", `average ${Math.round(stats.avg)} · ${stats.count} ${stats.count === 1 ? "reading" : "readings"}`),
    );
    $("hr-range").setAttribute("aria-label", `Today’s heart rate ranged from ${stats.min} to ${stats.max} bpm`);
  } else {
    span.classList.remove("on");
    text("hr-today", "Today: no readings yet");
    $("hr-range").setAttribute("aria-label", "No heart-rate readings yet today");
  }

  const list = $("hr-recent");
  list.textContent = "";
  for (const reading of state.recentHr) {
    const li = make("li");
    li.append(make("span", "", fmtTime(reading.epoch)), make("b", "", `${reading.bpm} bpm`));
    list.appendChild(li);
  }
}

/* ---------- the ruled day ---------- */

function svgRect(x, y, w, h, title) {
  const rect = document.createElementNS(SVG_NS, "rect");
  rect.setAttribute("x", x.toFixed(2));
  rect.setAttribute("y", y.toFixed(2));
  rect.setAttribute("width", w);
  rect.setAttribute("height", h.toFixed(2));
  const tip = document.createElementNS(SVG_NS, "title");
  tip.textContent = title;
  rect.appendChild(tip);
  return rect;
}

function buildDayRow(label, daysAgo, hourly) {
  const slots = daySamples(daysAgo);
  const stats = slots && sampleStats(slots);
  const row = make("div", "day-row");

  const gutter = make("div", "day-gutter");
  gutter.append(make("h3", "", label));
  gutter.append(make("p", "", stats ? `${stats.min}–${stats.max} bpm` : "no readings"));
  if (stats) gutter.append(make("p", "", `${stats.count} readings`));

  const hasLane = Array.isArray(hourly) && hourly.length === 24;
  const plot = make("div", `day-plot${daysAgo > 0 ? " past" : ""}${hasLane ? " has-steps" : ""}`);
  plot.setAttribute("role", "img");
  plot.setAttribute("aria-label", stats
    ? `${label}: heart rate between ${stats.min} and ${stats.max} bpm across ${stats.count} readings`
    : `${label}: no heart-rate readings`);

  if (!slots) {
    plot.appendChild(make("div", "plot-empty", "No heart-rate history came back for this day."));
  } else {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "hr-bars");
    svg.setAttribute("viewBox", "0 0 288 100");
    svg.setAttribute("preserveAspectRatio", "none");
    slots.forEach((bpm, i) => {
      if (bpm <= 0) return;
      const h = 4 + (hrPct(bpm) / 100) * 96;
      svg.appendChild(svgRect(i + 0.14, 100 - h, 0.72, h, `${slotLabel(i)} · ${bpm} bpm`));
    });
    plot.appendChild(svg);
  }

  if (hasLane) {
    const lane = make("div", "steps-lane");
    const peak = Math.max(...hourly);
    hourly.forEach((value, hour) => {
      const bar = make("i");
      bar.style.height = value > 0 && peak > 0 ? `${Math.max(8, (value / peak) * 100)}%` : "0";
      bar.title = `${pad(hour)}:00–${pad(hour + 1)}:00 · ${fmtInt(value)} steps`;
      lane.appendChild(bar);
    });
    plot.appendChild(lane);
  }

  if (daysAgo === 0) {
    plot.append(make("div", "future"), make("div", "now-line"), make("div", "now-flag"));
  }
  row.append(gutter, plot);
  return row;
}

function renderDay() {
  const axis = $("day-axis");
  if (!axis.childElementCount) {
    for (let h = 0; h <= 24; h += 3) {
      const mark = make("span", "", pad(h));
      mark.style.setProperty("--h", h);
      axis.appendChild(mark);
    }
  }
  const hist = state.history || {};
  const hourly = hist.hourly_activity && hist.hourly_activity.day_index === 0
    ? hist.hourly_activity.steps_by_bucket
    : null;
  const rows = $("day-rows");
  rows.textContent = "";
  rows.append(buildDayRow("Today", 0, hourly), buildDayRow("Yesterday", 1, null));
  updateNow();
}

function updateNow() {
  const plot = document.querySelector(".day-plot:not(.past)");
  if (!plot) return;
  const now = new Date();
  const fraction = (now.getHours() * 60 + now.getMinutes()) / 1440;
  plot.style.setProperty("--now", fraction.toFixed(4));
  const flag = plot.querySelector(".now-flag");
  if (flag) {
    flag.textContent = `now ${pad(now.getHours())}:${pad(now.getMinutes())}`;
    flag.classList.toggle("end", fraction > 0.82);
  }
}

/* ---------- history ---------- */

function goalCell(steps, goal) {
  const cell = make("td");
  if (!goal || steps === null || steps === undefined) {
    cell.textContent = "—";
    return cell;
  }
  const wrap = make("div", "gcell");
  const bar = make("span", "gbar");
  const fill = make("i");
  fill.style.width = `${Math.min(100, (steps / goal) * 100)}%`;
  bar.appendChild(fill);
  wrap.append(bar, make("span", "gpct", `${Math.round((steps / goal) * 100)}%`));
  cell.appendChild(wrap);
  return cell;
}

function renderRecentDays() {
  const hist = state.history || {};
  const goal = (state.snapshot && state.snapshot.step_goal) ?? hist.step_goal ?? null;
  const body = $("history-activity");
  body.textContent = "";
  let rows = 0;
  for (const [key, label] of [["1", "Yesterday"], ["2", "2 days ago"]]) {
    const entry = hist.past_activity ? hist.past_activity[key] : null;
    if (!entry) continue;
    rows += 1;
    const tr = make("tr");
    tr.append(
      make("td", "", label),
      make("td", "r", fmtInt(entry.steps)),
      make("td", "r", fmtKm(entry.distance_m)),
      make("td", "r", fmtKcal(entry.calories_kcal)),
      goalCell(entry.steps, goal),
    );
    body.appendChild(tr);
  }
  if (!rows) {
    const tr = make("tr", "empty");
    const td = make("td", "", "No daily activity has been collected yet.");
    td.colSpan = 5;
    tr.appendChild(td);
    body.appendChild(tr);
  }
}

function renderSleep(stages) {
  const box = $("history-sleep");
  box.textContent = "";
  if (stages && stages.length) {
    for (const stage of stages) {
      const row = make("div", "stage-row");
      row.append(make("span", "", `${pad(stage.hour)}:${pad(stage.minute)}`), make("b", "", stage.stage_name));
      box.appendChild(row);
    }
    return;
  }
  box.append(
    make("p", "empty-title", "Not recorded"),
    make("p", "fine", "The watch has reported no sleep. Nothing is estimated."),
  );
}

function renderHistory(data) {
  state.history = data;
  const info = data.device_info;
  if (info) {
    text("dv-manufacturer", info.manufacturer || "—");
    text("dv-firmware", info.firmware || "—");
    text("dv-software", info.software || "—");
    if (info.connected_name && $("dv-name").textContent === "—") text("dv-name", info.connected_name);
  }
  renderSteps();
  renderBattery();
  renderHeart();
  renderDay();
  renderRecentDays();
  renderSleep(data.sleep_today);
}

/* ---------- raw feed ---------- */

function renderDiagnostics(data) {
  const c = data.counters || {};
  text("diag-counters",
    `${c.notifications_total || 0} notifications · ${c.decoded_total || 0} decoded · ${c.rejected_total || 0} rejected · ${c.connection_errors_total || 0} connection errors`);

  const notifications = $("diag-notifications");
  notifications.textContent = "";
  const recent = (data.notifications || []).slice().reverse().slice(0, 40);
  for (const entry of recent) {
    const li = make("li");
    const name = entry.uuid === FEA1 ? "FEA1" : entry.uuid === FEE1 ? "FEE1" : entry.uuid.slice(4, 8);
    const left = make("span", `status-${entry.status}`, `${name} ${entry.status}: ${entry.payload_hex}`);
    if (entry.error) left.appendChild(make("span", "err", `↳ ${entry.error}`));
    li.append(left, make("span", "t", fmtTime(entry.received_at.epoch)));
    notifications.appendChild(li);
  }
  if (!recent.length) notifications.appendChild(make("li", "", "No notifications received yet."));

  const connections = $("diag-connection");
  connections.textContent = "";
  const events = (data.connection_events || []).slice().reverse().slice(0, 25);
  for (const entry of events) {
    const li = make("li");
    li.append(make("span", "", entry.event + (entry.detail ? `: ${entry.detail}` : "")), make("span", "t", fmtTime(entry.received_at.epoch)));
    connections.appendChild(li);
  }
  if (!events.length) connections.appendChild(make("li", "", "No connection events yet."));
}

/* ---------- banners and offline handling ---------- */

function showBanner(kind, message) {
  const banner = $("banner");
  banner.className = `banner banner-${kind}`;
  banner.textContent = message;
}
function hideBanner() {
  $("banner").className = "banner hidden";
}

function renderBridgeState() {
  if (!state.online) {
    showBanner("error", "The bridge is offline. Start it with “python -m bridge” from the project folder. The values below may be out of date.");
    setConnection("off", "Bridge offline");
    return;
  }
  if (state.lastError === BAD_TOKEN) {
    showBanner("warn", "The bridge rejected the token. Open settings and paste the token it printed.");
    return;
  }
  hideBanner();
}

/* ---------- polling ---------- */

async function pollStatusAndSnapshot() {
  const [statusPayload, snapshotPayload] = await Promise.all([api("/api/status"), api("/api/snapshot")]);
  // /api/status returns its fields at the top level; the other endpoints wrap them in "data".
  renderStatus(statusPayload.data || statusPayload);
  renderSnapshot(snapshotPayload.data);
  renderBridgeState();
}
async function pollHistory() {
  renderHistory((await api("/api/history")).data);
}
async function pollDiagnostics() {
  renderDiagnostics((await api("/api/diagnostics")).data);
}
async function safePoll(fn) {
  try {
    await fn();
  } catch (err) {
    renderBridgeState();
  }
}

/* ---------- clock ---------- */

function tickClock() {
  const now = new Date();
  text("clock-time", `${pad(now.getHours())}:${pad(now.getMinutes())}`);
  text("clock-date", now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }));
  text("clock-year", `${now.getFullYear()} · week ${isoWeek(now)}`);
  const key = now.toDateString();
  if (state.dayKey && state.dayKey !== key) safePoll(pollHistory);   // midnight: today becomes yesterday
  state.dayKey = key;
  updateNow();
}

/* ---------- settings ---------- */

function loadSettings() {
  return new Promise((resolve) => {
    if (typeof chrome === "undefined" || !chrome.storage) { resolve(); return; }
    chrome.storage.local.get({ token: "", port: DEFAULT_PORT }, (items) => {
      state.token = items.token;
      state.port = items.port || DEFAULT_PORT;
      resolve();
    });
  });
}

function saveToken() {
  const value = $("token-input").value.trim();
  if (!value) {
    text("settings-feedback", "The token is empty. Paste the value the bridge printed.");
    return;
  }
  chrome.storage.local.set({ token: value }, () => {
    state.token = value;
    text("settings-feedback", "Token saved.");
    safePoll(pollStatusAndSnapshot);
    safePoll(pollHistory);
  });
}

/* ---------- wiring ---------- */

function wireUi() {
  $("reconnect-btn").addEventListener("click", async () => {
    $("reconnect-btn").disabled = true;
    try {
      await api("/api/reconnect", { method: "POST" });
      text("last-sync", "reconnect requested");
      setTimeout(() => safePoll(pollStatusAndSnapshot), 1000);
    } catch (err) {
      renderBridgeState();
    } finally {
      setTimeout(() => { $("reconnect-btn").disabled = false; }, 1500);
    }
  });

  $("settings-btn").addEventListener("click", () => {
    const open = $("settings-panel").classList.toggle("hidden") === false;
    $("settings-btn").setAttribute("aria-expanded", String(open));
    if (open) $("token-input").focus();
  });

  $("token-save").addEventListener("click", saveToken);
  $("token-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveToken();
  });

  $("diag-refresh").addEventListener("click", () => safePoll(pollDiagnostics));
  $("diag-details").addEventListener("toggle", (event) => {
    if (event.target.open) {
      safePoll(pollDiagnostics);
      state.diagTimer = setInterval(() => safePoll(pollDiagnostics), POLL_MS);
    } else if (state.diagTimer) {
      clearInterval(state.diagTimer);
      state.diagTimer = null;
    }
  });

  document.querySelectorAll(".num").forEach((node) => {
    node.addEventListener("animationend", () => node.classList.remove("mark"));
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.token) {
      safePoll(pollStatusAndSnapshot);
      safePoll(pollHistory);
    }
  });
}

async function init() {
  wireUi();
  tickClock();
  setInterval(tickClock, 10000);

  await loadSettings();
  if (state.token) $("token-input").value = state.token;
  else showBanner("warn", "First run: open settings and paste the token the bridge printed when it started.");

  renderSteps();
  renderHeart();
  renderDay();
  setInterval(() => safePoll(pollStatusAndSnapshot), POLL_MS);
  setInterval(() => safePoll(pollHistory), HISTORY_POLL_MS);
  await Promise.all([safePoll(pollStatusAndSnapshot), safePoll(pollHistory)]);
}

init();