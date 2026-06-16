# Ground-Truth JSON Files

These files define the expected events used by the simulation and real-image scoring scripts.

Simulation mode:

```bash
python scripts/score_results.py --simulate --output results/evaluation.json
```

Real-image mode, requiring root and loop-device support:

```bash
sudo python scripts/run_real_image_tests.py \
  --output results/evaluation_realmode.json \
  --snap-dir data/raw_images \
  --gt-dir data/ground_truth \
  --scenarios S1,S2,S3,S4,S5
```

The latest committed single-run real-image results are stored in `results/evaluation_realmode.json` and summarized in `README.md`.
