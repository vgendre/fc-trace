# Code availability

FC-Trace source code and experiment drivers are publicly available for
research verification:

<https://github.com/vgendre/fc-trace>

The repository contains small ground-truth ledgers, a checksum manifest, and reference JSON outputs. The raw images and complete dataset provenance are distributed separately:

<https://doi.org/10.5281/zenodo.21807669>

The camera-ready manuscript and its presentation/supporting materials are
separate publication artifacts and are not part of this code repository.

## Verification

From the repository root:

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/test_fctrace.py -q
python3 scripts/score_results.py --simulate \
  --output /tmp/fc-trace-simulation.json
```

Real-image generation requires Linux root privileges, loop devices, ext4
fast-commit support, and mount permission. Analysis of an existing image is
read-only and does not mount the target.
