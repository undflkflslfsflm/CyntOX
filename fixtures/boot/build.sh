#!/usr/bin/env sh
set -eu
mkdir -p /lab/.oslab/fixture
rm -f /lab/.oslab/fixture/boot-sector.bin /lab/.oslab/fixture/base.raw /lab/.oslab/fixture/overlay.qcow2
nasm -Wall -Werror -f bin /lab/fixtures/boot/boot.asm -o /lab/.oslab/fixture/boot-sector.bin
cp /lab/.oslab/fixture/boot-sector.bin /lab/.oslab/fixture/base.raw
truncate -s 1M /lab/.oslab/fixture/base.raw
qemu-img create -q -f qcow2 -F raw -b /lab/.oslab/fixture/base.raw /lab/.oslab/fixture/overlay.qcow2
sha256sum /lab/.oslab/fixture/boot-sector.bin /lab/.oslab/fixture/base.raw /lab/.oslab/fixture/overlay.qcow2
