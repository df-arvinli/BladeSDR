#!/usr/bin/env python3
"""Live receiver: streams raw IQ (interleaved int16, 2 bytes I + 2 bytes Q per sample) to stdout.

Replaces the bladeRF-cli + FIFO pipeline, so it works wherever libbladeRF does (macOS, Linux, Windows).
Stops when its stdin is closed (--stdin-stop) or on SIGINT/SIGTERM. Errors go to stderr as one line.
Receive only; the bias tee is forced off.
"""
import argparse
import json
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from bladerf_io import Radio, watch_stdin  # noqa: E402

CHUNK = 32768   # samples per read; also the USB buffer size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True, help="JSON: rate, freq, gain")
    ap.add_argument("--stdin-stop", action="store_true", help="exit when stdin is closed")
    args = ap.parse_args()
    cfg = json.loads(args.cfg)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.stdin_stop:
        watch_stdin(stop)
    out = sys.stdout.buffer
    radio = None
    try:
        radio = Radio(cfg["rate"], cfg["gain"], cfg["freq"], CHUNK, buf_samples=CHUNK, num_buffers=32, num_xfers=16,
                      stream_timeout_ms=5000)
        while not stop.is_set():
            radio.rx(CHUNK)
            out.write(radio.buf[: CHUNK * 2].tobytes())
            out.flush()
    except (BrokenPipeError, OSError):
        pass   # the server went away
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(str(e) + "\n")
        sys.stderr.flush()
        sys.exit(2)
    finally:
        if radio is not None:
            radio.close()


if __name__ == "__main__":
    main()
