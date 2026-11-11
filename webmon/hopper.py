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
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from bladerf_io import Radio as _Radio, watch_stdin  # noqa: E402

NUM_BUFFERS, BUF_SAMPLES, NUM_XFERS = 8, 8192, 4
FFT_N = 2048


class Radio(_Radio):
    def __init__(self, rate, gain, first_freq, max_samples):
        super().__init__(rate, gain, first_freq, max_samples, fpga_tuning=True, num_buffers=NUM_BUFFERS,
                         buf_samples=BUF_SAMPLES, num_xfers=NUM_XFERS)


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


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


STDIN_STOP = [False]


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
    if STDIN_STOP[0]:
        watch_stdin(stop)

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
    ap.add_argument("--stdin-stop", action="store_true", help="stop when stdin is closed (used by the server)")
    args = ap.parse_args()
    STDIN_STOP[0] = args.stdin_stop
    cfg = json.loads(args.cfg)
    try:
        (calibrate if args.calibrate else hop)(cfg)
    except Exception as e:  # noqa: BLE001
        emit({"type": "error", "msg": str(e)})
        sys.exit(2)


if __name__ == "__main__":
    main()
