# DrivePrep - Build Specification (Ubuntu)

**Target implementer:** Claude Code
**Deliverable:** A Python 3 CLI tool for Ubuntu Server that securely erases used hard disk drives, health-tests them, and produces a listing-ready report (HTML + PNG) suitable for posting in an eBay listing. Supports running a queue of drives in parallel, unattended.

**Revision 2** (2026-08-03), after implementation review. Changes from revision 1, all resolving contradictions or gaps found before coding:

| § | Change |
|---|---|
| 4.5 | Identity tuples are now **per device class**. Loop and dm devices have no `/sys/block/<kname>/device/`, so the original SCSI-only tuple was unimplementable for every §14 fixture. |
| 4.4 | The confirmation token is computed over the **phase-0 eligible set**, not the post-self-test set, so `--confirm-token` is reproducible and a short-test failure no longer aborts the batch. Hash input is now pinned exactly. |
| 4.4 | Plan mode's scope stated: no phase-2 self-test. |
| 4.5 | Step 4 re-runs the **full** §4.2 rule set after open, not just mountinfo and swaps. |
| 4.2.1 | `--test-mode` may not be combined with `--all` (snapd keeps 15-25 loop devices mounted on a stock box). |
| 5 | `BLKRRPART` ordering fixed — it returns `EBUSY` while the tool's `O_EXCL` descriptor is open. `partprobe` dropped along with the undeclared `parted` dependency. |
| 6.1 | `--scan-open` is global; run once in the parent. |
| 6.3, 11.2, 12 | `erase.performed` / `verify.performed`, so a drive that fails the health gate is never reported as if it had been sanitized. |
| 6.5 | Thermal guard extended to phase 6, the longest hot phase. |
| 8.2 | New: signal handling, since `systemd-run` is the recommended launcher. |
| 8.1 | Per-drive `flock` covering phases 0-3, which hold no descriptor. |
| 9, 9.1 | `progress.json` split from `state.json`, resolving the once-per-second vs every-30-seconds conflict. |
| 10, 13 | `grading.yaml` → `grading.toml`; §2 commits to zero dependencies and 3.12 has `tomllib` but no YAML parser. |
| 11.3 | Chrome `--user-data-dir` (profile-lock races at `--jobs 4`); `SUDO_USER` resolution for the snap path, since `$HOME` is `/root` under sudo. |
| 12 | Added `smart.power_cycles` (required by a §11.2 tile), `smart.probe_log`, `identity.class`; `when_failed` corrected to `""` not `null`; `raw` normalization stated. |
| 13 | `queue.py` → `supervisor.py`; new `identity.py`. |
| 14 | New tests 15-17; `losetup -P` noted in test 7. |

---

## 1. Context

The operator has a stack of older used hard disk drives (spinning platters, mostly Western Digital) to sell individually on eBay. Most arrive in external USB enclosures (My Book / Elements / Easystore desktop units, My Passport portables); some will be shucked or docked and connected directly to SATA. Before listing, each drive must be:

1. Sanitized so no prior data is recoverable by ordinary means.
2. Health-tested so the listing can make an honest, evidenced claim about drive condition.
3. Documented with a single image the operator can upload as a listing photo.

Ten to thirty drives. The operator starts a batch, walks away, and comes back to finished reports. Optimize for **safety, unattended reliability, and clarity**, not throughput.

---

## 2. Environment

| Item | Requirement |
|---|---|
| OS | Ubuntu Server 24.04 LTS, bare metal. No desktop environment assumed. |
| Python | 3.12 (system Python on 24.04). Standard library only, no runtime dependencies. Config is TOML so `tomllib` covers it; see §10. |
| Privileges | root. Fail fast with a clear message if `os.geteuid() != 0`. |
| Packages | `smartmontools`, `util-linux` (present). Install line goes in the README. Optional: `zfsutils-linux` for the `zpool` check in §4.2, skipped cleanly when absent. |
| PNG rendering | Headless Chromium. See §11.3 for resolution order and the Ubuntu snap caveat. |
| Bus types | USB and SATA both supported and first class. |
| Out of scope | NVMe, SSDs, SAS, hardware RAID volumes. Detect and refuse each with a specific message. |

### 2.1 Notes on the platform choice

This tool exists on Linux specifically because the block layer makes the dangerous parts easy and the diagnostic parts rich:

- `/dev/disk/by-id/` gives stable, serial-bearing device names, which removes an entire class of "the disk got renumbered mid-run and we wrote the wrong device" bug.
- `O_DIRECT` with an `mmap` buffer is page aligned for free, so there is no manual buffer-alignment work.
- `/sys/block/<kname>/` exposes size, logical and physical block size, rotational flag, read-only flag, and holders without a single ioctl.
- `losetup` and `dm-error` make the destructive paths and the error-handling paths genuinely testable.
- The kernel log is a real health signal (§6.4) that is hard to get at otherwise.

---

## 3. Non-goals

Do not build these. If you think one is needed, stop and ask first.

- A GUI or a web UI. CLI only.
- A formal certificate of destruction. This is honest seller documentation, not a NAID-certified attestation. Report wording must not imply certification.
- SSD or NVMe sanitization. Different problem, different correct method.
- Multi-pass overwrite schemes (DoD 5220.22-M, Gutmann). See §5.
- ATA Secure Erase / `hdparm --security-erase`. See §5.2.
- `blkdiscard`. It is meaningless on rotational media and dangerous to reach for by habit.
- Any network calls, telemetry, or upload.
- Repairing or remapping bad sectors.

---

## 4. Safety

**This tool destroys data irreversibly. Implement this entire section before writing any code that opens a device for writing.**

### 4.1 Canonical device identity

The canonical identifier for a drive is its **`/dev/disk/by-id/` name**, preferring in this order:

1. `ata-<model>_<serial>` for SATA
2. `usb-<vendor>_<model>_<serial>-0:0` for USB
3. `wwn-0x...` if neither of the above exists

Never use `/dev/sdX` as an identifier. Kernel names are assigned in discovery order and move between boots and replugs. Resolve `by-id` to a kernel name only at the moment of use, and re-verify per §4.5.

- Ignore `by-id` entries ending in `-partN`; those are partitions, not disks.
- If a drive has no usable `by-id` entry at all, derive a synthetic identifier `dp-<first 12 hex of SHA1 of model + serial + size_bytes>` and require the operator to select it by `--device /dev/sdX` explicitly. Warn loudly, because this drive cannot be safely re-identified after a replug and must not be used in queue mode.
- The `by-id` name (or synthetic identifier), sanitized for filesystem use, is the output directory name.

Record both the **enclosure serial** (from the `by-id` name / `lsblk -o SERIAL`) and the **ATA serial** (from `smartctl` IDENTIFY) separately. For a bridged USB drive these routinely differ. Display both on the report when they differ. Only the enclosure-level identity is used for selection.

### 4.2 Ineligibility rules

A disk is **INELIGIBLE** if any of the following is true. Each is checked independently and the specific failing reason is recorded and displayed.

**Structural:**
- It is not a whole disk (it is a partition, an md device, a dm device, a loop device outside test mode, or a zram device).
- Its kernel name matches `nvme*`. NVMe is out of scope.
- **`holders/` is non-empty on the disk _or on any of its partitions_.** This is the check most likely to be built wrong. `/sys/block/sdb/holders/` is **empty** when the LVM PV, mdraid member, or dm-crypt backing device is `sdb1` rather than `sdb`; the holder symlink lives at `/sys/class/block/sdb1/holders/`. Checking only the whole disk lets an in-use data disk sail through eligibility and get zeroed. Enumerate `/sys/block/<kname>/<kname>*/holders/` as well as `/sys/block/<kname>/holders/`.
- **It or any of its partitions carries an on-disk signature identifying it as a member of a larger storage structure**, as reported by `blkid -p -s TYPE`: `LVM2_member`, `LVM1_member`, `linux_raid_member`, `crypto_LUKS`, `zfs_member`, `bcache`, `DDF_raid_member`, `isw_raid_member`, `ceph_bluestore`. A plain filesystem is fine — a drive being sold will have one — but these mean something else owns the device.

  This exists because **`holders/` only sees an *active* stack, and that is not the same thing as an *unused* disk.** A volume group that has been deactivated — `vgchange -an`, a disk carried over from another machine, a boot where the VG was never brought up — has an empty `holders/` on every one of its PVs. It is not mounted, it is not in `/proc/mdstat`, and its `fstab` entry resolves to `/dev/vg/lv`, which does not exist while the LV is inactive. It therefore passes **every** other rule in this section and would be zeroed, destroying the entire volume group. The signature survives deactivation; that is what catches it. The same reasoning covers an unassembled md member and an unopened LUKS container.

  Verified experimentally: creating an LV puts `dm-0` in the PV's `holders/`; `vgchange -an` empties it again while `blkid` continues to report `LVM2_member`.

  Note also that `pvcreate` **alone** creates no holder at all — a holder appears only once a logical volume is activated on top. An earlier draft of §14 test 7 used bare `pvcreate` to build the holders fixture, which meant it never exercised the holders path; that test now covers the signature rule, and the holders path is built with a `dm` linear target instead.

- It or any of its partitions appears in `/proc/mdstat` as an array member. Note that mdstat lists **partition** names (`sdb1[0]`), so map each member back to its parent disk before comparing.
- `zpool status -P` lists it or any of its partitions as a vdev, when `zpool` exists.
- It or any of its partitions appears in `/proc/self/mountinfo`.
- It or any of its partitions appears in `/proc/swaps`.
- **It or any of its partitions is referenced in `/etc/fstab` or `/etc/crypttab`**, resolving `UUID=`, `LABEL=`, `PARTUUID=`, and `PARTLABEL=` forms via `blkid` or `/dev/disk/by-*`. An idle internal data disk that is in fstab but happens not to be mounted right now passes every "currently active" check above. With SATA a first-class path (§4.3), this is the most realistic remaining wrong-device scenario in the whole design.
- It is the disk backing `/`, `/boot`, `/boot/efi`, `/home`, or the output directory. Resolve each mountpoint with `findmnt -no SOURCE <path>`, then recurse **`/sys/block/<kname>/slaves/`** down to every leaf physical disk. Do **not** use `lsblk -no PKNAME` for this walk: PKNAME is single-valued, so a mirrored root on md RAID1 resolves to one leg and leaves the other eligible, and a striped LV over N PVs protects one. PKNAME is also empty when queried against a dm or md node directly. `lsblk -sno KNAME` or a `slaves/` recursion both handle the multi-parent case; a single-valued column cannot.
- `blockdev --getro` reports read-only, or `/sys/block/<kname>/ro` is `1`.

**Media type:**

The underlying ATA semantics and smartctl's JSON encoding of them are **different, and inverted relative to each other**. Get this wrong and the tool either admits SSDs or refuses every drive.

- ATA IDENTIFY word 217: `0x0000` = rate not reported, `0x0001` = non-rotating media (solid state), `0x0401` and above = nominal RPM.
- `smartctl --json=c`, which is what this tool actually parses, does **not** mirror that. It emits `"rotation_rate": 0` **for a solid-state device**, and **omits the key entirely** when the rate is not reported.

The rule, stated against the JSON because that is the input:

- `rotation_rate` present and `== 0` → solid state → **INELIGIBLE**.
- `rotation_rate` present and `> 0` → spinning drive, record the RPM.
- `rotation_rate` **absent** → unknown → **eligible**, record `rotation_rpm: null`.
- Text output (`smartctl -i`) showing `Rotation Rate: Solid State Device` → INELIGIBLE, same as the JSON case.

Refusal requires **positive evidence of solid state**, never absence of evidence of rotation.

Note on `/sys/block/<kname>/queue/rotational`: it is **advisory only and frequently wrong for USB**. Many bridges report `1` for everything including SSDs, and a few report `0` for spinning drives. Log it, show it in inventory, and never let it alone decide eligibility. When `smartctl` and sysfs disagree, trust `smartctl` and record the discrepancy.

Writing the media rule as "require positive evidence of rotation" would make every SMART-blocked bridge (§6.2) and every test loop device (§14) unreachable, which is why it is written the other way.

### 4.2.1 Test mode

`--test-mode` exists solely so §14 can exercise the destructive paths. It relaxes exactly two things and nothing else:

- Loop devices (`/dev/loop*`) and device-mapper targets become eligible.
- The `by-id` requirement in §4.1 and §4.5 is satisfied by the synthetic-identity path defined in §4.1.

It does **not** relax any other rule in §4.2: a mounted, held, swapped, fstab-referenced, or root-backing loop device is still refused. The flag refuses to run at all if any target is not a loop or dm device, so it cannot be used to widen access to a real disk. It is the only exception to §4.3's "no flag relaxes §4.2," and it is scoped this narrowly for that reason.

**`--test-mode` may not be combined with `--all`.** Refuse the combination with a specific message. A stock Ubuntu Server 24.04 box runs snapd, which keeps every installed snap mounted through a loop device — commonly 15 to 25 of them, all `/dev/loopN`. `--test-mode --all` would sweep the entire set into a batch. Every one of them is refused correctly, on both the mounted and the read-only rules, so this is not a data-loss path; it is a usability trap that produces a screen of refusals and teaches the operator to ignore them. Test-mode targets must be named explicitly with `--device`. §14 test 14 asserts both the refusal of the combination and the individual refusal of a mounted snap loop device.

### 4.3 Bus type is informational, not a gate

Because SATA is a first-class supported path, the tool does **not** require a disk to be USB. The eligibility rules in §4.2 are the real protection and they are what actually distinguishes "a drive to sell" from "a drive in use."

Bus type is still recorded, displayed in the manifest, and any **non-USB** target is marked in the manifest with a visible `[INTERNAL BUS]` tag so the operator sees it at confirmation time.

`--usb-only` is offered as a convenience gate for operators who want belt and braces on a given run. Apart from the narrowly scoped `--test-mode` (§4.2.1), there is deliberately **no** flag that relaxes §4.2.

Rationale for not defaulting to USB-only with an override flag: the operator will be using SATA regularly, so they would pass the override on every run within a week, and a safety flag that is always passed is not a safety flag.

### 4.4 Confirmation gate

No device is opened for writing until all of the following have happened.

**Step 1: manifest.** Print, for every drive in the batch: by-id name, kernel name, model, enclosure serial, ATA serial, capacity, bus type with the `[INTERNAL BUS]` tag if applicable, current partition table and filesystem labels found, and the byte count that will be overwritten. Print the total across the batch and the estimated wall-clock (§7).

**Step 2: token.** Compute a confirmation token as `DP-<N>-<first 4 hex of SHA1 of the sorted list of per-drive identity strings>`, for example `DP-4-A7F3`. Print it after the manifest. Require the operator to type it exactly, case-insensitive and whitespace trimmed.

**The token is computed over the phase-0 eligible set** — the drives that passed §4.2 at inventory — **not over the set that survives the phase-2 short self-test.** The token's job is to detect a change in *which devices are attached* between planning and confirming, and the phase-0 set captures exactly that. Deriving it from the post-self-test set instead would make it depend on drive health, which is not stable across runs: a drive whose short test passes during planning and fails during execution would change the token and abort the entire batch, which is both the wrong blast radius and a direct defeat of the `--confirm-token` unattended path in step 3.

A drive that fails its short self-test is therefore **skipped at phase 4 without altering the token**, and still emits its own FAIL report per §6.3.

To be reproducible across versions, the hash input is defined exactly: `"\n".join(sorted(identity_strings)).encode("utf-8")`, with no trailing newline, sorted by Unicode code point. `N` is the count of drives in that set.

The per-drive identity string is, in order of preference:

1. The `by-id` name, for any drive that has one.
2. Otherwise the synthetic identifier from §4.1, which covers `--device` drives and every `--test-mode` loop or dm fixture.

Both forms are stable for the lifetime of a batch, so the token is well defined for real drives, escape-hatch drives, and test fixtures alike. The token is derived from the exact target set, so if a drive is plugged in or pulled between planning and confirming, the token no longer matches and the run stops.

**Step 3: flag.** `--execute` must be present. Without it the tool runs in plan mode: full inventory (phase 0), SMART snapshot (phase 1), read-only duration estimate, manifest, token printed, and **no writes**.

Plan mode **does not run the phase-2 short self-test.** It is not destructive, but it takes minutes per drive, it perturbs the drive's self-test log, and — now that the token comes from the phase-0 set — it has no bearing on the token the operator is being shown. Plan mode should be fast enough to run casually.

Under `--execute`, phase 2 runs for every drive **before** the manifest is printed, per §7, so the operator confirms with short-test results in hand. Because the token is phase-0 derived it is identical in both modes, which is what makes a token copied from a plan run usable in the execute run. In plan mode the manifest prints `short test: not run (plan mode)` for every drive, so the operator is not misled into thinking the batch was health-gated.

For unattended scripting, `--confirm-token DP-4-A7F3` pre-answers step 2. It must still match a token recomputed at run time from the live device set, and `--execute` is still required. There is no flag that skips confirmation.

**`resume` is not an exception.** A resumed run writes to devices, so it goes through the identical gate: it rebuilds the manifest from the stored state files, re-runs §4.2 eligibility against the live system, recomputes the token from the drives it is about to resume, and requires it. The token will differ from the original batch's token whenever the resumed set is smaller than the original, which is the correct behavior: a drive that finished, was pulled, or was replaced must not be silently swept back into a destructive run. `resume` also re-runs the §4.5 identity sequence per drive and refuses any drive whose identity tuple no longer matches its checkpoint.

### 4.5 Identity re-verification at open time

Checking identity and then opening the device are two separate operations, and a replug in between is exactly the hazard `by-id` exists to prevent. Checking before the open is not sufficient on its own.

At inventory time, record an **identity tuple** for each drive, sourced from sysfs so it can be recomputed from a bare file descriptor later. The tuple has two distinct halves and conflating them breaks reconnect handling.

**Matching fields.** Three fields are compared for equality and a mismatch aborts. **Their sources depend on the device class**, because loop and dm devices have no `/sys/block/<kname>/device/` directory at all — no `model`, no `vpd_pg80`, and `udevadm` reports no `ID_SERIAL_SHORT` for them. A single SCSI-shaped tuple is not implementable for the §14 fixtures, and every destructive test runs through them.

Resolve the class from sysfs first: `loop/` present → loop; `dm/` present → dm; `device/` present → scsi. Then:

| Class | `size_bytes` | `id_model` | `id_serial` |
|---|---|---|---|
| scsi (SATA and USB) | `/sys/block/<kname>/size * 512` | `device/model` (SCSI INQUIRY model; for a bridged drive this is the **enclosure's** model, e.g. `Elements 25A2`, truncated to 16 characters) | `ID_SERIAL_SHORT` from `udevadm info --query=property --name=<kname>`, falling back to decoding `device/vpd_pg80` |
| loop | same | literal `"loop"` | `loop/backing_file` + `":"` + `loop/offset` |
| dm | same | literal `"dm"` | `dm/uuid`, falling back to `dm/name` |

The tuple is `(class, size_bytes, id_model, id_serial)` and **`class` is part of the comparison**, so a loop device can never satisfy a disk's recorded tuple. `report.json` keeps the field names `sysfs_model` / `sysfs_serial` for the scsi case; §12's `drive.identity` gains a `class` key.

The rest of §4.5 — recompute from the open fd, abort on mismatch — is unchanged and applies identically to all three classes.

**Locator fields.** These are recorded and refreshed but **never** compared for equality:

- `hctl` from the `/sys/block/<kname>/device` symlink target (e.g. `6:0:0:0`)
- USB port path, where applicable, from the same symlink resolution
- current kernel name

Locator fields exist for kernel-log correlation (§6.4), not for identity. A drive that disconnects and comes back, or that the operator moves to a different USB port, will have a different `hctl`, port path, and kernel name while being unambiguously the same drive. Requiring the full tuple to match would abort exactly the reconnect case §9.1 is built to survive. **Every "identity tuple matches" statement in this document means the matching fields above only, never the locator fields.** On any reconnect, re-read the locator fields and use the new values from that point forward.

Loop and dm devices have no locator fields; record them as null and skip the §6.4 kernel-log correlation for those classes.

Note that `/sys/dev/block/<maj>:<min>/device/` has **no `serial` attribute** for SATA or SCSI disks, so the serial must come from `ID_SERIAL_SHORT` or `vpd_pg80`. Note also that `sysfs_model` and the model smartctl reports are **different strings on a bridged drive** and must never be compared to each other; §4.5 compares sysfs to sysfs only.

Required sequence, run for every device open, including every resume and every reopen after an I/O error:

1. `os.open()` the **`/dev/disk/by-id/` path** directly, not a resolved `/dev/sdX`. Use `O_RDWR | O_DIRECT | O_EXCL` for writing, `O_RDONLY | O_DIRECT | O_EXCL` for verifying. For a drive with no `by-id` entry (§4.1 synthetic identity, and every `--test-mode` fixture), open the `/dev/sdX` or `/dev/loopN` path given on the command line and rely entirely on step 3, which is what actually establishes identity.
2. Handle `EBUSY`. **Be precise about what `O_EXCL` actually does on a block device:** it fails only against a *kernel claim* on the device (a mount, an md or dm target holding it, a swap area) or against **another opener that also used `O_EXCL`**. It does **not** conflict with an ordinary non-exclusive opener, so a stray `dd of=/dev/sdX` running in another terminal will not produce `EBUSY`. `O_EXCL` is therefore a useful guard against the kernel-claim and second-DrivePrep-instance cases and nothing more; it is not a general "nobody else is touching this disk" assertion, and §4.2's checks remain the primary protection. Treat `EBUSY` as a fatal abort for that drive, never as retryable.
3. On the **open file descriptor**, `os.fstat()` and read `st_rdev`. Convert to major:minor, resolve through `/sys/dev/block/<maj>:<min>/`, and recompute the full identity tuple for that device's class. Abort unless every matching field agrees with what inventory recorded.
4. **Re-run the complete §4.2 rule set** against the kernel name resolved in step 3. Not a subset. Hours pass between phase 0 and phase 4 — short self-tests plus operator confirmation — and every §4.2 check is a cheap sysfs or procfs read, so there is no reason to re-check only the mounted and swap cases. A disk that gained an LVM PV, was added to an md array, or was written into `fstab` during the confirmation window is caught here and nowhere else. `udisks2` auto-mounting a drive the moment it is plugged in (Appendix A) is the common instance of this race, but it is not the only one.

Step 3 is the check that matters, because it interrogates the object that is about to be written rather than a name that may since have been reassigned. It works identically for `by-id` drives, `--device` drives, and test fixtures, which is why the by-id path in step 1 is a convenience rather than the security boundary.

### 4.6 Bounds and write-protection

- Never write past the device length reported by `BLKGETSIZE64` (or `/sys/block/<kname>/size` multiplied by 512, which is always in 512-byte units regardless of the drive's logical block size; do not multiply by the logical block size here, that is a classic 8x overrun bug on 4Kn drives).
- The final chunk is clamped to the remaining byte count and rounded up to the **logical** block size. Device length is always an exact multiple of the logical block size, so this never overruns.
- `EROFS`, `EACCES`, and `EPERM` on write are **fatal aborts**, never media errors. They mean the device or its bridge is write-protected, not that the platters are bad. Never let them fall into the §9 record-and-continue path, or a configuration problem gets reported as a drive with millions of bad sectors.

---

## 5. Erase method

**Single-pass zero fill across the entire device, followed by a full-surface verification read.**

Rationale, to be reflected in the report's methodology footer: for modern magnetic recording, a single overwrite pass renders data unrecoverable by any known practical technique. This corresponds to NIST SP 800-88 Rev. 1's "Clear" level for magnetic media. Multi-pass schemes are historical artifacts that would multiply a 20-hour job for no benefit.

Implementation:

- Pattern is `0x00` for the entire device, LBA 0 through the last block, including any partition table, the GPT backup header at the end of the disk, and all slack.
- **The tool never unmounts anything and never calls `swapoff`.** A mounted partition or an active swap area makes the drive INELIGIBLE per §4.2, full stop. An earlier draft had a pre-flight unmount step here; that directly contradicted §4.2 and would have permitted a tool that quietly unmounts the operator's live data disk in order to erase it. If the operator genuinely wants to erase a mounted drive, they unmount it themselves and re-run. The one-line console message on refusal should say exactly that.
- After the erase completes, tell the kernel to drop the now-nonexistent partition table. **Order matters and the obvious order fails.** `BLKRRPART` requires an exclusive claim on the device, so it returns `EBUSY` for as long as this tool still holds the descriptor it opened `O_EXCL`. The sequence is: `os.fsync(fd)` → `os.close(fd)` → issue `BLKRRPART` via `fcntl.ioctl` on a fresh short-lived `O_RDONLY` descriptor. Do not shell out to `blockdev --rereadpt`, and do not call `partprobe`: it lives in the `parted` package, which is not a dependency, and it adds nothing over the ioctl. A failure here is logged and **non-fatal** — the erase already succeeded, and a stale in-kernel partition table does not affect the verify, which addresses the whole-disk device by offset.
- Record start time, end time, bytes written, and throughput samples.

### 5.1 Encrypted bridge caveat

Many WD My Book units implement hardware AES in the USB bridge. Zeros written through such a bridge are stored on the platters as the ciphertext of zeros, not as literal zeros. This is still a complete and correct sanitization of the user data, and a verification read through the same bridge is the meaningful verification. State this in one sentence in the report methodology footer when the drive is a My Book class unit. Do not attempt to detect or defeat the encryption layer.

### 5.2 Why not ATA Secure Erase

Excluded from v1 on both bus types, for different reasons:

- **Over USB:** most bridges do not pass ATA security commands through. A partial attempt can leave a drive in a security-locked or frozen state that is annoying to recover.
- **Over SATA:** it genuinely works and is the NIST "Purge" level. It is excluded anyway because drives are almost always `frozen` by the BIOS at boot, requiring a suspend/resume cycle to clear, and a mishandled `--security-set-pass` leaves a password-locked drive that looks bricked. That is a bad failure mode for an unattended batch job.

Note this decision in the methodology footer. If the operator later wants Purge-level sanitization on SATA drives, that is a v2 conversation with its own safety design, not something to slip into this build.

### 5.3 SMR caveat

Some later WD Elements and Blue drives, particularly 2.5-inch units at 4 TB and above, use shingled magnetic recording. A full-device sequential write on SMR is slow and shows large throughput swings as the drive's media cache saturates. This is normal and is **not** a fault. Report throughput as min, mean, and max, and keep throughput out of the grading rubric entirely.

---

## 6. Health testing

Four sources of evidence, all captured.

### 6.1 SMART attributes

Snapshotted before and after the run, with a delta. Store both the parsed JSON and the raw human-readable output, because raw `smartctl -x` output is what a skeptical buyer will ask to see.

Device type detection. Branch on bus type first, then probe.

**For SATA devices** (`ata-*` by-id name, or `bus_type == "ata"`):

```
smartctl -i                <dev>    # no -d; succeeds on essentially every SATA drive
smartctl -i -d sat         <dev>    # fallback, e.g. behind some HBAs
```

**For USB devices**, in this order. Note that `--scan-open` is a **global** scan, not a per-device query: run it once in the parent at inventory time and share the parsed result with every child, rather than invoking it once per drive in a parallel batch.

```
smartctl --scan-open                 # consult the bundled USB VID/PID bridge database
smartctl -i -d auto        <dev>
smartctl -i -d sat         <dev>
smartctl -i -d sat,12      <dev>
smartctl -i -d usbjmicron  <dev>
smartctl -i -d usbprolific <dev>
smartctl -i -d usbsunplus  <dev>
```

**A parseable response is not an acceptance test.** A wrong passthrough type, `sat,12` above all, frequently returns structurally valid but fabricated IDENTIFY data rather than an error. Caching such a type would feed invented values straight into the grade.

Accept a probe only when both hold:

- **Capacity agrees with sysfs within one percent.** This is the load-bearing check and it is reliable on every bus.
- **The IDENTIFY response is plausible**: non-empty model, non-empty firmware revision, non-empty serial, and a capacity that is not zero.

**Do not require the smartctl model string to match the sysfs model.** On a USB-bridged drive these are legitimately different strings: sysfs reports the bridge's SCSI INQUIRY model (`Elements 25A2`, capped at 16 characters) while ATA IDENTIFY through the bridge returns the drive's own model (`WD40EZRZ-00GXCB0`). The §12 example record shows both. A model-equality requirement would reject every single bridged drive and route them all to the "SMART unavailable" path, defeating the purpose of the probe. On SATA the two do generally agree, and a mismatch there is worth logging, but it is never grounds for rejection.

Serial likewise differs between enclosure and drive on bridged units and is never a rejection criterion.

If no candidate passes, treat SMART as unavailable per §6.2 rather than using the least-bad guess.

Use `smartctl --json=c` for parsing and `smartctl -x` for the archived text. Also capture `-l selftest`, `-l xerror`, `-l devstat`, and `-l scterc`. Cache the winning `-d` value in the run state.

### 6.2 When SMART does not pass through

Some bridges, notably older WD My Book boards, block SMART entirely. Then:

- Record `smart.available: false` along with the full probe log.
- Continue with the erase and the full-surface read, which do not depend on SMART.
- The report shows the SMART section as "not available through this drive's USB bridge," never blank and never zeroed.
- The overall grade caps at **CAUTION**, never PASS, because power-on hours and reallocated sector counts are unknown. Say exactly that, in those words, as the grade reason.
- Print a console suggestion (not in the report) that removing the drive from its enclosure and connecting it to SATA would yield full SMART data. On this build that is a realistic fix, since SATA is supported.

### 6.3 Self-tests

- **Short test** (`smartctl -t short`), polled every 30 seconds. Typically under 5 minutes.
- **Gate:** if the short test fails, abort that drive before spending hours erasing it, and still emit a report graded FAIL with the reason. This is the highest-value optimization in the pipeline and the main reason phase 2 exists. The drive is skipped, the rest of the batch proceeds, and the batch token is unaffected (§4.4). Its report carries `erase.performed: false` and the §11.2 "NOT ERASED" notice, because the drive still holds its original data.
- **Extended test** (`smartctl -t long`), polled every 5 minutes, run after the erase and verify. Use the drive's own polling-time estimate from `-c` as the expected duration; warn past 150 percent of it.
- Some bridges accept `-t` but never update the self-test log. If the self-test log shows no progress change for 3x the estimated duration, mark the test `inconclusive`, not failed, and say so in the report.
- A self-test cannot run concurrently with heavy host I/O without skewing its own timing, so do not overlap phases 5 and 6 on the same drive.

### 6.4 Kernel log evidence

This is a Linux-specific signal worth capturing and is genuinely useful in a listing.

Record the run's start timestamp. At each phase boundary, collect `journalctl -k --since <run start> --output=short-iso` and filter for the device.

**Anchor the filter on the SCSI `H:C:T:L` address and the USB port path, not on the `by-id` name.** Kernel messages never contain by-id strings; a `blk_update_request` or `Buffer I/O error` line carries only the kernel name, while driver-level messages are prefixed with the SCSI address (`sd 6:0:0:0: [sdc] ...`). Capture the `H:C:T:L` and port path from the `/sys/block/<kname>/device` symlink target (§4.5's locator fields) and match on those plus the kernel name observed during the run window.

**Maintain a list of locator epochs, not a single value.** A disconnect and reconnect (§9.1) usually changes the kernel name and often the `H:C:T:L`, so a locator captured once at first open stops matching partway through, and the tool silently under-reports exactly the I/O errors this section exists to surface. On every open and every reconnect, append the current `(hctl, port_path, kernel_name, valid_from, valid_until)` to the drive's epoch list in the checkpoint, and sweep the kernel log against **every** epoch, each bounded to its own time window. That bounding is what keeps a reused kernel name from pulling in another drive's messages.

Classify and count:

- **I/O errors** (`blk_update_request: I/O error`, `Buffer I/O error`, `critical medium error`) → these are drive faults and contribute to FAIL.
- **USB resets and disconnects** (`reset high-speed USB device`, `usb disconnect`, `device descriptor read/64, error`) → these are usually cable, port, hub, or power problems rather than platter problems. Contribute to CAUTION with an explicit note saying so.
- **UAS or SCSI aborts and timeouts** (`uas_eh_abort_handler`, `task abort`, `Device offlined`) → CAUTION, with the same cable-or-bridge annotation.

Store the matched lines verbatim in `kernel-log.txt`. Show counts by category on the report, not the raw lines.

### 6.5 Thermal guard

A stack of drives running full-surface writes in a pile will get hot, and cooking a drive during the test is a self-inflicted wound.

Poll SMART temperature (attribute 194, or `temperature.current` in the JSON) every 60 seconds during phases 4, 5 **and 6**. Phase 6 is the extended self-test: 8 to 10 hours of continuous full-surface activity on a 4 TB drive, which is the longest hot stretch of the whole pipeline and the one an earlier draft left unmonitored. Behavior:

- Record the maximum temperature reached across the run and show it on the report.
- Above **50 C**: warn on the console and in the log.
- Above **55 C**: pause I/O for that drive, log the pause, and resume when it drops below **45 C**. Record total paused time and include it in the report as a note.
- Above **60 C** for more than 5 consecutive minutes despite pausing: abort that drive, grade the run `incomplete`, and tell the operator to improve airflow. Do not grade a thermally aborted drive as FAIL; the drive is not necessarily bad, the test conditions were.
- If temperature is unavailable (SMART blocked), skip the guard and note in the report that thermal monitoring was not possible.

During phase 6 the pause action is not available — a self-test is running on the drive itself and cannot be throttled by withholding host I/O. There the guard degrades to: record the maximum, warn at 50 C, and at 55 C abort the self-test with `smartctl -X`, mark the extended test `inconclusive` (not failed, per §6.3), and note the thermal reason in the report. The 60 C abort still applies.

Thresholds live in `config/grading.toml` alongside the rubric.

---

## 7. Pipeline

Per drive, in order. Each phase writes its result to the drive's state file before the next begins, so a crash is resumable.

| # | Phase | Destructive | Typical, 4 TB |
|---|---|---|---|
| 0 | Inventory and eligibility | No | seconds |
| 1 | SMART snapshot: before | No | seconds |
| 2 | SMART short self-test (gate) | No | ~2 min |
| 3 | Confirmation gate (§4.4, batch-wide) | No | operator-bound |
| 4 | Zero fill, LBA 0 to end | **Yes** | 6-9 hr |
| 5 | Full-surface verification read | No | 6-9 hr |
| 6 | SMART extended self-test | No | 7-10 hr |
| 7 | SMART snapshot: after, plus delta; kernel log sweep | No | seconds |
| 8 | Grade, render HTML, render PNG | No | seconds |

Roughly 20 to 28 hours for a 4 TB drive. Phases 0 to 2 run for every drive in the batch **before** the single batch-wide confirmation at phase 3, so the operator confirms once with full information including short-test results, then leaves.

**The duration estimate must be computed without writing anything.** Phase 3 is the confirmation gate, so any calibration write before it would put bytes on the platters, including LBA 0, before the operator has confirmed. Derive the estimate from a **read-only** 1 GB sequential read from the middle of the device, bounded against the Appendix B table. Refine it in place once phase 4 has been running for a minute on real measured write throughput.

`--skip-extended-test` cuts phase 6. A report produced without it is visibly marked and grades **CAUTION** per §10. There is no fourth grade.

---

## 8. CLI

Single entry point, `driveprep`.

```bash
# Inventory. Every attached disk with eligibility and per-disk reasons. Never writes.
sudo driveprep list

# Plan a batch. Full inventory, SMART, estimates, manifest, token. No writes.
sudo driveprep run --all

# Execute a batch of every eligible drive, 4 at a time.
sudo driveprep run --all --execute

# Specific drives.
sudo driveprep run --id usb-WD_Elements_25A2_575834314235-0:0 --execute

# Unattended, pre-confirmed.
sudo driveprep run --all --execute --confirm-token DP-4-A7F3 --jobs 4

# Resume everything interrupted in a previous batch. Re-confirms (see below).
sudo driveprep resume --execute

# Rebuild reports from stored state. Never touches a device.
sudo driveprep report --all
```

| Flag | Notes |
|---|---|
| `--id <by-id name>` | Repeatable. Primary selector. |
| `--device /dev/sdX` | Escape hatch for drives with no by-id entry (§4.1). Refused in queue mode unless `--test-mode` is set. |
| `--test-mode` | §4.2.1. Permits loop and dm devices only. Refuses to run if any target is a real disk. Requires explicit `--device`; cannot be combined with `--all`. |
| `--all` | Every eligible drive. Mutually exclusive with `--test-mode`. |
| `--execute` | Required for any write. |
| `--confirm-token` | Pre-answers the confirmation. Recomputed and matched at run time. |
| `--jobs N` | Concurrency. Default 4. See §8.1. |
| `--chunk-size` | Transfer chunk, default 8 MiB (§9). Must be a multiple of both block sizes; validated per drive at open, since the correct multiple is not known until the device is inspected. |
| `--usb-only` | Convenience gate, §4.3. |
| `--skip-extended-test` | Flagged in the report, grades CAUTION. |
| `--mask-serial` | Redact the middle of serials in the rendered report. Off by default. |
| `--output-root` | Default `/var/lib/driveprep`. |
| `--seller-name` | Optional line on the report. |

### 8.1 Concurrency and unattended operation

- Default `--jobs 4`. One OS process per drive, supervised by a parent that owns the queue, the manifest, and the batch index. Do not thread the I/O.
- **Take a per-drive lock for the whole lifetime of a drive's pipeline**, not just while its descriptor is open: `flock(LOCK_EX | LOCK_NB)` on `<output-root>/<id>/.lock`, acquired at phase 0 and held to phase 8. `O_EXCL` only protects phases 4 and 5; phases 0 through 3 hold no descriptor at all, so without this two operators — or one operator and a forgotten `systemd-run` unit — can plan the same drive concurrently and race each other's `state.json`. Refuse a locked drive by name and keep the rest of the batch going.
- Warn when more than 4 drives share a single USB host controller. Read the topology by resolving `/sys/block/<kname>/device` up to the `usbN` root hub and grouping.
- Warn when the batch contains more than 2 bus-powered 2.5-inch drives on one hub, which is a common cause of mid-run brownouts and USB resets. Self-powered 3.5-inch desktop units do not have this problem.
- Ship a systemd unit template, `driveprep@.service`, and document `systemd-run --unit=driveprep --collect driveprep run --all --execute --confirm-token ...` so an SSH disconnect does not kill a 24-hour batch. Mention `tmux` as the informal alternative.
- The parent process must survive a child crash: mark that drive failed with the reason, keep the rest of the queue running, and include the failure in the batch index.

### 8.2 Signals and shutdown

`systemd-run` is the recommended launcher, and `systemctl stop` delivers `SIGTERM` to every process in the cgroup, so this path is the most likely way a real batch ends early. It must be defined, not left to Python's default.

- **`SIGTERM` / `SIGINT` in a child:** stop at the next chunk boundary, write a full §9.1 checkpoint including accumulated findings, `fsync` and close the descriptor, and exit non-zero with a distinct code. Do not attempt to finish the phase. Do not skip the checkpoint — this is precisely the interruption §9.1's findings requirement and §14 test 5 exist to protect, and a signal path that bypasses it reintroduces the PASS-on-a-failing-drive bug through the back door.
- **`SIGTERM` / `SIGINT` in the parent:** stop dispatching new drives, forward the signal to every live child, wait up to 60 seconds for them to checkpoint, then `SIGKILL` any stragglers. Write the batch index with the surviving state.
- A second `SIGINT` within 5 seconds escalates immediately to `SIGKILL`, for the operator who genuinely wants out now. Say so on the console at the first one.
- Every drive interrupted this way grades **INCOMPLETE** (§10.2), never FAIL, and its report is marked not usable in a listing. An interrupted run is an unfinished measurement, not evidence about the drive.
- `resume` picks these up normally, through the full §4.4 gate.

---

## 9. Block I/O implementation

Python 3, `os` level, no third-party dependency.

```python
FLAGS = os.O_RDWR | os.O_DIRECT | os.O_EXCL   # writing
FLAGS = os.O_RDONLY | os.O_DIRECT | os.O_EXCL # verifying
```

**`O_DIRECT` is required, not optional.** Without it the verification read can be served from the page cache, which makes the verify partly circular and the throughput numbers fictional.

Requirements:

- **Buffer:** allocate with `mmap.mmap(-1, CHUNK)`. Anonymous mmap is page aligned, which satisfies `O_DIRECT`'s alignment requirement with no `ctypes` work. This is the single biggest simplification Linux buys here; do not reach for `ctypes` or `posix_memalign`.
- **Transfer calls: `os.preadv(fd, [buf], offset)` and `os.pwritev(fd, [buf], offset)`.** This is mandatory, not stylistic. `os.read(fd, n)` and `os.write(fd, some_bytes)` both fail with `EINVAL` on an `O_DIRECT` descriptor, because CPython's `bytes` payload is not page aligned. Only the vectored calls writing into and out of the mmap satisfy the alignment requirement. Using positional `preadv`/`pwritev` also removes any dependence on the file offset, which makes resume and retry-at-offset trivial.
- **Loop on short transfers.** `preadv` and `pwritev` may return fewer bytes than requested, and reliably do on the tail chunk at end of device. Advance by the returned count and repeat until the chunk is complete or the device end is reached. Treat a return of 0 before the expected end as an error, not as EOF.
- **Chunk size:** 8 MiB default, configurable. Must be an exact multiple of both block sizes.
- **Block sizes:** read `/sys/block/<kname>/queue/logical_block_size` and `physical_block_size`. Do not assume 512.
  - Transfer length and file offset must be multiples of the **logical** block size. That is what `O_DIRECT` constrains.
  - **LBA numbers** in the `nonzero_ranges` and `read_error_ranges` this tool computes are `byte_offset // logical_block_size`. Using the physical size makes every reported LBA wrong by 8x on an advanced-format drive, which is exactly the kind of error a buyer will catch. Note that `lba_of_first_error` in the self-test blocks comes from smartctl and is passed through unmodified.
- **Device size:** `ioctl(fd, BLKGETSIZE64)` via `fcntl.ioctl` with a packed `c_uint64`. Cross-check against `/sys/block/<kname>/size * 512` and abort on disagreement. Remember that the sysfs `size` file is always in 512-byte units even on 4Kn drives.
- **Zero check:** compare `memoryview(buf)[:n] == ZERO_BLOCK[:n]`, where `ZERO_BLOCK = bytes(CHUNK)` and `n` is the number of bytes actually read.

  Both halves of that expression matter:
  - **`memoryview(buf)`, not `buf`.** An `mmap` object has no rich comparison against `bytes`, so `mmap_obj == bytes(CHUNK)` evaluates to `False` unconditionally. Written that way, every sector on every drive reads as nonzero and **every drive grades FAIL**. Wrapping in `memoryview` gives a real content comparison that returns `True` for a zeroed buffer.
  - **Slice to `n` on both sides.** The tail chunk is short, so comparing a full-size `ZERO_BLOCK` against a partially filled buffer mismatches on length alone.

  CPython dispatches the sliced memoryview comparison to `memcmp`, which runs at memory bandwidth. Never loop per byte in Python; that alone would take longer than the disk read. On a mismatch, narrow within the chunk to produce exact byte ranges.
- **Error handling:** an `OSError` with `errno.EIO` at offset X must not abort the pass. Record the byte range, seek past the failing chunk, and continue, so the report can state total bad regions rather than "failed at 1.2 TB." On repeated `EIO`, optionally re-read the failing chunk at logical-block granularity once, to narrow the bad region from 8 MiB to the actual sectors. Cap recorded ranges at 1000 entries, then summarize, so a totally failed drive does not produce a gigabyte of JSON.
  - `EROFS` / `EACCES` / `EPERM`: fatal, per §4.6.
  - `ENODEV` / `ENXIO`: the device vanished, usually a USB drop. Checkpoint, then wait up to 60 seconds for the device to return: poll the `by-id` path where one exists, and otherwise poll for any block device whose §4.5 identity tuple matches the recorded one, since the kernel name will very likely have changed. Re-run the full §4.5 sequence before resuming. Count the event in the report as a disconnect. Give up after 3 reconnects in one phase.
- **Progress:** bytes done, percent, instantaneous and mean MB/s, ETA. Update at most once per second to the console and to `progress.json`. **`progress.json` is not the checkpoint.** It is a small, frequently rewritten, deliberately disposable file that exists so the parent can render its per-drive status table without reading a child's checkpoint; losing or corrupting it costs nothing. `state.json` is the durable checkpoint and is written on the §9.1 schedule only. Keeping them separate is what stops a once-per-second atomic rewrite of the full findings structure, and it resolves the cadence conflict between this bullet and §9.1.
- **fsync:** call `os.fsync(fd)` at the end of the write phase and before closing, even with `O_DIRECT`, so any bridge-level cache is committed before the verify begins.

### 9.1 Checkpoint

`<output-root>/<id>/state.json`, written atomically (write to `.tmp`, `os.replace()`) every 30 seconds and at every phase boundary. This is the durable record; the once-per-second `progress.json` from §9 is a separate, disposable file and is never read on resume.

Must include:

- Current phase and byte offset within it
- Resolved smartctl `-d` type
- by-id name, enclosure serial, ATA serial, model, capacity, both block sizes
- The §4.5 identity tuple: `class` plus the three matching fields, sourced per that section's table, so a resume can re-verify identity without trusting the kernel name
- The locator epoch list from §6.4, appended to on every open and reconnect
- Monotonic run id and batch id
- **Accumulated findings so far:** `verify.read_error_ranges`, `verify.nonzero_ranges` and their counts, erase throughput samples, max temperature, thermal pause seconds, disconnect count, and the kernel-log event counts so far.

That last item is not optional. §10 grades FAIL on any read error or any nonzero sector, so if findings are not checkpointed and re-hydrated on resume, a run interrupted at 40 percent that had already hit 30 read errors resumes with a clean slate and grades **PASS on a failing drive**. That is the single most dangerous bug available in this design, because it produces a confident, wrong document that a buyer relies on. Test 4b in §14 exists specifically to catch it.

On resume, restart the interrupted phase from the last recorded offset rounded **down** to a chunk boundary, after re-running the §4.5 identity sequence.

**The recorded offset belongs to one phase only.** It is valid solely when the phase being entered is the phase the checkpoint was already in. Entering a *new* phase always starts at offset 0. Getting this wrong is not a cosmetic error, and it was found on real hardware rather than by any test here:

> Phase 5 was entered using the offset left behind by the completed phase 4 — the end of the device. The verification pass therefore began at the last chunk, read 4 MB of a 500 GB drive, and stopped. And because `verify_zero()` sets `bytes_done = start_offset` on entry, the findings then reported the **full capacity as covered**. The report stated *"Full-surface verification read confirmed all 500,107,862,016 bytes read back as zero, with 0 read errors"* with a duration of **0 seconds**.

That is a positive, fabricated sanitization claim — strictly worse than the missing-claim case §11.2 guards against, because every individual field was self-consistent and the grade looked plausible. The only visible tell was the impossible duration.

Two defences are required, not one:

1. Phase entry computes its start offset as "resume only within the same phase, otherwise 0."
2. **`verify.performed` is set only if the pass actually reached the end of the device.** It drives a positive claim on the report, so short coverage — for any reason — must leave it `false` and the report must decline to make the claim. Never infer coverage from `bytes_done` alone: that field is a position, not a measure of work done, and it cannot distinguish a completed pass from one that started at the end.

---

## 10. Grading

Deterministic, published in the report itself, driven from `config/grading.toml` so thresholds can be tuned without editing code.

TOML rather than YAML: §2 commits to a zero-dependency runtime, and Python 3.12 ships `tomllib` in the standard library while it ships no YAML parser. `tomllib` is read-only, which is all the rubric needs, and TOML keeps comments — which matters for a file the operator is expected to read and tune.

### 10.1 Which number the rubric reads

Every SMART attribute has a normalized `value` (typically starting at 100 or 200 and counting **down** toward `thresh`) and a vendor-specific `raw`. Getting this backwards silently breaks the entire rubric.

- Attributes 5, 10, 187, 188, 196, 197, 198, 199 are evaluated on the **raw** value. Reading attribute 5's normalized value instead would compare a healthy drive's `200` against `> 0` and grade every drive CAUTION.
- Power-on hours: use smartctl's decoded `power_on_time.hours` from the JSON, not the raw integer. Attribute 9's raw field is vendor-encoded; some firmware reports minutes or seconds, and some packs several counters into the 48-bit raw. If the decoded value is absent or implausible (over 200000), record hours as unknown and do not let the threshold fire.
- Attribute 188 (Command Timeout) packs three counters into its raw field on many drives; evaluate the low 16 bits.
- The generic threshold test is `thresh > 0 and value <= thresh`. Many informational attributes carry `thresh = 0` meaning "no threshold." Simplest correct implementation: consume smartctl's own `when_failed` field and treat any non-empty value as the trigger.

### 10.2 Conditions

**FAIL** if any of:

- SMART overall-health self-assessment reports FAILED
- The short self-test failed (this aborts before phase 4 and still produces a report)
- The extended self-test completed with a read failure, servo failure, or handling damage
- Current Pending Sector (197 / 0xC5) raw > 0
- Offline Uncorrectable (198 / 0xC6) raw > 0
- Reported Uncorrectable Errors (187 / 0xBB) raw > 0
- Any read error encountered during the full-surface read
- Any nonzero sector found during verification, which means the write did not take
- Any kernel-log I/O error or medium error for this device during the run (§6.4)

**CAUTION** if any of the following and no FAIL condition is met:

- Reallocated Sector Count (5) raw > 0, or Reallocated Event Count (196 / 0xC4) raw > 0
- Spin Retry Count (10) raw > 0
- Command Timeout (188 / 0xBC) low 16 bits > 0
- UDMA CRC Error Count (199 / 0xC7) raw > 0, annotated as usually a cable or bridge symptom rather than a platter symptom
- Any USB reset, disconnect, or UAS abort in the kernel log (§6.4), same annotation
- Decoded power-on hours > 40000 (skipped if hours are unknown)
- Any attribute flagged failing per §10.1's threshold rule
- Maximum temperature exceeded 55 C, or the thermal guard paused the run (§6.5)
- SMART unavailable through the bridge (§6.2)
- Extended test skipped or inconclusive

**PASS** if none of the above.

**INCOMPLETE** is a separate outcome, not a grade: the run did not finish (thermal abort, too many disconnects, operator interrupt). An incomplete run renders a report clearly marked incomplete, with no PASS/CAUTION/FAIL badge, and must not be used in a listing. `grade.value` is one of exactly `PASS`, `CAUTION`, `FAIL`, `INCOMPLETE`.

Rules:

- The grade is computed, never operator-supplied. There is no override flag.
- Every raw number that fed the grade is printed on the report regardless of outcome, so a buyer can form their own judgment.
- When a threshold tipped the grade, name it explicitly: `"CAUTION: Reallocated Sector Count = 8"`.

---

## 11. Reports

### 11.1 Artifacts

Per drive, in `<output-root>/<id>/`:

| File | Purpose |
|---|---|
| `report.html` | Self-contained. Inline CSS, no external fetches, no CDN fonts. |
| `report.png` | The listing image. |
| `report.json` | Full structured record. |
| `report-print.html`, `report.pdf` | Two-page document to ship with the drive: the report, then buyer setup instructions. Separate from `report.png`, which is a listing photo and must stay a single image. Both pages use the same fixed 1200x1600 sheet, and the shared stylesheet lives in `templates/report.css` so the printed page and the listing image cannot drift apart. |
| `smart-before.txt`, `smart-after.txt` | Raw `smartctl -x` plus the log sections from §6.1. |
| `kernel-log.txt` | Matched kernel lines for the run window. |
| `run.log` | Timestamped full log. |
| `state.json` | Checkpoint, retained after completion. |

Per batch, in `<output-root>/batches/<batch-id>/`: `index.html` and `index.json` summarizing every drive, its grade, and a link to its report. Useful for a 12-drive run.

### 11.2 HTML layout

Legible as an eBay thumbnail first, readable at full size second. Portrait, **1200 x 1600 CSS px, fixed**.

Top to bottom:

1. **Header band.** Manufacturer, model, capacity as a seller would advertise it ("4 TB"), form factor. Large type, readable at thumbnail size.
2. **Grade badge.** PASS / CAUTION / FAIL, large and high contrast, with the word carrying the meaning so it survives grayscale and color-blind viewing. Color is reinforcement, never the sole signal.
3. **Four headline stat tiles**, largest type after the header: Power-On Hours, Power Cycles, Reallocated Sectors, Pending Sectors. These are what an informed buyer scans for.
4. **Erase block.** Method ("Single-pass zero overwrite, full device"), bytes written, start and end timestamps, duration, throughput min/mean/max, and the verification outcome as a plain sentence: "Full-surface verification read confirmed all 4,000,787,030,016 bytes read back as zero, with 0 read errors."

   When `erase.performed` is `false` (§12), this block is replaced by a high-contrast notice reading **"NOT ERASED — this drive failed its pre-erase health test and was not written to. Any previous data is still present."** It must be as prominent as the grade badge, never a footnote. The same applies to the verification sentence when `verify.performed` is `false`. This is the one place where a rendering shortcut turns an honest report into a false claim about data destruction.
5. **Self-test block.** Short and extended results, completion status, and for a failure the LBA of first error.
6. **Run conditions block.** Max temperature, thermal pauses if any, disconnect count, and kernel-log event counts by category. This is the section that distinguishes an honest report from a screenshot of CrystalDiskInfo.
7. **SMART table.** ID, attribute name, value, worst, threshold, raw, for the full attribute set rather than a curated subset. Small type is fine.
8. **Methodology footer.** Three or four sentences: what was done, the NIST 800-88 "Clear" alignment, why ATA Secure Erase was not used, the hardware-encryption note from §5.1 where applicable, and an explicit statement that this is seller-generated documentation and not a third-party certification.
9. **Footer line.** Tool name and version, report ID, generation timestamp, optional seller name.

Design constraints: light background only, no gradients (they hurt PNG compression), no web fonts, system font stack, high contrast throughout. This ends up as a recompressed image at 400 px wide in someone's browser.

**The layout must fit 1200 x 1600 by construction.** Do not measure `scrollHeight` and re-render at variable height; a variable-size listing image is worse for eBay and makes the output untestable. The only variable-height element is the SMART table, so give it a fixed region sized for 30 rows at the chosen type size, drop one type size step if a drive reports more, and if it still overflows, truncate with a visible "N further attributes, see report.json" line rather than growing the page.

### 11.3 PNG rendering

```
<chrome> --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --user-data-dir=<fresh temp dir> --no-first-run \
  --screenshot=<abs>/report.png --window-size=1200,1600 \
  --default-background-color=FFFFFFFF file://<abs>/report.html
```

`--no-sandbox` is required when running as root, which this tool always is.

`--user-data-dir` pointed at a fresh temp directory, removed afterwards, is also required. Without it Chrome uses `/root/.config/google-chrome` and takes a singleton lock there; two report renders racing at the end of a parallel batch — which is exactly what `--jobs 4` produces — will collide on it, and the loser fails with a profile-lock error that looks nothing like the actual cause.

Binary resolution order:

1. `/opt/google/chrome/chrome` (Google Chrome deb). **Document this as the recommended install** in the README, with the apt repo lines. It is a real deb with no confinement and is the most predictable option on Ubuntu Server.
2. `google-chrome-stable`, `chromium`, or `chromium-browser` on `PATH`.
3. If the resolved binary is the **Ubuntu snap** (`/snap/bin/chromium`), snap confinement blocks reading `file://` paths outside `$HOME` and `/media`. Detect this by resolving the binary path, and when it applies, copy the HTML to a temp directory under the invoking user's home before rendering and copy the PNG back. Do not silently fail here; a snap-confinement `file://` failure produces a zero-byte or missing PNG with no obvious error, which is a miserable thing to debug.

   **"The invoking user's home" is not `$HOME`.** This tool always runs under `sudo`, where `$HOME` is `/root` — which is outside the snap's confinement just as `/var/lib` is, so using it fixes nothing. Resolve `SUDO_USER` and take that account's home from `pwd.getpwnam()`, falling back to `SUDO_UID`. Write as that user (`os.seteuid` in a child, or simply `chown` the temp tree) so the snap can actually read it. If there is no `SUDO_USER` — a true root login — no such directory exists and the snap path cannot work; fall through to the message in the fallback paragraph rather than producing a zero-byte PNG.

Treat a PNG under 10 KB, or one that does not appear within 30 seconds, as a failure.

Fallback: if no Chromium is available, emit the HTML, print a clear message telling the operator to open it and screenshot it, and continue. **Never fail a 20-hour run over a missing browser.**

---

## 12. Data model

`report.json`, minimum shape:

```json
{
  "schema_version": 1,
  "report_id": "DP-20260802-usb-WD_Elements_25A2_575834314235",
  "batch_id": "B-20260802-0258",
  "tool": { "name": "driveprep", "version": "1.0.0" },
  "generated_utc": "2026-08-03T22:14:03Z",
  "drive": {
    "by_id": "usb-WD_Elements_25A2_575834314235-0:0",
    "kernel_name_at_run": "sdc",
    "vendor": "WDC", "model": "WDC WD40EZRZ-00GXCB0",
    "family": "Western Digital Blue",
    "enclosure_serial": "575834314235", "ata_serial": "WD-WCC4N1234567",
    "firmware": "80.00A80",
    "capacity_bytes": 4000787030016, "capacity_label": "4 TB",
    "logical_block_bytes": 512, "physical_block_bytes": 4096,
    "rotation_rpm": 5400, "form_factor": "3.5 inch",
    "bus_type": "usb", "enclosure": "WD Elements 25A2",
    "smartctl_device_type": "sat",
    "sysfs_rotational": 1,
    "identity": {
      "class": "scsi",
      "size_bytes": 4000787030016,
      "sysfs_model": "Elements 25A2",
      "sysfs_serial": "575834314235"
    },
    "locator_epochs": [
      { "hctl": "6:0:0:0", "usb_port_path": "2-1.4", "kernel_name": "sdc",
        "valid_from": "2026-08-02T03:00:00Z", "valid_until": "2026-08-02T18:02:41Z" }
    ]
  },
  "smart": {
    "available": true,
    "overall_health": "PASSED",
    "power_on_hours": 14203,
    "power_cycles": 412,
    "probe_log": [
      { "d_type": "auto", "accepted": false, "reason": "capacity 0 implausible" },
      { "d_type": "sat",  "accepted": true,  "reason": "capacity within 1% of sysfs" }
    ],
    "attributes": [
      { "id": 5, "name": "Reallocated_Sector_Ct", "value": 200, "worst": 200,
        "thresh": 140, "raw": 0, "flags": "PO--CK", "when_failed": "" }
    ],
    "before_after_delta": [
      { "id": 194, "name": "Temperature_Celsius", "before": 31, "after": 38 }
    ]
  },
  "self_tests": {
    "short":    { "run": true, "status": "completed_without_error", "duration_s": 118, "lba_of_first_error": null },
    "extended": { "run": true, "status": "completed_without_error", "duration_s": 29640, "lba_of_first_error": null }
  },
  "erase": {
    "performed": true,
    "method": "single_pass_zero",
    "standard_alignment": "NIST SP 800-88 Rev.1 Clear",
    "bytes_written": 4000787030016,
    "started_utc": "2026-08-02T03:00:00Z",
    "finished_utc": "2026-08-02T10:41:12Z",
    "duration_s": 27672,
    "throughput_mean_mbs": 144.6,
    "throughput_min_mbs": 96.1,
    "throughput_max_mbs": 171.3
  },
  "verify": {
    "performed": true,
    "bytes_read": 4000787030016,
    "nonzero_ranges": [],
    "read_error_ranges": [],
    "read_errors": 0,
    "duration_s": 26980,
    "result": "all_zero_no_errors"
  },
  "run_conditions": {
    "temp_max_c": 44,
    "thermal_pause_s": 0,
    "disconnects": 0,
    "kernel_events": { "io_errors": 0, "medium_errors": 0, "usb_resets": 0, "uas_aborts": 0 }
  },
  "grade": {
    "value": "PASS",
    "reasons": [],
    "rubric_version": 1
  },
  "flags": {
    "skipped_extended_test": false,
    "smart_via_bridge_unavailable": false,
    "thermally_aborted": false
  }
}
```

Notes on the shape:

- `drive.by_id` is canonical for selection and for the output directory name. `enclosure_serial` and `ata_serial` are recorded separately and both displayed when they differ (§4.1).
- `kernel_name_at_run` is recorded for log correlation only. It is never an identifier.
- `grade.value` is exactly one of `PASS`, `CAUTION`, `FAIL`, `INCOMPLETE`.
- `grade.reasons` is an array of human-readable strings naming the specific condition and its number. Empty only when the grade is PASS.
- `drive.rotation_rpm` is `null` when unavailable. Null is not a failure condition (§4.2).
- `drive.sysfs_rotational` is recorded for transparency and is explicitly advisory (§4.2).
- `drive.identity` holds only the §4.5 **matching** fields, including `class`. `sysfs_model` is the enclosure's SCSI INQUIRY model and is deliberately a different string from `drive.model`, which comes from ATA IDENTIFY. Never compare the two (§6.1). For `class: "loop"` and `class: "dm"` the model and serial fields carry the substitutes defined in §4.5's table.
- `erase.performed` and `verify.performed` are `false` when the phase did not run — most importantly for a drive that failed the §6.3 short-test gate, which is reported FAIL **without ever having been erased**. When either is `false`, every other key in that object is `null` and §11.2's corresponding block must say so in plain words. A report that implies a drive was sanitized when it was not is the single worst output this tool can produce.
- `smart.attributes[].raw` is smartctl's `raw.value` (the decoded 48-bit integer), flattened. `when_failed` is smartctl's string: `""`, `"past"`, or `"now"` — **never `null`** — so §10.1's "any non-empty value triggers" rule can be applied directly without a separate null check.
- `smart.power_cycles` feeds the §11.2 headline tile and is `null` when SMART is unavailable.
- `smart.probe_log` records every `-d` type attempted and why it was accepted or rejected (§6.1), and is retained even on success. It is the evidence for a `smart.available: false` result (§6.2).
- `drive.form_factor` is `null` when not reported, which is common over USB.
- `drive.locator_epochs` holds the §4.5 **locator** fields over time, one entry per open or reconnect. These are for kernel-log correlation (§6.4) and are never compared for identity. `kernel_name_at_run` above is the last epoch's kernel name, retained for readability.

---

## 13. Project layout

```
driveprep/
  pyproject.toml
  README.md
  driveprep/
    __main__.py          # CLI entry, subcommands
    identity.py          # §4.5 per-class identity tuples, locator epochs
    inventory.py         # sysfs + lsblk + by-id enumeration
    safety.py            # every §4 check, confirmation gate, token
    blockio.py           # O_DIRECT open, zero fill, verify, error ranges
    smart.py             # smartctl wrapper, -d probing, JSON parse, self-tests
    kernlog.py           # journalctl sweep and classification
    thermal.py           # §6.5 guard
    grade.py             # rubric
    report.py            # HTML render, PNG render, batch index
    state.py             # checkpoint, progress, resume
    supervisor.py        # multi-drive supervisor, locks, signals
    log.py
  templates/
    report.html
    batch-index.html
  config/
    grading.toml
  systemd/
    driveprep@.service
  tests/
    conftest.py
    make_loop.py         # losetup fixtures
    make_flaky.py        # dm-error / dm-flakey fixtures
    test_*.py
```

Package it as a plain `pip install -e .` or a single-file zipapp. Do not require a container.

`supervisor.py` rather than `queue.py`: the latter shadows the standard library's `queue` module. Absolute imports make it harmless in practice, but the name collision is pure downside.

---

## 14. Testing

**No test may target a physical disk.** Linux gives two clean ways to exercise the destructive and error paths safely; use both.

**Loop devices** for the happy paths. `truncate -s 512M /tmp/dp-test.img && losetup --find --show --sector-size 512 /tmp/dp-test.img` yields a real block device supporting `O_DIRECT`. Also build a 4096-logical-block fixture with `--sector-size 4096` to exercise the block-size math.

**`dm-error` and `dm-flakey`** for the unhappy paths. A device-mapper table that maps most of the range to a loop device and a slice of it to `error` produces genuine `EIO` at known offsets. This is the only realistic way to test the read-error-range logic, and it is the main reason this build is more testable than the Windows equivalent would have been.

Required cases:

1. Zero-fill a 512 MB loop device end to end; verify reports all-zero.
2. Write a known nonzero pattern, run verify only, confirm nonzero ranges are detected at correct byte offsets and correct LBAs.
3. 4096-logical-block fixture: confirm LBA math uses the logical block size, and that a device whose length is not a multiple of the chunk size has its tail handled with no write past the end.
4. Interrupt a zero-fill at roughly 40 percent, then `resume`; confirm it restarts at the chunk boundary and the final verify passes.
5. **Findings survive resume.** Seed nonzero data in the first 20 percent, run verify, kill at roughly 40 percent, resume. Assert the final report still contains the pre-interruption nonzero ranges and grades FAIL. A resumed run that reports clean is the failure this test exists to catch (§9.1).
6. `dm-error` fixture with a known bad slice: confirm the run continues past the errors, records the correct ranges, and grades FAIL.
7. Safety refusals, each asserted individually: mounted device; device in `/proc/swaps`; **holders on a partition rather than on the whole disk** (build it: `losetup -P` a device — the `-P` matters, without it the kernel creates no partition nodes and the fixture silently tests nothing — partition it, then stack a **`dm` linear target** on the partition. Do **not** use bare `pvcreate` for this: it writes PV metadata but creates no holder, so the fixture would pass without ever exercising the holders path); **an inactive LVM PV**, and **a volume group that has been deactivated with `vgchange -an`** — assert that `holders/` is empty and `check_holders` returns nothing, and that the drive is refused anyway on its `LVM2_member` signature (§4.2); mdraid member listed by partition name; a disk referenced in `/etc/fstab` by `UUID=` but not currently mounted; **the second leg of an md RAID1 backing `/`** (asserts the `slaves/` recursion rather than a single-valued `PKNAME` lookup); a read-only device; an `nvme*` name; a device whose smartctl JSON has `rotation_rate: 0`; and a device whose smartctl JSON omits `rotation_rate` entirely, which must be **allowed**, not refused.
8. `O_EXCL` contention: hold the loop device open **from a second process that also uses `O_EXCL`** (or mount it) and confirm the run aborts with `EBUSY`. Assert in the same test that a plain non-exclusive opener does **not** produce `EBUSY`, so the spec's §4.5 claim about `O_EXCL`'s actual scope stays honest and nobody later "fixes" the test by asserting the wrong behavior.
8b. Transfer primitives: assert that `os.read`/`os.write` on an `O_DIRECT` fd raise `EINVAL` and that `os.preadv`/`os.pwritev` into an mmap succeed, so a future refactor cannot quietly reintroduce the unaligned path.
8c. Zero comparison: assert `mmap.mmap(-1, 16) == bytes(16)` is `False` and `memoryview(mmap.mmap(-1, 16)) == bytes(16)` is `True`. This pins the §9 comparison rule that, if written the obvious way, fails every drive.
9. Token: confirm a token computed for a 4-device set is rejected after a fifth device appears, that a mismatched token aborts, and that `resume` recomputes its own token and rejects the original batch's token when the resumed set is smaller.
10. Grade fixtures: hand-built `report.json` for each rubric condition, at minimum short-test failed, extended-test failed, pending sectors, offline uncorrectable, reported uncorrectable, read errors, nonzero sectors, reallocated sectors, spin retry, CRC errors, kernel I/O error, USB reset, power-on hours over threshold, `when_failed` set, thermal pause, SMART unavailable, extended test skipped, extended test inconclusive, thermal abort (INCOMPLETE), and a fully clean drive. Confirm grade and specific reason string in each case.
11. smartctl parsing: unit-test against captured `--json` output from at least two real drives, one USB-bridged and one SATA, committed as fixtures. Capture these during development. Include a case asserting that a bridged drive whose smartctl model differs from its sysfs model is **accepted** by the §6.1 probe rule, since a model-equality check would silently route every bridged drive to "SMART unavailable."
12. Report: render HTML and PNG from a fixture; confirm the PNG exists, exceeds 10 KB, and is exactly 1200 x 1600.
13. Queue: run three loop devices concurrently under `--test-mode`, kill one child mid-run, and confirm the other two complete and the batch index records the failure.
14. `--test-mode` scope: assert it refuses to run when any target resolves to a non-loop, non-dm device, that it does not relax the mounted, holders, swap, or fstab refusals on a loop device, that `--test-mode --all` is refused outright (§4.2.1), and that a mounted snap loop device is individually refused.
15. **Identity tuples per device class (§4.5).** Assert a tuple is computed for a loop device, for a dm device, and for a scsi device, using each class's own sources; that `/sys/block/loop0/device` is confirmed absent so the test fails loudly if someone reintroduces the SCSI-only assumption; and that a loop device's tuple never compares equal to a scsi device's tuple even when `size_bytes` is identical. Then assert the full open sequence succeeds on a loop fixture, which is what every other destructive test depends on.
16. **`erase.performed: false` path.** Drive a fixture through a simulated short-test failure; assert the report grades FAIL, that `erase.performed` and `verify.performed` are `false` with their sibling keys null, that no write ever reached the device (compare a pre-seeded pattern before and after), and that the rendered HTML contains the "NOT ERASED" notice.
17. **Signal handling (§8.2).** `SIGTERM` a child mid-erase; assert it exits within the deadline, that `state.json` contains a byte offset and the accumulated findings up to that point, and that the drive grades INCOMPLETE rather than FAIL. Assert the parent forwards the signal and still writes a batch index.

`pytest`. Tests that need `losetup` or device-mapper are marked `@pytest.mark.root` and skipped with a clear message when not run as root. All destructive tests pass `--test-mode`.

---

## 15. Acceptance criteria

- [ ] `driveprep list` enumerates every attached disk with eligibility and per-disk reasons, on a machine with at least one USB drive and one SATA drive attached.
- [x] Running without `--execute` never opens a device for writing. Verified by code inspection **and** by an `strace`-based test asserting no `O_RDWR`/`O_WRONLY` open on a block device, for both `run` (plan mode) and `list`. Plan mode does legitimately open the device `O_RDONLY` for the §7 duration probe, and the test asserts that too — otherwise it would pass on a tool that never touched the disk at all. The strace parser has its own unit tests, including one that must detect a synthetic `O_RDWR` line, so the acceptance test cannot pass vacuously.
- [x] Every safety refusal in test 7 passes, including the holders-on-a-partition case (built with a `dm` linear target, not `pvcreate` — see §4.2), the fstab-referenced-but-unmounted case, the inactive and deactivated LVM cases, an **active swap area**, and the **md RAID1 second-leg case** — a real two-loop mirror, mounted and treated as a protected mountpoint, asserting `slaves/` recursion returns *both* legs and that neither is eligible. That test also asserts `lsblk -no PKNAME` reports fewer parents than the recursion finds, so the reason the recursion exists stays documented by a live measurement rather than a comment.

  All three were mutation-checked: making `leaf_disks()` single-valued, and making `check_swap()` always return clean, each fail their test. A safety test that has never been seen to fail is not evidence.
- [ ] Tests 8, 8b, and 8c pass, pinning `O_EXCL` scope, the `preadv`/`pwritev` requirement, and the `memoryview` comparison rule.
- [ ] A USB-bridged drive whose sysfs model differs from its smartctl model is accepted by the §6.1 probe and produces a full SMART section, not an "unavailable" one.
- [ ] Test 5 passes: findings recorded before an interruption are present after a resume, and the run grades FAIL.
- [ ] Test 15 passes: identity tuples are computed per device class, so the destructive test suite runs at all.
- [ ] Test 16 passes: a drive that fails the short-test gate is never written to, and its report says so where a reader cannot miss it.
- [ ] Test 17 passes: a `SIGTERM`ed run checkpoints its findings and grades INCOMPLETE.
- [ ] Test 6 passes: a `dm-error` slice is detected, reported with correct ranges, and grades FAIL.
- [ ] A full run on a real drive produces every artifact in §11.1, plus a batch index.
- [ ] `report.png` is legible at 400 px wide: model, capacity, and grade all readable.
- [ ] Every rubric outcome can be produced from a fixture and shows a correct, specific reason.
- [ ] A 3-drive batch survives an SSH disconnect when launched under `systemd-run`.
- [ ] A drive physically unplugged mid-verify **and replugged into a different USB port** is detected, waited for, re-identified per §4.5's matching fields, and resumed, with the disconnect counted, a second locator epoch recorded, and kernel-log events from both epochs attributed to it.
- [ ] `pytest` passes clean as root.
- [ ] README documents: apt prerequisites, the Google Chrome install for PNG rendering, expected runtimes by capacity, concurrency and USB power guidance, the systemd invocation, and a plain-language explanation of what the report claims and does not claim.

---

## Appendix A: Known quirks

- **Kernel names move; `by-id` does not.** This is the whole reason §4.1 exists. Never persist `/dev/sdX` anywhere.
- **`sysfs` `size` is always in 512-byte units**, even for 4Kn drives. Multiplying it by the logical block size is an 8x overrun bug.
- **`queue/rotational` lies over USB.** Many bridges hardcode `1`. Never gate eligibility on it alone (§4.2).
- **WD My Book vs Elements.** My Book units frequently use a bridge with hardware AES and are the most likely to block SMART passthrough. Elements and Easystore units are usually ASMedia or JMicron and pass `-d sat` cleanly. On this build, a My Book that blocks SMART has a real fix: shuck it and run it on SATA.
- **UAS vs usb-storage.** Some older bridges are buggy under the UAS driver and throw `uas_eh_abort_handler` under sustained load. If a specific drive is unstable, the `usb-storage.quirks=VID:PID:u` kernel parameter forces the older driver. Document this in the README as a troubleshooting step; do not automate it.
- **UDMA CRC errors and USB resets are usually the cable, hub, or power**, not the platters. The rubric flags them as CAUTION and the report carries the one-line explanation so the number is not misread as platter damage.
- **`udisks2`** is not installed by default on Ubuntu Server, but if something pulled it in it will auto-mount every drive plugged in, which then makes every drive INELIGIBLE (§4.2) and is confusing to diagnose. The tool must name udisks explicitly in the refusal message when it detects an auto-mount. README should recommend `systemctl mask udisks2` on a dedicated box. The tool does not unmount on the operator's behalf (§5).
- **`holders/` on partitions.** Worth repeating because it is the most likely single point of failure in the safety layer: `/sys/block/sdb/holders/` is empty for an LVM PV that lives on `sdb1`. Check every partition.
- **Bus-powered 2.5-inch drives brown out on hubs** under sustained write load, showing up as USB resets or disconnects. Use a powered hub or fewer concurrent portables (§8.1).
- **Thermal.** A pile of drives running full-surface writes needs airflow. §6.5 guards it, but a box fan pointed at the stack is the actual fix, and the README should say so.

---

## Appendix B: Runtime estimates

At a typical 130 to 160 MB/s sustained sequential rate for a 5400 to 7200 RPM drive. SATA and USB 3.0 both saturate at the drive, not the bus.

| Capacity | Zero fill | Verify read | Extended test | Total |
|---|---|---|---|---|
| 500 GB | ~1 hr | ~1 hr | ~1.5 hr | ~3.5 hr |
| 1 TB | ~2 hr | ~2 hr | ~2.5 hr | ~6.5 hr |
| 2 TB | ~4 hr | ~4 hr | ~4.5 hr | ~13 hr |
| 4 TB | ~8 hr | ~7.5 hr | ~8.5 hr | ~24 hr |
| 8 TB | ~15 hr | ~14 hr | ~16 hr | ~45 hr |

USB 2.0 connections run roughly 4x slower. Detect the link speed from `/sys/bus/usb/devices/*/speed` and warn at phase 3 if a drive negotiated 480 Mbps or lower.

---

## Appendix C: Suggested listing language

For the README, so report claims and listing copy stay consistent. Not generated by the tool.

> This drive was securely erased with a full single-pass zero overwrite across the entire device, then verified by reading back every sector to confirm the erase completed. It was health-tested with a SMART extended self-test and a full-surface read, with drive temperature and kernel-level I/O errors monitored throughout. The attached report shows the actual results, including power-on hours and sector counts, whatever they turned out to be. This is seller-provided documentation, not a third-party certification.
