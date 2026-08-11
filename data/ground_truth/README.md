# Operation-level ground truth

These JSON ledgers define the expected file operations for controlled
scenarios S1--S5. They are used by the scoring and comparison scripts when the
corresponding versioned raw images are available.

The ledgers are small reproducibility metadata. The raw images, checksums,
and complete provenance are distributed separately in the versioned Zenodo
dataset.

For a newly generated controlled run, use temporary paths:

```bash
sudo python3 scripts/run_real_image_tests.py \
  --output /tmp/fc-trace-evaluation.json \
  --snap-dir /tmp/fc-trace-images \
  --gt-dir /tmp/fc-trace-ground-truth \
  --scenarios S1,S2,S3,S4,S5
```
