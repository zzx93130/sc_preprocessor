# Offline-friendly Windows/Docker Desktop image.  The installer downloads the
# official NCBI archive on the Windows host because this Docker engine may not
# have direct outbound registry/package-manager access.
FROM ubuntu:24.04
ARG SRA_VERSION=3.2.1
COPY sratoolkit.3.2.1-ubuntu64.tar.gz /tmp/sratoolkit.tar.gz
RUN tar -xzf /tmp/sratoolkit.tar.gz -C /opt && \
    ln -s /opt/sratoolkit.${SRA_VERSION}-ubuntu64 /opt/sratoolkit && \
    rm /tmp/sratoolkit.tar.gz && \
    mkdir -p /root/.ncbi && \
    printf '/LIBS/GUID = "5c9f8e2a-scpre-0000-0000-000000000000"\nconfig/default = "false"\n' \
        > /root/.ncbi/user-settings.mkfg
ENV PATH=/opt/sratoolkit/bin:$PATH
ENV LANG=C.UTF-8
