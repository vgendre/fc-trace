# FC-Trace

FC-Trace is a read-only Python parser for ext4 fast-commit metadata. It
decodes block-structured JBD2 fast-commit records, verifies available
TAIL CRC-32C values, correlates supported LINK/UNLINK pairs into rename
events, and emits ordered JSON, CSV, and text reports.

FC-Trace is an additional forensic evidence layer. It does not replace a
general forensic suite, recover file contents, prove user intent, or restore
records that were never logged or have already been evicted from the finite
fast-commit area.

## Release and research scope

This repository is the FC-Trace v1.0.0 code release.

The controlled evaluation uses five 512 MiB ext4 images with operation-level
ground truth. The reported macro means are precision 0.967, F1 0.709, ordering
accuracy 0.975, and path recovery 1.000. Recall ranges from 0.333 to 1.000
because kernel logging and finite fast-commit retention determine which events
remain available.

The 29 GiB acquired removable-device image described in the paper is
qualitative only. It has no independent operation-level ground truth and is
not part of the scored dataset.

The repository contains source code, tests, experiment drivers, packaging metadata, reproducibility documentation, small operation-level ground-truth ledgers, a checksum manifest, and reference JSON outputs. It does not contain raw disk images, Autopsy exports, the manuscript, reviewer responses, presentation files, or supporting-material archives. The raw images and complete dataset provenance are published separately at [Zenodo DOI 10.5281/zenodo.21807669](https://doi.org/10.5281/zenodo.21807669).

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/fctrace/` | Image access, journal access, TLV decoding, CRC checking, event reconstruction, comparison, and report generation |
| `tests/` | Automated parser and reconstruction tests |
| `scripts/` | Dataset generation, controlled evaluation, scoring, workload, kernel, capacity, scalability, and comparison drivers |
| `data/` | Small controlled-corpus ledgers and the checksum manifest; raw images are excluded |
| `results/` | Reference real-mode and simulation JSON outputs |
| `REPRODUCIBILITY.md` | Paper-to-code map and reproducibility procedures |
| `CITATION.cff` | Citation metadata for the code release and dataset |
| `AVAILABILITY.md` | Code, dataset, licensing, and verification boundaries |
| `pyproject.toml` | Package metadata, dependencies, and the `fctrace` command |
| `.github/workflows/ci.yml` | Continuous-integration test and lint workflow |

## Requirements

For installation, simulation, and automated tests:

- Python 3.10 or newer
- Python virtual-environment and packaging support
- Internet access when installing uncached dependencies

Controlled image generation additionally requires Linux, `e2fsprogs`, loop
devices, mount permission, and an ext4 kernel with `fast_commit` support.
Analysis of an existing image is read-only and does not mount the image.

There is no separate `requirements.txt`; dependencies are declared in
`pyproject.toml`.

## Installation

```bash
git clone https://github.com/vgendre/fc-trace.git
cd fc-trace
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Optional plotting and tabular-analysis dependencies are available with:

```bash
python -m pip install -e ".[dev,eval]"
```

## Verification

Check the command-line interface:

```bash
fctrace --help
python -m fctrace.cli --help
```

Run the code-release test suite:

```bash
python -m pytest tests/test_fctrace.py -q
```

The verified code-release baseline is 84 passed tests. The 168-test figure in
the supporting verification record refers to the broader workspace
workspace verification and must not be substituted for this repository's
test count.

Run the no-root parser simulation:

```bash
python scripts/score_results.py --simulate \
  --output /tmp/fc-trace-simulation.json
```

Simulation validates parser and event-reconstruction behaviour; it is not a
real-image accuracy experiment.

## Read-only image analysis

```bash
fctrace /path/to/disk.img \
  --output-json /tmp/fctrace-events.json \
  --output-csv /tmp/fctrace-events.csv \
  --output-text /tmp/fctrace-events.txt
```

Keep reports outside the evidence directory. Preserve the original image,
its cryptographic hash, acquisition information, and the FC-Trace report
together as case evidence.

## Controlled evaluation

Read [REPRODUCIBILITY.md](REPRODUCIBILITY.md) before running privileged
experiments. The primary evaluation driver is:

```bash
sudo python scripts/run_real_image_tests.py \
  --output /tmp/fc-trace-evaluation.json \
  --snap-dir /tmp/fc-trace-snapshots \
  --gt-dir /tmp/fc-trace-ground-truth \
  --scenarios S1,S2,S3,S4,S5
```

Generated images and new experiment outputs belong in temporary directories or the separately published dataset package; do not overwrite the small reference artifacts committed in `data/` and `results/`. Regeneration can change inode numbers, offsets, timestamps, and
checksums even when the procedure is unchanged.

## Interpretation limits

FC-Trace reports retained fast-commit evidence. A structurally valid record or
CRC match is not a cryptographic authenticity guarantee and does not establish
user intent. The finite circular area can overwrite older records, and kernel
workload behaviour determines which operations are logged. Compare FC-Trace
with established directory, journal, content, and case-management evidence.

The paper's controlled metrics are not universal recall or production
accuracy claims. The real removable-device analysis is qualitative and is not
an independent scored benchmark.

## Citation and license

Please cite the code release and the versioned dataset using
`CITATION.cff`. The source code is distributed under the MIT License. Dataset
terms are stated in the Zenodo record.
