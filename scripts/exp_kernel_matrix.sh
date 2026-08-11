#!/bin/bash
# exp_kernel_matrix.sh — evidence availability across Linux kernel versions
# =========================================================================
# This script compares evidence availability across kernel versions. Containers
# cannot answer it: Docker and WSL2 share the host kernel. This script boots
# a real VM per kernel with QEMU, runs the FC-Trace real-image evaluation
# inside it, and captures the result from the serial console.
#
# The interesting output is not only the accuracy metrics but the per-kernel
# *evidence availability* map: which operations each kernel actually commits
# to the fast-commit area. That varies by version and is the contribution.
#
# Requires: qemu-system-x86, qemu-utils, cloud-image-utils, and cloud images
#           downloaded into $IMGDIR. KVM strongly recommended.
#
# Usage:  sudo ./scripts/exp_kernel_matrix.sh [scenario-list]
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/vm}"
IMGDIR="$WORK/images"
SCEN="${1:-S1,S2,S3,S4,S5}"
MEM="${MEM:-3072}"
CPUS="${CPUS:-2}"

mkdir -p "$WORK"
cd "$WORK" || exit 1

# ---- transfer disk carrying FC-Trace into the guest ---------------------
build_transfer() {
    echo "[*] building transfer disk"
    rm -f transfer.img
    dd if=/dev/zero of=transfer.img bs=1M count=256 status=none
    mkfs.ext4 -F -q transfer.img
    mkdir -p /mnt/xfer
    mount -o loop transfer.img /mnt/xfer || return 1
    cp -r "$REPO/src" "$REPO/scripts" /mnt/xfer/
    umount /mnt/xfer
}

# ---- cloud-init that runs the experiment and prints to the console ------
build_seed() {
    echo "[*] building cloud-init seed"
    cat > user-data <<EOF
#cloud-config
runcmd:
  - [ sh, -c, "echo '#####FCTRACE-BEGIN#####'" ]
  - [ sh, -c, "echo KERNEL=\$(uname -r)" ]
  - [ sh, -c, "echo E2FSPROGS=\$(mke2fs -V 2>&1 | head -1)" ]
  - [ sh, -c, "mkdir -p /mnt/x && mount /dev/vdb /mnt/x || echo MOUNT_FAILED" ]
  - [ sh, -c, "modprobe loop 2>/dev/null; for i in 0 1 2 3 4 5 6 7; do [ -e /dev/loop\$i ] || mknod /dev/loop\$i b 7 \$i; done" ]
  - [ sh, -c, "cd /mnt/x && PYTHONPATH=src python3 scripts/run_real_image_tests.py --output /tmp/r.json --snap-dir /tmp/snap --gt-dir /tmp/gt --scenarios $SCEN 2>&1 | tail -18" ]
  - [ sh, -c, "echo '#####JSON-BEGIN#####'; cat /tmp/r.json 2>/dev/null; echo; echo '#####JSON-END#####'" ]
  - [ sh, -c, "cd /mnt/x && PYTHONPATH=src python3 -c \"
import glob,sys,collections
sys.path.insert(0,'src')
from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import decode_fc_buffer
import logging; logging.disable(logging.CRITICAL)
for f in sorted(glob.glob('/tmp/snap/*_snap.img')):
    try:
        with Ext4Image(f) as i:
            j=JournalReader(i); j.open(); raw=j.read_fc_area()
            bs=j.jbd2_sb.block_size or i.block_size
        c=collections.Counter(r.tag.name for r in decode_fc_buffer(raw,block_size=bs))
        print('TAGS', f.split('/')[-1], dict(c))
    except Exception as e:
        print('TAGS', f.split('/')[-1], 'ERROR', e)
\" 2>&1 | tail -8" ]
  - [ sh, -c, "echo '#####FCTRACE-END#####'" ]
  - [ poweroff ]
EOF
    printf 'instance-id: fctrace\nlocal-hostname: fctrace\n' > meta-data
    cloud-localds seed.iso user-data meta-data
}

# ---- boot one kernel ----------------------------------------------------
run_kernel() {
    local img="$1" name="$2"
    if [ ! -f "$IMGDIR/$img" ]; then
        echo "[!] $name: image $img not found, skipping"
        return
    fi
    echo "[*] $name: booting"
    rm -f "work-$name.qcow2" "console-$name.log"
    qemu-img create -f qcow2 -F qcow2 -b "$IMGDIR/$img" "work-$name.qcow2" 12G \
        >/dev/null 2>&1 || cp "$IMGDIR/$img" "work-$name.qcow2"

    local accel="-enable-kvm"
    [ -e /dev/kvm ] || accel="-accel tcg"

    timeout 1800 qemu-system-x86_64 $accel -m "$MEM" -smp "$CPUS" -nographic \
        -drive file="work-$name.qcow2",if=virtio \
        -drive file=transfer.img,if=virtio,format=raw \
        -drive file=seed.iso,if=virtio,format=raw \
        -serial "file:console-$name.log" \
        -net nic -net user -display none >/dev/null 2>&1

    echo "=================== $name ==================="
    if grep -q '#####FCTRACE-BEGIN#####' "console-$name.log" 2>/dev/null; then
        sed -n '/#####FCTRACE-BEGIN#####/,/#####FCTRACE-END#####/p' "console-$name.log"
    else
        echo "[!] no marker found; last 20 console lines:"
        tail -20 "console-$name.log" 2>/dev/null
    fi
    echo
}

build_transfer || { echo "transfer disk failed"; exit 1; }
build_seed

run_kernel debian11-k5.10.qcow2   k5.10
run_kernel ubuntu2204-k5.15.qcow2 k5.15
run_kernel debian12-k6.1.qcow2    k6.1
run_kernel ubuntu2404-k6.8.qcow2  k6.8

echo "[*] console logs in $WORK/console-*.log"
