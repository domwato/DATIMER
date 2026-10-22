// Pretends to be the Stream Deck app: launches the plugin, places keys, presses them.
const { WebSocketServer } = require("ws");
const { spawn } = require("child_process");
const fs = require("fs");
const PREFIX = "com.gravitymedia.datimer.";
const keys = [
  ["clock", { source: "segment" }], ["clock", { source: "program" }],
  ["clock", { source: "seg_delta" }], ["clock", { source: "prog_delta" }],
  ["clock", { source: "tod" }], ["alltoggle", {}], ["allstart", {}], ["allstop", {}],
  ["segtoggle", {}], ["progtoggle", {}], ["next", {}], ["mark", {}], ["reset", {}],
  ["display", { target: "dual" }], ["display", { target: "segments" }],
];
const images = {}; const events = [];
const wss = new WebSocketServer({ port: 28196 });
wss.on("connection", (sock) => {
  sock.on("message", (raw) => {
    const m = JSON.parse(raw);
    if (m.event === "registerPlugin") {
      events.push("registered");
      keys.forEach(([a, s], i) => sock.send(JSON.stringify({ event: "willAppear", action: PREFIX + a, context: "k" + i, device: "D", payload: { settings: s, coordinates: { column: i % 5, row: Math.floor(i / 5) } } })));
    } else if (m.event === "setImage") images[m.context] = m.payload.image;
    else if (m.event === "showOk" || m.event === "showAlert") events.push(m.event + ":" + m.context);
  });
  const press = (ctx, holdMs = 50) => new Promise((res) => {
    sock.send(JSON.stringify({ event: "keyDown", context: ctx, action: "x", payload: {} }));
    setTimeout(() => { sock.send(JSON.stringify({ event: "keyUp", context: ctx, action: "x", payload: {} })); setTimeout(res, 600); }, holdMs);
  });
  const snap = (tag) => fs.writeFileSync(`/tmp/claude-0/deck_${tag}.json`, JSON.stringify(images));
  (async () => {
    await new Promise((r) => setTimeout(r, 1500)); snap("idle");
    await press("k6");                 // ALL START
    await new Promise((r) => setTimeout(r, 1800)); snap("running");
    await press("k11");                // MARK
    await press("k10");                // NEXT
    await press("k12", 100);           // RESET tap (should alert, not reset)
    await new Promise((r) => setTimeout(r, 600)); snap("after_next");
    await press("k8");                 // segment toggle -> stop segment
    await new Promise((r) => setTimeout(r, 600)); snap("mixed");
    await press("k12", 1300);          // RESET held -> resets
    await new Promise((r) => setTimeout(r, 800)); snap("after_reset");
    fs.writeFileSync("/tmp/claude-0/deck_events.json", JSON.stringify(events));
    process.exit(0);
  })();
});
spawn("node", ["com.gravitymedia.datimer.sdPlugin/bin/plugin.js", "-port", "28196", "-pluginUUID", "PLUGINUUID", "-registerEvent", "registerPlugin", "-info", "{}"], { stdio: "inherit" });
