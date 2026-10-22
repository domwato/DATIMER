"""
DA Timer - time-of-day sources.

  PC clock      Windows time (default)
  Network (NTP) asks a time server for the offset and applies it inside
                DA Timer only - no admin rights, Windows clock untouched
  LTC           decodes SMPTE linear timecode from an audio input
                (sound card / USB audio). Freewheels if the signal drops.

Everything in DA Timer that shows or stamps a time of day (TOD clock, log,
back-timing, Stream Deck, Excel export) asks TimeSource.now().
LTC needs numpy + sounddevice (pip install numpy sounddevice).
"""
import socket
import struct
import threading
import time
from datetime import datetime, timedelta

NTP_EPOCH = 2208988800          # seconds 1900-01-01 -> 1970-01-01
SYNC_WORD = (0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1)   # bits 64..79
FREEWHEEL_SECONDS = 10          # keep LTC time this long after signal loss


# --------------------------------------------------------------------- NTP
def ntp_query(server, port=123, timeout=2.0):
    """One SNTP request. Returns (offset_seconds, round_trip_seconds)."""
    packet = bytearray(48)
    packet[0] = 0x23                          # LI=0, VN=4, mode=3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.time()
        struct.pack_into("!II", packet, 40, int(t0 + NTP_EPOCH),
                         int((t0 % 1) * 2 ** 32))
        s.sendto(packet, (server, port))
        data, _ = s.recvfrom(512)
        t3 = time.time()
    if len(data) < 48:
        raise ValueError("short NTP reply")
    if data[1] == 0:
        raise ValueError("server not synchronised (stratum 0)")

    def ts(off):
        sec, frac = struct.unpack_from("!II", data, off)
        return sec - NTP_EPOCH + frac / 2 ** 32
    t1, t2 = ts(32), ts(40)                   # server receive / transmit
    offset = ((t1 - t0) + (t2 - t3)) / 2
    delay = (t3 - t0) - (t2 - t1)
    return offset, delay


# --------------------------------------------------------------------- LTC
class LTCDecoder:
    """Bi-phase mark decoder working on zero-crossing intervals.
    Feed it blocks of mono float samples; it calls on_frame(h, m, s, f,
    fps_estimate, drop_frame, sample_index_of_frame_end)."""

    def __init__(self, sample_rate, on_frame):
        self.sr = float(sample_rate)
        self.on_frame = on_frame
        self.bit_len = None            # samples per bit (adaptive)
        self.half_pending = False
        self.bits = []
        self.last_cross = None
        self.sample_pos = 0
        self.level = 1
        self.signal_level = 0.0

    def feed(self, block):
        import numpy as np
        x = np.asarray(block, dtype=np.float32)
        if x.size == 0:
            return
        x = x - float(np.mean(x))
        # light low-pass (3-tap) to knock off noise spikes, keeping state
        tail = getattr(self, "_tail", np.zeros(2, dtype=np.float32))
        xx = np.concatenate((tail, x))
        self._tail = xx[-2:].copy()
        x = (xx[2:] + xx[1:-1] + xx[:-2]) / 3.0
        # signal level from the 90th percentile (robust to spikes)
        level = float(np.percentile(np.abs(x), 90)) or 1e-6
        self.signal_level = level
        thr = level * 0.35                     # hysteresis band
        # +1 above thr, -1 below -thr, else hold previous level
        s = np.zeros(x.size, dtype=np.int8)
        s[x > thr] = 1
        s[x < -thr] = -1
        idx = np.nonzero(s)[0]
        if idx.size == 0:
            self.sample_pos += x.size
            return
        held = s[idx]
        # positions where the held level changes
        prev = np.concatenate(([self.level], held[:-1]))
        cross = idx[held != prev]
        self.level = int(held[-1])
        for c in cross:
            pos = self.sample_pos + int(c)
            if self.last_cross is not None:
                self._interval(pos - self.last_cross, pos)
            self.last_cross = pos
        self.sample_pos += x.size

    def _interval(self, n, pos):
        if self.bit_len is None:
            # LTC runs 1600-2400 bits/s -> ~20-30 samples/bit at 48k.
            guess = self.sr / 2000.0
            if 0.6 * guess < n < 1.6 * guess:
                self.bit_len = float(n)
            return
        if n > 1.6 * self.bit_len or n < 0.25 * self.bit_len:
            # glitch / dropout: resync
            self.bits.clear()
            self.half_pending = False
            if n > 4 * self.bit_len:
                self.bit_len = None
            return
        if n > 0.75 * self.bit_len:            # full cell -> 0
            if self.half_pending:              # lost phase, restart
                self.half_pending = False
                self.bits.clear()
            self._bit(0, pos)
            self.bit_len += 0.05 * (n - self.bit_len)
        else:                                   # half cell
            if self.half_pending:
                self.half_pending = False
                self._bit(1, pos)
            else:
                self.half_pending = True
            self.bit_len += 0.05 * (2 * n - self.bit_len)

    def _bit(self, b, pos):
        bits = self.bits
        bits.append(b)
        if len(bits) > 80:
            del bits[0]
        if len(bits) == 80 and tuple(bits[64:]) == SYNC_WORD:
            self._frame(bits, pos)
            bits.clear()

    def _frame(self, b, pos):
        def val(start, n):
            return sum(b[start + i] << i for i in range(n))
        frames = val(0, 4) + 10 * val(8, 2)
        secs = val(16, 4) + 10 * val(24, 3)
        mins = val(32, 4) + 10 * val(40, 3)
        hours = val(48, 4) + 10 * val(56, 2)
        drop = bool(b[10])
        if hours > 23 or mins > 59 or secs > 59 or frames > 29:
            return
        fps = self.sr / (80 * self.bit_len)
        self.on_frame(hours, mins, secs, frames, fps, drop, pos)


def nominal_fps(estimate, drop, max_frame=0):
    """Frame rate from the highest frame number seen (reliable once a full
    second has gone by), else the nearest standard rate to the bit timing."""
    if drop:
        return 29.97
    if max_frame >= 25:
        return 30
    if max_frame == 24:
        return 25
    if max_frame == 23 and estimate < 24.5:
        return 24
    return min((24, 25, 30), key=lambda f: abs(estimate - f))


# ------------------------------------------------------------- time source
class TimeSource:
    MODES = ("pc", "ntp", "ltc")

    def __init__(self, settings):
        self.lock = threading.Lock()
        self.offset = 0.0              # NTP: seconds to add to PC clock
        self.ltc_offset = None         # LTC: tc_seconds_of_day - monotonic
        self.ltc_last = None           # monotonic time of last good frame
        self.ltc_tc = ""               # last timecode string
        self.ltc_fps = None
        self.status = "PC clock"
        self.state = "ok"              # ok | warn | error
        self.mode = "pc"
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self.configure(settings)

    # configuration ------------------------------------------------------
    def configure(self, settings):
        self.stop()
        self._stop = threading.Event()
        self.mode = settings.get("time_source", "pc")
        self.ntp_server = settings.get("ntp_server", "time.windows.com")
        self.ltc_device = settings.get("ltc_device")       # name or None
        self.ltc_channel = int(settings.get("ltc_channel", 0))
        self.offset, self.ltc_offset, self.ltc_last = 0.0, None, None
        if self.mode == "ntp":
            self.status, self.state = f"NTP: contacting {self.ntp_server}…", "warn"
            self._thread = threading.Thread(target=self._ntp_loop, daemon=True)
            self._thread.start()
        elif self.mode == "ltc":
            self._start_ltc()
        else:
            self.status, self.state = "PC clock", "ok"

    def stop(self):
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # the clock ----------------------------------------------------------
    def now(self):
        pc = datetime.now()
        if self.mode == "ntp":
            return pc + timedelta(seconds=self.offset)
        if self.mode == "ltc" and self.ltc_offset is not None:
            age = time.monotonic() - (self.ltc_last or 0)
            if age > FREEWHEEL_SECONDS:
                return pc                      # give up, fall back
            sod = (time.monotonic() + self.ltc_offset) % 86400
            midnight = pc.replace(hour=0, minute=0, second=0, microsecond=0)
            t = midnight + timedelta(seconds=sod)
            # handle midnight wrap between LTC and the PC date
            if (t - pc).total_seconds() > 43200:
                t -= timedelta(days=1)
            elif (pc - t).total_seconds() > 43200:
                t += timedelta(days=1)
            return t
        return pc

    def label(self):
        """Short tag for the header next to the TOD clock."""
        return {"pc": "PC", "ntp": "NTP", "ltc": "LTC"}[self.mode]

    def refresh_status(self):
        """Called from the UI ~ every second: update LTC lock state."""
        if self.mode != "ltc" or self._stream is None:
            return
        if self.ltc_last is None:
            self.status, self.state = "LTC: no timecode on input", "error"
            return
        age = time.monotonic() - self.ltc_last
        if age < 0.5:
            fps = f"{self.ltc_fps:g} fps" if self.ltc_fps else ""
            self.status, self.state = f"LTC locked  {self.ltc_tc}  {fps}", "ok"
        elif age <= FREEWHEEL_SECONDS:
            self.status = f"LTC lost — freewheeling ({int(age)}s)"
            self.state = "warn"
        else:
            self.status, self.state = "LTC lost — using PC clock", "error"

    # NTP -------------------------------------------------------------
    def _ntp_loop(self):
        good = []
        while not self._stop.is_set():
            try:
                off, delay = ntp_query(self.ntp_server)
                good = (good + [(delay, off)])[-5:]
                # use the sample with the smallest network delay
                self.offset = min(good)[1]
                self.status = (f"NTP {self.ntp_server}  offset "
                               f"{self.offset:+.3f}s  delay {delay * 1000:.0f}ms")
                self.state = "ok"
                wait = 60
            except Exception as exc:
                self.status = f"NTP {self.ntp_server}: {exc}"
                self.state = "error" if not good else "warn"
                wait = 10
            self._stop.wait(wait)

    # LTC -------------------------------------------------------------
    @staticmethod
    def audio_inputs():
        """[(name, max_input_channels)] or raises ImportError."""
        import sounddevice as sd
        out, seen = [], set()
        for d in sd.query_devices():
            if d["max_input_channels"] > 0 and d["name"] not in seen:
                seen.add(d["name"])
                out.append((d["name"], d["max_input_channels"]))
        return out

    def _start_ltc(self):
        try:
            import numpy  # noqa: F401
            import sounddevice as sd
        except Exception:
            self.status = ("LTC needs numpy + sounddevice "
                           "(run 'Run DA Timer.bat' once)")
            self.state = "error"
            return
        try:
            device = None
            if self.ltc_device:
                for i, d in enumerate(sd.query_devices()):
                    if d["name"] == self.ltc_device and d["max_input_channels"] > 0:
                        device = i
                        break
            info = sd.query_devices(device, "input")
            channels = max(1, min(int(info["max_input_channels"]),
                                  self.ltc_channel + 1))
            sr = int(info["default_samplerate"]) or 48000
            self.decoder = LTCDecoder(sr, self._on_ltc_frame)
            self._sr = sr
            self._block_start = 0
            chan = min(self.ltc_channel, channels - 1)

            def callback(indata, frames, time_info, status):
                # time at which the LAST sample of this block was captured
                self._block_mono = time.monotonic()
                self._block_end = self.decoder.sample_pos + frames
                self.decoder.feed(indata[:, chan])

            self._stream = sd.InputStream(device=device, channels=channels,
                                          samplerate=sr, blocksize=1024,
                                          dtype="float32", callback=callback)
            self._stream.start()
            self.status, self.state = f"LTC: listening on {info['name']}", "warn"
        except Exception as exc:
            self.status, self.state = f"LTC audio error: {exc}", "error"
            self._stream = None

    def _on_ltc_frame(self, h, m, s, f, fps_est, drop, end_pos):
        self._max_frame = max(getattr(self, "_max_frame", 0), f)
        fps = nominal_fps(fps_est, drop, self._max_frame)
        # The frame's timecode labels its START; we've just received its end
        tc = h * 3600 + m * 60 + s + (f + 1) / fps
        # monotonic time at which the frame ended, from the block timing
        mono = self._block_mono - (self._block_end - end_pos) / self._sr
        off = tc - mono
        if self.ltc_offset is None or abs(((off - self.ltc_offset + 43200)
                                           % 86400) - 43200) > 1.0:
            self.ltc_offset = off              # jump / first lock
        else:
            self.ltc_offset += 0.1 * (off - self.ltc_offset)   # smooth jitter
        self.ltc_last = time.monotonic()
        self.ltc_fps = fps
        sep = ";" if drop else ":"
        self.ltc_tc = f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"
