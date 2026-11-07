// Pretends to be the Stream Deck app: launches the plugin, places keys, presses them.
const { WebSocketServer } = require("ws");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
// Where snapshots go: the first argument, or a fresh temporary folder.
const OUT = process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), "datimer-mock-"));
fs.mkdirSync(OUT, { recursive: true });
console.log("mock_streamdeck: writing to " + OUT);
const PREFIX = "io.github.domwato.datimer.";
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
  const snap = (tag) => fs.writeFileSync(path.join(OUT, `deck_${tag}.json`), JSON.stringify(images));
  const send = (o) => sock.send(JSON.stringify(o));
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
    // RESET held, but the key goes away (page change) before the second
    // is up: must NOT reset, even though no key-up ever arrives for it
    send({ event: "keyDown", context: "k12", action: "x", payload: {} });
    await new Promise((r) => setTimeout(r, 300));
    send({ event: "willDisappear", context: "k12", action: PREFIX + "reset", payload: {} });
    await new Promise((r) => setTimeout(r, 1400));
    events.push("page-change-hold-done");
    send({ event: "willAppear", action: PREFIX + "reset", context: "k12", device: "D", payload: { settings: {} } });
    await new Promise((r) => setTimeout(r, 300));
    await press("k12", 1300);          // RESET held -> resets
    await new Promise((r) => setTimeout(r, 800)); snap("after_reset");
    fs.writeFileSync(path.join(OUT, "deck_events.json"), JSON.stringify(events));
    process.exit(0);
  })();
});
spawn("node", ["io.github.domwato.datimer.sdPlugin/bin/plugin.js", "-port", "28196", "-pluginUUID", "PLUGINUUID", "-registerEvent", "registerPlugin", "-info", "{}"], { stdio: "inherit" });
