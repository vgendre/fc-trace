# Code Availability

FC-Trace source code and experiment scripts are available for research verification under the MIT License at:

https://github.com/vgendre/fc-trace

The synthetic disk-image dataset, ground truth, measured outputs, and checksums are deposited separately on Zenodo:

https://doi.org/10.5281/zenodo.21807669

## Code checks

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
```

Real-image generation requires Linux root privileges, loop devices, ext4 fast-commit support, and mount permission. Analysis of an existing image is read-only and does not mount the target.
