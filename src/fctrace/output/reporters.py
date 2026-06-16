"""
reporters.py — JSON, CSV, and plain-text output for FC-Trace results
====================================================================
Three reporters:
  JSONReporter   — writes ForensicEvent list to .json
  CSVReporter    — writes ForensicEvent list to .csv
  TextReporter   — writes human-readable summary to stdout or file
"""

import csv
import io
import json
import logging
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Fields included in CSV / JSON output (ordered)
EVENT_FIELDS = [
    'seq', 'tid', 'event_type', 'ino', 'parent_ino',
    'name', 'new_name', 'new_parent',
    'physical_block', 'logical_block', 'extent_len',
    'confidence', 'commit_complete', 'fc_offsets', 'decode_errors',
]


class JSONReporter:
    """Write forensic events to a JSON file."""

    def __init__(self, output_path: str | Path) -> None:
        self._path = Path(output_path)

    def write(self, events: List[Dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, 'w', encoding='utf-8') as fh:
            json.dump(events, fh, indent=2, ensure_ascii=False)
        logger.info("JSON report written: %s (%d events)", self._path, len(events))


class CSVReporter:
    """Write forensic events to a CSV file."""

    def __init__(self, output_path: str | Path) -> None:
        self._path = Path(output_path)

    def write(self, events: List[Dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, 'w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=EVENT_FIELDS,
                extrasaction='ignore',
            )
            writer.writeheader()
            for ev in events:
                # Serialise list fields as strings for CSV
                row = {k: (json.dumps(v) if isinstance(v, list) else v)
                       for k, v in ev.items()}
                writer.writerow(row)
        logger.info("CSV report written: %s (%d events)", self._path, len(events))


class TextReporter:
    """Write a human-readable summary to stdout or a file."""

    def __init__(self, output_path: Optional[str | Path] = None) -> None:
        self._path = Path(output_path) if output_path else None

    def write(self, events: List[Dict[str, Any]], image_path: str = '') -> None:
        buf = io.StringIO()
        buf.write("=" * 70 + "\n")
        buf.write("FC-Trace  Forensic Timeline Report\n")
        buf.write(f"Image   : {image_path}\n")
        buf.write(f"Events  : {len(events)}\n")
        buf.write("=" * 70 + "\n\n")

        for ev in events:
            etype = ev.get('event_type', '?')
            ino   = ev.get('ino', 0)
            name  = ev.get('name', '')
            tid   = ev.get('tid', 0)
            conf  = ev.get('confidence', 0.0)
            cc    = 'COMPLETE' if ev.get('commit_complete') else 'PARTIAL'
            errs  = ev.get('decode_errors', [])

            line = (
                f"[TID={tid:>6}] {etype:<20} ino={ino:<8} "
                f"name={name!r:<30} conf={conf:.2f} tx={cc}"
            )
            if etype == 'RENAME':
                line += (
                    f" -> {ev.get('new_name', '')!r} "
                    f"(new_parent={ev.get('new_parent', 0)})"
                )
            if etype in ('EXTENT_ADD', 'EXTENT_DEL'):
                line += (
                    f"  phys_blk={ev.get('physical_block', 0)} "
                    f"len={ev.get('extent_len', 0)}"
                )
            if errs:
                line += f"  ERRORS={errs}"
            buf.write(line + "\n")

        buf.write("\n" + "=" * 70 + "\n")
        output = buf.getvalue()

        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, 'w', encoding='utf-8') as fh:
                fh.write(output)
            logger.info("Text report written: %s", self._path)
        else:
            print(output)
