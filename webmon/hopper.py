#!/usr/bin/env python3
"""Zone-hopping receiver for a bladeRF 2.0 (xA4).

Cycles the RX centre frequency through a list of zones while the sample stream
keeps running (FPGA tuning mode), reduces each visit to an averaged and a
peak-hold spectrum, and prints one JSON line per visit on stdout.

--calibrate measures the receiver's own "smile" (extra noise toward the edges of
the sampled band) at quiet frequencies and stores it; later runs subtract it in
the power domain. Measured on this device the extra noise is a fixed level in
dBFS, independent of RF frequency and gain, so one profile per sample rate is
enough.

Receive only: no TX call is ever made, and the bias tee is forced off.
Levels are relative dBFS, not calibrated dBm.
"""
import argparse
import ctypes as C
import ctypes.util
import json
import queue
import signal
import sys
import threading
import time
from datetime import datetime

import numpy as np

RX0 = 0                  # BLADERF_CHANNEL_RX(0)
TUNING_FPGA = 1          # BLADERF_TUNING_MODE_FPGA
GAIN_MGC = 1             # BLADERF_GAIN_MGC
FMT_SC16_Q11 = 0
LAYOUT_RX_X1 = 0
NUM_BUFFERS, BUF_SAMPLES, NUM_XFERS = 8, 8192, 4
FFT_N = 2048


def load_lib():
    for name in ("/opt/homebrew/lib/libbladeRF.2.dylib", "/usr/local/lib/libbladeRF.2.dylib",
                 ctypes.util.find_library("bladeRF")):
        if name:
            try:
                return C.CDLL(name)
            except OSError:
                continue
    raise RuntimeError("libbladeRF not found")


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class Radio:
    """Thin ctypes wrapper: open, configure RX, retune while streaming, read."""

    def __init__(self, rate, gain, first_freq, max_samples):
        self.lib = lib = load_lib()
        vp = C.c_void_p
        lib.bladerf_open.argtypes = [C.POINTER(vp), C.c_char_p]
        lib.bladerf_set_tuning_mode.argtypes = [vp, C.c_int]
        lib.bladerf_set_sample_rate.argtypes = [vp, C.c_int, C.c_uint32, C.POINTER(C.c_uint32)]
        lib.bladerf_set_bandwidth.argtypes = [vp, C.c_int, C.c_uint32, C.POINTER(C.c_uint32)]
        lib.bladerf_set_gain_mode.argtypes = [vp, C.c_int, C.c_int]
        lib.bladerf_set_gain.argtypes = [vp, C.c_int, C.c_int]
        lib.bladerf_set_bias_tee.argtypes = [vp, C.c_int, C.c_bool]
        lib.bladerf_set_frequency.argtypes = [vp, C.c_int, C.c_uint64]
        lib.bladerf_sync_config.argtypes = [vp, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_uint, C.c_uint]
        lib.bladerf_enable_module.argtypes = [vp, C.c_int, C.c_bool]
        lib.bladerf_sync_rx.argtypes = [vp, C.c_void_p, C.c_uint, C.c_void_p, C.c_uint]
        lib.bladerf_close.argtypes = [vp]
        lib.bladerf_strerror.restype = C.c_char_p
        self.dev = vp()
        self.enabled = False
        self.chk(lib.bladerf_open(C.byref(self.dev), None), "open")
        act = C.c_uint32()
        # Order matters: switching tuning mode resets the gain, so set the mode first.
        self.chk(lib.bladerf_set_tuning_mode(self.dev, TUNING_FPGA), "tuning mode")
        self.chk(lib.bladerf_set_sample_rate(self.dev, RX0, int(rate), C.byref(act)), "sample rate")
        self.rate = int(act.value)
        self.chk(lib.bladerf_set_bandwidth(self.dev, RX0, min(56_000_000, int(self.rate * 1.4)), C.byref(act)), "bandwidth")
        self.bandwidth = int(act.value)
        self.chk(lib.bladerf_set_gain_mode(self.dev, RX0, GAIN_MGC), "gain mode")
        self.chk(lib.bladerf_set_gain(self.dev, RX0, int(gain)), "gain")
        self.chk(lib.bladerf_set_bias_tee(self.dev, RX0, False), "bias tee off")
        self.chk(lib.bladerf_set_frequency(self.dev, RX0, int(first_freq)), "frequency")
        self.chk(lib.bladerf_sync_config(self.dev, LAYOUT_RX_X1, FMT_SC16_Q11, NUM_BUFFERS, BUF_SAMPLES, NUM_XFERS, 3000), "sync config")
        self.chk(lib.bladerf_enable_module(self.dev, RX0, True), "enable rx")
        self.enabled = True
        self.buf = np.zeros(max_samples * 2, dtype=np.int16)
        self.ptr = self.buf.ctypes.data_as(C.c_void_p)

    def chk(self, r, what):
        if r != 0:
            raise RuntimeError(f"{what} failed: {self.lib.bladerf_strerror(r).decode()} ({r})")

    def retune(self, freq):
        self.chk(self.lib.bladerf_set_frequency(self.dev, RX0, int(freq)), "retune")

    def rx(self, n):
        self.chk(self.lib.bladerf_sync_rx(self.dev, self.ptr, n, None, 1500), "rx")  # short timeout: a stalled stream must not outlast a stop request

    def close(self):
        try:
            if self.enabled:
                self.lib.bladerf_enable_module(self.dev, RX0, False)
            self.lib.bladerf_close(self.dev)
        except Exception:  # noqa: BLE001
            pass


def calibrate(cfg):
    """Measure the edge-noise profile at quiet frequencies; save it as JSON."""
    rate = int(cfg["rate"])
    freqs = [float(f) for f in cfg["freqs"]]
    secs = float(cfg.get("secs", 0.4))
    chunk = 262144
    radio = Radio(rate, cfg.get("gain", 20), freqs[0], chunk)
    try:
        rate = radio.rate
        win = np.hanning(FFT_N).astype(np.float32)
        wnorm = float((win ** 2).sum())
        off = (np.arange(FFT_N) - FFT_N / 2) * rate / FFT_N / 1e6
        centre = (np.abs(off) < 3) & (np.abs(off) > 0.6)
        excess, floors = [], []
        for f in freqs:
            radio.retune(f)
            for _ in range(3):
                radio.rx(chunk)  # settle and drain
            acc = np.zeros(FFT_N)
            nseg = got = 0
            while got < rate * secs:
                radio.rx(chunk)
                x = radio.buf.astype(np.float32) / 2048.0
                seg = (x[0::2] + 1j * x[1::2]).reshape(-1, FFT_N)
                acc += (np.abs(np.fft.fft(seg * win, axis=1)) ** 2).sum(axis=0)
                nseg += seg.shape[0]
                got += chunk
            p = np.fft.fftshift(acc / nseg) / wnorm
            c = float(np.median(p[centre]))
            floors.append(round(10 * np.log10(c), 1))
            excess.append(np.maximum(p - c, 0.0))
            emit({"type": "calib_progress", "freq": f, "floor": floors[-1]})
        stack = np.stack(excess)
        # Lower envelope across the reference frequencies: better to under-correct than to dig a hole.
        e = np.min(stack, axis=0)
        e = np.convolve(e, np.ones(9) / 9, mode="same")
        # Near the centre the extra noise is far below the measurement error, so fade the correction in:
        # nothing below 20 % of Nyquist, full strength from 40 % of Nyquist outwards.
        w = np.clip((np.abs(off) / (rate / 2e6) - 0.2) / 0.2, 0.0, 1.0)
        e = e * w
        e[np.abs(off) < 0.6] = 0.0   # the LO/DC spike is handled separately
        edge = {}
        for o in (10, 12, 14, 16, 18, 22):
            m = np.abs(np.abs(off) - o) < 0.6
            if m.any():
                edge[str(o)] = round(float(10 * np.log10(max(e[m].mean(), 1e-12))), 1)
        m14 = np.abs(np.abs(off) - 14) < 0.6
        spread = float(np.std(10 * np.log10(np.maximum(stack[:, m14].mean(axis=1), 1e-12)))) if m14.any() else 0.0
        out = {"rate": rate, "bandwidth": radio.bandwidth, "fft": FFT_N, "created": datetime.now().isoformat(timespec="seconds"),
               "freqs_mhz": [f / 1e6 for f in freqs], "floors_dbfs": floors, "edge_dbfs": edge,
               "spread_db_at_14": round(spread, 2), "method": "min-envelope, faded in 20-40% of Nyquist", "excess": e.tolist()}
        with open(cfg["out"], "w") as fh:
            json.dump(out, fh)
        emit({"type": "calib", "ok": True, "rate": rate, "edge_dbfs": edge, "floors_dbfs": floors, "spread_db_at_14": out["spread_db_at_14"]})
    finally:
        radio.close()


def hop(cfg):
    zones = cfg["zones"]
    rate = int(cfg.get("rate", 40e6))
    dwell_ms = float(cfg.get("dwell_ms", 10))
    crop = float(cfg.get("crop", 0.15))
    disp = int(cfg.get("disp", 512))
    if not zones:
        raise SystemExit("no zones")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    keep = max(FFT_N, int(np.ceil(rate * dwell_ms / 1000.0 / FFT_N)) * FFT_N)
    discard = NUM_BUFFERS * BUF_SAMPLES + int(rate * 0.0015)  # drain samples buffered during the retune
    radio = Radio(rate, cfg.get("gain", 20), zones[0]["freq"], max(keep, discard))
    try:
        rate = radio.rate

        # optional edge-noise correction (power domain)
        excess, calib_state = None, None
        if cfg.get("calib_path"):
            try:
                with open(cfg["calib_path"]) as fh:
                    cal = json.load(fh)
                if cal.get("fft") == FFT_N and abs(cal.get("rate", 0) - rate) < 1000:
                    excess = np.asarray(cal["excess"], dtype=np.float64)
                    calib_state = "applied " + cal.get("created", "")
                else:
                    calib_state = "ignored: rate mismatch"
            except Exception as e:  # noqa: BLE001
                calib_state = f"ignored: {e}"

        win = np.hanning(FFT_N).astype(np.float32)
        wnorm = float((win ** 2).sum())
        n_crop = int(FFT_N * crop)
        idx = np.arange(n_crop, FFT_N - n_crop)
        df_bin = rate / FFT_N
        k = -(-len(idx) // disp)
        m = len(idx) // k
        dfd = df_bin * k
        offs = (idx[:m * k].reshape(m, k).mean(axis=1) - FFT_N / 2) * df_bin
        emit({"type": "hello", "rate": rate, "keep_ms": round(keep / rate * 1000, 1), "bins": int(m),
              "span_hz": float(offs[-1] - offs[0] + dfd), "zones": [z["name"] for z in zones], "calib": calib_state})

        q = queue.Queue(maxsize=3)
        st = {"dropped": 0, "hops": 0, "retune": 0.0, "hop": 0.0, "cycle": 0.0}

        def reduce(raw_bytes):
            x = raw_bytes.astype(np.float32) / 2048.0
            iq = x[0::2] + 1j * x[1::2]
            nseg = len(iq) // FFT_N
            sp = np.abs(np.fft.fft(iq[: nseg * FFT_N].reshape(nseg, FFT_N) * win, axis=1)) ** 2
            mean = np.fft.fftshift(sp.mean(axis=0)) / wnorm
            peak = np.fft.fftshift(sp.max(axis=0)) / wnorm
            if excess is not None:  # subtract the receiver's own edge noise; scale the peak by the same factor
                fixed = np.maximum(mean - excess, 0.25 * mean)
                peak = peak * (fixed / np.maximum(mean, 1e-30))
                mean = fixed
            c = FFT_N // 2
            for a in (mean, peak):  # remove the LO/DC leakage spike at the centre
                a[c - 3:c + 4] = np.interp(np.arange(c - 3, c + 4), [c - 4, c + 4], [a[c - 4], a[c + 4]])
            return mean[idx], peak[idx]

        def worker():
            while not (stop.is_set() and q.empty()):
                try:
                    zi, t, raw_bytes = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                mean, peak = reduce(raw_bytes)
                md = 10 * np.log10(mean + 1e-20)
                pd = 10 * np.log10(peak + 1e-20)
                emit({"type": "frame", "z": zi, "t": t, "f0": float(zones[zi]["freq"] + offs[0]), "df": dfd,
                      "p": np.round(md[:m * k].reshape(m, k).max(axis=1), 1).tolist(),
                      "pk": np.round(pd[:m * k].reshape(m, k).max(axis=1), 1).tolist(),
                      "floor": round(float(np.median(md)), 1), "max": round(float(md.max()), 1),
                      "bp": round(float(10 * np.log10(mean.mean() + 1e-20)), 1)})

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        radio.rx(discard)
        last_stats = last_cycle = time.time()
        while not stop.is_set():
            for zi, z in enumerate(zones):
                if stop.is_set():
                    break
                t0 = time.perf_counter()
                radio.retune(z["freq"])
                t1 = time.perf_counter()
                radio.rx(discard)
                t_epoch = time.time()
                radio.rx(keep)
                t2 = time.perf_counter()
                try:
                    q.put_nowait((zi, t_epoch, radio.buf[: keep * 2].copy()))
                except queue.Full:
                    st["dropped"] += 1
                a = 0.1
                st["retune"] += ((t1 - t0) * 1000 - st["retune"]) * a if st["hops"] else (t1 - t0) * 1000 - st["retune"]
                st["hop"] += ((t2 - t0) * 1000 - st["hop"]) * a if st["hops"] else (t2 - t0) * 1000 - st["hop"]
                st["hops"] += 1
            now = time.time()
            st["cycle"] = (now - last_cycle) * 1000
            last_cycle = now
            if now - last_stats >= 1.0:
                last_stats = now
                emit({"type": "stats", "hops": st["hops"], "retune_ms": round(st["retune"], 1), "hop_ms": round(st["hop"], 1),
                      "cycle_ms": round(st["cycle"], 1), "dropped": st["dropped"]})
        stop.set()
        th.join(2)
    finally:
        radio.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True, help="JSON configuration")
    ap.add_argument("--calibrate", action="store_true", help="measure and store the edge-noise profile")
    args = ap.parse_args()
    cfg = json.loads(args.cfg)
    try:
        (calibrate if args.calibrate else hop)(cfg)
    except Exception as e:  # noqa: BLE001
        emit({"type": "error", "msg": str(e)})
        sys.exit(2)


if __name__ == "__main__":
    main()
