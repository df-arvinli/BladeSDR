#!/usr/bin/env python3
"""Wideband spectrum sweep for bladeRF 2.0 using bladeRF-cli.

Steps the RX center frequency, captures an IQ burst at each step, computes an
averaged power spectrum, and stitches the steps into one plot.
Levels are relative (dBFS), not calibrated dBm.

Two modes:
  default  one bladeRF-cli process per step (slow, ~15 min for 70-6000 MHz)
  --fast   one libbladeRF session for the whole sweep (direct ctypes, no FIFOs, any OS);
           IQ is reduced on the fly (no disk), with 50 % overlapping steps.
           Also records a peak-hold (max over time) column.
"""
import argparse
import csv
import ctypes
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from bladerf_io import Radio  # noqa: E402

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


def fast_collect(args):
    """Quick sweep: one libbladeRF session, retuned from step to step (no bladeRF-cli, no FIFOs)."""
    centers = np.minimum(np.arange(args.start * 1e6 + HALF_WIN_HZ, args.stop * 1e6 + HALF_WIN_HZ, FAST_STEP_HZ), 6000e6)
    n = int(np.ceil(FS * args.fast_dwell / FAST_BUF)) * FAST_BUF
    print(f"{len(centers)} steps, {FAST_STEP_HZ/1e6:.0f} MHz apart, {n/FS*1000:.0f} ms each, gain {args.gain} dB (fast)", flush=True)
    axis = (np.arange(FFT_N) - FFT_N / 2) * FS / FFT_N
    keep = (np.abs(axis) > DC_MASK_HZ) & (np.abs(axis) <= HALF_WIN_HZ)
    win = np.hanning(FFT_N).astype(np.float32)
    wnorm = float((win ** 2).sum())
    means, peaks = [], []
    t0 = time.time()
    radio = Radio(FS, args.gain, centers[0], n, bandwidth=FS, num_buffers=16, buf_samples=FAST_BUF, num_xfers=8,
                  stream_timeout_ms=10000, rx_timeout_ms=10000)
    try:
        # samples still in flight from the previous frequency, plus the retune transient
        drain = radio.flush_samples() + FAST_SKIP
        radio.buf = np.zeros(max(n, drain) * 2, dtype=np.int16)
        radio.ptr = radio.buf.ctypes.data_as(ctypes.c_void_p)
        for i, fc in enumerate(centers):
            radio.retune(fc)
            radio.rx(drain)
            radio.rx(n)
            acc = np.zeros(FFT_N, np.float64)
            pk = np.zeros(FFT_N, np.float64)
            nseg = 0
            x = radio.buf[: n * 2].astype(np.float32) / 2048.0
            iq = (x[0::2] + 1j * x[1::2])[: n // FFT_N * FFT_N].reshape(-1, FFT_N)
            for off in range(0, iq.shape[0], 512):   # slices keep the FFT working set small
                sp = np.abs(np.fft.fft(iq[off:off + 512] * win, axis=1)) ** 2
                acc += sp.sum(axis=0)
                pk = np.maximum(pk, sp.max(axis=0))
                nseg += sp.shape[0]
            means.append(10 * np.log10(np.fft.fftshift(acc / nseg) / wnorm + 1e-20))
            peaks.append(10 * np.log10(np.fft.fftshift(pk) / wnorm + 1e-20))
            print(f"[{i+1}/{len(centers)}] {fc/1e6:8.1f} MHz  ({time.time()-t0:.0f}s)", flush=True)
    finally:
        radio.close()
    print(f"capture done in {time.time()-t0:.0f}s", flush=True)
    freqs = [(fc + axis)[keep] for fc in centers]
    return freqs, [m[keep] for m in means], [m[keep] for m in peaks]


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
