# Checks the clocks tick once a second with no stalls, even with a coarse
# (Windows-style) system clock. Run: xvfb-run python tests/smooth_ticks.py
#
# PASS/FAIL: exits 1 if any tick is irregular (outside 0.92-1.08 s), except
# the one deliberate re-alignment of the segment clock right after the test
# presses NEXT part-way through a second (at most 2 intervals, within 1.5 s
# of that press). Exits 0 when everything ticked cleanly.
import sys, os, shutil, time, statistics, datetime as dt
os.environ["APPDATA"]=os.path.join(os.environ.get("TMPDIR", "/tmp"), "datimer_test_ticks"); shutil.rmtree(os.environ["APPDATA"], ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tkinter as tk, da_timer as d
root=tk.Tk(); d.init_fonts(root); root.geometry("1600x900+0+0"); app=d.DATimerApp(root)
# Windows-like coarse wall clock: 15.6 ms steps, plus a little drift vs monotonic
real_now = app.clock.now
def coarse():
    t = time.time() * 1.00002          # wall clock drifting vs monotonic
    t = t - (t % 0.015625)
    return dt.datetime.fromtimestamp(t)
app.clock.now = coarse
changes = {"prog": [], "seg": []}
events = {"next": None}
failed = []
last = {"prog": None, "seg": None}
orig = d.OutputView.update_times
def spy(self, seg, show, *a, **k):
    now = time.monotonic()
    for k_, v in (("prog", show), ("seg", seg)):
        if last[k_] != v:
            last[k_] = v; changes[k_].append(now)
    return orig(self, seg, show, *a, **k)
d.OutputView.update_times = spy
def go():
    app.show.set(0.37); app.start_all(); app.open_output()
    def press_next():
        events["next"] = time.monotonic()
        app.segment.set(3.0)
        app.next_segment()
    root.after(7000, press_next)
    root.after(14000, lambda: app.open_preset("show"))
    root.after(25000, done)
def done():
    for k_ in ("prog", "seg"):
        times = changes[k_][3:]
        iv = [(b - a, b) for a, b in zip(times, times[1:])]
        bad = [(round(x, 2), at) for x, at in iv if x > 1.08 or x < 0.92]
        expected = [b for b in bad if k_ == "seg" and events["next"] and
                    abs(b[1] - events["next"]) < 1.5]
        unexpected = [b for b in bad if b not in expected]
        if len(expected) > 2 or unexpected or len(iv) < 15:
            failed.append(k_)
        print(f"{k_}: {len(iv)} ticks, min {min(x for x, _ in iv):.3f}s "
              f"max {max(x for x, _ in iv):.3f}s, irregular: "
              f"{[x for x, _ in bad]} (after NEXT: {[x for x, _ in expected]})")
    print("FAIL: " + ", ".join(failed) if failed else "PASS")
    root.destroy()
root.after(800, go); root.mainloop()
sys.exit(1 if failed else 0)
