"""Direct libbladeRF access through ctypes, shared by the live monitor, the zone hopper and the fast sweep.

Works on macOS, Linux and Windows: the library is found per platform (override with the BLADERF_LIB
environment variable), and nothing here depends on POSIX-only features such as FIFOs or signals.
Receive only: no TX call is ever made, and the bias tee is forced off.
"""
import ctypes as C
import ctypes.util
import os
import sys

import numpy as np

RX0 = 0                  # BLADERF_CHANNEL_RX(0)
TUNING_HOST = 0          # BLADERF_TUNING_MODE_HOST
TUNING_FPGA = 1          # BLADERF_TUNING_MODE_FPGA
GAIN_MGC = 1             # BLADERF_GAIN_MGC
FMT_SC16_Q11 = 0
LAYOUT_RX_X1 = 0


def _candidates():
    env = os.environ.get("BLADERF_LIB")
    if env:
        yield env
    if sys.platform == "darwin":
        yield "/opt/homebrew/lib/libbladeRF.2.dylib"
        yield "/usr/local/lib/libbladeRF.2.dylib"
    elif sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            for sub in (r"bladeRF\x64", r"bladeRF\bin", r"Nuand\bladeRF\x64"):
                yield os.path.join(base, sub, "bladeRF.dll")
    else:
        yield "libbladeRF.so.2"
        yield "libbladeRF.so"
    yield ctypes.util.find_library("bladeRF")


def load_lib():
    tried = []
    for name in _candidates():
        if not name:
            continue
        tried.append(name)
        try:
            if sys.platform.startswith("win") and os.path.isabs(name) and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(os.path.dirname(name))   # libusb etc. sit next to bladeRF.dll
            return C.CDLL(name)
        except OSError:
            continue
    raise RuntimeError("libbladeRF not found (set BLADERF_LIB to its full path); tried: " + ", ".join(tried))


class Radio:
    """Open the first bladeRF, configure RX, stream samples. Optionally retune while streaming (FPGA tuning)."""

    def __init__(self, rate, gain, first_freq, max_samples, fpga_tuning=False, bandwidth=None,
                 num_buffers=8, buf_samples=8192, num_xfers=4, stream_timeout_ms=3000, rx_timeout_ms=1500):
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
        self.num_buffers, self.buf_samples = num_buffers, buf_samples
        self.rx_timeout_ms = rx_timeout_ms
        self.dev = vp()
        self.enabled = False
        self.chk(lib.bladerf_open(C.byref(self.dev), None), "open")
        try:
            act = C.c_uint32()
            # Order matters: switching tuning mode resets the gain, so set the mode first.
            self.chk(lib.bladerf_set_tuning_mode(self.dev, TUNING_FPGA if fpga_tuning else TUNING_HOST), "tuning mode")
            self.chk(lib.bladerf_set_sample_rate(self.dev, RX0, int(rate), C.byref(act)), "sample rate")
            self.rate = int(act.value)
            bw = bandwidth if bandwidth is not None else min(56_000_000, max(200_000, int(self.rate * 1.4)))
            self.chk(lib.bladerf_set_bandwidth(self.dev, RX0, int(bw), C.byref(act)), "bandwidth")
            self.bandwidth = int(act.value)
            self.chk(lib.bladerf_set_gain_mode(self.dev, RX0, GAIN_MGC), "gain mode")
            self.chk(lib.bladerf_set_gain(self.dev, RX0, int(gain)), "gain")
            self.chk(lib.bladerf_set_bias_tee(self.dev, RX0, False), "bias tee off")
            self.chk(lib.bladerf_set_frequency(self.dev, RX0, int(first_freq)), "frequency")
            self.chk(lib.bladerf_sync_config(self.dev, LAYOUT_RX_X1, FMT_SC16_Q11, num_buffers, buf_samples, num_xfers,
                                             stream_timeout_ms), "sync config")
            self.chk(lib.bladerf_enable_module(self.dev, RX0, True), "enable rx")
            self.enabled = True
            self.buf = np.zeros(max_samples * 2, dtype=np.int16)
            self.ptr = self.buf.ctypes.data_as(C.c_void_p)
        except Exception:
            self.close()
            raise

    def chk(self, r, what):
        if r != 0:
            raise RuntimeError(f"{what} failed: {self.lib.bladerf_strerror(r).decode()} ({r})")

    def retune(self, freq):
        self.chk(self.lib.bladerf_set_frequency(self.dev, RX0, int(freq)), "retune")

    def rx(self, n):
        """Read n complex samples into self.buf (interleaved int16 I/Q). Short timeout so a stalled stream
        cannot outlast a stop request."""
        self.chk(self.lib.bladerf_sync_rx(self.dev, self.ptr, n, None, self.rx_timeout_ms), "rx")

    def flush_samples(self):
        """Samples that may still be in flight from before a retune."""
        return self.num_buffers * self.buf_samples

    def close(self):
        try:
            if self.enabled:
                self.lib.bladerf_enable_module(self.dev, RX0, False)
                self.enabled = False
            if self.dev:
                self.lib.bladerf_close(self.dev)
                self.dev = C.c_void_p()
        except Exception:  # noqa: BLE001
            pass


def watch_stdin(stop_event):
    """Set stop_event when the parent closes our stdin. A cross-platform way to ask a child to exit
    cleanly (signals cannot do that on Windows)."""
    import threading

    def run():
        try:
            while sys.stdin.buffer.read(1024):
                pass
        except Exception:  # noqa: BLE001
            pass
        stop_event.set()

    threading.Thread(target=run, daemon=True).start()
