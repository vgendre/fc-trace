# FC-Trace experiment manual

This document gives the reproducible commands for the experiment scripts in the code repository.

## 1. Install and verify

From the repository root:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip e2fsprogs util-linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest tests/test_fctrace.py -q
```

The verified release result is `84 passed`. Python tests and simulation do not require root privileges. Real-image experiments do.

## 2. Check ext4 fast-commit support

Use a temporary probe image, never an evidence image:

```bash
probe=$(mktemp --suffix=.img)
mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 "$probe" -q
dumpe2fs -h "$probe" 2>/dev/null | grep -i fast_commit
rm -f "$probe"
```

The output should contain `fast_commit`. Real-image experiments also require working loop devices and mount permission.

## 3. Parser simulation

Simulation uses canonical fast-commit TLV buffers and needs no disk images:

```bash
python scripts/score_results.py --simulate \
  --output /tmp/fc-trace-simulation.json
```

The output JSON records parser and event-reconstruction metrics. Simulation is not a real-image accuracy claim.

## 4. Generate synthetic images

Use temporary directories. The generated `.img` files are large and must not be copied into the GitHub code repository:

```bash
sudo python scripts/generate_dataset.py \
  --output-dir /tmp/fc-trace-images \
  --gt-dir /tmp/fc-trace-ground-truth \
  --scenarios S1,S2,S3,S4,S5
```

The generator writes scenario images and ground-truth JSON files. The versioned published images are available in the separate Zenodo dataset deposit.

## 5. Run real-image evaluation

```bash
sudo python scripts/run_real_image_tests.py \
  --output /tmp/fc-trace-evaluation.json \
  --snap-dir /tmp/fc-trace-snapshots \
  --gt-dir /tmp/fc-trace-ground-truth \
  --scenarios S1,S2,S3,S4,S5
```

Use a subset such as `--scenarios S1,S2` for a smaller run. The snapshot directory may require several gigabytes of free space. Record the Linux kernel, `e2fsprogs` version, Python version, host type, and command-line arguments with each run.

## 6. Analyze an existing image

The input image is opened read-only and is not mounted:

```bash
fctrace /path/to/disk.img \
  --output-json /tmp/events.json \
  --output-csv /tmp/events.csv \
  --output-text /tmp/events.txt
```

## 7. Optional experiments

Run these only after obtaining snapshots and ground truth. Keep outputs outside the source tree.

### Tool comparison

```bash
python scripts/exp_tool_comparison.py \
  --snap-dir /tmp/fc-trace-snapshots \
  --gt-dir /tmp/fc-trace-ground-truth \
  --output /tmp/fc-trace-tool-comparison.json
```

The comparison harness treats Sleuth Kit, `debugfs`, Autopsy, and plaso as optional external tools. Missing tools are reported as unavailable; they are not required for FC-Trace tests.

### Repeatability

```bash
sudo python scripts/exp_repeatability.py -n 5 \
  --snap-dir /tmp/fc-trace-repeatability \
  --output /tmp/fc-trace-repeatability.json
```

### Fast-commit evidence window

```bash
sudo python scripts/exp_evidence_window.py --mode capacity \
  --output /tmp/fc-trace-capacity.json
sudo python scripts/exp_evidence_window.py --mode fcsize \
  --output /tmp/fc-trace-fcsize.json
```

These experiments measure retention in the finite circular fast-commit area. They do not imply that every workload produces the same number of recoverable events.

## 8. Reproducibility and scope

- Keep generated images, snapshots, and result JSON outside the GitHub checkout unless they are intentionally reviewed fixtures.
- Regeneration on another host can change inode allocation, timestamps, block offsets, and image checksums.
- A missing event can reflect kernel logging policy or buffer eviction; it is not automatically a parser failure.
- Report metrics with the tested corpus, kernel, storage environment, and workload scope.
- The versioned dataset and measured deposit is at <https://doi.org/10.5281/zenodo.21807669>.

Use the tool only on disk images that you are legally authorized to examine.
