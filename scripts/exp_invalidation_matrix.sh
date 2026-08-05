#!/bin/bash
# exp_invalidation_matrix.sh — does the invalidation result hold across kernels?
# =============================================================================
# exp_invalidation.py established on one kernel that fast-commit records survive
# sync, remount, cache drops and even a clean unmount, and are destroyed only by
# circular-buffer wrap. Since that is a JBD2 implementation detail, this script
# repeats the decisive subset of conditions on every available kernel.
#
# Conditions run: baseline, sync, clean_umount, churn.
#
# Requires: qemu-system-x86, cloud-image-utils, and cloud images in $IMGDIR.
# Usage: sudo ./scripts/exp_invalidation_matrix.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/vm}"
IMGDIR="$WORK/images"
COND="${COND:-baseline,sync,clean_umount,churn}"

mkdir -p "$WORK"
cd "$WORK" || exit 1

echo "[*] building transfer disk"
rm -f inv_transfer.img
dd if=/dev/zero of=inv_transfer.img bs=1M count=256 status=none
mkfs.ext4 -F -q inv_transfer.img
mkdir -p /mnt/invxfer
mount -o loop inv_transfer.img /mnt/invxfer || exit 1
cp -r "$REPO/src" "$REPO/scripts" /mnt/invxfer/
umount /mnt/invxfer

echo "[*] building cloud-init seed"
cat > user-data-inv <<EOF
#cloud-config
runcmd:
  - [ sh, -c, "echo '#####INV-BEGIN#####'" ]
  - [ sh, -c, "echo KERNEL=\$(uname -r)" ]
  - [ sh, -c, "mkdir -p /mnt/x && mount /dev/vdb /mnt/x" ]
  - [ sh, -c, "cd /mnt/x && PYTHONPATH=src python3 scripts/exp_invalidation.py --only $COND --output /tmp/inv.json 2>&1 | tail -12" ]
  - [ sh, -c, "echo '#####INV-END#####'" ]
  - [ poweroff ]
EOF
printf 'instance-id: inv\nlocal-hostname: inv\n' > meta-data-inv
cloud-localds seed-inv.iso user-data-inv meta-data-inv

run_inv() {
    img="$1"; name="$2"
    [ -f "$IMGDIR/$img" ] || { echo "[!] $name: image missing"; return; }
    rm -f "inv-$name.qcow2" "inv-console-$name.log"
    qemu-img create -f qcow2 -F qcow2 -b "$IMGDIR/$img" "inv-$name.qcow2" 12G \
        >/dev/null 2>&1 || cp "$IMGDIR/$img" "inv-$name.qcow2"
    accel="-enable-kvm"; [ -e /dev/kvm ] || accel="-accel tcg"
    timeout 1800 qemu-system-x86_64 $accel -m 3072 -smp 2 -nographic \
        -drive file="inv-$name.qcow2",if=virtio \
        -drive file=inv_transfer.img,if=virtio,format=raw \
        -drive file=seed-inv.iso,if=virtio,format=raw \
        -serial "file:inv-console-$name.log" \
        -net nic -net user -display none >/dev/null 2>&1
    echo "=========== $name ==========="
    sed -n '/#####INV-BEGIN#####/,/#####INV-END#####/p' "inv-console-$name.log" \
        | sed 's/.*cloud-init\[[0-9]*\]: //' \
        | grep -E 'KERNEL=|baseline|sync|clean_umount|churn|condition|verdict' \
        | head -12
    echo
}

echo
echo "=== invalidation conditions across kernels ==="
run_inv debian11-k5.10.qcow2   k5.10
run_inv ubuntu2204-k5.15.qcow2 k5.15
run_inv debian12-k6.1.qcow2    k6.1
run_inv ubuntu2404-k6.8.qcow2  k6.8
