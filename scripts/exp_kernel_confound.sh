#!/bin/bash
# Isolate kernel from e2fsprogs.
#
# The kernel matrix showed kernels 5.10/5.15 (e2fsprogs 1.46.x) emitting no
# parent-directory CREAT record, while 6.1+ (e2fsprogs 1.47.x) do. The two
# variables moved together. Here the filesystem is created ONCE on the host
# with e2fsprogs 1.47.0 and the identical image is exercised under each
# kernel, so only the kernel varies.
set -u
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
W="${WORK:-$HOME/vm}"
cd "$W" || exit 1

echo "[*] host e2fsprogs: $(mke2fs -V 2>&1 | head -1)"

# ---- one image, built once, by one e2fsprogs -----------------------------
rm -f probe_fs.img
dd if=/dev/zero of=probe_fs.img bs=1M count=64 status=none
mkfs.ext4 -O fast_commit -b 4096 -F -J size=16 probe_fs.img -q
echo "[*] probe filesystem built on host"

# ---- probe payload placed on the transfer disk ---------------------------
rm -f probe.img
dd if=/dev/zero of=probe.img bs=1M count=128 status=none
mkfs.ext4 -F -q probe.img
mkdir -p /mnt/pxfer
mount -o loop probe.img /mnt/pxfer
cp -r "$REPO/src" /mnt/pxfer/
cp probe_fs.img /mnt/pxfer/

cat > /mnt/pxfer/probe.sh <<'PROBE'
#!/bin/sh
# Runs inside the guest. Exercises the host-built filesystem and reports
# which dentry names the kernel committed to the fast-commit area.
modprobe loop 2>/dev/null
for i in 0 1 2 3 4 5 6 7; do
    [ -e /dev/loop$i ] || mknod /dev/loop$i b 7 $i
done
cp /mnt/x/probe_fs.img /tmp/p.img
L=$(losetup -f)
losetup "$L" /tmp/p.img
mkdir -p /mnt/p
mount "$L" /mnt/p || { echo "PROBE mount failed"; exit 1; }

# warm-up so the first full JBD2 commit is out of the way
echo warm > /mnt/p/.warm
sync
sleep 6

# the S1 pattern: make a directory, then create a file inside it with O_SYNC
mkdir /mnt/p/testdir
python3 -c "
import os
fd = os.open('/mnt/p/testdir/a.txt', os.O_WRONLY|os.O_CREAT|os.O_SYNC, 0o644)
os.write(fd, b'payload')
os.close(fd)
"
blockdev --flushbufs "$L"
dd if="$L" of=/tmp/snap.img bs=4096 status=none
umount -l /mnt/p
losetup -d "$L"

PYTHONPATH=/mnt/x/src python3 -c "
import sys, logging
sys.path.insert(0, '/mnt/x/src')
logging.disable(logging.CRITICAL)
from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.parser.fc_tags import FCTag
with Ext4Image('/tmp/snap.img') as i:
    j = JournalReader(i); j.open()
    raw = j.read_fc_area(); bs = j.jbd2_sb.block_size or i.block_size
recs = decode_fc_buffer(raw, block_size=bs)
names = [r.payload.name for r in recs if r.tag == FCTag.CREAT and r.payload]
print('PROBE_CREAT_NAMES=' + repr(names))
print('PROBE_PARENT_DIR_LOGGED=' + str('testdir' in names))
"
PROBE
chmod +x /mnt/pxfer/probe.sh
umount /mnt/pxfer
echo "[*] probe disk built"

# ---- cloud-init that runs only the probe ---------------------------------
cat > user-data-probe <<'EOF'
#cloud-config
runcmd:
  - [ sh, -c, "echo '#####PROBE-BEGIN#####'" ]
  - [ sh, -c, "echo KERNEL=$(uname -r)" ]
  - [ sh, -c, "echo E2FSPROGS_GUEST=$(mke2fs -V 2>&1 | head -1)" ]
  - [ sh, -c, "mkdir -p /mnt/x && mount /dev/vdb /mnt/x" ]
  - [ sh, -c, "sh /mnt/x/probe.sh 2>&1 | tail -6" ]
  - [ sh, -c, "echo '#####PROBE-END#####'" ]
  - [ poweroff ]
EOF
printf 'instance-id: probe\nlocal-hostname: probe\n' > meta-data-probe
cloud-localds seed-probe.iso user-data-probe meta-data-probe

run_probe() {
    img="$1"; name="$2"
    [ -f "$W/images/$img" ] || { echo "[!] $name: no image"; return; }
    rm -f "probe-$name.qcow2" "probe-console-$name.log"
    qemu-img create -f qcow2 -F qcow2 -b "$W/images/$img" "probe-$name.qcow2" 12G >/dev/null 2>&1 \
        || cp "$W/images/$img" "probe-$name.qcow2"
    accel="-enable-kvm"; [ -e /dev/kvm ] || accel="-accel tcg"
    timeout 900 qemu-system-x86_64 $accel -m 3072 -smp 2 -nographic \
        -drive file="probe-$name.qcow2",if=virtio \
        -drive file=probe.img,if=virtio,format=raw \
        -drive file=seed-probe.iso,if=virtio,format=raw \
        -serial "file:probe-console-$name.log" \
        -net nic -net user -display none >/dev/null 2>&1
    echo "--- $name ---"
    sed -n '/#####PROBE-BEGIN#####/,/#####PROBE-END#####/p' "probe-console-$name.log" \
        | grep -E 'KERNEL=|E2FSPROGS_GUEST=|PROBE_CREAT_NAMES=|PROBE_PARENT_DIR_LOGGED=|failed' \
        | sed 's/.*cloud-init\[[0-9]*\]: //'
    echo
}

echo
echo "=== identical host-built filesystem, varying only the kernel ==="
run_probe debian11-k5.10.qcow2   k5.10
run_probe ubuntu2204-k5.15.qcow2 k5.15
run_probe debian12-k6.1.qcow2    k6.1
run_probe ubuntu2404-k6.8.qcow2  k6.8
