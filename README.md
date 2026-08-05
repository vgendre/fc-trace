# FC-Trace code release

FC-Trace is a Python research tool for parsing ext4 fast-commit records from raw disk images and reconstructing ordered, integrity-checked forensic events.

## Code scope

This GitHub package contains the source code, tests, experiment harnesses, dataset-generation code, packaging metadata, and documentation. It does not contain the large raw disk images or measured result deposit. The corresponding synthetic dataset is published separately through Zenodo. The camera-ready manuscript is supplied separately as an Overleaf source package.

## Requirements

- Linux kernel with ext4 fast-commit support for real-image experiments
- Python 3.10 or newer
- e2fsprogs with `mkfs.ext4 -O fast_commit`
- Root privileges and loop devices only for real-image generation

## Install and test

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
fctrace --help
```

The unit-test suite is the code-release check. Real-image experiments require the host prerequisites above and write their outputs to paths supplied by the user.

## Analyze an image

```bash
python3 -m fctrace.cli path/to/disk.img \
  --output-json /tmp/events.json \
  --output-csv /tmp/events.csv \
  --output-text /tmp/events.txt
```

The CLI opens the image read-only and never mounts the evidence image.

## Generate the publication dataset

The scripts `generate_dataset.py` and `run_real_image_tests.py` are included for reproducibility. The generated image files belong in the separate Zenodo dataset deposit and must not be committed to the GitHub repository.

```bash
sudo python3 scripts/generate_dataset.py \
  --output-dir ./generated/raw_images \
  --gt-dir ./generated/ground_truth \
  --scenarios S1,S2,S3,S4,S5
```

To run the real-image evaluator after generation:

```bash
sudo python3 scripts/run_real_image_tests.py \
  --output ./generated/evaluation_realmode.json \
  --snap-dir ./generated/raw_images \
  --gt-dir ./generated/ground_truth \
  --scenarios S1,S2,S3,S4,S5
```

The evaluator requires Linux loop devices and root privileges. Existing raw images can be analyzed without mounting them.

## Container verification

```bash
docker build -t fc-trace:1.0 .
docker run --rm fc-trace:1.0 python3 -m fctrace.cli --help
```

Dataset generation inside the container requires `--privileged` and a mounted output directory.

## Publication artifacts

- GitHub: this code-only release.
- Zenodo: synthetic raw images, ground truth, measured results, checksums, and provenance.
- Overleaf: `main_camera_ready.tex` and the compiled seven-page PDF in the separate camera-ready package.

The versioned dataset is published on Zenodo at https://doi.org/10.5281/zenodo.21807669. The numerical claim verifier is kept with the paper-support results because it reads the separate Zenodo measured-result files.

## License

The source code is available for research verification under the MIT License. Dataset files have their own CC BY 4.0 license in the Zenodo deposit.
