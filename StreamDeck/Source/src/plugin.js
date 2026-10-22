/*
 * DA Timer — Stream Deck plugin
 *
 * Talks to the Stream Deck app over its WebSocket (SDK protocol) and to
 * DA Timer over DA Timer's local control API (http://127.0.0.1:47800).
 * Keys are drawn live as SVG so they show the actual clocks and states.
 */
"use strict";
const WebSocket = require("ws");

// ----------------------------------------------------------------- launch
const args = {};
for (let i = 2; i < process.argv.length - 1; i += 2) {
  args[process.argv[i].replace(/^-+/, "")] = process.argv[i + 1];
}
const PREFIX = "com.gravitymedia.datimer.";
const DEFAULT_HOST = "127.0.0.1:47800";
const POLL_MS = 250;
const HOLD_MS = 1000;

let host = DEFAULT_HOST;
let state = null;
let online = false;
const contexts = new Map(); // context -> { action, settings, last, downAt }

const ws = new WebSocket(`ws://127.0.0.1:${args.port}`);
const send = (msg) => {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
};

ws.on("open", () => {
  send({ event: args.registerEvent, uuid: args.pluginUUID });
  send({ event: "getGlobalSettings", context: args.pluginUUID });
});
ws.on("close", () => process.exit(0));

ws.on("message", (raw) => {
  let m;
  try {
    m = JSON.parse(raw);
  } catch {
    return;
  }
  const { event, context } = m;
  const payload = m.payload || {};
  switch (event) {
    case "willAppear":
      contexts.set(context, {
        action: m.action.replace(PREFIX, ""),
        settings: payload.settings || {},
        last: null,
        downAt: 0,
      });
      render(context);
      break;
    case "willDisappear":
      contexts.delete(context);
      break;
    case "didReceiveSettings": {
      const c = contexts.get(context);
      if (c) {
        c.settings = payload.settings || {};
        c.last = null;
        render(context);
      }
      break;
    }
    case "didReceiveGlobalSettings":
      host = cleanHost((payload.settings || {}).host);
      break;
    case "keyDown":
      onKeyDown(context);
      break;
    case "keyUp":
      onKeyUp(context);
      break;
    case "sendToPlugin":
      onPropertyInspector(context, m.action, payload);
      break;
  }
});

function cleanHost(h) {
  h = String(h || "").trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  return h || DEFAULT_HOST;
}

// ----------------------------------------------------------- DA Timer API
async function fetchState() {
  try {
    const r = await fetch(`http://${host}/api/state`, {
      signal: AbortSignal.timeout(800),
    });
    if (!r.ok) throw new Error(String(r.status));
    state = await r.json();
    online = !!(state && state.segment);
  } catch {
    online = false;
  }
}

async function command(cmd, context) {
  try {
    const r = await fetch(`http://${host}/api/action/${cmd}`, {
      method: "POST",
      headers: { "X-DA-Timer": "1" },
      signal: AbortSignal.timeout(1500),
    });
    if (!r.ok) throw new Error(String(r.status));
    setTimeout(poll, 80); // refresh keys quickly after a press
    return true;
  } catch {
    send({ event: "showAlert", context });
    return false;
  }
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    if (contexts.size) await fetchState();
    for (const ctx of contexts.keys()) render(ctx);
  } finally {
    polling = false;
  }
}
setInterval(poll, POLL_MS);

// ----------------------------------------------------------------- keys
function pressCommand(c) {
  const s = c.settings;
  switch (c.action) {
    case "clock": {
      const press = s.press || "toggle";
      const src = s.source || "segment";
      if (press === "none") return null;
      if (press === "next") return "next_segment";
      if (press === "mark") return "mark";
      return src.startsWith("prog") ? "prog_toggle" : "seg_toggle";
    }
    case "alltoggle": return "all_toggle";
    case "allstart": return "all_start";
    case "allstop": return "all_stop";
    case "segtoggle": return "seg_toggle";
    case "progtoggle": return "prog_toggle";
    case "next": return "next_segment";
    case "mark": return "mark";
    case "undo": return "undo";
    case "display": return `${s.target || "dual"}_toggle`;
    default: return null;
  }
}

function onKeyDown(context) {
  const c = contexts.get(context);
  if (!c) return;
  c.downAt = Date.now();
  if (c.action === "reset") {
    c.holding = true;
    c.last = null;
    render(context); // show "keep holding"
    c.holdTimer = setTimeout(() => {
      c.held = true;
      command("reset_program", context).then((ok) => {
        if (ok) send({ event: "showOk", context });
      });
    }, HOLD_MS);
    return;
  }
  const cmd = pressCommand(c);
  if (cmd) {
    command(cmd, context).then((ok) => {
      if (ok && c.action === "mark") send({ event: "showOk", context });
    });
  }
}

function onKeyUp(context) {
  const c = contexts.get(context);
  if (!c || c.action !== "reset") return;
  clearTimeout(c.holdTimer);
  if (!c.held) send({ event: "showAlert", context }); // released too early
  c.held = false;
  c.holding = false;
  c.last = null;
  render(context);
}

function onPropertyInspector(context, action, payload) {
  if (payload.host !== undefined) host = cleanHost(payload.host);
  if (payload.test) {
    fetchState().then(() => {
      send({
        event: "sendToPropertyInspector",
        action,
        context,
        payload: {
          testResult: online
            ? `Connected to ${state.app} ${state.version} — show "${state.show}"`
            : `Can't reach DA Timer at ${host}. Is it running?`,
          ok: online,
        },
      });
    });
  }
}

// ------------------------------------------------------------- drawing
const C = {
  bg: "#0a0d11", surface: "#12171e", text: "#eef3f6", muted: "#8592a3",
  faint: "#4a5566", cyan: "#00e5f0", green: "#22d37f", red: "#ff4d5e",
  amber: "#ffb547", greenBg: "#0d3a24", redBg: "#3a1419", cyanBg: "#073a40",
};

function short(sec) {
  sec = Math.abs(Math.round(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}
const signed = (sec) => (sec > 0 ? "+" : sec < 0 ? "−" : "") + short(sec);

function esc(t) {
  return String(t).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;",
  })[ch]);
}

function fit(text, max) {
  text = String(text || "");
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

function mainSize(text) {
  const n = String(text).length;
  return n <= 2 ? 64 : n <= 4 ? 50 : n <= 5 ? 44 : n <= 6 ? 38 : n <= 7 ? 33 : 29;
}

function key({ label = "", main = "", sub = "", bg = C.bg, labelColor = C.cyan,
  mainColor = C.text, subColor = C.muted, bar = null, size = null }) {
  const ms = size || mainSize(main);
  const font = "Segoe UI, Bahnschrift, Arial, sans-serif";
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">` +
    `<rect width="144" height="144" fill="${bg}"/>` +
    `<text x="72" y="30" text-anchor="middle" font-family="${font}" font-size="19" font-weight="700" letter-spacing="1" fill="${labelColor}">${esc(fit(label, 12))}</text>` +
    `<text x="72" y="${sub ? 84 : 92}" text-anchor="middle" dominant-baseline="middle" font-family="${font}" font-size="${ms}" font-weight="700" fill="${mainColor}">${esc(main)}</text>` +
    (sub ? `<text x="72" y="122" text-anchor="middle" font-family="${font}" font-size="17" font-weight="700" fill="${subColor}">${esc(fit(sub, 13))}</text>` : "") +
    (bar ? `<rect x="0" y="136" width="144" height="8" fill="${bar}"/>` : "") +
    `</svg>`;
  return "data:image/svg+xml;base64," + Buffer.from(svg).toString("base64");
}

const stateColor = (st) =>
  st === "over" ? C.red : st === "warn" ? C.amber : st === "under" ? C.green :
  st === "ok" ? C.green : st === "ontime" ? C.cyan : C.muted;

function segClock(seg, compact) {
  const color = seg.state === "over" ? C.red : seg.state === "warn" ? C.amber :
    seg.running ? C.text : "#b9c2cc";
  return {
    label: `${(seg.label || "SEG").toUpperCase().slice(0, 7)} ${seg.number}`,
    main: short(seg.seconds),
    mainColor: color,
    sub: seg.status || (seg.running ? "" : "STOPPED"),
    subColor: seg.status ? stateColor(seg.state) : C.muted,
    bar: seg.running ? C.green : C.red,
    ...(compact || {}),
  };
}

function progClock(prog) {
  return {
    label: (prog.label || "PROGRAM").toUpperCase(),
    main: short(prog.seconds),
    mainColor: prog.running ? C.text : "#b9c2cc",
    sub: prog.status || (prog.running ? "" : "STOPPED"),
    subColor: prog.status ? stateColor(prog.state) : C.muted,
    bar: prog.running ? C.green : C.red,
  };
}

function design(c, holding) {
  if (!online || !state) {
    return { label: "DA TIMER", main: "OFF", sub: "not running",
      mainColor: C.faint, labelColor: C.faint, subColor: C.faint, size: 40 };
  }
  const seg = state.segment, prog = state.program, t = state.toggles || {};
  const both = seg.running && prog.running;
  const none = !seg.running && !prog.running;
  switch (c.action) {
    case "clock": {
      const src = c.settings.source || "segment";
      if (src === "program") return progClock(prog);
      if (src === "tod") {
        const tag = { ntp: "TOD \u00b7 NTP", ltc: "TOD \u00b7 LTC" }[state.time_source] || "TOD";
        return { label: tag, main: state.tod, size: 31 };
      }
      if (src === "to_out" || src === "backtime") {
        const bt = state.backtime;
        if (!bt) return { label: "HARD OUT", main: "\u2014", sub: "NOT SET" };
        const col = { past: C.red, cut: C.red, spare: C.green, ontime: C.cyan }[bt.state] || C.text;
        if (src === "to_out") {
          const to = bt.to_out;
          return { label: "TO OUT", main: to >= 0 ? short(to) : "+" + short(to),
            mainColor: to < 0 ? C.red : C.text, sub: `out ${bt.out}`, bar: col };
        }
        if (bt.state === "ontime")
          return { label: "BACK-TIME", main: "ON", sub: "ON TIME", mainColor: col, subColor: col, bar: col, size: 50 };
        if (bt.state === "past")
          return { label: "BACK-TIME", main: "+" + short(-bt.to_out), sub: "PAST OUT", mainColor: col, subColor: col, bar: col };
        return { label: "BACK-TIME", main: short(Math.abs(bt.slack)), sub: bt.verdict.split(" ")[0],
          mainColor: col, subColor: col, bar: col };
      }
      if (src === "seg_delta") {
        if (seg.delta === null || seg.delta === undefined)
          return { label: `SEG ${seg.number}`, main: "—", sub: "NO GOAL" };
        return { label: `SEG ${seg.number}`, main: signed(seg.delta),
          mainColor: stateColor(seg.state),
          sub: seg.delta > 0 ? "OVER" : "LEFT", subColor: stateColor(seg.state),
          bar: stateColor(seg.state) };
      }
      if (src === "prog_delta") {
        if (prog.delta === null || prog.delta === undefined)
          return { label: "TOTAL", main: "—", sub: "NO GOALS" };
        const col = stateColor(prog.state);
        return { label: "TOTAL", main: signed(prog.delta), mainColor: col,
          sub: prog.delta > 0 ? "OVER" : prog.delta < 0 ? "UNDER" : "ON TIME",
          subColor: col, bar: col };
      }
      return segClock(seg);
    }
    case "alltoggle":
      return { label: "ALL", main: both ? "STOP" : "START", size: 40,
        bg: both ? C.greenBg : none ? C.redBg : C.bg,
        mainColor: both ? C.green : none ? C.red : C.text,
        sub: both ? "RUNNING" : none ? "STOPPED" : "MIXED",
        subColor: both ? C.green : none ? "#ff8592" : C.amber,
        bar: both ? C.green : none ? C.red : C.amber };
    case "allstart":
      return { label: "ALL", main: "START", size: 40,
        bg: both ? C.green : C.bg, labelColor: both ? "#04140b" : C.cyan,
        mainColor: both ? "#04140b" : C.text,
        sub: both ? "RUNNING" : "", subColor: "#04140b" };
    case "allstop":
      return { label: "ALL", main: "STOP", size: 40,
        bg: none ? C.redBg : C.bg, mainColor: none ? "#ff8592" : C.text,
        sub: none ? "STOPPED" : "", subColor: "#ff8592" };
    case "segtoggle":
      return { ...segClock(seg), bg: seg.running ? C.greenBg : C.bg };
    case "progtoggle":
      return { ...progClock(prog), bg: prog.running ? C.greenBg : C.bg };
    case "next":
      return { label: "NEXT", main: `▶ ${seg.number + 1}`, size: 44,
        sub: seg.next_name || `after seg ${seg.number}`,
        subColor: seg.next_name ? C.text : C.muted };
    case "mark":
      return { label: "LOG", main: "MARK", mainColor: C.amber, size: 40,
        sub: state.show };
    case "undo":
      return { label: "UNDO", main: "UNDO", size: 40,
        mainColor: state.undo ? C.amber : C.faint,
        sub: state.undo || "nothing to undo", subColor: state.undo ? C.text : C.faint };
    case "reset":
      return holding
        ? { label: "PROGRAM", main: "HOLD…", mainColor: C.red,
            bg: C.redBg, sub: "keep holding", subColor: "#ff8592", size: 36 }
        : { label: "PROGRAM", main: "RESET", mainColor: "#ff8592", size: 38,
            sub: "hold 1 sec", subColor: C.muted, bar: C.red };
    case "display": {
      const target = c.settings.target || "dual";
      const names = { dual: "DUAL SCRN", tod: "TOD", log: "LOG",
        segments: "SEGMENTS" };
      const on = !!t[target];
      return { label: names[target] || target.toUpperCase(),
        main: on ? "ON" : "OFF", size: 52,
        bg: on ? C.cyanBg : C.bg, mainColor: on ? C.cyan : C.faint,
        bar: on ? C.cyan : null };
    }
    default:
      return { label: "DA TIMER", main: "?" };
  }
}

function render(context) {
  const c = contexts.get(context);
  if (!c) return;
  const image = key(design(c, !!c.holding));
  if (image === c.last) return;
  c.last = image;
  send({ event: "setImage", context, payload: { image, target: 0 } });
}
