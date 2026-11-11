#!/usr/bin/env python3
"""Wideband spectrum sweep for bladeRF 2.0 using bladeRF-cli.

Steps the RX center frequency, captures an IQ burst at each step, computes an
averaged power spectrum, and stitches the steps into one plot.
Levels are relative (dBFS), not calibrated dBm.

Two modes:
  default  one bladeRF-cli process per step (slow, ~15 min for 70-6000 MHz)
  --fast   one bladeRF-cli process for the whole sweep; IQ is streamed through
           FIFOs and reduced on the fly (no disk), with 50 % overlapping steps.
           Also records a peak-hold (max over time) column.
"""
import argparse
import csv
import os
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

FS = 56e6          # sample rate, Hz (xA4 usable bandwidth ~56 MHz)
FFT_N = 2048
DC_MASK_HZ = 3e6   # mask this far around each step centre (LO/DC leakage spike)
HALF_WIN_HZ = 12e6 # keep only +/-12 MHz: noise floor rises ~25 dB toward the 56 MHz edges


def capture(freq_hz, gain, nsamples, path, bladerf_cli):
    cmds = [
        f"set samplerate rx {int(FS)}",
        f"set bandwidth rx {int(FS)}",
        f"set frequency rx {int(freq_hz)}",
        "set agc rx off",
        f"set gain rx {gain}",
        f"rx config file={path} format=bin n={nsamples} timeout=10000",
        "rx start",
        "rx wait",
    ]
    for attempt in range(1, 4):
        if os.path.exists(path):
            os.remove(path)
        try:
            r = subprocess.run(
                [bladerf_cli, "-e", "; ".join(cmds)],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(f"  timeout at {freq_hz/1e6:.1f} MHz, retry {attempt}/3", flush=True)
            time.sleep(1)
            continue
        if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return
        print(f"  capture error at {freq_hz/1e6:.1f} MHz, retry {attempt}/3", flush=True)
        time.sleep(1)
    raise RuntimeError(f"capture failed at {freq_hz/1e6:.1f} MHz after 3 attempts")


def spectrum(path, skip=0):
    raw = np.fromfile(path, dtype=np.int16)[skip * 2:]
    raw = raw[: len(raw) // 2 * 2].astype(np.float32) / 2048.0
    iq = raw[0::2] + 1j * raw[1::2]
    nseg = len(iq) // FFT_N
    iq = iq[: nseg * FFT_N].reshape(nseg, FFT_N)
    win = np.hanning(FFT_N)
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq * win, axis=1), axes=1)) ** 2
    pwr = spec.mean(axis=0) / (win ** 2).sum()
    return 10 * np.log10(pwr + 1e-20)


def slow_collect(args):
    step = HALF_WIN_HZ  # windows [c-12,c-3] and [c+3,c+12] of neighbouring steps tile exactly
    centers = np.arange(args.start * 1e6 + HALF_WIN_HZ, args.stop * 1e6 + HALF_WIN_HZ, step)
    centers = np.minimum(centers, 6000e6)  # xA4 tuning limit
    nsamples = int(FS * args.dwell) // FFT_N * FFT_N
    print(f"{len(centers)} steps, {step/1e6:.0f} MHz apart, gain {args.gain} dB")

    freqs, levels = [], []
    axis = (np.arange(FFT_N) - FFT_N / 2) * FS / FFT_N
    keep = (np.abs(axis) > DC_MASK_HZ) & (np.abs(axis) <= HALF_WIN_HZ)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "iq.bin")
        for i, fc in enumerate(centers, 1):
            if os.path.exists(path):
                os.remove(path)
            capture(fc, args.gain, nsamples, path, args.cli)
            db = spectrum(path)
            freqs.append((fc + axis)[keep])
            levels.append(db[keep])
            print(f"[{i}/{len(centers)}] {fc/1e6:8.1f} MHz  peak {db[keep].max():6.1f} dBFS  ({time.time()-t0:.0f}s)", flush=True)
    return freqs, levels, None


FAST_STEP_HZ = 8e6        # 50 % overlap of the +/-12 MHz window: overlapped steps are averaged
FAST_SKIP = 32768         # samples discarded after each retune
FAST_BUF = 32768          # samples per USB buffer; n must be a multiple of this
CHUNK_BYTES = 4 << 20


def fast_collect(args):
    """One bladeRF-cli process runs all steps; each step's IQ goes through its own FIFO."""
    centers = np.minimum(np.arange(args.start * 1e6 + HALF_WIN_HZ, args.stop * 1e6 + HALF_WIN_HZ, FAST_STEP_HZ), 6000e6)
    n = int(np.ceil(FS * args.fast_dwell / FAST_BUF)) * FAST_BUF
    print(f"{len(centers)} steps, {FAST_STEP_HZ/1e6:.0f} MHz apart, {n/FS*1000:.0f} ms each, gain {args.gain} dB (fast)", flush=True)
    axis = (np.arange(FFT_N) - FFT_N / 2) * FS / FFT_N
    keep = (np.abs(axis) > DC_MASK_HZ) & (np.abs(axis) <= HALF_WIN_HZ)
    win = np.hanning(FFT_N).astype(np.float32)
    wnorm = float((win ** 2).sum())
    seg_bytes = FFT_N * 4
    result = {"mean": [None] * len(centers), "peak": [None] * len(centers), "done": 0, "err": None}
    tmp = tempfile.mkdtemp(prefix="bladerf_fast_")
    fifos = []
    try:
        lines = ["set biastee rx1 off", "set biastee rx2 off", f"set samplerate rx {int(FS)}",
                 f"set bandwidth rx {int(FS)}", "set agc rx off", f"set gain rx {args.gain}"]
        for i, fc in enumerate(centers):
            path = os.path.join(tmp, f"iq_{i:04d}.fifo")
            os.mkfifo(path)
            fifos.append(path)
            lines += [f"set frequency rx {int(fc)}",
                      f"rx config file={path} format=bin n={n} samples={FAST_BUF} buffers=16 xfers=8 timeout=10000",
                      "rx start", "rx wait"]
        lines.append("quit")
        script = os.path.join(tmp, "sweep.script")
        with open(script, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        log = open(os.path.join(tmp, "cli.log"), "w")
        proc = subprocess.Popen([args.cli, "-s", script], stdout=log, stderr=subprocess.STDOUT)
        t0 = time.time()

        def reader():
            try:
                for i, path in enumerate(fifos):
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        seen, skipped, carry = False, 0, b""
                        acc = np.zeros(FFT_N, np.float64)
                        pk = np.zeros(FFT_N, np.float64)
                        nseg = 0
                        t_wait = time.time()
                        while True:
                            try:
                                chunk = os.read(fd, CHUNK_BYTES)
                            except BlockingIOError:
                                chunk = None
                            if chunk is None:
                                time.sleep(0.001)
                            elif chunk == b"":
                                if seen:
                                    break  # writer closed: step complete
                                if proc.poll() is not None or time.time() - t_wait > 30:
                                    raise RuntimeError(f"no data at step {i}")
                                time.sleep(0.002)
                            else:
                                seen = True
                                if skipped < FAST_SKIP * 4:  # drop retune transient
                                    cut = min(len(chunk), FAST_SKIP * 4 - skipped)
                                    skipped += cut
                                    chunk = chunk[cut:]
                                data = carry + chunk
                                m = len(data) // seg_bytes * seg_bytes
                                carry = data[m:]
                                if m:
                                    raw = np.frombuffer(data[:m], dtype=np.int16).astype(np.float32) / 2048.0
                                    iq = (raw[0::2] + 1j * raw[1::2]).reshape(-1, FFT_N)
                                    sp = np.abs(np.fft.fft(iq * win, axis=1)) ** 2
                                    acc += sp.sum(axis=0)
                                    pk = np.maximum(pk, sp.max(axis=0))
                                    nseg += sp.shape[0]
                        if nseg == 0:
                            raise RuntimeError(f"empty step {i}")
                        result["mean"][i] = 10 * np.log10(np.fft.fftshift(acc / nseg) / wnorm + 1e-20)
                        result["peak"][i] = 10 * np.log10(np.fft.fftshift(pk) / wnorm + 1e-20)
                        result["done"] = i + 1
                        print(f"[{i+1}/{len(centers)}] {centers[i]/1e6:8.1f} MHz  ({time.time()-t0:.0f}s)", flush=True)
                    finally:
                        os.close(fd)
            except Exception as e:  # noqa: BLE001
                result["err"] = str(e)

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        while th.is_alive():
            th.join(0.5)
            if proc.poll() is not None and proc.returncode != 0 and th.is_alive():
                result["err"] = result["err"] or "bladeRF-cli exited early"
                break
            if time.time() - t0 > 900:
                result["err"] = "fast sweep timed out"
                break
        if proc.poll() is None:
            if result["err"]:
                proc.kill()
            else:
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        log.close()
        if result["err"] or result["done"] != len(centers):
            tail = open(os.path.join(tmp, "cli.log")).read().strip().splitlines()[-4:]
            raise RuntimeError(f"fast sweep failed at step {result['done']}/{len(centers)}: {result['err']} | " + " | ".join(tail))
        print(f"capture done in {time.time()-t0:.0f}s", flush=True)
    finally:
        for p in fifos:
            try:
                os.remove(p)
            except OSError:
                pass
        for fn in ("sweep.script", "cli.log"):
            try:
                os.remove(os.path.join(tmp, fn))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    freqs = [(fc + axis)[keep] for fc in centers]
    return freqs, [m[keep] for m in result["mean"]], [m[keep] for m in result["peak"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=70.0, help="start MHz")
    ap.add_argument("--stop", type=float, default=6000.0, help="stop MHz")
    ap.add_argument("--gain", type=int, default=20, help="RX gain dB")
    ap.add_argument("--dwell", type=float, default=0.05, help="capture seconds per step (full mode)")
    ap.add_argument("--fast-dwell", type=float, default=0.15, help="capture seconds per step (fast mode)")
    ap.add_argument("--out", default="sweep_out", help="output prefix")
    ap.add_argument("--cli", default="bladeRF-cli")
    ap.add_argument("--fast", action="store_true", help="quick sweep, about 3 minutes for 70-6000 MHz")
    args = ap.parse_args()

    freqs, levels, peaks = fast_collect(args) if args.fast else slow_collect(args)

    f = np.concatenate(freqs)
    p = np.concatenate(levels)
    # merge overlapping steps: average dB per frequency bin (peak: max)
    res = FS / FFT_N
    idx = np.round(f / res).astype(np.int64)
    uniq, inv = np.unique(idx, return_inverse=True)
    p = np.bincount(inv, weights=p) / np.bincount(inv)
    pk = None
    if peaks is not None:
        pk = np.full(len(uniq), -200.0)
        np.maximum.at(pk, inv, np.concatenate(peaks))
    f = uniq * res / 1e6

    with open(args.out + ".csv", "w", newline="") as fh:
        w = csv.writer(fh)
        if pk is None:
            w.writerow(["freq_mhz", "power_dbfs"])
            w.writerows(zip(f.tolist(), p.tolist()))
        else:
            w.writerow(["freq_mhz", "power_dbfs", "peak_dbfs"])
            w.writerows(zip(f.tolist(), p.tolist(), pk.tolist()))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(16, 6))
    if pk is not None:
        ax.plot(f, pk, lw=0.4, color="#ffb454", alpha=0.7, label="peak hold")
    ax.plot(f, p, lw=0.5, label="mean")
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Relative power (dBFS)")
    ax.set_title(f"bladeRF xA4 {'fast ' if args.fast else ''}sweep {args.start:.0f}-{args.stop:.0f} MHz, gain {args.gain} dB")
    ax.grid(True, alpha=0.3)
    if pk is not None:
        ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(args.out + ".png", dpi=130)
    print(f"saved {args.out}.png and {args.out}.csv")


if __name__ == "__main__":
    sys.exit(main())
