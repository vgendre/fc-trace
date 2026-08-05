"""
cli.py — FC-Trace command-line interface
=========================================
Usage::

    python -m fctrace [OPTIONS] IMAGE_PATH

Options:
  --output-json PATH   Write events to JSON  (default: outputs/fctrace_output.json)
  --output-csv  PATH   Write events to CSV
  --output-text PATH   Write text summary
  --no-text-stdout     Suppress text output to stdout
  --ground-truth PATH  Compare against ground-truth JSON and print metrics
  --baseline {b1,b2,b3}  Also run the specified baseline and compare
  --verbose            Enable DEBUG logging
  --quiet              Only log WARNING and above

Exit codes:
  0  — success
  1  — image not found or not ext4
  2  — no fast-commit feature on image
  3  — journal cannot be located
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger('fctrace')


def _configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet
                                           else logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
        datefmt='%H:%M:%S',
    )


def run_fctrace(image_path: str) -> Tuple[List[dict], dict]:
    """
    Full FC-Trace pipeline: image → journal → FC area → TLV decode → events.

    Returns ``(events, meta)`` where *events* is a list of event dicts and
    *meta* carries provenance the caller needs for exit status and reporting:
    ``has_fast_commit``, ``fc_area_heuristic``, ``crc_checked``,
    ``crc_failures``, ``resyncs``.
    """
    from fctrace.io.image_reader import Ext4Image
    from fctrace.io.journal_reader import JournalReader
    from fctrace.parser.tlv_decoder import TLVDecoder
    from fctrace.parser.fc_tags import FCTag
    from fctrace.reconstruct.event_builder import EventBuilder

    meta = {
        'has_fast_commit':   False,
        'fc_area_heuristic': False,
        'crc_checked':       0,
        'crc_failures':      0,
        'resyncs':           0,
    }

    with Ext4Image(image_path) as img:
        meta['has_fast_commit'] = img.has_fast_commit
        if not img.has_fast_commit:
            logger.warning(
                "fast_commit feature not set on this image. "
                "Results will be empty."
            )

        jr = JournalReader(img)
        jr.open()
        meta['fc_area_heuristic'] = jr.fc_range_is_heuristic

        fc_bytes = jr.read_fc_area()
        logger.info("Read %d bytes from fast-commit area.", len(fc_bytes))

        if not fc_bytes:
            logger.warning("Fast-commit area is empty.")
            return [], meta

        # The FC area is block-structured; the decoder needs the journal block
        # size to skip TAIL padding and resynchronise. See TLVDecoder.
        fc_block_size = (
            jr.jbd2_sb.block_size if jr.jbd2_sb else img.block_size
        ) or img.block_size

        decoder = TLVDecoder(fc_bytes, block_size=fc_block_size)
        records = [r for r in decoder.decode() if r.tag != FCTag.PAD]
        meta['crc_checked']  = decoder.crc_checked
        meta['crc_failures'] = decoder.crc_failures
        meta['resyncs']      = decoder.resync_count
        logger.info("Decoded %d FC records.", len(records))

        if decoder.crc_failures:
            logger.warning(
                "%d of %d fast commits failed CRC verification. Those commits "
                "are not intact and their events are reported at LOW "
                "confidence.",
                decoder.crc_failures, decoder.crc_checked,
            )

        builder = EventBuilder(
            records, fc_area_heuristic=jr.fc_range_is_heuristic
        )
        events = builder.build()

    return [ev.to_dict() for ev in events], meta


def _print_metrics(result) -> None:
    print("\n── Evaluation Metrics ──────────────────────────────────────")
    print(f"  Method   : {result.method}")
    print(f"  Scenario : {result.scenario}")
    print(f"  TP / FP / FN  : {result.tp} / {result.fp} / {result.fn}")
    print(f"  Recall        : {result.recall:.4f}")
    print(f"  Precision     : {result.precision:.4f}")
    print(f"  F1            : {result.f1:.4f}")
    print(f"  Ordering Acc  : {result.ordering_acc:.4f}")
    print(f"  Path Rate     : {result.path_rate:.4f}")
    print(f"  Runtime       : {result.runtime_s:.3f} s")
    print("──────────────────────────────────────────────────────────\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fctrace',
        description='FC-Trace: ext4 fast-commit forensic timeline reconstructor',
    )
    parser.add_argument('image', help='Path to raw ext4 disk image or device')
    parser.add_argument('--output-json', default='outputs/fctrace_output.json',
                        help='JSON output path')
    parser.add_argument('--output-csv',  default=None,
                        help='CSV output path (optional)')
    parser.add_argument('--output-text', default=None,
                        help='Text report path (optional)')
    parser.add_argument('--no-text-stdout', action='store_true',
                        help='Suppress text output on stdout')
    parser.add_argument('--ground-truth', default=None,
                        help='Ground-truth JSON file for metric evaluation')
    parser.add_argument('--baseline', choices=['b1', 'b2', 'b3'], default=None,
                        help='Also run and compare a baseline')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--quiet',   action='store_true')

    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)

    image_path = args.image
    if not Path(image_path).exists():
        logger.error("Image not found: %s", image_path)
        return 1

    # ── Run FC-Trace ──────────────────────────────────────────────
    from fctrace.io.image_reader import ImageReadError
    from fctrace.io.journal_reader import JournalReadError

    t0 = time.perf_counter()
    try:
        events, meta = run_fctrace(image_path)
    except ImageReadError as exc:
        logger.error("Cannot read image as ext4: %s", exc, exc_info=args.verbose)
        return 1
    except JournalReadError as exc:
        logger.error("Cannot locate journal: %s", exc, exc_info=args.verbose)
        return 3
    except Exception as exc:
        logger.error("FC-Trace pipeline failed: %s", exc, exc_info=args.verbose)
        return 3
    fc_runtime = time.perf_counter() - t0

    logger.info("FC-Trace produced %d events in %.3f s", len(events), fc_runtime)

    if meta['crc_checked']:
        logger.info(
            "Integrity: %d/%d fast commits passed CRC verification.",
            meta['crc_checked'] - meta['crc_failures'], meta['crc_checked'],
        )

    # Documented exit code 2: the image has no fast-commit feature, so there
    # was no evidence source to read.
    if not meta['has_fast_commit'] and not events:
        logger.error(
            "Image has no fast_commit feature and produced no events."
        )
        return 2

    # ── Write outputs ─────────────────────────────────────────────
    from fctrace.output.reporters import JSONReporter, CSVReporter, TextReporter

    JSONReporter(args.output_json).write(events)

    if args.output_csv:
        CSVReporter(args.output_csv).write(events)

    text_path = args.output_text if args.output_text else None
    if not args.no_text_stdout:
        TextReporter(text_path).write(events, image_path)
    elif text_path:
        TextReporter(text_path).write(events, image_path)

    # ── Optional evaluation ───────────────────────────────────────
    if args.ground_truth:
        from fctrace.compare.diff_engine import evaluate_against_ground_truth
        result = evaluate_against_ground_truth(
            predicted=events,
            ground_truth_path=args.ground_truth,
            method='FC-Trace',
            scenario=Path(image_path).stem,
            runtime_s=fc_runtime,
        )
        _print_metrics(result)

    # ── Optional baseline ─────────────────────────────────────────
    if args.baseline:
        t1 = time.perf_counter()
        bl_events: List[dict] = []

        if args.baseline == 'b1':
            from fctrace.compare.baseline_ext4 import InodeOnlyAnalyzer
            bl_events = InodeOnlyAnalyzer(image_path).analyze()
        elif args.baseline == 'b2':
            from fctrace.compare.baseline_ext4 import JournalOnlyAnalyzer
            bl_events = JournalOnlyAnalyzer(image_path).analyze()
        elif args.baseline == 'b3':
            from fctrace.compare.baseline_ext4 import SleuthKitAdapter
            bl_events = SleuthKitAdapter(image_path).analyze()

        bl_runtime = time.perf_counter() - t1
        logger.info(
            "Baseline %s produced %d events in %.3f s",
            args.baseline.upper(), len(bl_events), bl_runtime,
        )

        if args.ground_truth:
            from fctrace.compare.diff_engine import evaluate_against_ground_truth
            result_bl = evaluate_against_ground_truth(
                predicted=bl_events,
                ground_truth_path=args.ground_truth,
                method=f'Baseline-{args.baseline.upper()}',
                scenario=Path(image_path).stem,
                runtime_s=bl_runtime,
            )
            _print_metrics(result_bl)

    return 0


if __name__ == '__main__':
    sys.exit(main())
