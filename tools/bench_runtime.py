#!/usr/bin/env python3
"""Cross-platform process memory sampling for AFC experiments.

The sampler reports the operating system's current resident working set, so it
includes Python and native C++ allocations. It deliberately does not use
``tracemalloc`` (Python heap only) or a parent process high-water mark.
"""

import os
import sys
import threading
import time


def _windows_rss_kb():
    import ctypes
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", size_t),
            ("WorkingSetSize", size_t),
            ("QuotaPeakPagedPoolUsage", size_t),
            ("QuotaPagedPoolUsage", size_t),
            ("QuotaPeakNonPagedPoolUsage", size_t),
            ("QuotaNonPagedPoolUsage", size_t),
            ("PagefileUsage", size_t),
            ("PeakPagefileUsage", size_t),
            ("PrivateUsage", size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCountersEx), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    handle = kernel32.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(
        handle, ctypes.byref(counters), counters.cb)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.WorkingSetSize // 1024


def current_rss_kb():
    """Return current resident memory in KiB for this process."""
    if os.name == "nt":
        return int(_windows_rss_kb())
    if os.path.exists("/proc/self/statm"):
        page = os.sysconf("SC_PAGE_SIZE")
        with open("/proc/self/statm", "rb") as stream:
            resident_pages = int(stream.read().split()[1])
        return resident_pages * page // 1024

    # macOS/BSD fallback. ru_maxrss is a high-water mark rather than current
    # RSS, but it is still the OS process total and includes native memory.
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value // 1024 if sys.platform == "darwin" else value)


def measure_peak(call, interval=0.001):
    """Run ``call`` and return ``(result, baseline_kib, peak_kib)``."""
    baseline = current_rss_kb()
    peak = [baseline]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            value = current_rss_kb()
            if value > peak[0]:
                peak[0] = value
            stop.wait(interval)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        result = call()
        value = current_rss_kb()
        if value > peak[0]:
            peak[0] = value
        return result, baseline, peak[0]
    finally:
        stop.set()
        thread.join(timeout=1.0)
