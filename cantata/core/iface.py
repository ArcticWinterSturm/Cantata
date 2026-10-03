"""Interface byte counters with NO process spawn.

Why this module exists
----------------------
The original byte counter shelled out to ``netstat -e`` once a second. Cantata
runs under ``pythonw.exe`` (no console — see ``launch.bat``), and **a
console-less Windows process that spawns a console application forces the OS to
allocate a console for it**. That allocates a ``conhost.exe``, flashes a window,
and tears both down within milliseconds.

Measured on this machine, 4 s of ``subprocess.run(["netstat", "-e"])`` at 1 Hz
from ``pythonw.exe``: 4 new ``netstat.exe`` PIDs and **35 new ``conhost.exe``
PIDs**. Static process counts never show it because the conhosts live for
milliseconds — which is exactly why this reads as "something keeps starting and
killing" with nothing visible in Task Manager.

``iphlpapi.GetIfTable`` is a plain syscall into the network stack: microseconds,
no process, no stdout to parse, nothing for real-time AV to hook. It also sums
every non-loopback interface, where the old ``netstat`` parser could only ever
read the first row.

This mirrors the fix that landed in DeKodX2's live tracker.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Optional

# --------------------------------------------------------------------------- #
# Windows: read the interface table straight from the network stack
# --------------------------------------------------------------------------- #

_IF_TYPE_SOFTWARE_LOOPBACK = 24
_ERROR_INSUFFICIENT_BUFFER = 122


class _MIB_IFROW(ctypes.Structure):
    """MIB_IFROW from iphlpapi.h. Field order and widths matter — do not reorder."""

    _fields_ = [
        ("wszName", wintypes.WCHAR * 256),
        ("dwIndex", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwMtu", wintypes.DWORD),
        ("dwSpeed", wintypes.DWORD),
        ("dwPhysAddrLen", wintypes.DWORD),
        ("bPhysAddr", wintypes.BYTE * 8),
        ("dwAdminStatus", wintypes.DWORD),
        ("dwOperStatus", wintypes.DWORD),
        ("dwLastChange", wintypes.DWORD),
        ("dwInOctets", wintypes.DWORD),
        ("dwOutOctets", wintypes.DWORD),
        ("dwInUcastPkts", wintypes.DWORD),
        ("dwInNUcastPkts", wintypes.DWORD),
        ("dwOutUcastPkts", wintypes.DWORD),
        ("dwOutNUcastPkts", wintypes.DWORD),
        ("dwInDiscards", wintypes.DWORD),
        ("dwOutDiscards", wintypes.DWORD),
        ("dwInErrors", wintypes.DWORD),
        ("dwOutErrors", wintypes.DWORD),
        ("dwInUnknownProtos", wintypes.DWORD),
        ("dwOutQLen", wintypes.DWORD),
        ("dwDescrLen", wintypes.DWORD),
        ("bDescr", wintypes.BYTE * 256),
    ]


def _win_iface_bytes() -> Optional[tuple[int, int]]:
    """(rx, tx) byte totals over all non-loopback interfaces, via GetIfTable."""
    iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    size = wintypes.DWORD(0)
    # First call sizes the buffer; it always returns ERROR_INSUFFICIENT_BUFFER.
    if iphlp.GetIfTable(None, ctypes.byref(size), False) != _ERROR_INSUFFICIENT_BUFFER:
        return None
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if iphlp.GetIfTable(buf, ctypes.byref(size), False) != 0:
        return None
    base = ctypes.addressof(buf)
    num_entries = wintypes.DWORD.from_address(base).value
    row_size = ctypes.sizeof(_MIB_IFROW)
    rx = tx = 0
    for i in range(num_entries):
        row = _MIB_IFROW.from_address(base + ctypes.sizeof(wintypes.DWORD) + i * row_size)
        if row.dwType == _IF_TYPE_SOFTWARE_LOOPBACK:
            continue
        rx += row.dwInOctets
        tx += row.dwOutOctets
    return (rx, tx) if (rx or tx) else None


# --------------------------------------------------------------------------- #
# Linux
# --------------------------------------------------------------------------- #

def _proc_iface_bytes() -> Optional[tuple[int, int]]:
    """Sum /proc/net/dev (rx, tx) over all non-loopback interfaces."""
    rx = tx = 0
    found = False
    with open("/proc/net/dev", "r", encoding="ascii") as fh:
        for line in fh:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() == "lo":
                continue
            f = rest.split()
            if len(f) >= 9:
                rx += int(f[0])
                tx += int(f[8])
                found = True
    return (rx, tx) if found else None


# --------------------------------------------------------------------------- #
# last-resort fallback (spawns a process - avoid)
# --------------------------------------------------------------------------- #

def _netstat_iface_bytes() -> Optional[tuple[int, int]]:
    """Parse ``netstat -e`` / ``netstat -ib`` data rows.

    A data row ends with six integers; header lines never do, so no line-position
    assumptions are needed. Spawns a process — only ever reached if the syscall
    path fails, because spawning is what causes the console flash on Windows.
    """
    import subprocess
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-e"], capture_output=True, text=True, timeout=5).stdout
            rx = tx = 0
            found = False
            for line in out.splitlines():
                if "loopback" in line.lower():
                    continue
                parts = line.split()
                if len(parts) < 8:
                    continue
                try:
                    vals = [int(x) for x in parts[-6:]]
                except ValueError:
                    continue                      # header / junk line
                rx += vals[0]
                tx += vals[1]
                found = True
            return (rx, tx) if found else None
        out = subprocess.run(["netstat", "-ib"], capture_output=True, text=True, timeout=5).stdout
        rx = tx = 0
        seen = set()
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 10 and p[0] not in seen and not p[0].startswith("lo") and p[2].startswith("<Link"):
                try:
                    rx += int(p[6])
                    tx += int(p[9])
                    seen.add(p[0])
                except ValueError:
                    continue
        return (rx, tx) if seen else None
    except Exception:
        return None


def iface_bytes() -> Optional[tuple[int, int]]:
    """Total (rx, tx) non-loopback interface bytes — cheapest reliable source first.

    On Windows this is a syscall, never a subprocess. See the module docstring.
    """
    if sys.platform == "win32":
        try:
            result = _win_iface_bytes()
            if result is not None:
                return result
        except Exception:
            pass
    if os.path.exists("/proc/net/dev"):
        try:
            return _proc_iface_bytes()
        except (OSError, ValueError):
            pass
    try:
        return _netstat_iface_bytes()
    except Exception:
        return None
