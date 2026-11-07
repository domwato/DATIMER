"""Headless logic tests: one or more per reported bug / security finding.

Run:  python -m pytest -q          (no window, audio, Stream Deck or network)
"""
import hashlib
import http.client
import json
import multiprocessing
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import types

import pytest

import da_deck
import da_sync
import da_timer as d

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------- helpers --
def fake_app(**extra):
    """Just enough of DATimerApp to run one of its methods without Tk."""
    app = types.SimpleNamespace(
        show=d.Stopwatch(), segment=d.Stopwatch(), segment_times=[0.0],
        current_show=d.copy_show(d.DEFAULT_SHOW), log=d.ShowLog(),
        feat=lambda k: True, running=False)
    for name in ("_state_file", "_read_recovery", "_remove_recovery",
                 "_save_show_state", "_offer_restore", "_undoable", "undo",
                 "duplicate_show", "preview_output_show", "end_output_preview",
                 "_out_show", "_out_seg_clock"):
        setattr(app, name, getattr(d.DATimerApp, name).__get__(app))
    app.__dict__.update(extra)
    return app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------- #2 atomic saving --
def _slow_save(path, ready):
    """Child process: start an atomic save, then hang just before the
    swap so the parent can kill it at the worst moment."""
    import da_timer as dt
    real = dt.os.replace

    def hang(a, b):
        ready.set()
        time.sleep(60)
        real(a, b)
    dt.os.replace = hang
    dt.atomic_write_json(path, {"version": "new", "pad": "x" * 2_000_000})


def test_atomic_save_survives_killed_writer(tmp_path):
    path = str(tmp_path / "settings.json")
    d.atomic_write_json(path, {"version": "old"})
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    p = ctx.Process(target=_slow_save, args=(path, ready))
    p.start()
    assert ready.wait(60), "child never reached the swap"
    p.kill()
    p.join()
    assert d.load_json(path, "test") == {"version": "old"}
    d.atomic_write_json(path, {"version": "new"})          # normal case
    assert d.load_json(path, "test") == {"version": "new"}


def test_damaged_file_is_kept_and_reported(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    (data_dir / "settings.json").write_text('{"current_show": "Ne', "utf-8")
    assert d.load_settings() == {}
    kept = [f for f in os.listdir(data_dir) if ".damaged-" in f]
    assert kept, "damaged settings were not kept"
    assert d.PROBLEMS and d.PROBLEMS[-1]["kind"] == "load"


def test_save_failure_is_reported_not_hidden(tmp_path):
    assert not d.save_json(str(tmp_path / "missing" / "x.json"), {}, "Thing")
    assert "Thing" in d._FAILING and d.PROBLEMS[-1]["what"] == "Thing"
    assert d.save_json(str(tmp_path / "x.json"), {}, "Thing")
    assert "Thing" not in d._FAILING


# ------------------------------------------------- #1 crash recovery --
def write_recovery(app, running=True):
    data = {"saved_wall": time.time() - 5, "show_name": "Default",
            "show": {"elapsed": 600.0, "running": running},
            "seg": {"elapsed": 42.0, "running": running},
            "segment_times": [100.0, 42.0]}
    d.atomic_write_json(app._state_file(), data)
    return data


def test_recovery_survives_first_idle_autosave(monkeypatch):
    app = fake_app()
    write_recovery(app)
    before = open(app._state_file(), "rb").read()
    app._pending_restore = app._read_recovery()        # startup reads first
    app._save_show_state()                             # first tick, idle
    assert open(app._state_file(), "rb").read() == before

    asked = []
    monkeypatch.setattr(d.messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or False)
    app.root = types.SimpleNamespace(after=lambda ms, fn: None)
    app._offer_log_restore = lambda: None
    app._offer_restore()                               # "No" = discard
    assert asked, "the restore question was never asked"
    assert not os.path.exists(app._state_file())


def test_recovery_restore_brings_timers_back(monkeypatch):
    app = fake_app(_rebuild_seg_list=lambda: None, _update_visuals=lambda: None,
                   _offer_log_restore=lambda: None,
                   root=types.SimpleNamespace(after=lambda ms, fn: None))
    write_recovery(app)
    app._pending_restore = app._read_recovery()
    monkeypatch.setattr(d.messagebox, "askyesno", lambda *a, **k: True)
    app._offer_restore()
    assert app.show.elapsed() >= 605          # includes the time since
    assert app.segment_times[0] == 100.0 and len(app.segment_times) == 2
    assert os.path.exists(app._state_file())  # saved again, now current


# ------------------------------------------------------- #3 undo ---------
def test_timer_undo_keeps_later_note_edit():
    app = fake_app(_in_action=False, _undo_stack=[], UNDO_DEPTH=15,
                   _update_undo_button=lambda: None,
                   _show_toast=lambda *a, **k: None,
                   _rebuild_seg_list=lambda: None,
                   _rebuild_log_tree=lambda: None,
                   _update_visuals=lambda: None,
                   _save_show_state=lambda: None)
    sig = iter(range(1000))
    app._state_sig = lambda: next(sig)
    app._add_entry = lambda kind, note: app.log.add(kind, note, 0, 1, 0)
    note = app.log.add("NOTE", "Original", 0, 1, 0)

    def start():
        app.show.start()
        app.log.add("START", "All start", 0, 1, 0)
    app._undoable("All start", start)
    note["note"] = "Corrected"                          # edited afterwards
    app.log.add("NOTE", "Later note", 0, 1, 0)
    app.undo()
    notes = [(e["type"], e["note"]) for e in app.log.entries]
    assert ("NOTE", "Corrected") in notes
    assert ("NOTE", "Later note") in notes
    assert ("START", "All start") not in notes          # the action's own
    assert not app.show.running
    saved = d.ShowLog.load_backup()
    assert "Corrected" in [e["note"] for e in saved.entries]


def test_undo_of_clear_log_puts_entries_back_in_order():
    log = [{"id": "a", "note": "1"}, {"id": "b", "note": "2"}]
    snap = {"log_added": [], "log_changed": {},
            "log_removed": [(0, log[0]), (1, log[1])]}
    later = [{"id": "c", "note": "3"}]
    assert [e["id"] for e in d.undo_log_changes(later, snap)] == ["a", "b", "c"]


# ---------------------------------------------- #4 / #5 show names -------
def test_duplicate_detects_same_file_name(monkeypatch):
    d.save_show(dict(d.copy_show(d.DEFAULT_SHOW), name="News Live", title="N"))
    news = open(d.show_path("News Live"), "rb").read()
    assert d.find_show("News.Live") == "News Live"
    app = fake_app(use_show=lambda s: None,
                   root=None)
    app.current_show = dict(d.copy_show(d.DEFAULT_SHOW), name="Weather")
    asked = []
    monkeypatch.setattr(d.simpledialog, "askstring", lambda *a, **k: "News.Live")
    monkeypatch.setattr(d.messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or False)
    app.duplicate_show()
    assert asked, "no 'replace?' question for a clashing file name"
    assert open(d.show_path("News Live"), "rb").read() == news


def test_duplicate_is_a_deep_copy():
    show = d.copy_show(d.DEFAULT_SHOW)
    show["output_layout"] = d.output_layout(show)
    show["output_layout"][0]["box"] = [0, 0, 1, 0.2]
    twin = d.copy_show(show)
    twin["output_layout"][0]["box"][3] = 0.9
    assert show["output_layout"][0]["box"][3] == 0.2


def test_case_only_rename_keeps_the_show(monkeypatch):
    d.save_show(dict(d.copy_show(d.DEFAULT_SHOW), name="news"))
    asked = []
    monkeypatch.setattr(d.messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or True)
    ed = types.SimpleNamespace(
        show=d.load_show("news"), original_name="news", is_new=False,
        vars={"name": types.SimpleNamespace(get=lambda: "News")},
        _read_hard_out=lambda: {"mode": "off", "tod": "", "trt": 0},
        _read_count=lambda: 0, on_save=lambda s: None, destroy=lambda: None)
    saved = []
    ed.on_save = saved.append
    d.ShowEditor.save(ed)
    assert saved, "the editor didn't save"
    assert not asked, "asked 'already exists' about the show itself"
    files = [f for f in os.listdir(d.data_dir("shows"))
             if f.lower() == "news.json"]
    assert len(files) == 1
    assert d.load_show("News")["name"] == "News"


# ------------------------------------------- #6 preview vs real output --
def test_real_output_numbers_follow_the_edited_layout():
    app = fake_app(output=None, _goal=lambda n: 600)
    app.segment.base = 120
    edited = d.copy_show(app.current_show)
    edited["output_layout"] = d.output_layout(edited)
    for r in edited["output_layout"]:
        if r["id"] == "seg_time":
            r["count"] = "down"
    assert app._out_seg_clock()[0] == "00:02:00"
    app.preview_output_show(edited)
    assert app._out_seg_clock()[0] == "00:08:00"      # same as the preview
    app.end_output_preview()                          # Cancel / Done
    assert app._out_seg_clock()[0] == "00:02:00"


# --------------------------------------------------------- #7 / #8 LTC --
def feed(ts, frames, fps, start_mono):
    """Feed LTC frames as if one arrived every 1/fps seconds."""
    tods = []
    for i, (h, m, s, f) in enumerate(frames):
        mono = start_mono + i / fps
        ts._block_mono, ts._block_end, ts._sr = mono, 1000, 48000
        ts._on_ltc_frame(h, m, s, f, fps, False, 1000)
        tods.append((mono + ts.ltc_offset) % 86400)
    return tods


def frames_from(h, m, s, f, n, fps):
    out = []
    for _ in range(n):
        out.append((h, m, s, f))
        f += 1
        if f == fps:
            f, s = 0, s + 1
            if s == 60:
                s, m = 0, m + 1
                if m == 60:
                    m, h = 0, (h + 1) % 24
    return out


@pytest.mark.parametrize("fps", [24, 25, 30])
def test_ltc_midnight_has_no_spike(fps):
    ts = da_sync.TimeSource({"time_source": "pc"})
    tods = feed(ts, frames_from(23, 59, 59, fps - 6, 12, fps), fps, 1000.0)
    steps = [da_sync.wrapped_diff(b, a) for a, b in zip(tods, tods[1:])]
    assert all(0 < st < 0.2 for st in steps), steps


def test_ltc_rate_detection_resets():
    ts = da_sync.TimeSource({"time_source": "pc"})
    feed(ts, frames_from(10, 0, 0, 0, 70, 30), 30, 100.0)
    assert ts.ltc_fps == 30
    ts.configure({"time_source": "pc"})                 # new input
    feed(ts, frames_from(10, 0, 0, 0, 60, 25), 25, 200.0)
    assert ts.ltc_fps == 25
    ts.ltc_last = time.monotonic() - 2                  # signal lost...
    feed(ts, frames_from(11, 0, 0, 0, 55, 24), 24, 300.0)   # ...back at 24
    assert ts.ltc_fps == 24


def test_wrapped_diff():
    assert da_sync.wrapped_diff(0.5, 86399.5) == pytest.approx(1.0)
    assert da_sync.wrapped_diff(86399.5, 0.5) == pytest.approx(-1.0)


# ---------------------------------------------------- #11 NTP workers --
def test_stale_ntp_worker_cannot_publish(monkeypatch):
    release = threading.Event()

    def fake_query(server, *a, **k):
        if server == "old":
            release.wait(5)
            return 999.0, 0.01
        return 1.0, 0.01
    monkeypatch.setattr(da_sync, "ntp_query", fake_query)
    ts = da_sync.TimeSource({"time_source": "ntp", "ntp_server": "old"})
    time.sleep(0.1)
    ts.configure({"time_source": "ntp", "ntp_server": "new"})
    for _ in range(50):
        if ts.offset == 1.0:
            break
        time.sleep(0.02)
    release.set()
    time.sleep(0.3)
    assert ts.offset == 1.0 and "new" in ts.status
    ts.stop()


# ------------------------------------------------ S5 NTP validation -----
def fake_ntp_server(mode=4, echo=True, hold=0.0, stratum=2):
    """One-shot local NTP server. `hold` = how long the server claims it
    kept the packet (a big one makes the round trip negative)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))

    def serve():
        data, addr = sock.recvfrom(512)
        now = time.time() + 5               # server is 5 s ahead
        reply = bytearray(48)
        reply[0] = (0 << 6) | (4 << 3) | mode
        reply[1] = stratum
        reply[24:32] = data[40:48] if echo else b"\x01" * 8

        def put(off, t):
            struct.pack_into("!II", reply, off, int(t + da_sync.NTP_EPOCH),
                             int((t % 1) * 2 ** 32))
        put(32, now)
        put(40, now + hold)
        sock.sendto(bytes(reply), addr)
        sock.close()
    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


def test_ntp_good_reply():
    off, delay = da_sync.ntp_query("127.0.0.1", fake_ntp_server(), timeout=2)
    assert 4.5 < off < 5.5 and delay >= 0


@pytest.mark.parametrize("bad", [dict(echo=False), dict(mode=3),
                                 dict(stratum=0), dict(hold=30)])
def test_ntp_forged_or_bad_replies_rejected(bad):
    with pytest.raises((ValueError, OSError)):
        da_sync.ntp_query("127.0.0.1", fake_ntp_server(**bad), timeout=1)


# ------------------------------------------- S1-S3 local HTTP server ----
@pytest.fixture
def api():
    port = free_port()
    a = d.ControlAPI(port, lan=False, key="secret-key-123")
    a.live = lambda: {"tod": 0}
    yield a
    a.close()


def request(api, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    hdrs = {"Host": f"127.0.0.1:{api.port}"}
    hdrs.update(headers or {})
    for k, v in hdrs.items():
        if v is not None:
            c.putheader(k, v)
    c.endheaders(body)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


def test_host_allowed():
    assert d.host_allowed("127.0.0.1:47800", 47800)
    assert d.host_allowed("localhost:47800", 47800)
    assert d.host_allowed("[::1]:47800", 47800)
    assert d.host_allowed("192.168.1.20:47800", 47800)
    assert not d.host_allowed("evil.example:47800", 47800)
    assert not d.host_allowed("127.0.0.1:9999", 47800)
    assert not d.host_allowed("", 47800)


def test_api_refuses_foreign_host_dns_rebinding(api):
    assert request(api, "GET", "/api/state")[0] == 200
    assert request(api, "GET", "/api/state",
                   {"Host": f"evil.example:{api.port}"})[0] == 403
    st = request(api, "POST", "/api/action/all_stop",
                 {"Host": f"evil.example:{api.port}", "X-DA-Timer": "1",
                  "Content-Length": "0"})[0]
    assert st == 403 and api.queue.empty()


def test_api_commands_need_post_header_and_same_origin(api):
    assert request(api, "GET", "/api/action/all_stop",
                   {"X-DA-Timer": "1"})[0] == 405
    assert request(api, "POST", "/api/action/all_stop",
                   {"Content-Length": "0"})[0] == 403
    assert request(api, "POST", "/api/action/all_stop",
                   {"X-DA-Timer": "1", "Origin": "http://evil.example",
                    "Content-Length": "0"})[0] == 403
    assert request(api, "POST", "/api/action/all_stop",
                   {"X-DA-Timer": "1", "Content-Length": "0"})[0] == 200
    assert api.queue.get_nowait() == "all_stop"


def test_api_no_wildcard_cors(api):
    _, headers, _ = request(api, "GET", "/api/state")
    assert "Access-Control-Allow-Origin" not in headers


def test_api_body_and_queue_limits(api):
    st, _, _ = request(api, "POST", "/api/message",
                       {"X-DA-Timer": "1", "Content-Length": "100000"})
    assert st == 413
    for _ in range(d.API_MAX_QUEUE):
        api.submit("mark")
    st = request(api, "POST", "/api/action/mark",
                 {"X-DA-Timer": "1", "Content-Length": "0"})[0]
    assert st == 503


# --------------------------------------------------- S4 logo paths -----
def test_logo_path_stays_inside_logos_folder(data_dir):
    logos = d.data_dir("logos")
    png = os.path.join(logos, "ok.png")
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    with open(os.path.join(logos, "fake.png"), "wb") as f:
        f.write(b"not an image")
    d.save_settings({"x": 1})
    assert d.logo_path("ok.png") == os.path.realpath(png)
    for bad in ("../settings.json", "..\\settings.json",
                os.path.abspath(d.settings_path()), "fake.png",
                "/etc/passwd", "C:\\Windows\\win.ini", ""):
        assert d.logo_path(bad) == "", bad


# ------------------------------------------- one rounding rule ---------
@pytest.mark.parametrize("sec,want", [(2.5, "00:02"), (3.5, "00:03"),
                                      (59.99, "00:59"), (3600.5, "1:00:00")])
def test_rounding_matches_everywhere(sec, want):
    assert d.fmt_short(sec) == want
    assert da_deck.short(sec) == want
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed - JS half not checked")
    for src in ("overlay.html", "StreamDeck/Source/src/plugin.js"):
        text = open(os.path.join(ROOT, src), encoding="utf-8").read()
        start = text.index("function short(")
        body = text[start:text.index("\n  }", start) + 4] \
            if src == "overlay.html" else \
            text[start:text.index("\n}", start) + 2]
        js = ("function pad(n){return (n<10?'0':'')+n}\n" + body +
              f"\nprocess.stdout.write(short({sec}))")
        out = subprocess.run([node, "-e", js], capture_output=True,
                             text=True, timeout=20).stdout
        assert out == want, (src, out)


# ------------------------------------- clocks never shown below zero ----
def test_tick_alignment_never_makes_a_clock_negative():
    sw = d.Stopwatch()
    sw.start()
    sw.started_at = time.monotonic() - 0.02       # just started: 0.02 s
    d.DATimerApp._nudge(sw, 0.96)                 # wants to go back 0.06 s
    assert sw.elapsed() >= 0
