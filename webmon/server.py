#!/usr/bin/env python3
"""Local web monitor for a bladeRF 2.0 (xA4): live spectrum, settings, sweeps, events.

Receive only. Binds to localhost. Uses bladeRF-cli (IQ via a FIFO) so no extra
SDR libraries are required. Levels are relative dBFS, not calibrated dBm.
"""
import argparse
import collections
import csv
import gzip
import json
import os
import re
import select
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
SWEEP_DIR = os.path.abspath(os.path.join(ROOT, "..", "sweep"))
SWEEP_PY = os.path.join(SWEEP_DIR, "bladerf_sweep.py")
SWEEP_PYTHON = os.path.join(SWEEP_DIR, ".venv", "bin", "python")
HOPPER_PY = os.path.join(ROOT, "hopper.py")
CALIB_DIR = os.path.join(ROOT, "calib")
EVENTS_CSV = os.path.join(ROOT, "events.csv")
CONFIG_JSON = os.path.join(ROOT, "config.json")
REC_DIR = os.path.join(ROOT, "recordings")
REC_NAME = re.compile(r"^(live|hop)_\d{8}_\d{6}\.jsonl\.gz$")
CLI = "bladeRF-cli"

LIMITS = {"freq": (70e6, 6e9), "rate": (1e6, 56e6), "gain": (-15, 60), "zones": 200}
NFFTS = (512, 1024, 2048, 4096, 8192)
MAX_POINTS = 1024


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Recorder:
    """One gzip'd JSON-lines file per recording. Line 1 is a header; every other line is one record.

    live: one record per second ({"t","p","floor"}, p = max over that second, geometry in the header
          line that precedes it). hop: one record per zone frame ({"t","z","f0","df","p","pk","floor"}).
    """

    def __init__(self, kind, header):
        os.makedirs(REC_DIR, exist_ok=True)
        self.kind = kind
        self.name = f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.jsonl.gz"
        self.path = os.path.join(REC_DIR, self.name)
        self.fh = gzip.open(self.path, "wt", compresslevel=4)
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.last_flush = self.t0
        self.records = 0
        self.write({"type": "hdr", "kind": kind, "started": self.t0, **header})

    def write(self, obj):
        with self.lock:
            if self.fh is None:
                return
            self.fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self.records += 1
            if time.time() - self.last_flush > 5:   # sync-flush so a crash loses seconds, not the whole file
                self.fh.flush()
                self.last_flush = time.time()

    def close(self):
        with self.lock:
            if self.fh is not None:
                self.fh.close()
                self.fh = None

    def info(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {"name": self.name, "secs": round(time.time() - self.t0), "bytes": size, "records": self.records}


class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.ctl = threading.RLock()   # serialises start/stop/restart of the bladeRF-cli stream
        self.children = []             # every bladeRF-cli we spawned, so none can be orphaned
        self.cond = threading.Condition()
        self.cfg = {
            "freq": 2437e6, "rate": 20e6, "gain": 20, "nfft": 2048, "avg": 8,
            "fps": 8, "crop": 0.10, "dc_fix": True, "occ_thr": 10,
            "watch_on": False, "watch_f": 2437e6, "watch_bw": 20e6, "watch_thr": 6,
        }
        self.mode = "idle"          # idle | live | sweep
        self.error = ""
        self.proc = None
        self.thread = None
        self.stop_evt = threading.Event()
        self.seq = 0
        self.frame_json = ""
        self.device = {}
        self.sweep = {"running": False, "progress": 0, "total": 0, "out": "", "msg": ""}
        self.events = []
        self.resume_live = False
        # zone-hopping monitor
        self.hop = {"running": False, "error": "", "hello": None, "stats": None, "cfg": None}
        self.hop_cfg = {"zones": [], "rate": 56e6, "gain": 20, "dwell_ms": 10, "crop": 0.25, "calib": True,
                        "trig_db": 8, "log_events": True}
        self.hop_proc = None
        self.hop_thread = None
        self.hop_frames = collections.deque(maxlen=600)
        self.hop_seq = 0
        self.hop_cond = threading.Condition()
        self.rec = {"live": None, "hop": None}
        self.rec_acc = None   # live recording: max-hold of the current second
        try:
            with open(CONFIG_JSON) as fh:
                saved = json.load(fh)
            self.cfg.update({k: v for k, v in saved.items() if k in self.cfg})
            self.hop_cfg.update(saved.get("hop", {}))
        except (OSError, ValueError):
            pass
        self.refresh_device()

    # ---------------- recording ----------------
    def rec_start(self, kind):
        with self.lock:
            if kind not in self.rec:
                return False, "unknown kind"
            if self.rec[kind] is not None:
                return False, "already recording"
            if kind == "live" and self.mode != "live":
                return False, "即時監控沒有在執行"
            if kind == "hop" and not self.hop["running"]:
                return False, "分區監控沒有在執行"
            hdr = {"cfg": dict(self.cfg)} if kind == "live" else {"cfg": self.hop_cfg, "hello": self.hop["hello"]}
            self.rec_acc = None
            self.rec[kind] = Recorder(kind, hdr)
        return True, self.rec[kind].name

    def rec_stop(self, kind):
        with self.lock:
            r, self.rec[kind] = self.rec.get(kind), None
        if r is not None:
            r.close()

    def _rec_live(self, frame):
        r = self.rec["live"]
        if r is None:
            return
        p = np.asarray(frame["p"], dtype=np.float32)
        geom = [frame["f0"], frame["df"], len(p), frame["rate"], frame["freq"]]
        acc = self.rec_acc
        if acc is None or acc["geom"] != geom:
            if acc is not None and acc["n"]:
                self._rec_live_flush(r)
            r.write({"type": "geom", "f0": frame["f0"], "df": frame["df"], "n": len(p),
                     "rate": frame["rate"], "freq": frame["freq"]})
            acc = self.rec_acc = {"geom": geom, "p": p.copy(), "floor": [], "t": frame["t"], "n": 0}
        acc["p"] = np.maximum(acc["p"], p)
        acc["floor"].append(frame["floor"])
        acc["n"] += 1
        if frame["t"] - acc["t"] >= 1.0:
            self._rec_live_flush(r)

    def _rec_live_flush(self, r):
        acc = self.rec_acc
        if not acc or not acc["n"]:
            return
        r.write({"t": round(acc["t"], 2), "p": np.round(acc["p"], 1).tolist(),
                 "floor": round(float(np.median(acc["floor"])), 1)})
        acc.update(p=np.full_like(acc["p"], -1e9), floor=[], t=acc["t"] + 1.0, n=0)

    def _rec_hop(self, o):
        r = self.rec["hop"]
        if r is not None:
            r.write({"t": o["t"], "z": o["z"], "f0": o["f0"], "df": o["df"], "p": o["p"], "pk": o["pk"],
                     "floor": o["floor"]})

    def list_recs(self):
        out = []
        if os.path.isdir(REC_DIR):
            for n in sorted(os.listdir(REC_DIR), reverse=True):
                if REC_NAME.match(n):
                    st = os.stat(os.path.join(REC_DIR, n))
                    out.append({"name": n, "kind": n.split("_")[0], "bytes": st.st_size, "mtime": st.st_mtime,
                                "active": any(r is not None and r.name == n for r in self.rec.values())})
        return out

    def delete_rec(self, name):
        if not REC_NAME.match(name) or any(r is not None and r.name == name for r in self.rec.values()):
            return False
        src = os.path.join(REC_DIR, name)
        if not os.path.exists(src):
            return False
        trash = os.path.join(REC_DIR, ".trash")
        os.makedirs(trash, exist_ok=True)
        os.replace(src, os.path.join(trash, name))   # moved, never hard-deleted
        return True

    # ---------------- device info ----------------
    def refresh_device(self):
        info = {}
        try:
            p = subprocess.run([CLI, "-p"], capture_output=True, text=True, timeout=15)
            m = re.search(r"Description:\s*(.+)", p.stdout)
            info["board"] = m.group(1).strip() if m else ""
            m = re.search(r"Serial:\s*(\S+)", p.stdout)
            info["serial"] = m.group(1) if m else ""
            info["present"] = bool(info["board"])
            if info["present"]:
                v = subprocess.run([CLI, "-e", "version"], capture_output=True, text=True, timeout=15)
                for key, pat in (("firmware", r"Firmware version:\s*(\S+)"),
                                 ("fpga", r"FPGA version:\s*(.+)"),
                                 ("libbladerf", r"libbladeRF version:\s*(\S+)")):
                    m = re.search(pat, v.stdout)
                    info[key] = m.group(1).strip() if m else ""
        except Exception as e:  # noqa: BLE001
            info["present"] = False
            info["error"] = str(e)
        self.device = info

    # ---------------- live control ----------------
    def set_config(self, new):
        with self.lock:
            c = self.cfg
            if "freq" in new:
                c["freq"] = clamp(float(new["freq"]), *LIMITS["freq"])
            if "rate" in new:
                c["rate"] = clamp(float(new["rate"]), *LIMITS["rate"])
            if "gain" in new:
                c["gain"] = int(clamp(float(new["gain"]), *LIMITS["gain"]))
            if "nfft" in new and int(new["nfft"]) in NFFTS:
                c["nfft"] = int(new["nfft"])
            if "avg" in new:
                c["avg"] = int(clamp(int(new["avg"]), 1, 64))
            if "fps" in new:
                c["fps"] = int(clamp(int(new["fps"]), 1, 20))
            if "crop" in new:
                c["crop"] = clamp(float(new["crop"]), 0.0, 0.4)
            if "dc_fix" in new:
                c["dc_fix"] = bool(new["dc_fix"])
            if "occ_thr" in new:
                c["occ_thr"] = clamp(float(new["occ_thr"]), 3, 40)
            if "watch_on" in new:
                c["watch_on"] = bool(new["watch_on"])
            if "watch_f" in new:
                c["watch_f"] = clamp(float(new["watch_f"]), *LIMITS["freq"])
            if "watch_bw" in new:
                c["watch_bw"] = clamp(float(new["watch_bw"]), 10e3, 56e6)
            if "watch_thr" in new:
                c["watch_thr"] = clamp(float(new["watch_thr"]), 1, 40)
            live = self.mode == "live"
            try:
                with open(CONFIG_JSON, "w") as fh:
                    json.dump({**c, "hop": self.hop_cfg}, fh)
            except OSError:
                pass
        if live:
            with self.ctl:
                self.stop_live()
                self.start_live()

    @staticmethod
    def _terminate(p):
        """Stop one bladeRF-cli: SIGINT, then SIGTERM, then SIGKILL."""
        if p is None or p.poll() is not None:
            return
        for sig, wait in ((signal.SIGINT, 6), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):  # SIGKILL mid-stream can wedge the USB link, so give a clean exit time
            try:
                p.send_signal(sig)
                p.wait(wait)
                return
            except subprocess.TimeoutExpired:
                continue
            except ProcessLookupError:
                return

    def _reap_children(self):
        alive = [p for p in self.children if p.poll() is None]
        for p in alive:
            self._terminate(p)
        self.children = []

    def start_live(self):
        with self.ctl:
            with self.lock:
                if self.mode != "idle":
                    return False, f"busy ({self.mode})"
            self._reap_children()  # never start while an old bladeRF-cli could still hold the device
            with self.lock:
                self.mode = "live"
                self.error = ""
                self.stop_evt.clear()
                self.thread = threading.Thread(target=self._live_loop, daemon=True)
                self.thread.start()
        return True, "ok"

    def stop_live(self):
        with self.ctl:
            with self.lock:
                self.stop_evt.set()
                t = self.thread
            if t:
                t.join(20)  # the loop's own cleanup stops its bladeRF-cli
            self._reap_children()
            with self.lock:
                self.thread = None
                self.proc = None
                if self.mode == "live":
                    self.mode = "idle"

    def _live_loop(self):
        cfg = dict(self.cfg)
        tmp = tempfile.mkdtemp(prefix="bladerf_mon_")
        fifo = os.path.join(tmp, "iq.fifo")
        logp = os.path.join(tmp, "cli.log")
        os.mkfifo(fifo)
        rate, nfft, navg = int(cfg["rate"]), cfg["nfft"], cfg["avg"]
        bw = int(min(56e6, max(200e3, rate * 1.4)))
        cmds = [
            "set biastee rx1 off", "set biastee rx2 off",
            f"set samplerate rx {rate}", f"set bandwidth rx {bw}",
            f"set frequency rx {int(cfg['freq'])}", "set agc rx off",
            f"set gain rx {cfg['gain']}",
            f"rx config file={fifo} format=bin n=0 samples=32768 buffers=32 xfers=16 timeout=5000",
            "rx start", "rx wait",
        ]
        logf = open(logp, "w")
        fd = -1
        proc = None
        try:
            proc = subprocess.Popen([CLI, "-e", "; ".join(cmds)], stdout=logf, stderr=subprocess.STDOUT)
            with self.lock:
                self.proc = proc
                self.children.append(proc)
            fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            need = nfft * 4 * navg
            win = np.hanning(nfft).astype(np.float32)
            wnorm = float((win ** 2).sum())
            buf = bytearray()
            last_emit = 0.0
            n_crop = int(nfft * cfg["crop"])
            idx = np.arange(n_crop, nfft - n_crop)
            freqs = cfg["freq"] + (idx - nfft / 2) * (rate / nfft)
            occ = np.zeros(len(idx), np.float32)
            last_occ = 0.0
            baseline, trig_n, quiet_n, active = None, 0, 0, None
            got_data = False
            started = time.time()
            while not self.stop_evt.is_set():
                if proc.poll() is not None:
                    logf.flush()
                    msg = open(logp).read().strip().splitlines()
                    self.error = ("bladeRF-cli exited: " + " | ".join(msg[-3:]))[:400]
                    break
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    try:
                        chunk = os.read(fd, 1 << 20)
                    except BlockingIOError:
                        chunk = b""
                    if chunk:
                        got_data = True
                        buf += chunk
                        if len(buf) > 2 * need:
                            del buf[: (len(buf) - need) // 4 * 4]
                    else:
                        time.sleep(0.02)
                if not got_data and time.time() - started > 15:
                    self.error = "no samples received from bladeRF"
                    break
                now = time.time()
                if len(buf) < need or now - last_emit < 1.0 / cfg["fps"]:
                    continue
                last_emit = now
                raw = np.frombuffer(bytes(buf[-need:]), dtype=np.int16).astype(np.float32) / 2048.0
                iq = (raw[0::2] + 1j * raw[1::2]).reshape(navg, nfft)
                spec = np.abs(np.fft.fftshift(np.fft.fft(iq * win, axis=1), axes=1)) ** 2
                lin = spec.mean(axis=0) / wnorm
                if cfg["dc_fix"]:
                    c = nfft // 2
                    lin[c - 3:c + 4] = np.interp(np.arange(c - 3, c + 4), [c - 4, c + 4], [lin[c - 4], lin[c + 4]])
                db = 10 * np.log10(lin[idx] + 1e-20)
                floor = float(np.median(db))
                occ += 0.02 * ((db > floor + cfg["occ_thr"]).astype(np.float32) - occ)

                band_db, alert = None, False
                if cfg["watch_on"]:
                    m = np.abs(freqs - cfg["watch_f"]) <= cfg["watch_bw"] / 2
                    if m.any():
                        band_db = float(10 * np.log10(np.mean(10 ** (db[m] / 10)) + 1e-20))
                        if baseline is None:
                            baseline = band_db
                        trig = band_db > baseline + cfg["watch_thr"]
                        if trig:
                            trig_n, quiet_n = trig_n + 1, 0
                            fpk = float(freqs[m][np.argmax(db[m])])
                            if trig_n >= 2 and active is None:
                                active = {"t0": now, "peak": band_db, "fpk": fpk, "base": baseline}
                            if active is not None:
                                alert = True
                                if band_db > active["peak"]:
                                    active.update(peak=band_db, fpk=fpk)
                        else:
                            trig_n, quiet_n = 0, quiet_n + 1
                            baseline = 0.98 * baseline + 0.02 * band_db
                            if active is not None and quiet_n >= 3:
                                self._log_event(active, now, cfg)
                                active = None
                else:
                    baseline, trig_n, quiet_n, active = None, 0, 0, None

                n = len(db)
                if n > MAX_POINTS:
                    k = -(-n // MAX_POINTS)  # ceil: pool k bins per display point
                    m = n // k
                    db_out = db[: m * k].reshape(m, k).max(axis=1)
                    f0 = freqs[0] + (k - 1) / 2 * (rate / nfft)
                    df = rate / nfft * k
                    occ_out = occ[: m * k].reshape(m, k).max(axis=1)
                else:
                    db_out, f0, df, occ_out = db, freqs[0], rate / nfft, occ
                frame = {
                    "t": now, "f0": f0, "df": df, "p": np.round(db_out, 1).tolist(),
                    "floor": round(floor, 1), "max": round(float(db.max()), 1),
                    "band": None if band_db is None else round(band_db, 1),
                    "base": None if baseline is None else round(baseline, 1),
                    "alert": alert, "rate": rate, "freq": cfg["freq"],
                }
                if now - last_occ > 1.0:
                    last_occ = now
                    frame["occ"] = np.round(occ_out * 100).astype(int).tolist()
                self._rec_live(frame)
                with self.cond:
                    self.seq += 1
                    self.frame_json = json.dumps(frame)
                    self.cond.notify_all()
        except Exception as e:  # noqa: BLE001
            self.error = f"live error: {e}"
        finally:
            self.rec_stop("live")
            if fd >= 0:
                os.close(fd)
            logf.close()
            self._terminate(proc)  # this thread's own process, never someone else's
            try:
                os.remove(fifo)
                os.remove(logp)
                os.rmdir(tmp)
            except OSError:
                pass
            with self.lock:
                if self.mode == "live" and not self.stop_evt.is_set():
                    self.mode = "idle"  # ended on its own (error)

    def _log_event(self, ev, now, cfg):
        row = {
            "time": datetime.fromtimestamp(ev["t0"]).isoformat(timespec="seconds"),
            "duration_s": round(now - ev["t0"], 1),
            "watch_mhz": round(cfg["watch_f"] / 1e6, 3),
            "bw_mhz": round(cfg["watch_bw"] / 1e6, 3),
            "peak_db": round(ev["peak"], 1),
            "baseline_db": round(ev["base"], 1),
            "peak_freq_mhz": round(ev["fpk"] / 1e6, 3),
        }
        self.events.append(row)
        self.events = self.events[-300:]
        new = not os.path.exists(EVENTS_CSV)
        with open(EVENTS_CSV, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    # ---------------- zone hopping ----------------
    def start_hop(self, body):
        zones = []
        if len(body.get("zones") or []) > LIMITS["zones"]:
            return False, f"too many zones (max {LIMITS['zones']})"
        for z in (body.get("zones") or []):
            zones.append({"name": str(z.get("name", ""))[:24] or "zone",
                          "freq": clamp(float(z["freq"]), *LIMITS["freq"])})
        if not zones:
            return False, "no zones"
        cfg = {
            "zones": zones,
            "rate": clamp(float(body.get("rate", 56e6)), 10e6, 56e6),
            "gain": int(clamp(float(body.get("gain", 20)), *LIMITS["gain"])),
            "dwell_ms": clamp(float(body.get("dwell_ms", 10)), 2, 100),
            "crop": clamp(float(body.get("crop", 0.25)), 0.05, 0.35),
            "calib": bool(body.get("calib", True)),
            "trig_db": clamp(float(body.get("trig_db", 8)), 2, 40),
            "log_events": bool(body.get("log_events", True)),
        }
        with self.ctl:
            if self.mode == "live":
                self.stop_live()
            if self.hop["running"]:
                self.stop_hop()
            with self.lock:
                if self.mode != "idle":
                    return False, f"busy ({self.mode})"
                self.mode = "hop"
            self._reap_children()
            self.hop_cfg = cfg
            try:
                with open(CONFIG_JSON, "w") as fh:
                    json.dump({**self.cfg, "hop": cfg}, fh)
            except OSError:
                pass
            with self.hop_cond:
                self.hop_frames.clear()
            self.hop = {"running": True, "error": "", "hello": None, "stats": None, "cfg": cfg}
            argcfg = {k: cfg[k] for k in ("zones", "rate", "gain", "dwell_ms", "crop")}
            if cfg["calib"] and os.path.exists(calib_file(cfg["rate"])):
                argcfg["calib_path"] = calib_file(cfg["rate"])
            arg = json.dumps(argcfg)
            self.hop_err = tempfile.TemporaryFile()
            self.hop_proc = subprocess.Popen([SWEEP_PYTHON, HOPPER_PY, "--cfg", arg], stdout=subprocess.PIPE,
                                             stderr=self.hop_err, text=True, bufsize=1)
            self.children.append(self.hop_proc)
            self.hop_thread = threading.Thread(target=self._hop_reader, args=(self.hop_proc, cfg), daemon=True)
            self.hop_thread.start()
        return True, "ok"

    def stop_hop(self):
        with self.ctl:
            p, t = self.hop_proc, self.hop_thread
            self._terminate(p)
            if t:
                t.join(10)
            self._reap_children()
            with self.lock:
                self.hop_proc = None
                self.hop_thread = None
                self.hop["running"] = False
                if self.mode == "hop":
                    self.mode = "idle"

    def _hop_reader(self, proc, cfg):
        zst = {}
        try:
            for line in proc.stdout:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                kind = o.get("type")
                if kind == "hello":
                    self.hop["hello"] = o
                    if self.rec["hop"] is not None:
                        self.rec["hop"].write({"type": "hello", **{k: v for k, v in o.items() if k != "type"}})
                elif kind == "stats":
                    self.hop["stats"] = o
                elif kind == "error":
                    self.hop["error"] = o.get("msg", "error")
                elif kind == "frame":
                    self._rec_hop(o)
                    if cfg["log_events"]:
                        self._hop_event(zst, o, cfg)
                    with self.hop_cond:
                        self.hop_seq += 1
                        o["id"] = self.hop_seq
                        self.hop_frames.append(o)
                        self.hop_cond.notify_all()
            proc.wait()
            if proc.returncode not in (0, -2, -15) and not self.hop["error"]:
                try:
                    self.hop_err.seek(0)
                    tail = self.hop_err.read().decode(errors="replace").strip().splitlines()[-3:]
                except OSError:
                    tail = []
                self.hop["error"] = f"hopper exited ({proc.returncode}) " + " | ".join(tail)
        finally:
            self.rec_stop("hop")
            with self.lock:
                self.hop["running"] = False
                if self.mode == "hop" and self.hop_proc is proc:
                    self.mode = "idle"

    def _hop_event(self, zst, o, cfg):
        """Per-zone interference detector: zone power rising above its own slow baseline."""
        zi, bp, now = o["z"], o["bp"], o["t"]
        s = zst.setdefault(zi, {"base": None, "trig": 0, "quiet": 0, "active": None})
        if s["base"] is None:
            s["base"] = bp
        if bp > s["base"] + cfg["trig_db"]:
            s["trig"], s["quiet"] = s["trig"] + 1, 0
            i = max(range(len(o["p"])), key=o["p"].__getitem__)
            fpk = o["f0"] + i * o["df"]
            if s["trig"] >= 2 and s["active"] is None:
                s["active"] = {"t0": now, "peak": bp, "fpk": fpk, "base": s["base"]}
            if s["active"] is not None and bp > s["active"]["peak"]:
                s["active"].update(peak=bp, fpk=fpk)
        else:
            s["trig"], s["quiet"] = 0, s["quiet"] + 1
            s["base"] = 0.98 * s["base"] + 0.02 * bp
            if s["active"] is not None and s["quiet"] >= 3:
                z = cfg["zones"][zi]
                span = len(o["p"]) * o["df"]
                self._log_event(s["active"], now, {"watch_f": z["freq"], "watch_bw": span})
                s["active"] = None

    def calibrate(self, body):
        """Measure the receiver's edge-noise profile at quiet frequencies (antenna stays connected)."""
        rate = clamp(float(body.get("rate", 56e6)), 10e6, 56e6)
        gain = int(clamp(float(body.get("gain", 20)), *LIMITS["gain"]))
        with self.ctl:
            if self.hop["running"]:
                self.stop_hop()
            if self.mode == "live":
                self.stop_live()
            with self.lock:
                if self.mode != "idle":
                    return False, f"busy ({self.mode})"
                self.mode = "calib"
            try:
                os.makedirs(CALIB_DIR, exist_ok=True)
                cfg = {"rate": rate, "gain": gain, "freqs": [3700e6, 3900e6, 4250e6, 4700e6, 4900e6],
                       "secs": 0.4, "out": calib_file(rate)}
                p = subprocess.run([SWEEP_PYTHON, HOPPER_PY, "--calibrate", "--cfg", json.dumps(cfg)],
                                   capture_output=True, text=True, timeout=90)
                res = None
                for line in p.stdout.splitlines():
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("type") in ("calib", "error"):
                        res = o
                if p.returncode != 0 or not res or res.get("type") == "error":
                    return False, (res or {}).get("msg") or p.stderr.strip()[-200:] or "calibration failed"
                return True, res
            except subprocess.TimeoutExpired:
                return False, "calibration timed out"
            finally:
                with self.lock:
                    self.mode = "idle"

    # ---------------- sweep ----------------
    def start_sweep(self, start, stop, gain, fast=False):
        with self.lock:
            if self.sweep["running"]:
                return False, "sweep already running"
            if not os.path.exists(SWEEP_PYTHON):
                return False, "sweep virtualenv missing"
            start = clamp(float(start), 70, 6000)
            stop = clamp(float(stop), start + 1, 6000)
            gain = int(clamp(float(gain), *LIMITS["gain"]))
            self.resume_live = self.mode == "live"
        if self.hop["running"]:
            self.stop_hop()
        if self.resume_live:
            self.stop_live()
        with self.lock:
            if self.mode != "idle":
                return False, f"busy ({self.mode})"
            self.mode = "sweep"
            out = f"web_{datetime.now():%Y%m%d_%H%M%S}_{int(start)}-{int(stop)}MHz_g{gain}{'_fast' if fast else ''}"
            self.sweep = {"running": True, "progress": 0, "total": 0, "out": out, "msg": "starting"}
        threading.Thread(target=self._sweep_run, args=(start, stop, gain, out, bool(fast)), daemon=True).start()
        return True, out

    def _sweep_run(self, start, stop, gain, out, fast=False):
        try:
            p = subprocess.Popen(
                [SWEEP_PYTHON, SWEEP_PY, "--start", str(start), "--stop", str(stop),
                 "--gain", str(gain), "--out", out] + (["--fast"] if fast else []),
                cwd=SWEEP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                m = re.match(r"\[(\d+)/(\d+)\]", line)
                if m:
                    self.sweep.update(progress=int(m.group(1)), total=int(m.group(2)), msg=line.strip())
                elif "steps" in line:
                    m = re.match(r"(\d+) steps", line)
                    if m:
                        self.sweep["total"] = int(m.group(1))
                else:
                    self.sweep["msg"] = line.strip()[:200]
            p.wait()
            self.sweep["msg"] = "done" if p.returncode == 0 else f"failed (exit {p.returncode})"
        except Exception as e:  # noqa: BLE001
            self.sweep["msg"] = f"error: {e}"
        finally:
            self.sweep["running"] = False
            with self.lock:
                self.mode = "idle"
                resume = self.resume_live
                self.resume_live = False
            if resume:
                self.start_live()


mon = Monitor()
_sweep_cache = {}


def calib_file(rate):
    return os.path.join(CALIB_DIR, f"shape_{int(round(rate / 1e6))}M.json")


_calib_cache = {}


def list_calibs():
    out = []
    if os.path.isdir(CALIB_DIR):
        for fn in sorted(os.listdir(CALIB_DIR)):
            if not (fn.startswith("shape_") and fn.endswith(".json")):
                continue
            path = os.path.join(CALIB_DIR, fn)
            key = (path, os.path.getmtime(path))
            if key not in _calib_cache:
                try:
                    with open(path) as fh:
                        d = json.load(fh)
                    _calib_cache[key] = {"rate": d["rate"], "created": d.get("created", ""), "edge": d.get("edge_dbfs", {}),
                                         "floors": d.get("floors_dbfs", []), "spread": d.get("spread_db_at_14")}
                except (OSError, ValueError, KeyError):
                    _calib_cache[key] = None
            if _calib_cache[key]:
                out.append(_calib_cache[key])
    return out


def _csv_range(p):
    """First/last frequency of a sweep CSV, read from its head and tail only."""
    try:
        with open(p, "rb") as fh:
            fh.readline()
            first = float(fh.readline().split(b",")[0])
            fh.seek(max(0, os.path.getsize(p) - 200))
            last = float(fh.read().strip().splitlines()[-1].split(b",")[0])
        return first, last
    except (OSError, ValueError, IndexError):
        return None, None


def list_sweeps():
    out = []
    if os.path.isdir(SWEEP_DIR):
        for fn in sorted(os.listdir(SWEEP_DIR)):
            if fn.endswith(".csv"):
                p = os.path.join(SWEEP_DIR, fn)
                lo, hi = _csv_range(p)
                out.append({"name": fn, "mtime": datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds"),
                            "size": os.path.getsize(p), "fmin": lo, "fmax": hi})
    return out


def load_sweep(name):
    if name != os.path.basename(name) or not name.endswith(".csv"):
        raise ValueError("bad name")
    p = os.path.join(SWEEP_DIR, name)
    key = (p, os.path.getmtime(p))
    if key not in _sweep_cache:
        d = np.loadtxt(p, delimiter=",", skiprows=1)
        _sweep_cache.clear()
        _sweep_cache[key] = (d[:, 0], d[:, 1], d[:, 2] if d.shape[1] > 2 else None)
    return _sweep_cache[key]


def delete_sweep(name):
    """Move a sweep's CSV (and matching PNG) into sweep/.trash so a mistaken delete can be undone."""
    if name != os.path.basename(name) or not name.endswith(".csv"):
        raise ValueError("invalid sweep name")
    base = name[:-4]
    if mon.sweep.get("running") and mon.sweep.get("out") == base:
        raise ValueError("this sweep is still running")
    if not os.path.exists(os.path.join(SWEEP_DIR, name)):
        raise ValueError("sweep not found")
    trash = os.path.join(SWEEP_DIR, ".trash")
    os.makedirs(trash, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    moved = []
    for ext in (".csv", ".png"):
        src = os.path.join(SWEEP_DIR, base + ext)
        if os.path.exists(src):
            os.replace(src, os.path.join(trash, f"{stamp}_{base}{ext}"))
            moved.append(base + ext)
    _sweep_cache.clear()
    return moved


_floor_cache = {}


def noise_floor(name):
    """Local noise floor of a sweep: 10th percentile of the mean trace over a +-27 MHz window (5 MHz cells).
    Per-band gain-table steps (e.g. at 4 GHz) show up in it, so subtracting it gives dB-above-floor."""
    f, p, _ = load_sweep(name)
    key = (name, len(f), float(f[0]), float(p.sum()))
    if key not in _floor_cache:
        cell = np.floor((f - f[0]) / 5.0).astype(int)
        n = cell[-1] + 1
        cf = np.full(n, np.nan)
        cp = np.full(n, np.nan)
        for c in range(n):
            m = cell == c
            if m.any():
                cf[c], cp[c] = f[m].mean(), p[m].mean()
        ok = ~np.isnan(cp)
        cf, cp = cf[ok], cp[ok]
        k = 5
        pad = np.pad(cp, k, mode="edge")
        win = np.lib.stride_tricks.sliding_window_view(pad, 2 * k + 1)
        fl = np.percentile(win, 10, axis=1)
        _floor_cache.clear()
        _floor_cache[key] = (cf, fl)
    cf, fl = _floor_cache[key]
    return cf, fl


def pooled(name, lo, hi, points):
    f, p, pk = load_sweep(name)
    i0, i1 = np.searchsorted(f, lo), np.searchsorted(f, hi)
    f, p = f[i0:i1], p[i0:i1]
    pk = p if pk is None else pk[i0:i1]  # sweeps recorded with --fast also carry a peak-hold column
    if len(f) == 0:
        return {"f": [], "max": [], "mean": []}
    m = max(1, len(f) // points)
    n = len(f) // m
    f, p, pk = f[: n * m].reshape(n, m), p[: n * m].reshape(n, m), pk[: n * m].reshape(n, m)
    fm = f.mean(axis=1)
    cf, fl = noise_floor(name)
    return {"f": np.round(fm, 3).tolist(), "max": np.round(pk.max(axis=1), 1).tolist(),
            "mean": np.round(p.mean(axis=1), 1).tolist(), "floor": np.round(np.interp(fm, cf, fl), 1).tolist()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _status(self):
        return {"mode": mon.mode, "error": mon.error, "cfg": mon.cfg, "device": mon.device,
                "sweep": mon.sweep, "limits": LIMITS, "nffts": NFFTS,
                "rec": {k: (r.info() if r is not None else None) for k, r in mon.rec.items()},
                "hop": {"running": mon.hop["running"], "error": mon.hop["error"], "hello": mon.hop["hello"],
                        "stats": mon.hop["stats"], "cfg": mon.hop_cfg, "calibs": list_calibs()}}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, self._status())
            if u.path == "/api/stream":
                return self._stream()
            if u.path == "/api/hopstream":
                return self._hopstream()
            if u.path == "/api/sweeps":
                return self._send(200, list_sweeps())
            if u.path == "/api/sweep":
                return self._send(200, pooled(q["name"], float(q.get("lo", 0)), float(q.get("hi", 1e5)),
                                              int(q.get("points", 3000))))
            if u.path == "/api/compare":
                lo, hi, n = float(q.get("lo", 0)), float(q.get("hi", 1e5)), int(q.get("points", 3000))
                a, b = pooled(q["a"], lo, hi, n), pooled(q["b"], lo, hi, n)
                if not a["f"] or not b["f"]:
                    return self._send(200, {"f": [], "diff": []})
                fb = np.interp(a["f"], b["f"], b["mean"])
                return self._send(200, {"f": a["f"], "diff": np.round(np.array(a["mean"]) - fb, 1).tolist()})
            if u.path == "/api/recs":
                return self._send(200, mon.list_recs())
            if u.path == "/api/rec/download":
                name = q.get("name", "")
                path = os.path.join(REC_DIR, name)
                if not REC_NAME.match(name) or not os.path.isfile(path):
                    return self._send(404, {"error": "not found"})
                with open(path, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/events":
                return self._send(200, mon.events[::-1])
            if u.path == "/api/events.csv":
                body = open(EVENTS_CSV, "rb").read() if os.path.exists(EVENTS_CSV) else b""
                return self._send(200, body, "text/csv")
            self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/config":
                mon.set_config(body)
                return self._send(200, self._status())
            if path == "/api/hop/start":
                ok, msg = mon.start_hop(body)
                return self._send(200 if ok else 409, {"ok": ok, "msg": msg})
            if path == "/api/hop/calibrate":
                ok, res = mon.calibrate(body)
                return self._send(200 if ok else 409, {"ok": True, **res} if ok else {"ok": False, "msg": res})
            if path == "/api/rec":
                kind = str(body.get("kind", ""))
                if body.get("action") == "start":
                    ok, msg = mon.rec_start(kind)
                else:
                    if kind in mon.rec:
                        mon.rec_stop(kind)
                    ok, msg = True, "stopped"
                return self._send(200 if ok else 409, {"ok": ok, "msg": msg})
            if path == "/api/rec/delete":
                return self._send(200, {"ok": mon.delete_rec(str(body.get("name", "")))})
            if path == "/api/hop/stop":
                mon.stop_hop()
                return self._send(200, {"ok": True})
            if path == "/api/live":
                if body.get("action") == "start":
                    ok, msg = mon.start_live()
                else:
                    mon.stop_live()
                    ok, msg = True, "stopped"
                return self._send(200 if ok else 409, {"ok": ok, "msg": msg})
            if path == "/api/sweep/start":
                ok, msg = mon.start_sweep(body.get("start", 70), body.get("stop", 6000), body.get("gain", 20),
                                          bool(body.get("fast", False)))
                return self._send(200 if ok else 409, {"ok": ok, "msg": msg})
            if path == "/api/sweep/delete":
                return self._send(200, {"ok": True, "moved": delete_sweep(str(body.get("name", "")))})
            if path == "/api/refresh_device":
                if mon.mode == "idle":
                    mon.refresh_device()
                return self._send(200, self._status())
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})

    def _hopstream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = mon.hop_seq
        try:
            while True:
                with mon.hop_cond:
                    mon.hop_cond.wait_for(lambda: mon.hop_seq != last, timeout=10)
                    new = [f for f in mon.hop_frames if f["id"] > last]
                    if mon.hop_seq < last:  # restarted
                        new = list(mon.hop_frames)
                    last = mon.hop_seq
                if not new:
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(b"".join(b"data: " + json.dumps(f, separators=(",", ":")).encode() + b"\n\n" for f in new))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = -1
        try:
            while True:
                with mon.cond:
                    mon.cond.wait_for(lambda: mon.seq != last, timeout=10)
                    seq, data = mon.seq, mon.frame_json
                if seq == last or not data:
                    self.wfile.write(b": ping\n\n")
                else:
                    last = seq
                    self.wfile.write(b"data: " + data.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"bladeRF monitor on http://{args.host}:{args.port}  (device: {mon.device})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop_hop()
        mon.stop_live()
        mon.rec_stop("live")
        mon.rec_stop("hop")


if __name__ == "__main__":
    main()
