FROM debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       nasm qemu-system-x86 qemu-utils gdb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /lab

