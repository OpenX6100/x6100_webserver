#!/usr/bin/env python3
"""Dev-only: pack a mock ft8_remote_state_t into a file (same layout as GUI shm)."""

from __future__ import annotations

import argparse
import os
import struct
import time

FT8_REMOTE_MAGIC = 0x46543852
FT8_REMOTE_VERSION = 3
FT8_REMOTE_MAX_ROWS = 512

_ROW_FMT = "<II BBBBB 3x hhhh 40s 16s 8s"
_ROW_SIZE = struct.calcsize(_ROW_FMT)
_STATE_HDR_FMT = (
    "<I H H I"
    "11B"
    "x"
    "hhhh"
    "24s 16s 16s 32s 16s 8s 64s"
    "BBHHhI"
    "H H"
)
_STATE_HDR_SIZE = struct.calcsize(_STATE_HDR_FMT)
_STATE_SIZE = _STATE_HDR_SIZE + FT8_REMOTE_MAX_ROWS * _ROW_SIZE

assert _ROW_SIZE == 88 and _STATE_HDR_SIZE == 224


def _pad(s: str, n: int) -> bytes:
    b = s.encode("ascii", "replace")[: n - 1]
    return b + b"\x00" * (n - len(b))


def pack_row(
    rid: int,
    time_utc: int,
    rtype: int,
    *,
    odd: int = 0,
    call_worked: int = 0,
    grid_worked: int = 0,
    call_grid_worked: int = 0,
    snr_db: int = -10,
    delta_hz: int = 1200,
    dist_km: int = 1000,
    text: str = "",
    call: str = "",
    grid: str = "",
) -> bytes:
    return struct.pack(
        _ROW_FMT,
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
        0,
        _pad(text, 40),
        _pad(call, 16),
        _pad(grid, 8),
    )


def pack_state(
    *,
    active: int = 1,
    protocol: int = 1,
    cq_state: int = 1,
    auto_level: int = 1,
    auto_mode: int = 0,
    processor: int = 0,
    show_all: int = 0,
    hold_freq: int = 0,
    tx_call: int = 1,
    auto_dnf: int = 0,
    tx_active: int = 0,
    tx_delta_hz: int = 1200,
    filter_low: int = 100,
    filter_high: int = 2900,
    band_label: str = "20m",
    cq_modifier: str = "",
    free_msg: str = "",
    next_tx: str = "CQ BG7NZL OL72",
    de_call: str = "BG7NZL",
    de_grid: str = "OL72",
    status: str = "Next TX: CQ BG7NZL OL72",
    autodnf_valid: int = 0,
    autodnf_applied: int = 0,
    autodnf_center_hz: int = 0,
    autodnf_half_width_hz: int = 35,
    autodnf_delta_db: int = 0,
    autodnf_time_utc: int = 0,
    rows: list[bytes] | None = None,
    seq: int = 2,
) -> bytes:
    rows = rows or []
    row_count = min(len(rows), FT8_REMOTE_MAX_ROWS)
    hdr = struct.pack(
        _STATE_HDR_FMT,
        FT8_REMOTE_MAGIC,
        FT8_REMOTE_VERSION,
        0,
        seq,
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
        0,
        _pad(band_label, 24),
        _pad(cq_modifier, 16),
        _pad(free_msg, 16),
        _pad(next_tx, 32),
        _pad(de_call, 16),
        _pad(de_grid, 8),
        _pad(status, 64),
        autodnf_valid,
        autodnf_applied,
        autodnf_center_hz,
        autodnf_half_width_hz,
        autodnf_delta_db,
        autodnf_time_utc,
        row_count,
        FT8_REMOTE_MAX_ROWS,
    )
    body = b"".join(rows[:row_count])
    body += b"\x00" * (_STATE_SIZE - len(hdr) - len(body))
    return hdr + body


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-o",
        "--output",
        default="/dev/shm/x6100_ft8_state",
        help="output path (default: %(default)s)",
    )
    args = ap.parse_args()

    now = int(time.time())
    rows = [
        pack_row(
            40,
            now - 30,
            4,
            snr_db=-12,
            delta_hz=800,
            dist_km=5000,
            text="CQ K1ABC FN42",
            call="K1ABC",
            grid="FN42",
            grid_worked=1,
        ),
        pack_row(
            41,
            now - 15,
            5,
            snr_db=-5,
            delta_hz=1500,
            dist_km=3200,
            text="BG7NZL JA1XYZ PM95",
            call="JA1XYZ",
            grid="PM95",
            call_worked=1,
        ),
        pack_row(
            42,
            now,
            6,
            snr_db=0,
            delta_hz=1200,
            dist_km=0,
            text="CQ BG7NZL OL72",
            call="BG7NZL",
            grid="OL72",
        ),
    ]
    blob = pack_state(rows=rows)
    assert len(blob) == _STATE_SIZE

    out = args.output
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    # Truncate/create fixed-size file
    with open(out, "wb") as f:
        f.write(blob)
    print(f"wrote {len(blob)} bytes to {out}")


if __name__ == "__main__":
    main()
