# Minimal x86_64 base for running the (bind-mounted) Cell Ranger bundle.
# Cell Ranger itself is NOT baked in — mount tools/cellranger-10.1.0 at /opt/cellranger.
FROM --platform=linux/amd64 ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps ca-certificates && \
    rm -rf /var/lib/apt/lists/*
ENV LANG=C.UTF-8
