# NCBI sra-tools (prefetch / fasterq-dump) + pigz, x86_64.
FROM --platform=linux/amd64 ubuntu:22.04
ARG SRA_VERSION=3.2.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates pigz && \
    wget -q "https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/${SRA_VERSION}/sratoolkit.${SRA_VERSION}-ubuntu64.tar.gz" \
        -O /tmp/sratoolkit.tar.gz && \
    tar -xzf /tmp/sratoolkit.tar.gz -C /opt && \
    ln -s /opt/sratoolkit.${SRA_VERSION}-ubuntu64 /opt/sratoolkit && \
    rm /tmp/sratoolkit.tar.gz && \
    apt-get purge -y wget && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/sratoolkit/bin:$PATH
# accept the toolkit config non-interactively; cache goes to the mounted workdir
RUN mkdir -p /root/.ncbi && \
    printf '/LIBS/GUID = "5c9f8e2a-scpre-0000-0000-000000000000"\nconfig/default = "false"\n' \
        > /root/.ncbi/user-settings.mkfg
ENV LANG=C.UTF-8
