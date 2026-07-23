"""Minimal ADIF parser for Logbook table display."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

_TAG_RE = re.compile(r"<([A-Za-z_]+):([0-9]+)(?::[^>]*)?>", re.IGNORECASE)
_EOR_RE = re.compile(r"<EOR>", re.IGNORECASE)

_FIELDS = (
    "CALL",
    "QSO_DATE",
    "TIME_ON",
    "MODE",
    "SUBMODE",
    "BAND",
    "FREQ",
    "RST_SENT",
    "RST_RCVD",
    "GRIDSQUARE",
    "MY_GRIDSQUARE",
    "STATION_CALLSIGN",
)


def _parse_record(chunk: str) -> dict[str, str]:
    record = {k: "" for k in _FIELDS}
    for m in _TAG_RE.finditer(chunk):
        tag = m.group(1).upper()
        if tag not in record:
            continue
        length = int(m.group(2))
        start = m.end()
        record[tag] = chunk[start : start + length]
    return record


def parse_adif_text(text: str) -> list[dict[str, str]]:
    """Parse ADIF text into a list of field dicts (file order)."""
    # Drop header before first <EOH> if present.
    eoh = re.search(r"<EOH>", text, re.IGNORECASE)
    body = text[eoh.end() :] if eoh else text

    records: list[dict[str, str]] = []
    pos = 0
    for m in _EOR_RE.finditer(body):
        chunk = body[pos : m.start()]
        pos = m.end()
        rec = _parse_record(chunk)
        if any(rec[k] for k in _FIELDS):
            records.append(rec)
    return records


def parse_adif_file(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    return parse_adif_text(text)


def records_newest_first(records: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return list(reversed(list(records)))
