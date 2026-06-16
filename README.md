# FC-Trace

FC-Trace is a research prototype for parsing ext4 fast-commit records from raw disk images and reconstructing recent file-system metadata events. It targets post-mortem forensic triage of ext4 volumes formatted with `fast_commit` enabled.

## Repository Contents

```text
src/fctrace/              FC-Trace library and CLI
tests/                    unit and integration tests
scripts/                  dataset generation, real-image test, and scoring tools
data/ground_truth/        ground-truth JSON for five scenarios
data/raw_images/          local raw-image README and checksum manifest
results/                  latest simulation and real-image evaluation JSON
.github/workflows/        GitHub Actions test workflow
Dockerfile                reproducible Linux test environment
```

The repository intentionally does not track raw `.img` files because each generated ext4 image is 512 MiB. Raw-image checksums from the latest local run are recorded in `data/raw_images/SHA256SUMS.txt`.

## Requirements

- Linux kernel with ext4 fast-commit support, kernel >= 5.10
- Python >= 3.10
- `e2fsprogs` with `mkfs.ext4 -O fast_commit`
- Root privileges and loop devices only for real-image generation
- Existing image analysis is read-only and does not require mounting the target image

## Install

```bash
python3 -m pip install -e '.[dev]'
```

## Run Tests

```bash
pytest -q
```

Expected result for this artifact snapshot:

```text
56 passed
```

## Run FC-Trace

```bash
python -m fctrace.cli path/to/disk.img \
  --output-json outputs/fctrace_output.json \
  --output-csv outputs/fctrace_output.csv \
  --output-text outputs/fctrace_output.txt
```

## Simulation Evaluation

Simulation mode validates parser and event-reconstruction behavior with canonical TLV buffers. It is not a real-image accuracy claim.

```bash
python scripts/score_results.py --simulate --output results/evaluation.json
```

Latest simulation results in `results/evaluation.json`: mean recall `1.000`, precision `1.000`, F1 `1.000`, ordering accuracy `0.790`, path recovery `1.000`.

## Real-Image Evaluation

Real-image generation requires root and loop-device support. This command creates five ext4 loopback images and five mounted-device snapshots under `data/raw_images/`, refreshes ground truth under `data/ground_truth/`, and writes `results/evaluation_realmode.json`.

```bash
sudo python scripts/run_real_image_tests.py \
  --output results/evaluation_realmode.json \
  --snap-dir data/raw_images \
  --gt-dir data/ground_truth \
  --scenarios S1,S2,S3,S4,S5
```

Latest local real-image results, run on 2026-06-16:

| Scenario | TP | FP | FN | Recall | Precision | F1 | Ordering | Path |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| S1_normal_workload | 5 | 1 | 1 | 0.8333 | 0.8333 | 0.8333 | 0.7500 | 1.0000 |
| S2_crash_before_commit | 5 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| S3_antiforensic_burst | 10 | 0 | 20 | 0.3333 | 1.0000 | 0.5000 | 1.0000 | 1.0000 |
| S4_shortlived_files | 9 | 0 | 15 | 0.3750 | 1.0000 | 0.5455 | 0.7500 | 1.0000 |
| S5_deep_rename_tree | 3 | 0 | 3 | 0.5000 | 1.0000 | 0.6667 | 0.5000 | 1.0000 |

Mean real-image metrics: recall `0.6083`, precision `0.9667`, F1 `0.7091`, ordering accuracy `0.8000`, path recovery `1.0000`.

These are single-run loopback-image results on one host. Do not present them as multi-kernel, multi-run, or production-general accuracy claims.

## Docker

```bash
docker build -t fc-trace:latest .
docker run --rm fc-trace:latest pytest -q
```

Real-image generation inside Docker requires `--privileged`:

```bash
docker run --rm --privileged -v "$PWD":/work -w /work fc-trace:latest \
  sudo python3 scripts/run_real_image_tests.py \
  --output results/evaluation_realmode.json \
  --snap-dir data/raw_images \
  --gt-dir data/ground_truth \
  --scenarios S1,S2,S3,S4,S5
```

## Evidence-Source Limits

Real-image tests show high precision in this harness, but recall is bounded by what the Linux kernel writes to the fast-commit area. In the tested kernel, directory creation is not emitted as a `CREAT` fast-commit dentry record, and deletion/truncation of files created within the same fast-commit window may not be logged as `UNLINK` or `DEL_RANGE`.

## Ethics

Use FC-Trace only on disk images you are legally authorized to examine. The included experiments use generated test images and no real user data.

## Authors and Affiliation

This research artifact is authored by Vinod Gendre and Nitesh K Bharadwaj, Department of Computer Science and Engineering, National Institute of Technology Raipur, India. Authors are listed in `AUTHORS.md`; citation metadata is provided in `CITATION.cff`.

## License

All rights reserved. Source code is provided for academic review and reproducibility assessment only. See `LICENSE`.
