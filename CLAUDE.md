# DA Timer: notes for Claude (and other coding agents)

DA Timer is a live-broadcast timer: a crash, a stalled clock or a lost log
during a show is the worst outcome. Keep changes small and boring.

## Reliability rules
- Keep changes small and tied to one behaviour or issue.
- Every bug fix starts with a failing test that shows the bug; then the smallest fix.
- Run `python -m pytest -q` before saying a change is done (and
  `xvfb-run python tests/smooth_ticks.py` after touching tick / clock code);
  say which checks you did not run.
- Logic must be testable without Tk: inject the clock, `data_dir()` (via
  `APPDATA`) and network calls. See `tests/conftest.py`.

### Saving
- settings.json, shows/*.json, logs/current_log.json and show_state.json are
  saved only through `save_json()` / `atomic_write_json()` (same-folder temp
  file, flush, fsync, close, os.replace) and loaded through `load_json()`.
- Never swallow save or load errors: `save_json` reports them (the red
  "Not saved" pill), and `load_json` keeps a damaged file as `*.damaged-*`.
- Startup reads crash recovery (`_pending_restore`) before any autosave
  runs; the file is deleted only on an explicit Restore / Discard.

### Undo and the log
- Log entries have stable ids (`"id"`); undo reverses only the action's own
  changes (`undo_log_changes`) and never overwrites a later edit.

### Time
- Durations use the monotonic `Stopwatch`; never `datetime.now()`.
- Time-of-day / timecode comparisons use `da_sync.wrapped_diff`, for both
  jump detection and smoothing.
- One rounding rule for every displayed duration: whole seconds, fraction
  dropped (`whole()` / `fmt_short()` in Python, `Math.floor` in the overlay
  and plugin). Over/under figures are computed from whole seconds.
- Tick alignment (`_nudge`) moves clocks by under a second, never backwards,
  never skipping a number and never below zero.
- In da_sync.py, LTC frame-rate detection resets on reconfigure and reacquire.

### Threads
- Each worker owns its stop Event, generation and captured settings; stale
  generations never publish (see `TimeSource._ntp_loop`).
- Worker threads never touch Tk; the main thread drains queues via after(),
  with a budget (`_process_api` handles at most 20 commands per tick).

### Network and API (ControlAPI, overlay.html, StreamDeck/Source/src/plugin.js)
- Bind 127.0.0.1 unless the LAN setting is on; LAN clients can only view and
  need the per-install overlay key.
- Check Host (`host_allowed`) on every request and Origin when present;
  commands only on POST, from this computer, with `X-DA-Timer`.
- No wildcard CORS. Socket timeouts, a connection cap, body size checked
  before reading, bounded command queue. Every overlay fetch has a deadline.
- Plugin holds are cancelled on key-up, willDisappear and context replacement.

### Files
- Show file names go through `_slug` / `show_key`; collisions are checked
  with `find_show` (case-insensitive); never delete the file just written
  (`same_show_file`, checked after saving).
- Logo paths must resolve to an image inside the logos folder (`logo_path`).

### Dependencies and CI
- Python packages: exact versions + hashes in `requirements*.txt`, generated
  from `requirements/*.in` with `uv pip compile --universal --generate-hashes`.
  Update them in their own commit. Plugin: `package-lock.json`, `npm ci`.
- Workflows: read-only permissions by default; only the release `attach`
  jobs can write. Actions are pinned to commit SHAs (Dependabot updates them).
- Releases: the user runs Actions -> "Publish release (new version)". Agents
  can't push tags or create releases from here.
