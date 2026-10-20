# velocyto + samtools, built linux/amd64 (runs under Rosetta on Apple Silicon).
#
# velocyto 0.17.17's setup.py hardcodes -ffast-math, which makes gcc emit
# glibc libmvec vector-math symbols (_ZGVbN2v_log10 / _ZGVnN2v_log10) that
# fail to resolve at import time on both amd64 and arm64 Debian bases.
# Fix: patch the sdist to compile with -fno-fast-math instead.
# Pins match the last known-good velocyto 0.17 stack.
FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        samtools build-essential zlib1g-dev libbz2-dev liblzma-dev \
        libcurl4-openssl-dev && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "numpy==1.23.5" "cython<3"
RUN pip install --no-cache-dir \
        "scipy==1.10.1" "pandas==1.5.3" "h5py==3.8.0" "loompy==3.0.7" \
        "pysam==0.21.0" "click" "matplotlib==3.7.5"
RUN pip download --no-build-isolation --no-deps --no-binary :all: \
        "velocyto==0.17.17" -d /tmp/vsrc && \
    tar -xzf /tmp/vsrc/velocyto-0.17.17.tar.gz -C /tmp/vsrc && \
    sed -i 's/-ffast-math/-fno-fast-math/g' /tmp/vsrc/velocyto-0.17.17/setup.py && \
    pip install --no-cache-dir --no-build-isolation /tmp/vsrc/velocyto-0.17.17 && \
    rm -rf /tmp/vsrc
RUN velocyto --version
ENV LANG=C.UTF-8
