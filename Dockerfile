# ────────────────────────────────────────────────────────────
# FC-Trace Reproducibility Container
# Provides: Python 3.12, e2fsprogs >= 1.46.3, sleuthkit, pytest
#
# Build:   docker build -t fc-trace:latest .
# Run:     docker run --rm -it --privileged fc-trace:latest bash
#
# Note: --privileged is required for losetup / mount operations
#       (dataset generation only).  Analysis of existing images
#       does NOT require elevated privileges.
# ────────────────────────────────────────────────────────────

FROM ubuntu:22.04

LABEL maintainer="Vinod Gendre <vgendre.phd2024.cse@nitrr.ac.in>"
LABEL description="FC-Trace: ext4 fast-commit forensic analysis"
LABEL version="0.1.0"

# Use noninteractive apt frontend
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ── System dependencies ──────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        e2fsprogs \
        util-linux \
        sleuthkit \
        git \
        curl \
        sudo \
    && rm -rf /var/lib/apt/lists/*


# ── Working directory ────────────────────────────────────────
WORKDIR /opt/fc-trace

# ── Copy source ──────────────────────────────────────────────
COPY src/           ./src/
COPY tests/         ./tests/
COPY scripts/       ./scripts/
COPY data/          ./data/
COPY results/       ./results/
COPY pyproject.toml README.md AUTHORS.md CITATION.cff LICENSE ./

# ── Install package (editable, no deps) ─────────────────────
RUN python3 -m pip install --no-cache-dir -e ".[dev]"

# ── Create data directories ──────────────────────────────────
RUN mkdir -p data/raw_images data/ground_truth data/processed results

# ── Verify installation ──────────────────────────────────────
RUN python3 -m pytest tests/test_fctrace.py -q --tb=short \
    && echo "✓ All tests pass"

# ── Default command ──────────────────────────────────────────
CMD ["bash"]

# ── Usage examples ───────────────────────────────────────────
# Run tests:
#   docker run --rm fc-trace:latest pytest tests/ -v
#
# Analyse an image (mount host directory):
#   docker run --rm -v /path/to/images:/images fc-trace:latest \
#     python -m fctrace /images/disk.img --output-json /images/out.json
#
# Generate dataset (requires privileged):
#   docker run --rm --privileged -v $(pwd)/data:/opt/fc-trace/data \
#     fc-trace:latest \
#     sudo python3 scripts/generate_dataset.py --output-dir data/raw_images
