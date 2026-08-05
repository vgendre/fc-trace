# FC-Trace — Measured Experimental Results

Everything below was **executed**, not estimated. Where a measurement
contradicted an earlier claim of mine, the correction is recorded in place.

## Environment

| | |
|---|---|
| Host | Windows 11, WSL2 |
| Guest | Ubuntu 24.04 LTS |
| Kernel | **6.18.33.1-microsoft-standard-WSL2** |
| e2fsprogs | 1.47.0 (5-Feb-2023) |
| Sleuth Kit | 4.12.1 |
| Python | 3.12.3 |
| Filesystem | genuine loopback ext4, `mkfs.ext4 -O fast_commit -b 4096 -J size=64` |

`dumpe2fs` confirms `fast_commit` and `64bit` in the feature set. Loop devices
required `mknod /dev/loop{1..7}` — WSL2 pre-creates only `loop0`.

**Note on Docker.** Containers share the host kernel, so Docker provides no
kernel diversity and cannot serve reviewer comment 2's kernel-version ask.
Only virtual machines can. WSL2 gives one additional real kernel (6.18),
independent of the manuscript's 6.19.14.

---

## R1. Independent replication on a second kernel

The manuscript reports kernel 6.19.14. Re-running `run_real_image_tests.py`
unchanged on kernel 6.18.33.1 reproduces every accuracy metric exactly:

| Scenario | GT | TP | FP | FN | Recall | Prec. | F1 | Paper match |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| S1_normal_workload | 6 | 5 | 1 | 1 | 0.833 | 0.833 | 0.833 | identical |
| S2_crash_before_commit | 5 | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 | identical |
| S3_antiforensic_burst | 30 | 10 | 0 | 20 | 0.333 | 1.000 | 0.500 | identical |
| S4_shortlived_files | 24 | 9 | 0 | 15 | 0.375 | 1.000 | 0.545 | identical |
| S5_deep_rename_tree | 6 | 3 | 0 | 3 | 0.500 | 1.000 | 0.667 | identical |
| **Mean** | | | | | **0.608** | **0.967** | **0.709** | identical |

This is a genuine second data point for comment 2: the kernel logging
boundaries the paper identifies (mkdir not logged; same-window delete not
logged) hold on 6.18 as well as 6.19.14.

**Ordering accuracy improved** after the event-emission fix:

| | S1 | S2 | S3 | S4 | S5 | Mean |
|---|---:|---:|---:|---:|---:|---:|
| Manuscript (Table III) | 0.750 | 1.000 | 1.000 | 0.750 | 0.500 | 0.800 |
| Measured after fix | **1.000** | 1.000 | 1.000 | **0.875** | **1.000** | **0.975** |

Recall, precision, F1 and path rate are unchanged — the fix corrects only
emission order, not what is recovered.

---

## R2. Repeatability (N = 5)

Five complete independent runs of S1–S5, fresh images each time:

| Scenario | Recall | Precision | F1 | Ordering | Path |
|---|---|---|---|---|---|
| S1 | 0.833 ± 0.000 | 0.833 ± 0.000 | 0.833 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 |
| S2 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 |
| S3 | 0.333 ± 0.000 | 1.000 ± 0.000 | 0.500 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 |
| S4 | 0.375 ± 0.000 | 1.000 ± 0.000 | 0.545 ± 0.000 | 0.875 ± 0.000 | 1.000 ± 0.000 |
| S5 | 0.500 ± 0.000 | 1.000 ± 0.000 | 0.667 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 |
| **Mean** | **0.608 ± 0.000** | **0.967 ± 0.000** | **0.709 ± 0.000** | **0.975 ± 0.000** | **1.000 ± 0.000** |

**Zero variance.** Every metric was identical in all five runs.

This is a positive result and should be reported as one: because every
operation is forced with `O_SYNC`/`fsync`, the fast-commit content is
deterministic, so the method is exactly reproducible. It answers the
single-run weakness directly.

It also means **repetition is the wrong axis for demonstrating variation** —
see R4.

---

## R3. Comparative benchmark against The Sleuth Kit (comment 3)

Same snapshots, same ground truth, same metric implementation
(`fctrace.compare.diff_engine`). Each tool's native output mapped onto a
common `(event_type, ino, name)` model; capability gaps reported as n/a
rather than 0.

| Scenario | Tool | TP | FP | FN | Recall | Prec. | F1 | Ordering | Runtime |
|---|---|---:|---:|---:|---:|---:|---:|---|---:|
| S1 | FC-Trace | 5 | 1 | 1 | 0.833 | 0.833 | **0.833** | 1.000 | 2.0 ms |
| S1 | TSK `fls` | 3 | 4 | 3 | 0.500 | 0.429 | 0.462 | n/a | 41.5 ms |
| S2 | FC-Trace | 5 | 0 | 0 | 1.000 | 1.000 | **1.000** | 1.000 | 2.0 ms |
| S2 | TSK `fls` | 5 | 3 | 0 | 1.000 | 0.625 | 0.769 | n/a | 40.5 ms |
| S3 | FC-Trace | 10 | 0 | 20 | 0.333 | 1.000 | 0.500 | 1.000 | 2.0 ms |
| S3 | TSK `fls` | 20 | 3 | 10 | 0.667 | 0.870 | **0.755** | n/a | 38.2 ms |
| S4 | FC-Trace | 9 | 0 | 15 | 0.375 | 1.000 | 0.545 | 0.875 | 2.0 ms |
| S4 | TSK `fls` | 16 | 3 | 8 | 0.667 | 0.842 | **0.744** | n/a | 38.5 ms |
| S5 | FC-Trace | 3 | 0 | 3 | 0.500 | 1.000 | **0.667** | 1.000 | 1.6 ms |
| S5 | TSK `fls` | 4 | 3 | 2 | 0.667 | 0.571 | 0.615 | n/a | 40.2 ms |

Also run per scenario: `jls` listed 16 454 journal blocks with no dentry
semantics; `debugfs logdump` completed in 6–25 ms.

**Read this honestly — TSK wins F1 in S3 and S4.** The pattern is consistent
and is the right story for the paper:

- FC-Trace: **precision 1.000** in four of five scenarios, and it is the only
  tool producing an ordered event sequence.
- TSK: higher **recall** where files were deleted, because directory-entry
  remnants survive independently of the FC window.
- FC-Trace is **~20× faster** (≈2 ms vs ≈40 ms).

This supports positioning FC-Trace as a **complementary evidence module**,
not a replacement for TSK — which is also the strongest answer to comment 5
on real incident response.

---

## R4. Corrections to my own earlier claims

Recorded because they changed the code, the tests, and the manuscript.

### C1. The "TAIL overshoot swallows HEAD records" claim was wrong

I claimed `fc_len = bsize − off + sizeof(ext4_fc_tail)` causes a 12-byte
overshoot past the block boundary, silently consuming each following HEAD.
Direct measurement of a live capture disproved it:

```
blk  off   tag    fc_len   rec_end   lands at      predicted
  1  572   TAIL     3520      8192   blk 2 off 0   3532  DIFFERS
  2  202   TAIL     3890     12288   blk 3 off 0   3902  DIFFERS
  3  192   TAIL     3900     16384   blk 4 off 0   3912  DIFFERS
```

`572 + 4 + 3520 = 4096` exactly. The kernel evaluates
`off = s_fc_bytes % bsize` *after* `ext4_fc_reserve_space` has consumed the
12 bytes for the tag, so the effective length is `bsize − off_taghdr − 4` and
every TAIL terminates precisely on the block boundary.

Flat-walk vs block-aware decoding on the real snapshots:

| Scenario | Records (flat / aware) | Dentries (flat / aware) | Decode errors |
|---|---|---|---|
| S1 | 23 / 23 | 7 / 7 | 0 / 0 |
| S2 | 26 / 26 | 5 / 5 | 0 / 0 |
| S3 | 51 / 51 | 10 / 10 | 0 / 0 |
| S4 | 44 / 44 | 11 / 11 | 0 / 0 |
| S5 | 14 / 14 | 5 / 5 | 0 / 0 |
| **Total** | | **38 / 38** | **0 / 0** |

**No difference.** The bug was an artifact of my test fixture. Fixture, tests
and manuscript text have been corrected. The block-aware code is retained and
described accurately as resynchronisation hardening for damaged or
partially-overwritten areas, not as a correctness fix.

### C2. HEAD is emitted per transaction, not per fast commit

Captured S1 tag sequence:

```
blk 1  off   0  HEAD   tid=3          blk 3  off   0  LINK   'hardlink.txt'
blk 1  off 180  CREAT  'testdir'      blk 3  off 192  TAIL   crc ok
blk 1  off 387  CREAT  'a.txt'        blk 4  off   0  UNLINK 'hardlink.txt'
blk 1  off 572  TAIL   crc ok         blk 4  off 192  TAIL   crc ok
blk 2  off   0  LINK   'b.txt'        blk 5  off 188  CREAT  'c.txt'
blk 2  off  17  UNLINK 'a.txt'        blk 5  off 373  TAIL   crc ok
blk 2  off 202  TAIL   crc ok
```

**One HEAD, five TAILs, every record under `tid=3`.** The kernel writes HEAD
only for the first fast commit of a JBD2 transaction.

Consequence for the manuscript: the claim that ordering accuracy "reflects
the number of distinct transactions present" cannot be right, because these
workloads produce exactly one transaction. Ordering must derive from record
offsets within the FC area. Corrected in `main_revised.tex`.

### C3. `debugfs logdump` IS fast-commit aware

```
$ debugfs -R "logdump -O -n 3" S1_normal_workload_snap.img
*** Fast Commit Area ***
tag HEAD, features 0x0, tid 3
tag CREAT_DENTRY, parent 2, ino 8193, name "testdir"
tag ADD_RANGE, inode 13, lblk 0, pblk 33281, len 1
tag CREAT_DENTRY, parent 8193, ino 13, name "a.txt"
tag ADD_ENTRY, parent 8193, ino 13, name "b.txt"
tag DEL_ENTRY, parent 8193, ino 13, name "a.txt"
...
```

e2fsprogs 1.47.0 decodes fast-commit TLV records and prints tags, inodes,
parents and names. The manuscript's claims that *"no forensic tool currently
extracts or interprets ext4 fast-commit TLV records"* (§I) and *"there are
currently no dedicated tools for analyzing fast-commit in ext4"* (§II) are
**falsifiable in one command** and must be rewritten.

Defensible replacement: FC-Trace is the first to **reconstruct a forensic
event timeline** from fast-commit records — rename inference, ordering,
confidence scoring, CRC integrity verification, and structured
JSON/CSV output — whereas `debugfs logdump` produces an unordered textual
dump intended for filesystem debugging, with no event model, no ordering, no
integrity verdict and no machine-readable output.

**Upside:** debugfs independently confirms FC-Trace's parse. Both recover the
same records with the same names, inodes and parents. That is genuine
cross-validation of the decoder against a separate implementation, and worth
reporting under comment 6 (reproducibility).

---

## R5. Fast-commit CRC verification

Newly implemented, validated against the published CRC-32C check vector
(`crc32c_standard(b"123456789") == 0xE3069283`), then run on real captures:

| Scenario | Commits checked | CRC passed | CRC failed |
|---|---:|---:|---:|
| S1 | 5 | 5 | 0 |
| S2 | 5 | 5 | 0 |
| S3 | 10 | 10 | 0 |
| S4 | 9 | 9 | 0 |
| S5 | 3 | 3 | 0 |
| **Total** | **32** | **32** | **0** |

All 32 real fast commits verify. Synthetic tamper tests confirm a
single-byte alteration is detected and localised to the correct commit.
This is the tamper-detection primitive for comment 5.

---

## R6. The fast-commit evidence window — measured (comments 2, 4, 5)

This is the axis along which FC-Trace's recall genuinely varies, and it
yields a quantitative result that does not appear in the literature.

### R6.1 Capacity sweep — journal size × operation count

512 MiB image, 4 KiB blocks, N files each created with `O_SYNC`:

| Journal | num_fc_blks | FC area | Ops | Recovered | Recall | Oldest surviving index |
|---:|---:|---:|---:|---:|---:|---:|
| 16 MiB | 64 | 256 KiB | 10 | 10 | 1.000 | 0 |
| 16 MiB | 64 | 256 KiB | 25 | 25 | 1.000 | 0 |
| 16 MiB | 64 | 256 KiB | 50 | 50 | 1.000 | 0 |
| 16 MiB | 64 | 256 KiB | 100 | 63 | 0.630 | 36 |
| 16 MiB | 64 | 256 KiB | 200 | 63 | 0.315 | 136 |
| 16 MiB | 64 | 256 KiB | 400 | 63 | 0.158 | 336 |
| 64 MiB | 64 | 256 KiB | 10 → 400 | identical to above | | |

Two facts fall straight out:

1. **The FC area holds exactly 63 single-`fsync` operations.** Recovery is
   capped at 63 no matter how many operations occur.
2. **Eviction is strict FIFO.** `oldest surviving index` = `ops − 64` at every
   point (100→36, 200→136, 400→336). The buffer is a sliding window over the
   most recent operations.

So recall is not a property of the parser at all. It is
`min(1, 63 / N)` for `N` fsync operations since the last full commit.

### R6.2 The window is configurable — and the default is small

`s_num_fc_blks` read directly from the JBD2 superblock:

| `-J size` | `fast_commit_size` | max_len | num_fc_blks | FC area |
|---:|---:|---:|---:|---:|
| 16 MiB | default | 4160 | 64 | 256 KiB |
| 32 MiB | default | 8256 | 64 | 256 KiB |
| 64 MiB | default | 16448 | 64 | 256 KiB |
| 128 MiB | default | 32832 | 64 | 256 KiB |
| 64 MiB | 256 KiB | 16448 | 64 | 256 KiB |
| 64 MiB | 1024 KiB | 16640 | **256** | 1 MiB |
| 64 MiB | 4096 KiB | 17408 | **1024** | 4 MiB |
| 64 MiB | 16384 KiB | 20480 | **4096** | 16 MiB |

**The default fast-commit area is a fixed 256 KiB (64 blocks) regardless of
journal size.** Note this contradicts the `mke2fs(8)` man page, which states
the default is "journal-size / 64 megabytes" — that would give 1 MiB for a
64 MiB journal, but the measured value is 256 KiB. Worth reporting as a
documentation/behaviour discrepancy in e2fsprogs 1.47.0.

Setting `-J fast_commit_size=` scales the area linearly.

### R6.3 Enlarging the window recovers everything

200 `fsync` operations, same workload, varying only `fast_commit_size`:

| `fast_commit_size` | num_fc_blks | Recovered / 200 | Recall |
|---:|---:|---:|---:|
| default | 64 | 63 | 0.315 |
| 1024 KiB | 256 | **200** | **1.000** |
| 16384 KiB | 4096 | **200** | **1.000** |

Recall goes from 0.315 to 1.000 by changing one mkfs parameter. The
parser is unchanged; only the amount of retained evidence differs.

### R6.4 What this means for the paper

- **Comment 2 (workloads).** Workload *intensity* relative to FC capacity is
  the variable that moves recall. This is the "diverse storage workloads"
  evidence, and it explains the low S3/S4 recall as a capacity effect layered
  on top of the kernel logging limits already documented.
- **Comment 4 (scalability).** The FC area is a fixed 256 KiB by default and
  never grows with image or journal size, which is exactly why FC-Trace's
  runtime is flat (measured ≈2 ms across all scenarios versus ≈40 ms for
  `fls`). Cost is bounded by `s_num_fc_blks`, not volume size.
- **Comment 5 (anti-forensics).** An adversary needs only **≈64 trivial
  `fsync` operations** to evict all prior fast-commit evidence on a
  default-configured volume — no journal tampering, no root-level block
  writes, no special tooling. A shell loop suffices. This is a concrete,
  quantified anti-forensic capability and belongs in the adversary model.
- **Practical guidance.** Organisations wanting forensic readiness on ext4
  should provision `-J fast_commit_size=` far above the default. This is an
  actionable deployment recommendation, which strengthens the practical-impact
  argument both reviewers asked about.

---

## R5a. Kernel matrix — evidence availability across versions (comment 2)

Four kernels in QEMU/KVM virtual machines, plus the two measured natively.
Scenarios S1, S2 and S5 (S3/S4 omitted for run time). Each VM booted a
distribution cloud image, mounted a transfer disk carrying FC-Trace, ran the
evaluation, and reported over the serial console.
`scripts/exp_kernel_matrix.sh`.

| Kernel | Distribution | e2fsprogs | S1 TP/FP/FN | S1 Prec. | S1 F1 | S2 | S5 | Mean Prec. |
|---|---|---|---|---:|---:|---:|---:|---:|
| 5.10.0-45 | Debian 11 | 1.46.2 | 5 / **0** / 1 | **1.000** | **0.909** | 1.000 | 0.667 | **1.000** |
| 5.15.0-186 | Ubuntu 22.04 | 1.46.5 | 5 / **0** / 1 | **1.000** | **0.909** | 1.000 | 0.667 | **1.000** |
| 6.1.0-51 | Debian 12 | 1.47.0 | 5 / **1** / 1 | 0.833 | 0.833 | 1.000 | 0.667 | 0.944 |
| 6.8.0-136 | Ubuntu 24.04 | 1.47.0 | 5 / **1** / 1 | 0.833 | 0.833 | 1.000 | 0.667 | 0.944 |
| 6.18.33.1 | WSL2 | 1.47.0 | 5 / **1** / 1 | 0.833 | 0.833 | 1.000 | 0.667 | 0.967* |
| 6.19.14 | (authors' host) | 1.47.4 | 5 / **1** / 1 | 0.833 | 0.833 | 1.000 | 0.667 | 0.967* |

\* means over all five scenarios; the VM rows cover three.

### The finding

**A behavioural change between kernel 5.15 and 6.1.** On 5.10 and 5.15 the
S1 workload produces **no false positive**; from 6.1 onward it produces one.

The false positive is a `CREAT` record for the parent directory `testdir`.
Newer kernels cascade the directory's dentry into the same fast commit as the
first `O_SYNC` file creation inside it; older kernels do not. Ground truth
scores only file operations, so the extra record counts against precision.

Consequences for the paper:

1. **The S1 false positive is kernel behaviour, not a parser defect.** On
   kernels 5.10 and 5.15 FC-Trace achieves precision **1.000 across every
   scenario**.
2. Everything else is stable. Recall, ordering and path recovery are identical
   on all six kernels; S2 is 1.000 and S5 is 0.667 throughout. The
   documented limits (mkdir not logged; same-window deletion not logged) hold
   across the whole range.
3. It reframes an apparent weakness as a measured observation about kernel
   logging policy, which is exactly what reviewer comment 2 asked for.

### Confound resolved — the kernel is the cause

Across the distributions the kernel co-varied with e2fsprogs (1.46.x on the
versions lacking the record, 1.47.x on those emitting it), so the first result
could not distinguish the two.

Resolved by building **one** ext4 fast-commit image on the host with
e2fsprogs 1.47.0 and executing that byte-identical image under each kernel.
The probe created a directory, then a file inside it with `O_SYNC`, and
reported which dentry names reached the fast-commit area:

| Kernel | Guest e2fsprogs | CREAT names recovered | Parent dir logged |
|---|---|---|---|
| 5.10.0-45 | 1.46.2 | `['a.txt']` | **False** |
| 5.15.0-186 | 1.46.5 | `['a.txt']` | **False** |
| 6.1.0-51 | 1.47.0 | `['testdir', 'a.txt']` | **True** |
| 6.8.0-136 | 1.47.0 | `['testdir', 'a.txt']` | **True** |

The image was identical in all four runs, so the guest's e2fsprogs version is
irrelevant — it never formatted anything. **The additional record is caused by
the kernel alone**, and the manuscript states it as such rather than as an
association.

Reproduce with the probe in `scripts/` (see `HOWTO_REMAINING.md` §1).

### FC-Trace did not originally run on kernel 5.10's distribution

The first attempt failed on Debian 11:

```
TypeError: unsupported operand type(s) for |: 'type' and 'type'
  File "src/fctrace/io/image_reader.py", line 60
    def __init__(self, image_path: str | Path) -> None:
```

Debian 11 ships Python 3.9; PEP 604 union syntax needs 3.10. This mattered
because **Debian 11 carries kernel 5.10, the first kernel with fast commit** —
the tool could not run on the oldest fast-commit-capable distribution.

Fixed by adding `from __future__ import annotations` to `image_reader.py`,
`journal_reader.py`, `reporters.py` and `exp_evidence_window.py`, which defers
annotation evaluation and restores Python 3.9 compatibility. All 84 tests
still pass. Consider relaxing `requires-python` in `pyproject.toml` after
testing the rest of the code path on 3.9.

---

## R6a. Diverse storage workloads (comment 2)

Standard and application workloads rather than hand-scripted scenarios.
512 MiB image, 64 MiB journal, kernel 6.18.33.1.
`results/measured/workloads.json`.

| Workload | Files on disk | FC records | Events | CREATE | UNLINK | CRC ok | FC used |
|---|---:|---:|---:|---:|---:|---:|---:|
| fio, fsync every write | 3 | 154 | 90 | **0** | 0 | 63 | 98.6% |
| fio, fsync every 16 | 3 | 168 | 104 | **0** | 0 | 63 | 98.4% |
| fio, fsync every 256 | 3 | 190 | 126 | **0** | 0 | 63 | 98.6% |
| small files (×150, O_SYNC) | 151 | 316 | 252 | **63** | 0 | 63 | 98.6% |
| tar create + extract (96 files) | 98 | **0** | **0** | 0 | 0 | 0 | **0.0%** |
| SQLite, 400 inserts, sync=FULL | 2 | 12 | 9 | 1 | 1 | 2 | 3.3% |

### Three findings that bound applicability

**1. No `fsync`, no evidence.** The tar workload created 96 files and left
**nothing whatsoever** in the fast-commit area — zero records, zero commits,
0% utilisation. `tar` does not call `fsync` per file, so no fast commit is
ever triggered. Bulk file operations are therefore invisible to FC-Trace
unless something forces a sync.

This is the sharpest limit on the technique and belongs in the paper: FC-Trace
observes `fsync`-triggered activity, not file-system activity in general.

**2. fsync-heavy does not mean dentry-rich.** All three fio configurations
filled the area (98%+ utilisation, 63 commits) yet yielded **zero CREATE and
zero UNLINK events**. fio creates a few files once and then rewrites them, so
the area fills with `INODE` and `ADD_RANGE` records describing data-extent
updates. An examiner gets a great deal of evidence that *a file changed* and
almost none about *which files existed*.

Notably, fsync frequency barely mattered (154 / 168 / 190 records for fsync
every 1 / 16 / 256 writes). The area saturates either way.

**3. The 63-commit ceiling holds across workloads.** The small-file workload
created 151 files and recovered exactly **63** — the same capacity limit
measured in R6, reached by an independent route.

### Implication for the paper

These results define when FC-Trace helps:

- **Best case:** metadata-intensive, fsync-heavy workloads — mail servers,
  databases, package managers, container runtimes.
- **Blind spot:** bulk operations without `fsync` — archive extraction, file
  copies, build outputs.
- **Partial:** long-running write workloads, where data-extent evidence
  survives but dentry history is evicted.

Cite fio (Axboe) and Postmark (Katcher, NetApp TR3022) for the workload
choices; the small-file workload is Postmark-like rather than Postmark itself.

Reproduce with `scripts/exp_workloads.py`.

---

## R6b. plaso cannot process ext4 volumes with `fast_commit` enabled

An unexpected result from the comparative benchmark, isolated to a single
cause. plaso / log2timeline **20260119** on Ubuntu 24.04.

Running it on the scenario snapshots produced zero events:

```
Path specifications that could not be processed:
  type: OS, location: <snapshot_root>/S1_normal_workload_snap.img
  type: RAW
  type: EXT, location: /
Total events: 0
```

This is *not* "plaso scored poorly". plaso failed to parse the filesystem at
all. Reporting it as F1 = 0.000 alongside the other tools would badly
misrepresent it, so the harness now reports it as a processing failure.

### Isolation

Identical 64 MiB images, one file written, cleanly unmounted, varying only the
`mkfs` options:

| `mkfs.ext4` options | `fast_commit` | plaso recovers the file |
|---|---|---|
| *(defaults)* | no | **yes** |
| `-b 4096` | no | **yes** |
| `-b 4096 -J size=16` | no | **yes** |
| `-O fast_commit` | **yes** | **no** |
| `-O fast_commit -b 4096` | **yes** | **no** |

Block size and journal size are irrelevant. **The single determining variable
is the `fast_commit` feature flag.**

Control: on a plain ext4 image plaso produced 16 events normally, so the
installation is functional.

### Why this matters

The Sleuth Kit parses the same `fast_commit` images without difficulty — we
scored `fls` on them throughout. The failure is therefore specific to plaso's
ext backend (dfvfs / libfsext) rather than a property of the images.

The forensic consequence is larger than the fast-commit artifact itself: on a
volume with `fast_commit` enabled, plaso recovers **no timeline at all** —
not merely no fast-commit events. Since plaso is the de facto standard
super-timeline tool, an examiner using it on such a volume would obtain an
empty timeline and might reasonably conclude the volume held nothing of
interest.

### Caveats before citing this

- One plaso version (20260119) on one host. Verify on another before making a
  strong claim.
- Worth reporting upstream to the plaso/dfvfs maintainers; it reads like a
  parser bug rather than intended behaviour.
- Frame it as "the tested version could not process such volumes", not as a
  permanent property of plaso.

Reproduce with `scripts/exp_tool_comparison.py --with-plaso`.

---

## R6c. What actually invalidates fast-commit evidence

Eight candidate mechanisms, each on a freshly built volume carrying twelve
`O_SYNC` file creations. `results/measured/invalidation.json`.

| Condition | Action | CREATEs recovered (of 12) |
|---|---|---:|
| baseline | none | 12 |
| sync | `sync(1)` | 12 |
| sync + wait | `sync`, then 15 s idle | 12 |
| idle | 15 s idle, no sync | 12 |
| drop caches | `sync` + `drop_caches` | 12 |
| remount | `mount -o remount` | 12 |
| **clean unmount** | `umount`, snapshot the backing file | **12** |
| **churn** | 64 further `fsync` operations | **0** |

**Only buffer wrap destroys the records.** Seven conditions left every record
intact, including a clean unmount. In the churn case the survivors were
precisely the 63 most recent (`churn_*`), with the original `ev_*` files
evicted — consistent with the FIFO behaviour in R6.

### This corrected three statements in the paper

1. **§Limitations** claimed a full JBD2 commit "overwrites fast-commit
   records". It does not; a commit checkpoints metadata but does not scrub
   the fast-commit region.
2. **§Experimental Setup** justified snapshot-before-unmount by asserting that
   a clean unmount "would flush the FC area via a full commit". Measured: all
   12 records survived a clean unmount. The procedure is retained because it
   faithfully models live acquisition, but the stated reason was wrong.
3. **§Applicability to Incident Response** repeated the unmount-flush claim.

The corrected story is simpler and more favourable: evidence is lost through
*subsequent synchronised activity*, not through committing or unmounting. An
investigator who images after an orderly shutdown has not thereby destroyed
the evidence.

### Confirmed across five kernels

The decisive conditions were repeated on kernels 5.10, 5.15, 6.1 and 6.8 in
VMs, in addition to 6.18. Identical on all five:

| Kernel | baseline | sync | clean_umount | churn |
|---|---:|---:|---:|---:|
| 5.10.0-45 | 12 | 12 | 12 | 0 (63 newest survive) |
| 5.15.0-186 | 12 | 12 | 12 | 0 |
| 6.1.0-51 | 12 | 12 | 12 | 0 |
| 6.8.0-136 | 12 | 12 | 12 | 0 |
| 6.18.33.1 | 12 | 12 | 12 | 0 |

The behaviour is stable across the whole range of kernels supporting fast
commit, so the paper states it without a single-kernel caveat.

Reproduce with `scripts/exp_invalidation.py` (one kernel) or
`scripts/exp_invalidation_matrix.sh` (VM matrix).

---

## R7. Computational performance and scalability (comment 4)

Sparse ext4 images 512 MiB → 512 GiB, median of five timed runs after a
warm-up. `results/measured/scalability.json`.

| Image | `num_fc_blks` | FC area | FC-Trace | Peak RSS | `fls` | `fls` RSS | Runtime ratio (fls/FC-Trace) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 MiB | 64 | 256 KiB | 0.24 ms | 16.4 MiB | 106.1 ms | 17.5 MiB | 448 |
| 2 GiB | 256 | 1 MiB | 0.99 ms | 18.3 MiB | 24.2 ms | 17.4 MiB | 24 |
| 8 GiB | 256 | 1 MiB | 0.94 ms | 18.3 MiB | 120.3 ms | 17.5 MiB | 128 |
| 32 GiB | 1024 | 4 MiB | 3.88 ms | 24.5 MiB | 434.2 ms | 17.7 MiB | 112 |
| 128 GiB | 4096 | 16 MiB | 19.61 ms | 49.0 MiB | 2625.2 ms | 17.5 MiB | 134 |
| 512 GiB | 4096 | 16 MiB | 19.19 ms | 49.0 MiB | 18214.3 ms | 17.5 MiB | **949** |

### The controlled comparison

`mkfs` scales the journal — and so the fast-commit area — with filesystem
size, so image size and FC area grow together above. Two pairs of rows hold
the FC area constant while the image grows 4×, which separates the effects:

| Pair | `num_fc_blks` | Image | FC-Trace | `fls` |
|---|---:|---:|---|---|
| 2 GiB → 8 GiB | 256 both | ×4 | 0.99 → 0.94 ms (**×0.95**) | ×5.0 |
| 128 GiB → 512 GiB | 4096 both | ×4 | 19.61 → 19.19 ms (**×0.98**) | ×6.9 |

**FC-Trace's cost is independent of image size and proportional to the
fast-commit area.** Normalised per block it is constant at **3.7–4.8 µs**
across the whole range. Over a 1024 image-size increase `fls` slows 172×.

### Memory — the one honest caveat

Peak RSS grows 16.4 → 49.0 MiB because `read_fc_area()` buffers the entire
fast-commit region before decoding. It is bounded by the FC area rather than
the image, and stays modest, but a streaming decoder would remove even that
dependence. `fls` is flat at ~17.5 MiB.

### Measurement note

`/proc/self/io` `read_bytes` reported 0 for every run: the images had just
been created, so all reads were served from page cache. Do **not** cite
bytes-read from this run. To measure it properly, drop caches between runs
(`echo 3 > /proc/sys/vm/drop_caches`) and re-run.

---

## R8. Anti-forensic resilience — tier-2 adversary (comment 5)

256 MiB image, 16 MiB journal, 12 files created with `O_SYNC`, then the
attack applied. `results/measured/antiforensic.json`.

| ID | Attack | Dentries recovered | CRC ok / bad | Outcome |
|---|---|---:|---|---|
| A1 | baseline (no interference) | 12 | 12 / 0 | 1 HEAD, 12 TAILs |
| A2 | full journal wipe (`dd` zeros over FC blocks) | **12 → 0** | 0 / 0 | evidence destroyed, but **detectable**: no HEAD or TAIL at all |
| A3 | partial wipe (one FC block zeroed) | **12 → 11** | 11 / 0 | decoder **resynchronised** (52 resyncs) and recovered the rest |
| A4 | forced full commit (`sync`) | **12** | 12 / 0 | **no evidence lost** — see below |
| A5 | `tune2fs -O ^fast_commit` | **12** | 12 / 0 | disabling the feature does not erase existing records |
| A6 | `shred -u` on every file | **12** | 12 / 0 | **filenames survive content destruction** |
| A7 | timestomp (`touch -t 1990…`) | **12** | 12 / 0 | fast-commit records unaffected; ordering intact |

### Findings that matter

**A3 vindicates the resync code.** A single zeroed block inside the FC area
would desynchronise a linear walk for the remainder of the buffer. The
block-aware decoder resynchronised 52 times and still recovered 11 of 12
records. This is the first case where that change demonstrably affects
recovery on real data — note it is damage tolerance, not the HEAD-swallowing
bug I originally and wrongly claimed (§R4/C1).

Detection nuance: zeroed blocks yield no parseable TAIL, so the CRC does not
*fail* — the commit simply disappears. Tamper detection here comes from the
resync count and missing commits, not from a CRC mismatch. Both signals are
worth reporting to an examiner.

**A6 is a strong anti-anti-forensic result.** `shred -u` overwrites file
contents and unlinks the file, yet all twelve filenames remained recoverable
from the fast-commit area. Secure-deletion tools operate on file data and
directory entries; they do not scrub the journal. An investigator can
therefore recover *what was deleted* even when the content is unrecoverable.

**A5 and A7 are also favourable.** Disabling `fast_commit` after the fact
leaves existing records intact, and forged inode timestamps do not touch the
append-only fast-commit records, so record order remains a valid cross-check
against timestomping.

### ⚠ A4 contradicts the current manuscript

`main.tex` §Limitations states: *"A full JBD2 commit (automatic or manual,
e.g. `sync`) overwrites fast-commit records, so recent evidence may be lost if
imaging is delayed or a commit is forced."*

**Measured: it did not.** After `sync` plus a 7 s wait, all 12 records were
still recoverable with all CRCs valid.

The likely explanation is that a full commit checkpoints the metadata but does
not scrub the fast-commit region; records persist until *overwritten by new
fast commits*. That is consistent with the R6 eviction result, where loss came
from buffer wrap rather than from checkpointing.

**Do not simply flip the claim on one measurement.** Verify on your 6.19.14
host, and with an explicit unmount as well as `sync`, before rewriting the
Limitations text. But as it stands the sentence is unsupported, and a reviewer
who tests it will find what I found.

---

## R9. `fast_commit` is not a mkfs default

```
$ mkfs.ext4 -F -q def.img            # no -O flags
$ dumpe2fs -h def.img | grep features
Filesystem features: has_journal ext_attr resize_inode dir_index filetype
                     extent 64bit flex_bg sparse_super large_file huge_file
                     dir_nlink extra_isize metadata_csum
```

`fast_commit` **absent**. `/etc/mke2fs.conf` default feature set confirms it.

This is the empirical basis for stating that no public forensic corpus
(NIST CFReDS, Digital Corpora) contains ext4 fast-commit volumes, and for
proposing the release of one as a contribution.

---

## Reproducing

All experiment scripts are in the session scratchpad and were run as root
inside WSL2:

| Script | Purpose |
|---|---|
| `wsl_realimage.sh` | S1–S5 real-image evaluation |
| `exp_repeatability.py N` | N independent runs, mean ± sd |
| `exp_tool_comparison.py` | FC-Trace vs TSK `fls`/`jls`/`debugfs` |
| `analyse_real_fc.py` | HEAD/TAIL/CRC census of captured FC areas |
| `compare_decoders.py` | flat vs block-aware decoding |
| `probe_tail.py` | measured on-disk TAIL `fc_len` |
| `exp_evidence_window.py` | journal size × operation count sweep |
