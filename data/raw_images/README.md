# Real-Image Test Artifacts

This directory is used for generated ext4 loopback images and snapshots from the real-image evaluation harness.

The raw `.img` files are intentionally not committed to the normal Git repository because each image is 512 MiB and exceeds normal GitHub file-size limits. They are generated locally by:

```bash
sudo python scripts/run_real_image_tests.py --output results/evaluation_realmode.json --snap-dir data/raw_images --gt-dir data/ground_truth --scenarios S1,S2,S3,S4,S5
```

After generation, `SHA256SUMS.txt` records the local artifact checksums for auditability. To publish the raw images later, use GitHub Releases with large-file support, Git LFS, Zenodo, or OSF.
