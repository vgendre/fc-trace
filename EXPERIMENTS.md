# FC-Trace — Experiment Manual

Step-by-step instructions for the experiments that answer each reviewer
comment. Everything here is copy-pasteable. Each section states what to
record and where it belongs in the manuscript.

**Status key**
`[DONE]` measured already — data in `results/measured/`
`[AUTO]` scripted in this repo, just run it
`[MANUAL]` needs hardware, a GUI, or a download you must do yourself

---

## Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y e2fsprogs util-linux sleuthkit python3 python3-pip \
                        qemu-system-x86 qemu-utils cloud-image-utils wget
python3 -m pip install -e '.[dev]'
```

Verify fast-commit support before anything else:

```bash
mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 /tmp/probe.img -q
dumpe2fs -h /tmp/probe.img 2>/dev/null | grep -i "features"
# must list: fast_commit
uname -r          # must be >= 5.10
mke2fs -V         # must be >= 1.46.3
```

If `losetup -f` fails with "No such file or directory" (common in WSL2 and
some containers), create the device nodes:

```bash
sudo modprobe loop
for i in $(seq 0 7); do
    [ -e /dev/loop$i ] || sudo mknod /dev/loop$i b 7 $i
done
```

---

# Comment 2 — Kernel versions, real-world datasets, diverse workloads

This comment has three separable asks. Treat them separately; they need
different evidence and only one needs virtual machines.

## 2A. Parser compatibility across kernel versions `[MANUAL, no VMs needed]`

Answers "does FC-Trace decode what each kernel writes?" without booting
anything. The fast-commit on-disk format is defined by
`fs/ext4/fast_commit.h`; if the tags and structs are unchanged across a
release range, parser compatibility across that range follows.

```bash
mkdir -p /tmp/fcabi && cd /tmp/fcabi
for TAG in v5.10 v5.15 v6.1 v6.6 v6.12 v6.18; do
    wget -q -O fast_commit-$TAG.h \
      https://raw.githubusercontent.com/torvalds/linux/$TAG/fs/ext4/fast_commit.h
done

# Tag values per release
for f in fast_commit-*.h; do
    echo "=== $f ==="
    grep -E '#define EXT4_FC_TAG_' $f
done

# Struct layouts per release
for f in fast_commit-*.h; do
    echo "=== $f ==="
    sed -n '/struct ext4_fc_tl {/,/};/p;/struct ext4_fc_head {/,/};/p;/struct ext4_fc_tail {/,/};/p;/struct ext4_fc_dentry_info {/,/};/p;/struct ext4_fc_del_range {/,/};/p' $f
done

# Diff consecutive releases -- empty output means the ABI is unchanged
diff fast_commit-v5.10.h fast_commit-v6.18.h
```

**Record:** a table of kernel version × tag set × struct layout, and whether
FC-Trace's `fc_tags.py` matches. **Already verified against current master:**
nine tags (`ADD_RANGE` 0x1 … `HEAD` 0x9), `ext4_fc_del_range = {ino, lblk,
len}`, `ext4_fc_dentry_info = {parent_ino, ino, name[]}` — all match.

**Goes in:** a new subsection in Section IV, and it lets you state parser
compatibility over a range you never booted.

## 2B. Kernel matrix — evidence availability `[MANUAL, needs VMs]`

Answers the different question: "which operations does each kernel actually
*write* to the fast-commit area?" This is your strongest scientific result,
because the answer varies and nobody has published it.

> **Docker will not work.** Containers share the host kernel. Neither will
> WSL2 alone — it provides exactly one kernel. You need virtual machines.

### Kernels to cover

| Kernel | Cloud image | Role |
|---|---|---|
| 5.4 | Ubuntu 20.04 `focal-server-cloudimg-amd64.img` | negative control, pre-fast-commit |
| 5.10 | Debian 11 `debian-11-genericcloud-amd64.qcow2` | first release with fast commit |
| 5.15 | Ubuntu 22.04 `jammy-server-cloudimg-amd64.img` | widely deployed LTS |
| 6.1 | Debian 12 `debian-12-genericcloud-amd64.qcow2` | LTS |
| 6.8 | Ubuntu 24.04 `noble-server-cloudimg-amd64.img` | LTS |
| 6.19.14 | your existing host | already in the paper |

### Step 1 — download

```bash
mkdir -p ~/vm/images && cd ~/vm/images
wget https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-genericcloud-amd64.qcow2
wget https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2
wget https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
wget https://cloud-images.ubuntu.com/focal/current/focal-server-cloudimg-amd64.img
```

### Step 2 — build a transfer disk holding FC-Trace

The VM needs the code in, and results out. A small ext4 disk attached as a
second drive is the most reliable route (no networking or 9p needed).

```bash
cd ~/vm
dd if=/dev/zero of=transfer.img bs=1M count=256
mkfs.ext4 -F transfer.img -q
mkdir -p /mnt/transfer && sudo mount -o loop transfer.img /mnt/transfer
sudo cp -r /path/to/fc-trace/src /path/to/fc-trace/scripts \
           /path/to/fc-trace/data /mnt/transfer/
sudo umount /mnt/transfer
```

### Step 3 — cloud-init that runs the experiment and prints to the console

```bash
cat > user-data <<'EOF'
#cloud-config
password: fctrace
chpasswd: { expire: False }
runcmd:
  - [ mkdir, -p, /mnt/x ]
  - [ mount, /dev/vdb, /mnt/x ]
  - [ sh, -c, "echo '#####FCTRACE-BEGIN#####'" ]
  - [ sh, -c, "uname -r" ]
  - [ sh, -c, "mke2fs -V 2>&1 | head -1" ]
  - [ sh, -c, "cd /mnt/x && PYTHONPATH=src python3 scripts/run_real_image_tests.py --output /tmp/r.json --snap-dir /tmp/snap --gt-dir /tmp/gt --scenarios S1,S2,S3,S4,S5 2>&1 | tail -25" ]
  - [ sh, -c, "cat /tmp/r.json" ]
  - [ sh, -c, "echo '#####FCTRACE-END#####'" ]
  - [ poweroff ]
EOF
echo "instance-id: fctrace; local-hostname: fctrace" > meta-data
cloud-localds seed.iso user-data meta-data
```

### Step 4 — boot each kernel and capture the console

```bash
run_kernel() {
    IMG=$1; NAME=$2
    cp ~/vm/images/$IMG ~/vm/work-$NAME.qcow2
    qemu-img resize ~/vm/work-$NAME.qcow2 12G
    qemu-system-x86_64 \
        -enable-kvm -m 3072 -smp 2 -nographic \
        -drive file=$HOME/vm/work-$NAME.qcow2,if=virtio \
        -drive file=$HOME/vm/transfer.img,if=virtio,format=raw \
        -drive file=$HOME/vm/seed.iso,if=virtio,format=raw \
        -serial file:$HOME/vm/console-$NAME.log \
        -net nic -net user
    echo "=== $NAME ==="
    sed -n '/#####FCTRACE-BEGIN#####/,/#####FCTRACE-END#####/p' \
        $HOME/vm/console-$NAME.log
}

run_kernel debian-11-genericcloud-amd64.qcow2 k5.10
run_kernel jammy-server-cloudimg-amd64.img     k5.15
run_kernel debian-12-genericcloud-amd64.qcow2  k6.1
run_kernel noble-server-cloudimg-amd64.img     k6.8
run_kernel focal-server-cloudimg-amd64.img     k5.4   # expect: no fast_commit
```

Without KVM add `-accel tcg` instead of `-enable-kvm` (much slower but works).

### Step 5 — what to record

For each kernel, one row:

| Kernel | e2fsprogs | S1 R/P/F1 | S2 | S3 | S4 | S5 | mkdir logged? | same-window UNLINK logged? | DEL_RANGE logged? |
|---|---|---|---|---|---|---|---|---|---|

The last three columns are the contribution. Extract them with:

```bash
sudo python3 -c "
import sys; sys.path.insert(0,'src')
from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
from fctrace.parser.tlv_decoder import decode_fc_buffer
from fctrace.parser.fc_tags import FCTag
from collections import Counter
with Ext4Image('/tmp/snap/S5_deep_rename_tree_snap.img') as i:
    j = JournalReader(i); j.open()
    raw = j.read_fc_area(); bs = j.jbd2_sb.block_size or i.block_size
print(Counter(r.tag.name for r in decode_fc_buffer(raw, block_size=bs)))
"
```

**Goes in:** replaces the single-kernel Table III with a per-kernel table, and
turns Threats to Validity from "one kernel" into "characterised across six".

## 2C. Real-world forensic datasets `[MANUAL — and read this first]`

**You cannot satisfy this ask literally, and you should say so in the paper.**

`fast_commit` is not in the default `mke2fs` feature set. Verified on
e2fsprogs 1.47.0:

```
$ mkfs.ext4 -F -q def.img && dumpe2fs -h def.img | grep features
has_journal ext_attr resize_inode dir_index filetype extent 64bit flex_bg
sparse_super large_file huge_file dir_nlink extra_isize metadata_csum
# fast_commit: ABSENT
```

Consequently **no public forensic corpus contains an ext4 fast-commit
volume** — NIST CFReDS, Digital Corpora (M57-Patents, govdocs1), Lone Wolf and
Nitroba are Windows/NTFS or pre-date the feature.

### Do this instead — three parts

**(i) Negative control on a genuine forensic image.** Download one real corpus
image and show FC-Trace handles it correctly and reports honestly:

```bash
wget https://cfreds-archive.nist.gov/dfr-images/dfr-01-ext4.dd    # or any ext4 image
python3 -m fctrace.cli dfr-01-ext4.dd --output-json /tmp/o.json --no-text-stdout
echo "exit=$?"      # expect 2: no fast_commit feature
```

This demonstrates the tool degrades gracefully on real-world evidence and
correctly identifies the absence of the artifact. It is a legitimate,
reportable result.

**(ii) Release your own corpus.** Publish the generated images (or generator
plus `SHA256SUMS.txt`) on Zenodo with a DOI, and cite Garfinkel et al. (2009)
on standardised corpora. This becomes *the first public ext4 fast-commit
forensic corpus* — a contribution in its own right, and it also closes
comment 6's availability ask.

**(iii) One genuinely aged volume.** Enable fast-commit on a real working
machine, use it normally for a week, then image it:

```bash
sudo tune2fs -O fast_commit /dev/sdXN          # unmounted, non-root filesystem
# ... one week of ordinary use ...
sudo dd if=/dev/sdXN of=aged.img bs=4M status=progress
python3 -m fctrace.cli aged.img --output-json aged_events.json
```

Report event counts by type and the wall-clock span recovered. One such image
converts "synthetic only" into "synthetic plus real deployment".

## 2D. Diverse storage workloads `[MANUAL]`

Replaces hand-scripted scenarios with citable standard benchmarks.

```bash
sudo apt-get install -y fio filebench
```

Mount a fast-commit filesystem at `/mnt/fc`, run each workload, snapshot the
loop device **before unmounting**, then analyse.

```bash
# 1. fio, fsync-heavy -- the workload fast commit exists for
fio --name=fsynctest --directory=/mnt/fc --rw=randwrite --bs=4k \
    --size=64M --numjobs=4 --fsync=1 --runtime=60 --time_based \
    --group_reporting

# 2. fio, fsync frequency sweep -- gives the evidence-window curve
for F in 1 4 16 64 256; do
    fio --name=sweep$F --directory=/mnt/fc --rw=randwrite --bs=4k \
        --size=32M --fsync=$F --runtime=30 --time_based
done

# 3. Filebench varmail -- canonical mail-server profile, heavy fsync
echo 0 | sudo tee /proc/sys/kernel/randomize_va_space
sudo filebench -f /usr/share/filebench/workloads/varmail.f

# 4. Filebench fileserver -- metadata-heavy, different mix
sudo filebench -f /usr/share/filebench/workloads/fileserver.f

# 5. Linux kernel compile -- realistic developer workload
tar xf linux-6.1.tar.xz -C /mnt/fc && cd /mnt/fc/linux-6.1
make defconfig && make -j$(nproc)

# 6. SQLite -- application fsync pattern
python3 -c "
import sqlite3
c = sqlite3.connect('/mnt/fc/t.db')
c.execute('PRAGMA synchronous=FULL')
c.execute('CREATE TABLE t(a,b)')
for i in range(5000):
    c.execute('INSERT INTO t VALUES(?,?)', (i, 'x'*100)); c.commit()
"
```

After each: snapshot and analyse.

```bash
sudo blockdev --flushbufs /dev/loop0
sudo dd if=/dev/loop0 of=workload_snap.img bs=4096 status=none
python3 -m fctrace.cli workload_snap.img --output-json workload_events.json
```

**Record per workload:** events recovered by type, distinct transactions,
CRC pass rate, FC-area utilisation, and elapsed wall-clock covered.
**Cite:** Tarasov et al. (2016) for Filebench, Axboe for fio, Katcher (1997)
for Postmark.

---

# Comment 3 — Comparative analysis with Sleuth Kit and Autopsy

## 3A. Sleuth Kit `[DONE]`

Measured on identical snapshots with the same metric implementation:
`results/measured/tool_comparison.json`.

```bash
python3 scripts/exp_tool_comparison.py --snap-dir data/raw_images \
    --gt-dir data/ground_truth
```

| Scenario | FC-Trace F1 | TSK `fls` F1 |
|---|---:|---:|
| S1 | **0.833** | 0.462 |
| S2 | **1.000** | 0.769 |
| S3 | 0.500 | **0.755** |
| S4 | 0.545 | **0.744** |
| S5 | **0.667** | 0.615 |

TSK wins S3 and S4. Report it — it is what makes the comparison credible and
it supports the complementary-tool framing.

Also measured: `jls` lists 16 454 journal blocks with **zero** fast-commit
references; `debugfs logdump` **does** decode fast-commit records but produces
an unordered dump with no event pairing, no sequence numbers, no integrity
verdict and no machine-readable output.

## 3B. Autopsy `[MANUAL — GUI, cannot be scripted here]`

Autopsy is a Java GUI over the TSK engine. Two defensible options:

**Option 1 (recommended): state the relationship and benchmark the engine.**
Autopsy's file-system parsing *is* TSK, so the measured TSK numbers already
characterise its ext4 capability. Say so explicitly in the paper and cite
Carrier. This is honest and avoids comparing a parser against a
case-management platform.

**Option 2: run Autopsy for completeness.**

```bash
# Download from https://www.autopsy.com/download/  (Linux .zip)
unzip autopsy-4.21.0.zip && cd autopsy-4.21.0
./unix_setup.sh
./autopsy
```

Then in the GUI:
1. **Create New Case** → name it `FC-Trace-S1` → Next → Finish
2. **Add Data Source** → *Disk Image or VM File* → select
   `data/raw_images/S1_normal_workload_snap.img`
3. Ingest modules: enable **File Type Identification**, **Extension Mismatch
   Detector**, **Recent Activity**; disable hash lookup and keyword search
   (they add time without affecting file-system event recovery)
4. Wait for ingest to complete; note wall-clock from the ingest progress bar
5. **Tools → Generate Report → Results - CSV** → export to `autopsy_S1.csv`
6. Open the **Timeline** view (Tools → Timeline) and export events

**Record:** ingest wall-clock, peak RSS (`ps -o rss= -p $(pgrep -f autopsy)`
sampled during ingest), number of files/events reported, and — critically —
whether the pre-rename name `a.txt` appears anywhere in the output.

**Expected result:** it will not. `a.txt` exists only in the fast-commit area.
That single fact is your clearest novelty demonstration; confirming it in
Autopsy makes it unimpeachable.

Score the CSV with the same metric code so the numbers are comparable:

```bash
python3 -c "
import csv, json, sys
sys.path.insert(0,'src')
from fctrace.compare.diff_engine import DiffEngine
gt = json.load(open('data/ground_truth/S1_normal_workload_gt.json'))
pred = []
for row in csv.DictReader(open('autopsy_S1.csv')):
    name = row.get('Name') or row.get('File Name') or ''
    pred.append({'event_type':'CREATE','ino':0,'name':name,
                 'parent_ino':0,'new_name':'','new_parent':0})
r = DiffEngine(gt, pred, method='Autopsy', scenario='S1').evaluate()
print(r.to_dict())
"
```

## 3C. plaso / log2timeline `[MANUAL]`

The standard timeline-reconstruction baseline, which R2 comment 3 asks for by
name ("timeline reconstruction approaches").

```bash
sudo add-apt-repository ppa:gift/stable && sudo apt-get update
sudo apt-get install -y plaso-tools

log2timeline.py --storage-file /tmp/s1.plaso \
    data/raw_images/S1_normal_workload_snap.img
psort.py -o dynamic -w /tmp/s1_timeline.csv /tmp/s1.plaso
wc -l /tmp/s1_timeline.csv
grep -c "a.txt" /tmp/s1_timeline.csv     # expect 0
```

**Record:** runtime, event count, and again whether `a.txt` is recoverable.

## 3D. Comparison table for the paper

| Tool | Reads FC | Event model | Ordered | Integrity | Recovers pre-rename name | Runtime |
|---|---|---|---|---|---|---|
| TSK `fls`/`istat` | No | No | No | No | **No** (measured) | 40 ms |
| TSK `jls`/`jcat` | No | No | No | No | No | 42 ms |
| Autopsy | No | No | Timestamp | No | *fill in* | *fill in* |
| plaso | No | No | Timestamp | No | *fill in* | *fill in* |
| `debugfs logdump` | **Yes** | No | No | No | Partial (raw records only) | 25 ms |
| **FC-Trace** | **Yes** | **Yes** | **Yes** | **Yes** | **Yes** | **2 ms** |

---

# Comment 4 — Overhead, scalability, memory `[AUTO + MANUAL]`

Partially measured. The image-size scaling curve and memory figures are the
gap.

```bash
# Sparse images -- costs almost no disk. Do NOT use dd for the large ones.
for SZ in 512M 2G 8G 32G 128G 512G; do
    truncate -s $SZ /tmp/scale_$SZ.img
    mkfs.ext4 -O fast_commit -b 4096 -F -J size=64 /tmp/scale_$SZ.img -q
done

# FC-Trace: wall-clock, peak RSS, bytes read
for SZ in 512M 2G 8G 32G 128G 512G; do
    /usr/bin/time -v python3 -m fctrace.cli /tmp/scale_$SZ.img \
        --output-json /tmp/o.json --no-text-stdout --quiet 2>&1 \
      | grep -E "Elapsed|Maximum resident"
done

# TSK for contrast
for SZ in 512M 2G 8G 32G 128G 512G; do
    /usr/bin/time -v fls -r /tmp/scale_$SZ.img > /dev/null 2>/tmp/t.log
    grep -E "Elapsed|Maximum resident" /tmp/t.log
done
```

Bytes actually read (the cleanest evidence for the O(1) claim):

```bash
python3 -c "
import subprocess, os
p = subprocess.Popen(['python3','-m','fctrace.cli','/tmp/scale_512G.img',
                      '--output-json','/tmp/o.json','--no-text-stdout','--quiet'])
p.wait()
" &
cat /proc/$!/io 2>/dev/null | grep read_bytes
```

**Expected:** FC-Trace flat (reads only `s_num_fc_blks` blocks — 256 KiB by
default) while image size grows 1000×. Plot both curves on one axis; that
single figure answers R1 #4 and R2 #4.

Already measured: FC-Trace ≈2 ms vs `fls` ≈40 ms on 512 MiB images, and the
FC area is a fixed 256 KiB regardless of journal or image size
(`results/measured/evidence_window.json`).

---

# Comment 5 — Anti-forensic resilience `[PARTLY DONE]`

Done: CRC tamper detection (32/32 verified, single-byte alteration localised)
and log overwriting (≈64 fsync operations evicts everything).

Remaining scenarios. For each: run it, snapshot, analyse, record what FC-Trace
reports.

```bash
# A2 -- direct journal wipe
sudo python3 -c "
import subprocess,sys; sys.path.insert(0,'src')
from fctrace.io.image_reader import Ext4Image
from fctrace.io.journal_reader import JournalReader
with Ext4Image('snap.img') as i:
    j=JournalReader(i); j.open(); print('FC blocks:', j.fc_blocks[0], len(j.fc_blocks))
"
sudo dd if=/dev/zero of=snap.img bs=4096 seek=<first_fc_block> count=64 conv=notrunc
python3 -m fctrace.cli snap.img --output-json wiped.json
# expect: no HEAD/TAIL found -> report as tamper indicator

# A3 -- forced full commit destroys the FC area
sync                        # then snapshot and compare event counts

# A4 -- feature disabled mid-life
sudo tune2fs -O ^fast_commit /dev/loop0

# A5 -- secure delete
sudo apt-get install -y secure-delete
shred -u /mnt/fc/secret.bin
srm -z /mnt/fc/secret2.bin
# expect: dentry records survive in FC even though content is destroyed

# A6 -- timestomp, then cross-check against FC ordering
touch -t 199001010000 /mnt/fc/evidence.txt
sudo debugfs -w -R "set_inode_field /evidence.txt ctime 199001010000" /dev/loop0
# expect: FC record order contradicts the forged timestamps -> corroboration
```

**Record per scenario:** events before/after, CRC verdicts, and whether the
manipulation is *detectable*. A6 is the strongest: fast-commit records are
append-only log entries and are not timestomp-able the way inode times are.

**Also update §Adversary Model.** It currently states the adversary cannot
"directly change journal blocks", which excludes exactly what R2 #5 asks about.
Add a tier-2 adversary with block-device write access.

---

# Comment 6 — Presentation and availability `[MOSTLY DONE]`

Done: 15 manuscript corrections, CI lint green (was failing with 61 errors),
tests 56 → 84, code hygiene.

Remaining, and only you can do it:

1. ~~Decide the licence.~~ Done: MIT, stated consistently in `LICENSE`,
   `pyproject.toml`, `CITATION.cff`, `AVAILABILITY.md`, `README.md` and the
   manuscript.
2. Publish the repository and mint a Zenodo DOI, then run
   `python3 scripts/set_doi.py <doi>`, which updates the manuscript,
   `AVAILABILITY.md` and `CITATION.cff` together so they cannot disagree.
3. Update `LICENSE`, `AVAILABILITY.md`, `README.md` and `CITATION.cff` to say
   the same thing.
4. Improve figures: add the scalability curve (comment 4), the evidence-window
   curve (comment 2/5), and the per-kernel evidence map (comment 2). Give each
   a caption stating the takeaway, not just the axes.

---

# Recording results

Keep every run's JSON under `results/measured/` and note, for each:

- kernel (`uname -r`), e2fsprogs (`mke2fs -V`), CPU, RAM, storage type
- whether the host was a VM, container, or bare metal
- image size, `-J size`, `-J fast_commit_size`, resulting `s_num_fc_blks`
- FC-Trace commit hash

Reviewers of a forensics artifact check reproducibility first. The environment
table matters as much as the metrics.
