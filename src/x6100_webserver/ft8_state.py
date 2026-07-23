"""FT8 remote structured-state shm reader + command whitelist.

Binary layout mirrors x6100_gui ft8_remote.h (FT8_REMOTE_VERSION=3).
"""

from __future__ import annotations

import mmap
import os
import re
import struct
import time
from typing import Any, Optional

from . import settings

FT8_STATE_PATH = getattr(settings, "FT8_STATE_PATH", "/dev/shm/x6100_ft8_state")
FT8_REMOTE_MAGIC = 0x46543852  # 'FT8R'
FT8_REMOTE_VERSION = 3
FT8_REMOTE_MAX_ROWS = 512

# ft8_remote_row_t — 88 bytes
_ROW_FMT = "<II BBBBB 3x hhhh 40s 16s 8s"
_ROW_SIZE = struct.calcsize(_ROW_FMT)

# Header before rows[] — 224 bytes (incl. 1-byte implicit pad after tx_active)
_STATE_HDR_FMT = (
    "<I H H I"  # magic, version, _pad0, seq
    "11B"  # active..tx_active
    "x"  # implicit padding before int16
    "hhhh"  # tx_delta_hz, filter_low, filter_high, _pad1
    "24s 16s 16s 32s 16s 8s 64s"  # strings
    "BBHHhI"  # autodnf_valid..autodnf_time_utc
    "H H"  # row_count, row_capacity
)
_STATE_HDR_SIZE = struct.calcsize(_STATE_HDR_FMT)
_ROWS_OFFSET = _STATE_HDR_SIZE
_STATE_SIZE = _ROWS_OFFSET + FT8_REMOTE_MAX_ROWS * _ROW_SIZE
_SEQ_OFFSET = 8

assert _ROW_SIZE == 88 and _STATE_HDR_SIZE == 224, "ft8_remote.h layout drift"

_PROTOCOL = {0: "FT4", 1: "FT8"}
_CQ_STATE = {0: "OFF", 1: "EVEN", 2: "ODD"}
_AUTO_LEVEL = {0: "OFF", 1: "RES", 2: "FULL", 3: "PRE"}
_AUTO_MODE = {0: "SNR", 1: "DIST", 2: "RND", 3: "GRID"}
_PROCESSOR = {0: "NORMAL", 1: "NAVHF"}
_ROW_TYPE = {
    2: "START_QSO",
    3: "RX_MSG",
    4: "RX_CQ",
    5: "RX_TO_ME",
    6: "TX_MSG",
}
_RX_KINDS = {"RX_MSG", "RX_CQ", "RX_TO_ME"}
_TX_KINDS = {"TX_MSG"}
_SKIP_TYPES = {"START_QSO"}

_ENUM_ARGS = {
    "TXCQ": frozenset({"OFF", "EVEN", "ODD"}),
    "AUTO": frozenset({"OFF", "RES", "FULL", "PRE"}),
    "AUTOMODE": frozenset({"SNR", "DIST", "RND", "GRID"}),
    "PROCESSOR": frozenset({"NORMAL", "NAVHF"}),
    "SHOW": frozenset({"CQ", "ALL"}),
    "MODE": frozenset({"FT8", "FT4"}),
    "HOLDFREQ": frozenset({"ON", "OFF"}),
    "TXCALL": frozenset({"ON", "OFF"}),
    "AUTODNF": frozenset({"ON", "OFF"}),
    "BAND": frozenset({"UP", "DOWN"}),
}
_NO_ARG = frozenset({"TIMESYNC", "FORCESAVE"})
_INT_ARGS = frozenset({"TXDELTA", "CLICK"})
_TEXT_ARGS = {
    "CQMOD": 15,
    "FREEMSG": 13,
}

_mm: Optional[mmap.mmap] = None
_mm_fd: Optional[int] = None
_mm_path: Optional[str] = None
_SEQLOCK_RETRIES = 16


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def _close_mm() -> None:
    global _mm, _mm_fd, _mm_path
    if _mm is not None:
        try:
            _mm.close()
        except Exception:
            pass
        _mm = None
    if _mm_fd is not None:
        try:
            os.close(_mm_fd)
        except Exception:
            pass
        _mm_fd = None
    _mm_path = None


def _open_mm(path: Optional[str] = None) -> Optional[mmap.mmap]:
    global _mm, _mm_fd, _mm_path
    p = path or FT8_STATE_PATH
    if _mm is not None and _mm_path == p:
        return _mm
    if _mm is not None:
        _close_mm()
    try:
        fd = os.open(p, os.O_RDONLY)
        mm = mmap.mmap(fd, _STATE_SIZE, mmap.MAP_SHARED, mmap.PROT_READ)
    except Exception:
        _close_mm()
        return None
    _mm_fd = fd
    _mm = mm
    _mm_path = p
    return _mm


def _read_seq(mm: mmap.mmap) -> int:
    return struct.unpack_from("<I", mm, _SEQ_OFFSET)[0]


def _read_consistent(path: Optional[str] = None) -> Optional[bytes]:
    """Seqlock consistent snapshot of the whole state blob."""
    for _ in range(_SEQLOCK_RETRIES):
        mm = _open_mm(path)
        if mm is None:
            return None
        try:
            seq1 = _read_seq(mm)
            if seq1 & 1:
                continue
            buf = bytes(mm[:_STATE_SIZE])
            seq2 = _read_seq(mm)
            if seq1 == seq2 and (seq2 & 1) == 0:
                return buf
        except Exception:
            _close_mm()
            continue
    return None


def _row_kind(type_name: str) -> str:
    if type_name in _RX_KINDS:
        return "rx"
    if type_name in _TX_KINDS:
        return "tx"
    return "other"


def _parse_row(raw: bytes) -> Optional[dict[str, Any]]:
    (
        rid,
        time_utc,
        rtype,
        odd,
        call_worked,
        grid_worked,
        call_grid_worked,
        snr_db,
        delta_hz,
        dist_km,
        _pad1,
        text,
        call,
        grid,
    ) = struct.unpack(_ROW_FMT, raw)
    type_name = _ROW_TYPE.get(rtype)
    if type_name is None or type_name in _SKIP_TYPES:
        return None
    return {
        "id": rid,
        "time": time_utc,
        "kind": _row_kind(type_name),
        "type": type_name,
        "odd": bool(odd),
        "call": _cstr(call),
        "grid": _cstr(grid),
        "delta_hz": delta_hz,
        "snr_db": snr_db,
        "dist_km": dist_km,
        "call_worked": bool(call_worked),
        "grid_worked": bool(grid_worked),
        "call_grid_worked": bool(call_grid_worked),
        "text": _cstr(text),
    }


def _slot_info(protocol: str, server_time_ms: int) -> dict[str, int]:
    period_ms = 7500 if protocol == "FT4" else 15000
    elapsed_ms = server_time_ms % period_ms
    return {
        "period_ms": period_ms,
        "elapsed_ms": elapsed_ms,
        "next_in_ms": period_ms - elapsed_ms,
    }


def to_dict(path: Optional[str] = None) -> dict[str, Any]:
    """Read shm and map to JSON-friendly dict. Missing/invalid → active:false."""
    inactive = {"active": False}
    buf = _read_consistent(path)
    if buf is None:
        return inactive

    try:
        hdr = struct.unpack_from(_STATE_HDR_FMT, buf, 0)
    except struct.error:
        return inactive

    (
        magic,
        version,
        _pad0,
        _seq,
        active,
        protocol,
        cq_state,
        auto_level,
        auto_mode,
        processor,
        show_all,
        hold_freq,
        tx_call,
        auto_dnf,
        tx_active,
        tx_delta_hz,
        filter_low,
        filter_high,
        _pad1,
        band_label,
        cq_modifier,
        free_msg,
        next_tx,
        de_call,
        de_grid,
        status,
        autodnf_valid,
        autodnf_applied,
        autodnf_center_hz,
        autodnf_half_width_hz,
        autodnf_delta_db,
        autodnf_time_utc,
        row_count,
        row_capacity,
    ) = hdr

    if magic != FT8_REMOTE_MAGIC or version != FT8_REMOTE_VERSION:
        return inactive
    if not active:
        return inactive

    proto_name = _PROTOCOL.get(protocol, "FT8")
    n = min(int(row_count), FT8_REMOTE_MAX_ROWS, int(row_capacity) or FT8_REMOTE_MAX_ROWS)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        off = _ROWS_OFFSET + i * _ROW_SIZE
        row = _parse_row(buf[off : off + _ROW_SIZE])
        if row is not None:
            rows.append(row)

    autodnf = None
    if autodnf_valid:
        autodnf = {
            "time": int(autodnf_time_utc),
            "center_hz": int(autodnf_center_hz),
            "half_width_hz": int(autodnf_half_width_hz),
            "delta_db": int(autodnf_delta_db),
            "applied": bool(autodnf_applied),
        }

    server_time_ms = int(time.time() * 1000)
    return {
        "active": True,
        "protocol": proto_name,
        "cq_state": _CQ_STATE.get(cq_state, "OFF"),
        "auto_level": _AUTO_LEVEL.get(auto_level, "OFF"),
        "auto_mode": _AUTO_MODE.get(auto_mode, "SNR"),
        "processor": _PROCESSOR.get(processor, "NORMAL"),
        "show_all": bool(show_all),
        "hold_freq": bool(hold_freq),
        "tx_call": bool(tx_call),
        "auto_dnf": bool(auto_dnf),
        "tx_active": bool(tx_active),
        "tx_delta_hz": int(tx_delta_hz),
        "filter": {"low": int(filter_low), "high": int(filter_high)},
        "band_label": _cstr(band_label),
        "cq_modifier": _cstr(cq_modifier),
        "free_msg": _cstr(free_msg),
        "next_tx": _cstr(next_tx),
        "de_call": _cstr(de_call),
        "de_grid": _cstr(de_grid),
        "status": _cstr(status),
        "autodnf": autodnf,
        "server_time_ms": server_time_ms,
        "slot": _slot_info(proto_name, server_time_ms),
        "row_count": len(rows),
        "rows": rows,
    }


def _sanitize_text(text: str, max_len: int) -> str:
    # ASCII printable only; strip CR/LF and collapse runs of space
    cleaned = "".join(c for c in text if 32 <= ord(c) < 127)
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return cleaned[:max_len]


def build_command_line(verb: Any, arg: Any = None) -> tuple[Optional[str], Optional[str]]:
    """Validate and build FIFO line ``FT8 {verb} [{arg}]``.

    Returns (line, None) on success, or (None, error_message) on failure.
    """
    if verb is None:
        return None, "verb is required"
    v = str(verb).strip().upper()
    if not v:
        return None, "verb is required"

    if v in _NO_ARG:
        return f"FT8 {v}", None

    if v in _ENUM_ARGS:
        if arg is None or str(arg).strip() == "":
            return None, f"{v} requires arg"
        a = str(arg).strip().upper()
        if a not in _ENUM_ARGS[v]:
            return None, f"invalid arg for {v}"
        return f"FT8 {v} {a}", None

    if v in _INT_ARGS:
        if arg is None or str(arg).strip() == "":
            return None, f"{v} requires arg"
        try:
            n = int(str(arg).strip())
        except (TypeError, ValueError):
            return None, f"invalid integer arg for {v}"
        if v == "TXDELTA":
            n = max(0, min(5000, n))
        elif v == "CLICK":
            if n < 0:
                return None, "CLICK id must be >= 0"
        return f"FT8 {v} {n}", None

    if v in _TEXT_ARGS:
        max_len = _TEXT_ARGS[v]
        text = _sanitize_text("" if arg is None else str(arg), max_len)
        if text:
            return f"FT8 {v} {text}", None
        return f"FT8 {v}", None

    return None, f"unknown verb: {v}"
