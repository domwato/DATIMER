# Checks the clocks tick once a second with no stalls, even with a coarse
# (Windows-style) system clock. Run: xvfb-run python tests/smooth_ticks.py
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
    root.after(7000, lambda: (app.segment.set(3.0), app.next_segment()))
    root.after(14000, lambda: app.open_preset("show"))
    root.after(25000, done)
def done():
    for k_ in ("prog", "seg"):
        iv = [b - a for a, b in zip(changes[k_][3:], changes[k_][4:])]
        bad = [round(x, 2) for x in iv if x > 1.08 or x < 0.92]
        print(f"{k_}: {len(iv)} ticks, min {min(iv):.3f}s max {max(iv):.3f}s, irregular: {bad}")
    root.destroy()
root.after(800, go); root.mainloop()
