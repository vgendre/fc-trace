"""
event_builder.py — Forensic event builder and timeline correlator
=================================================================
Converts a flat list of :class:`FCRecord` objects into a structured,
ordered list of :class:`ForensicEvent` objects suitable for presentation
to an investigator.

Key responsibilities
--------------------
1. Group records by transaction ID (HEAD … TAIL window).
2. Infer rename-like outcomes from LINK + UNLINK records with the same inode.
3. Emit one ForensicEvent per significant operation.
4. Assign a confidence score to each event.
5. Flag incomplete transactions (HEAD without matching TAIL) with
   CONFIDENCE_PARTIAL.

Confidence model
----------------
HIGH   (0.9) — complete transaction (HEAD + TAIL + CRC present);
               dentry name is non-empty; ino > 10.
MEDIUM (0.6) — transaction lacks TAIL (crash-interrupted) but records
               are internally consistent.
LOW    (0.3) — TLV value decode error, or heuristic FC scan fallback.
"""

import logging
from dataclasses import dataclass, field
from itertools import groupby
from typing import Dict, List, Optional, Tuple

from fctrace.parser.tlv_decoder import (
    FCRecord,
    FCDentry,
    FCExtent,
    FCDelRange,
    FCInodeUpdate,
    FCCommitHead,
    FCCommitTail,
    FCTag,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------
CONFIDENCE_HIGH    = 0.9
CONFIDENCE_MEDIUM  = 0.6
CONFIDENCE_LOW     = 0.3


# ---------------------------------------------------------------------------
# Forensic event dataclass
# ---------------------------------------------------------------------------

@dataclass
class ForensicEvent:
    """
    A single investigator-usable forensic event reconstructed from
    fast-commit records.

    Fields
    ------
    tid         : Transaction ID from JBD2/FC HEAD record
    seq         : Sequence number within the timeline (0-based)
    event_type  : One of CREATE / UNLINK / RENAME / LINK /
                  EXTENT_ADD / EXTENT_DEL / INODE_UPDATE / COMMIT_COMPLETE
    ino         : Primary inode number involved
    parent_ino  : Parent directory inode (dentry events only)
    name        : Directory entry name (dentry events only)
    new_name    : Destination name (inferred RENAME events only)
    new_parent  : Destination parent inode (inferred RENAME events only)
    physical_block : Physical start block (extent events only)
    logical_block  : Logical block number (extent events only)
    extent_len     : Extent length in blocks
    confidence  : Float in [0, 1] — see confidence model above
    commit_complete : True if the enclosing transaction had a valid TAIL
    fc_offsets  : Byte offsets of contributing FC records (for provenance)
    decode_errors : Any error strings from the TLV decoder
    """
    tid:             int   = 0
    seq:             int   = 0
    event_type:      str   = ''
    ino:             int   = 0
    parent_ino:      int   = 0
    name:            str   = ''
    new_name:        str   = ''
    new_parent:      int   = 0
    physical_block:  int   = 0
    logical_block:   int   = 0
    extent_len:      int   = 0
    confidence:      float = CONFIDENCE_MEDIUM
    commit_complete: bool  = False
    fc_offsets:      List[int] = field(default_factory=list)
    decode_errors:   List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'tid':             self.tid,
            'seq':             self.seq,
            'event_type':      self.event_type,
            'ino':             self.ino,
            'parent_ino':      self.parent_ino,
            'name':            self.name,
            'new_name':        self.new_name,
            'new_parent':      self.new_parent,
            'physical_block':  self.physical_block,
            'logical_block':   self.logical_block,
            'extent_len':      self.extent_len,
            'confidence':      round(self.confidence, 3),
            'commit_complete': self.commit_complete,
            'fc_offsets':      self.fc_offsets,
            'decode_errors':   self.decode_errors,
        }


# ---------------------------------------------------------------------------
# Transaction container
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    tid:     int
    head:    Optional[FCCommitHead] = None
    tail:    Optional[FCCommitTail] = None
    records: List[FCRecord] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.head is not None and self.tail is not None

    @property
    def base_confidence(self) -> float:
        if self.is_complete:
            return CONFIDENCE_HIGH
        if self.head is not None:
            return CONFIDENCE_MEDIUM
        return CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class EventBuilder:
    """
    Converts a list of FCRecord objects into a timeline of ForensicEvent
    objects, grouped and ordered by transaction.
    """

    def __init__(self, records: List[FCRecord]) -> None:
        self._records = records
        self._transactions: Dict[int, Transaction] = {}

    def build(self) -> List[ForensicEvent]:
        """
        Run the full build pipeline:
          1. Group records into transactions.
          2. Within each transaction emit forensic events.
          3. Return sorted list (by tid, then record offset).
        """
        self._group_transactions()
        events: List[ForensicEvent] = []
        seq = 0

        for tid in sorted(self._transactions):
            tx = self._transactions[tid]
            tx_events = self._emit_events(tx)
            for ev in tx_events:
                ev.seq = seq
                ev.commit_complete = tx.is_complete
                ev.confidence = self._compute_confidence(ev, tx)
                seq += 1
            events.extend(tx_events)

        logger.info(
            "EventBuilder: %d transactions → %d forensic events",
            len(self._transactions), len(events),
        )
        return events

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------

    def _group_transactions(self) -> None:
        """Partition records into Transaction objects by tid."""
        for rec in self._records:
            tid = rec.tid

            if tid not in self._transactions:
                self._transactions[tid] = Transaction(tid=tid)
            tx = self._transactions[tid]

            if rec.tag == FCTag.HEAD and isinstance(rec.payload, FCCommitHead):
                tx.head = rec.payload
            elif rec.tag == FCTag.TAIL and isinstance(rec.payload, FCCommitTail):
                tx.tail = rec.payload
            else:
                tx.records.append(rec)

        logger.debug(
            "Grouped %d records into %d transactions (%d complete)",
            len(self._records),
            len(self._transactions),
            sum(1 for t in self._transactions.values() if t.is_complete),
        )

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_events(self, tx: Transaction) -> List[ForensicEvent]:
        """Emit ForensicEvent objects for one transaction."""
        events: List[ForensicEvent] = []
        used_offsets: set[int] = set()

        # The kernel records rename results as LINK(new name) + UNLINK(old name)
        # + INODE, not as dedicated rename tags. Pair same-inode outcomes when
        # both dentry sides are present in one transaction.
        for link_rec in tx.records:
            if (
                link_rec.tag != FCTag.LINK
                or link_rec.decode_error
                or not isinstance(link_rec.payload, FCDentry)
            ):
                continue
            unlink_rec = next(
                (
                    rec for rec in tx.records
                    if rec.offset not in used_offsets
                    and rec.tag == FCTag.UNLINK
                    and not rec.decode_error
                    and isinstance(rec.payload, FCDentry)
                    and rec.payload.ino == link_rec.payload.ino
                    and (
                        rec.payload.parent_ino != link_rec.payload.parent_ino
                        or rec.payload.name != link_rec.payload.name
                    )
                ),
                None,
            )
            if unlink_rec is None:
                continue
            events.append(self._build_inferred_rename(unlink_rec, link_rec, tx.tid))
            used_offsets.add(unlink_rec.offset)
            used_offsets.add(link_rec.offset)

        for rec in tx.records:
            if rec.offset in used_offsets:
                continue
            tag = rec.tag

            # Skip records with decode errors but still record them
            if rec.decode_error:
                ev = ForensicEvent(
                    tid=tx.tid,
                    event_type='DECODE_ERROR',
                    fc_offsets=[rec.offset],
                    decode_errors=[rec.decode_error],
                    confidence=CONFIDENCE_LOW,
                )
                events.append(ev)
                continue

            if tag == FCTag.CREAT and isinstance(rec.payload, FCDentry):
                ev = self._from_dentry(rec, 'CREATE', tx.tid)
                events.append(ev)

            elif tag == FCTag.UNLINK and isinstance(rec.payload, FCDentry):
                ev = self._from_dentry(rec, 'UNLINK', tx.tid)
                events.append(ev)

            elif tag == FCTag.LINK and isinstance(rec.payload, FCDentry):
                ev = self._from_dentry(rec, 'LINK', tx.tid)
                events.append(ev)

            elif tag == FCTag.ADD_RANGE and isinstance(rec.payload, FCExtent):
                ev = self._from_add_range(rec, tx.tid)
                events.append(ev)

            elif tag == FCTag.DEL_RANGE and isinstance(rec.payload, FCDelRange):
                ev = self._from_del_range(rec, tx.tid)
                events.append(ev)

            elif tag == FCTag.INODE and isinstance(rec.payload, FCInodeUpdate):
                ev = ForensicEvent(
                    tid=tx.tid,
                    event_type='INODE_UPDATE',
                    ino=rec.payload.ino,
                    fc_offsets=[rec.offset],
                )
                events.append(ev)

        return events

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _from_dentry(rec: FCRecord, event_type: str, tid: int) -> ForensicEvent:
        d: FCDentry = rec.payload
        return ForensicEvent(
            tid=tid,
            event_type=event_type,
            ino=d.ino,
            parent_ino=d.parent_ino,
            name=d.name,
            fc_offsets=[rec.offset],
        )

    @staticmethod
    def _build_inferred_rename(
        src: FCRecord,
        dst: FCRecord,
        tid: int,
    ) -> ForensicEvent:
        d_src: FCDentry = src.payload
        d_dst: FCDentry = dst.payload
        return ForensicEvent(
            tid=tid,
            event_type='RENAME',
            ino=d_dst.ino,
            parent_ino=d_src.parent_ino,
            name=d_src.name,
            new_name=d_dst.name,
            new_parent=d_dst.parent_ino,
            fc_offsets=[src.offset, dst.offset],
        )

    @staticmethod
    def _from_add_range(rec: FCRecord, tid: int) -> ForensicEvent:
        """ADD_RANGE: physical extent allocation."""
        e: FCExtent = rec.payload
        return ForensicEvent(
            tid=tid,
            event_type='EXTENT_ADD',
            ino=e.ino,
            physical_block=e.physical_block,
            logical_block=e.ee_block,
            extent_len=e.ee_len,
            fc_offsets=[rec.offset],
        )

    @staticmethod
    def _from_del_range(rec: FCRecord, tid: int) -> ForensicEvent:
        """
        DEL_RANGE: logical block range deletion.
        DEL_RANGE stores a logical range (lblk_start + lblk_len),
        not a physical extent. physical_block is not available; logical_block
        and extent_len carry the logical range.
        """
        d: FCDelRange = rec.payload
        return ForensicEvent(
            tid=tid,
            event_type='EXTENT_DEL',
            ino=d.ino,
            physical_block=0,        # not available in DEL_RANGE
            logical_block=d.lblk_start,
            extent_len=d.lblk_len,
            fc_offsets=[rec.offset],
        )

    @staticmethod
    def _compute_confidence(ev: ForensicEvent, tx: Transaction) -> float:
        """Adjust confidence based on event-level and transaction-level signals."""
        base = tx.base_confidence
        if ev.decode_errors:
            return CONFIDENCE_LOW
        if ev.event_type in ('CREATE', 'UNLINK', 'RENAME') and not ev.name:
            return CONFIDENCE_LOW
        if ev.ino < 11:    # ino < 11 are reserved ext4 inodes
            return CONFIDENCE_LOW
        return base
