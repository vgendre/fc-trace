# Reproducibility guide

This guide maps the camera-ready evaluation to the public code release. It
separates executable code, controlled evidence, and paper-only artifacts so
that a reader can identify exactly what is required for each verification
level.

## Paper-to-code map

| Camera-ready component | Code-release location | Required external evidence |
| --- | --- | --- |
| Read-only image access and journal discovery | `src/fctrace/io/` | A legally acquired image supplied by the user |
| Fast-commit tag decoding and CRC-32C checks | `src/fctrace/parser/` | None for unit tests; controlled images for image-level validation |
| Ordered event reconstruction and rename correlation | `src/fctrace/reconstruct/` | Ground-truth ledger for scored evaluation |
| JSON, CSV, and text reporting | `src/fctrace/output/` | None beyond the input image and output directory |
| Matching and capability comparison | `src/fctrace/compare/`, `scripts/exp_tool_comparison.py` | Same image snapshots and operation-level ground truth |
| Five controlled scenarios S1--S5 | `scripts/generate_dataset.py`, `scripts/run_real_image_tests.py` | Versioned Zenodo images, ledgers, checksums, and provenance |
| Scoring and metric calculation | `scripts/score_results.py` | The corresponding ground-truth ledger |
| Kernel, retention, workload, scalability, and anti-forensic procedures | `scripts/exp_*.py`, `scripts/exp_*.sh` | Linux privileges, compatible tools, and recorded environment |

The camera-ready paper, PDF, reviewer response, presentation, Autopsy
exports, and supporting-material archive are not code-release inputs.

## Verification levels

### Level 1: installation and parser tests

These checks require no root privileges, loop devices, or disk image:

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/test_fctrace.py -q
```

The code-release baseline is 84 passed tests.

### Level 2: no-root simulation

```bash
python3 scripts/score_results.py --simulate \
  --output /tmp/fc-trace-simulation.json
```

This verifies canonical TLV parsing and event reconstruction. It is an
execution check, not a real-image accuracy result.

### Level 3: controlled image regeneration

Controlled image generation requires Linux, `e2fsprogs`, an ext4 kernel with
`fast_commit`, loop devices, mount permission, and root privileges:

```bash
sudo python3 scripts/run_real_image_tests.py \
  --output /tmp/fc-trace-evaluation.json \
  --snap-dir /tmp/fc-trace-snapshots \
  --gt-dir /tmp/fc-trace-ground-truth \
  --scenarios S1,S2,S3,S4,S5
```

Use temporary output directories. A regenerated image is a new validation
run, not a byte-for-byte replacement for the versioned Zenodo image: inode
allocation, offsets, timestamps, and image checksums may differ by host.

### Level 4: external-tool comparison

The comparison driver can invoke TSK, `debugfs`, plaso, and Autopsy exports
when those tools are installed and their outputs are supplied. Capability
gaps must remain capability gaps; an Autopsy file-list export is not an
independent fast-commit event parser or a scored equivalent of FC-Trace.

```bash
python3 scripts/exp_tool_comparison.py --help
```

Use the same snapshots and the same operation-level ground truth for every
scored comparison. Preserve native tool outputs separately from normalized
FC-Trace events.

## Evidence and claim boundaries

The camera-ready paper reports five controlled 512 MiB images with
operation-level ground truth. Its reported macro means are precision 0.967,
F1 0.709, ordering accuracy 0.975, and path recovery 1.000. These values
describe the controlled corpus and its tested kernels/workloads.

The six-kernel behaviour matrix, workload experiments, retention sweeps,
scalability measurements, and anti-forensic checks require the corresponding
environment and recorded outputs. They are not silently recreated by running
the unit tests.

The 29 GiB removable-device image is qualitative only because it lacks
independent operation-level ground truth. It must not be added to the scored
denominator or described as an external accuracy benchmark.

## Reproducibility records

For every controlled run, record at least:

- repository commit and Python version;
- kernel version and `e2fsprogs` version;
- image size, filesystem features, and fast-commit area parameters;
- operation-level ground-truth source and checksum;
- command line, output paths, and generated report checksum.

Keep raw images, complete measured outputs, and full provenance with the versioned dataset record. This repository retains only the small ledgers, checksum manifest, and reference JSON needed to map the code to the paper. Do not publish an acquired real-device image or case material
without the required authorization and evidence-handling review.
