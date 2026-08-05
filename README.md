# FC-Trace code release v1.0.0

This directory contains the FC-Trace v1.0.0 code release.

FC-Trace is a Python research tool for parsing ext4 fast-commit records from raw disk images and reconstructing ordered, integrity-checked forensic events. It is intended for post-mortem analysis of ext4 volumes created with the `fast_commit` feature enabled.

## Code release scope

This repository contains the FC-Trace source code, command-line interface, tests, experiment scripts, packaging metadata, and documentation. It does not contain raw disk images, the Zenodo dataset deposit.

- `src/fctrace/` — parser, event reconstruction, reporters, and CLI
- `tests/` — automated unit and integration tests
- `scripts/` — simulation, image generation, scoring, and experiment tools
- `.github/workflows/` — continuous-integration workflow
- `pyproject.toml` — package metadata and dependencies
- `Dockerfile` — optional container test environment

## Requirements

### Required for installation and tests

- Python 3.10 or newer
- `python3-venv` and `python3-pip`
- Internet access for the first installation, unless the packages are already cached

There is no separate `requirements.txt`. The package and development dependency are declared in `pyproject.toml`.

### Additional requirements for real-image experiments

- Linux with an ext4 kernel that supports `fast_commit`
- `e2fsprogs`, including `mkfs.ext4` and `dumpe2fs`
- root privileges, loop devices, and mount permission

These additional requirements are not needed to install FC-Trace, run the parser tests, run simulation mode, or analyze an existing image read-only.

## Install step by step

On Debian or Ubuntu, install the Python prerequisites:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
```

Clone the code repository and enter its root directory:

```bash
git clone https://github.com/vgendre/fc-trace.git
cd fc-trace
```

Create and activate an isolated environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Upgrade packaging tools and install FC-Trace in editable mode with its test dependency:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The installation creates the `fctrace` command. Keep the virtual environment activated while using it.

## Test step by step

Confirm that the command-line interface is installed:

```bash
fctrace --help
python -m fctrace.cli --help
```

Run the complete automated test file:

```bash
python -m pytest tests/test_fctrace.py -q
```

The verified result for this release is:

```text
84 passed
```

The test suite uses synthetic buffers and temporary files. It does not require root privileges, loop devices, or a disk image.

## Analyze an existing image

FC-Trace opens the input image read-only and does not mount it. Write reports outside the evidence directory:

```bash
fctrace /path/to/disk.img \
  --output-json /tmp/fctrace-events.json \
  --output-csv /tmp/fctrace-events.csv \
  --output-text /tmp/fctrace-events.txt
```

The equivalent module command is:

```bash
python -m fctrace.cli /path/to/disk.img --output-json /tmp/fctrace-events.json
```

## Run the no-root simulation

Simulation validates parser and event-reconstruction behaviour with canonical TLV buffers:

```bash
python scripts/score_results.py --simulate \
  --output /tmp/fc-trace-simulation.json
```

This is a parser validation result, not a real-image accuracy claim.

## Run real-image evaluation

Read `EXPERIMENTS.md` before running privileged commands. The short command is:

```bash
sudo python scripts/run_real_image_tests.py \
  --output /tmp/fc-trace-evaluation.json \
  --snap-dir /tmp/fc-trace-snapshots \
  --gt-dir /tmp/fc-trace-ground-truth \
  --scenarios S1,S2,S3,S4,S5
```

The command creates synthetic ext4 images and snapshots. Use temporary output directories because the raw images are large and must not be committed to GitHub. Regenerating on another host can change inode numbers, timestamps, offsets, and checksums.

## Optional dependencies and Docker

Install plotting and tabular-analysis dependencies only when needed:

```bash
python -m pip install -e ".[dev,eval]"
```

The optional comparison scripts may use `fls`, `jls`, `debugfs`, Autopsy, or plaso when separately installed.

```bash
docker build -t fc-trace:1.0 .
docker run --rm fc-trace:1.0 python -m pytest tests/test_fctrace.py -q
```

Real-image generation in a container requires loop-device access and a privileged container.

## Dataset and limitations

The matching versioned synthetic dataset and measured outputs are published at [Zenodo DOI 10.5281/zenodo.21807669](https://doi.org/10.5281/zenodo.21807669). They are separate from this code repository.

FC-Trace reconstructs only evidence represented in the finite circular ext4 fast-commit area. Kernel logging behaviour varies by workload and kernel version. Results from the controlled corpus do not establish universal recall or production accuracy.

Use FC-Trace only on disk images that you are legally authorized to examine.

## Citation and license

Code citation metadata is in `CITATION.cff`. The source code is released under the MIT License. Dataset licensing is stated in the Zenodo record.
