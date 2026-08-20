# DrivePrep

> **This tool irreversibly destroys data.** It overwrites whole disks with
> zeroes. There is no undo, and no recovery from a mistake. Read the
> [Safety](#safety) section before running it on anything you care about, and
> confirm every serial number yourself. Provided as-is, with no warranty of any
> kind — see [LICENSE](LICENSE).

Securely erase, health-test, and document used hard disk drives for resale.

DrivePrep takes a stack of old spinning drives, zero-fills each one end to end,
reads every sector back to prove the erase took, runs SMART self-tests, watches
the kernel log and the drive temperature throughout, and produces a single
listing-ready image you can upload to eBay.

It is built for the case where you start a batch, walk away, and come back a day
later to finished reports. It optimizes for **safety, unattended reliability,
and clarity** — not throughput.

**It destroys data irreversibly.** Nothing is written without `--execute` and a
confirmation token derived from the exact set of attached drives.

---

## Requirements

| | |
|---|---|
| OS | Ubuntu Server 24.04 LTS, bare metal. No desktop needed. |
| Python | 3.12 (the system Python). No third-party runtime dependencies. |
| Privileges | root |
| Drives | Spinning HDDs on USB or SATA. Both are first class. |
| Not supported | SSDs, NVMe, SAS, hardware RAID volumes — each is detected and refused with a specific reason. |

```bash
sudo apt update
sudo apt install smartmontools util-linux python3
```

`zfsutils-linux` is optional; if `zpool` is present, DrivePrep checks it and
refuses any drive in use as a vdev. If it is absent, that check is skipped
cleanly.

### Install

```bash
git clone https://github.com/CastleRockSky/driveprep.git && cd driveprep
sudo pip install .
```

Or run it straight from the checkout with no install step at all:

```bash
sudo python3 -m driveprep list
```

Both work. The grading config and report templates ship inside the package, so
an installed copy is self-contained and does not need the source tree.

To tune the grading thresholds on an installed copy, edit the `grading.toml`
that ships with it:

```bash
python3 -c 'import driveprep.grade as g; print(g._DEFAULT_CONFIG)'
```

Running from a checkout instead makes `driveprep/config/grading.toml` the file
you edit, which is usually what you want while you are still calibrating.

### PNG rendering (recommended)

The HTML report is always produced. The PNG — the thing you actually upload —
needs a headless browser. **Google Chrome as a real `.deb` is the most
predictable option on Ubuntu Server**, because it has no snap confinement:

```bash
wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
  | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" \
  | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install google-chrome-stable
```

The Chromium **snap** also works but is awkward: snap confinement cannot read
`file://` paths outside the invoking user's home, so DrivePrep stages the HTML
through `$SUDO_USER`'s home directory and copies the PNG back. If you are
logged in directly as root there is no such directory and the PNG is skipped
with a message.

If no browser is found at all, DrivePrep prints the path to the HTML and
carries on. **A 20-hour run never fails over a missing browser.**

---

## Quick start

```bash
# What is attached, and why each disk is or is not eligible. Never writes.
sudo driveprep list

# Plan a batch: full inventory, SMART, duration estimate, manifest, token.
# Still never writes.
sudo driveprep run --all

# Actually do it, four drives at a time.
sudo driveprep run --all --execute --jobs 4

# One specific drive.
sudo driveprep run --id usb-WD_Elements_25A2_575834314235-0:0 --execute

# Unattended: pre-answer the confirmation prompt.
sudo driveprep run --all --execute --confirm-token DP-4-A7F3 --jobs 4

# Pick up a batch that was interrupted. Re-confirms; see "Resume" below.
sudo driveprep resume --execute

# Rebuild reports from stored state. Never touches a device.
sudo driveprep report --all
```

### Flags

| Flag | Meaning |
|---|---|
| `--id <by-id name>` | Repeatable. The primary selector. |
| `--device /dev/sdX` | Escape hatch for a drive with no `by-id` entry. Not allowed in queue mode. |
| `--all` | Every eligible drive. Cannot be combined with `--test-mode`. |
| `--execute` | Required for any write. |
| `--confirm-token` | Pre-answers the prompt. Still recomputed and matched at run time. |
| `--jobs N` | Concurrency, default 4. |
| `--chunk-size` | Transfer chunk in bytes, default 8 MiB. |
| `--usb-only` | Convenience gate: refuse anything not on USB. |
| `--skip-extended-test` | Skips phase 6. The report is marked and grades CAUTION. |
| `--mask-serial` | Redacts the middle of serials in the rendered report. |
| `--output-root` | Default `/var/lib/driveprep`. |
| `--seller-name` | Optional line on the report. |
| `--test-mode` | Development only. Permits loop and dm devices *only*. |

---

## How long it takes

At a typical 130–160 MB/s sustained sequential rate. SATA and USB 3.0 both
saturate at the drive, not the bus.

| Capacity | Zero fill | Verify read | Extended test | Total |
|---|---|---|---|---|
| 500 GB | ~1 hr | ~1 hr | ~1.5 hr | **~3.5 hr** |
| 1 TB | ~2 hr | ~2 hr | ~2.5 hr | **~6.5 hr** |
| 2 TB | ~4 hr | ~4 hr | ~4.5 hr | **~13 hr** |
| 4 TB | ~8 hr | ~7.5 hr | ~8.5 hr | **~24 hr** |
| 8 TB | ~15 hr | ~14 hr | ~16 hr | **~45 hr** |

A **USB 2.0** link runs roughly 4× slower. DrivePrep reads the negotiated link
speed and warns at the confirmation gate if a drive came up at 480 Mbps or
below — usually a bad cable or a front-panel port.

Some later WD Elements and Blue drives (especially 2.5-inch, 4 TB and up) use
**shingled recording (SMR)**. A full sequential write on SMR is slow and shows
large throughput swings as the drive's media cache saturates. That is normal,
not a fault, and throughput is deliberately kept out of the grade.

---

## Running unattended

An SSH disconnect must not kill a 24-hour batch. Use systemd:

```bash
sudo systemd-run --unit=driveprep --collect \
  driveprep run --all --execute --confirm-token DP-4-A7F3 --jobs 4

journalctl -u driveprep -f      # watch it
systemctl stop driveprep        # stop it cleanly (see below)
```

A unit template is included:

```bash
sudo cp systemd/driveprep@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start driveprep@DP-4-A7F3     # the token is the instance name
```

`tmux` works too, informally.

**Stopping cleanly.** `systemctl stop` sends `SIGTERM` to the whole group.
Every drive stops at the next chunk boundary, writes a full checkpoint
including everything it has found so far, and exits. Those drives grade
**INCOMPLETE** — an interrupted run is an unfinished measurement, not a verdict
on the drive — and `driveprep resume` picks them up. Pressing Ctrl-C twice
within five seconds kills immediately instead.

### Concurrency and USB power

- Default `--jobs 4`. One OS process per drive.
- DrivePrep warns if more than four drives share a single USB host controller.
- It also warns about more than two **bus-powered 2.5-inch** drives on one hub.
  These brown out under sustained write load and show up as USB resets or
  disconnects. Use a powered hub, or fewer portables at once. Self-powered
  3.5-inch desktop units do not have this problem.
- **Point a box fan at the stack.** DrivePrep guards temperature (warn at
  50 °C, pause at 55 °C, abort at 60 °C sustained) but airflow is the actual
  fix. A drive that had to be paused is flagged on its report.

---

## Safety

This is the part worth reading before you trust it with a machine that has data
on it.

A disk is refused unless **every** one of these is clear. Each is checked
independently and the specific failing reason is printed:

- It is a whole disk, not a partition, md device, dm target, loop device, or
  zram device.
- Neither the disk **nor any of its partitions** has anything in `holders/` —
  no LVM physical volume, mdraid member, or dm-crypt backing device.
- It is not listed in `/proc/mdstat`, `zpool status`, `/proc/self/mountinfo`,
  or `/proc/swaps`.
- It is not referenced in `/etc/fstab` or `/etc/crypttab`, including by
  `UUID=`, `LABEL=`, `PARTUUID=`, or `PARTLABEL=`. *An idle internal data disk
  that is configured but not currently mounted is caught here and nowhere
  else.*
- It does not back `/`, `/boot`, `/boot/efi`, `/home`, or the output directory —
  resolved by walking `slaves/` down to every leaf disk, so **both legs of a
  RAID1 root are protected**, not just one.
- It is not read-only.
- smartctl does not positively identify it as solid state.

**DrivePrep never unmounts anything and never calls `swapoff`.** A mounted
partition or an active swap area simply makes the drive ineligible. If you
really want to erase it, unmount it yourself and re-run.

Drives are identified by their `/dev/disk/by-id/` name, never `/dev/sdX`.
Kernel names move between boots and replugs. Before any write, DrivePrep
reopens the device, recomputes its identity from the **open file descriptor**,
and aborts unless it matches what was recorded — then re-runs the entire
eligibility rule set once more, in case something changed while you were
reading the manifest.

### The confirmation gate

Three things must happen before a single byte is written:

1. **The manifest** prints every drive: identity, model, both serials,
   capacity, bus (with an `[INTERNAL BUS]` tag for anything not USB), current
   partition table and filesystem labels, and the exact byte count that will be
   destroyed.
2. **The token** — e.g. `DP-4-A7F3` — is derived from the exact set of drives.
   You type it back. If a drive was plugged in or pulled between planning and
   confirming, the token no longer matches and the run stops.
3. **`--execute`** must be present.

`--confirm-token` pre-answers step 2 for scripting, but the token is still
recomputed from the live device set and must match. **There is no flag that
skips confirmation.**

### Resume

`resume` writes to devices, so it goes through the identical gate: it rebuilds
the manifest, re-runs eligibility against the live system, re-verifies each
drive's identity against its checkpoint, and recomputes the token. That token
will differ from the original batch's whenever the resumed set is smaller —
which is correct. A drive that finished, was pulled, or was swapped must not be
silently swept back into a destructive run.

---

## What the report says

Each drive gets its own directory under `/var/lib/driveprep/<by-id name>/`:

| File | What it is |
|---|---|
| `report.png` | **The listing image.** 1200×1600, legible as a thumbnail. |
| `report.html` | The same report, self-contained, no external fetches. |
| `report.pdf` | **Two pages to put in the box:** the test report, then setup instructions for the buyer. |
| `report-print.html` | The same two pages as HTML, if you would rather print from a browser. |
| `report.json` | The full structured record. |
| `smart-before.txt`, `smart-after.txt` | Raw `smartctl -x` output plus the log sections. |
| `kernel-log.txt` | Every kernel message matched to this drive during the run. |
| `run.log` | Timestamped full log. |
| `state.json` | The checkpoint, kept after completion. |

Batches also get `batches/<batch-id>/index.html` summarizing every drive.

`samples/` holds a rendered `report.png` for each outcome — `pass`, `caution`,
`fail`, `notreased`, `nosmart`, `incomplete`, and `many` (a drive reporting more
SMART attributes than the table holds). They are generated output, not inputs;
delete the directory if you would rather not carry the images.

### Printing

Every run also produces a two-page document intended to ship with the drive:
page 1 is the test report, page 2 tells the buyer how to initialise and look
after it. A buyer's first reaction to a securely erased drive is that it is
broken — it shows as uninitialised — so page 2 leads with that and walks
through Windows, macOS and Linux setup, which file system to pick, and how to
care for a mechanical drive.

```bash
sudo driveprep print --all                    # print every completed drive
sudo driveprep print --id <by-id> --copies 2
sudo driveprep print --all --dry-run          # render the PDFs, print nothing
```

It never touches a device, so it can be run long after the drive is packed.
Drives graded INCOMPLETE are refused — that report says DO NOT USE IN A LISTING,
and a printed page outlives the warning on screen.

Adding a network printer, if you have none configured:

```bash
lpstat -p -d                                  # what CUPS already knows
sudo lpadmin -p MyPrinter -E -v ipp://<address>/ipp/print -m everywhere
sudo lpadmin -d MyPrinter                     # make it the default
```

`-m everywhere` is driverless IPP; no vendor drivers are needed for any
AirPrint-class printer. Give the printer a **DHCP reservation** on your router —
the queue holds its address, so a new lease silently breaks it.

Printing needs the same headless Chrome as the PNG (see above). Without it the
`report-print.html` is still written and you can print that from any browser.

### The grade

Computed from published rules in `config/grading.toml`. **There is no override
flag** — you cannot tell DrivePrep to call a drive PASS.

- **PASS** — nothing below was found.
- **CAUTION** — reallocated or pending-event sectors, spin retries, command
  timeouts, CRC errors, USB resets, over 40,000 power-on hours, a thermal
  pause, a skipped or inconclusive extended test, or SMART unreadable through
  the bridge.
- **FAIL** — SMART self-assessment failed, a failed self-test, pending or
  uncorrectable sectors, any read error during the full-surface read, any
  sector that did not read back as zero, or a kernel-level I/O error.
- **INCOMPLETE** — the run did not finish. Not a grade, and **not usable in a
  listing**.

Every number that fed the grade is printed on the report whatever the outcome,
so a buyer can form their own judgment. When a threshold tips the grade, it is
named explicitly: `CAUTION: Reallocated Sector Count = 8`.

### What it claims, and what it does not

**It claims:** the entire device was overwritten once with zeros, from LBA 0 to
the last block, including the partition table and all slack; and every sector
was then read back to confirm it. That is the **"Clear"** level of NIST SP
800-88 Rev. 1 for magnetic media. For modern magnetic recording, a single
overwrite pass renders data unrecoverable by any known practical technique —
multi-pass schemes like DoD 5220.22-M and Gutmann are historical artifacts that
would multiply a 20-hour job for no benefit.

**It does not claim** to be a certificate of destruction or any third-party
certification. It is seller-generated documentation. The report says so
explicitly, and the wording is deliberate.

**ATA Secure Erase is not used.** Over USB most bridges do not pass ATA
security commands through, and a partial attempt can leave a drive locked or
frozen. Over SATA it genuinely works and is the stronger "Purge" level, but
drives are almost always frozen by the BIOS at boot, and a mishandled password
leaves a drive that looks bricked. That is a bad failure mode for an unattended
batch.

**Hardware-encrypting bridges.** Many WD My Book units do AES in the bridge, so
your zeros land on the platters as the ciphertext of zeros rather than literal
zeros. This is still a complete and correct sanitization of the user data, and
reading back through the same bridge is the meaningful verification. The report
notes this when it applies.

### Suggested listing copy

> This drive was securely erased with a full single-pass zero overwrite across
> the entire device, then verified by reading back every sector to confirm the
> erase completed. It was health-tested with a SMART extended self-test and a
> full-surface read, with drive temperature and kernel-level I/O errors
> monitored throughout. The attached report shows the actual results, including
> power-on hours and sector counts, whatever they turned out to be. This is
> seller-provided documentation, not a third-party certification.

---

## Troubleshooting

**Every drive shows up INELIGIBLE as "mounted".**
`udisks2` auto-mounts drives the moment they are plugged in. On a dedicated
box: `sudo systemctl mask --now udisks2`. DrivePrep names udisks explicitly in
the refusal when it detects this.

**SMART is unavailable on a My Book.**
Older WD My Book bridges block SMART entirely. The erase and full-surface read
still work, but the grade caps at CAUTION because power-on hours and
reallocated sector counts are unknown. **The real fix is to shuck the drive and
connect it to SATA**, which this build fully supports.

**A drive keeps throwing `uas_eh_abort_handler` under load.**
Some older bridges are buggy under the UAS driver. Force the older
`usb-storage` driver for that specific device with a kernel parameter:

```
usb-storage.quirks=VID:PID:u
```

Find the VID:PID with `lsusb`. DrivePrep deliberately does not automate this.

**UDMA CRC errors or USB resets appear on the report.**
These are almost always the cable, hub, or power supply — not the platters. The
rubric grades them CAUTION and the report carries that explanation so the
number is not misread as damage. Try a different cable and a powered hub.

**The PNG is missing or zero bytes.**
Almost certainly snap confinement (see PNG rendering above). Install Google
Chrome as a `.deb`. The HTML report is always complete regardless — open it and
screenshot it at 1200×1600.

**A drive vanished mid-run.**
DrivePrep checkpoints, waits up to 60 seconds for it to come back, re-identifies
it by its identity tuple (not its kernel name, which will likely have changed —
even a different USB port is fine), and resumes. It gives up after three
reconnects in one phase and counts every disconnect on the report.

---

## Design notes

[`docs/SPEC.md`](docs/SPEC.md) is the specification this tool was built from —
the full rationale for the identity model, the safety checks, the O_DIRECT
block I/O rules, and the grading rubric. If you are wondering *why* something
works the way it does (for example why `lsblk -no PKNAME` is never used to find
a parent device, or why the zero comparison is `memoryview(buf)[:n]` rather
than comparing the mmap directly), the answer is almost certainly in there.

## Development

```bash
sudo -E python3 -m pytest            # the full suite
python3 -m pytest -m 'not root'      # the parts that do not need root
```

**No test targets a physical disk.** Happy paths run on `losetup` loop devices
— real block devices that support `O_DIRECT`, including a 4096-byte-sector
fixture for the block-size arithmetic. Error paths use `dm-error` to produce
genuine `EIO` at known offsets, which is the only realistic way to test the
read-error-range logic.

Tests needing `losetup` or device-mapper are marked `@pytest.mark.root` and
skip with a clear message otherwise. Some also need `lvm2` and `sfdisk` for the
holders-on-a-partition fixture.

The build specification, including the reasoning behind each safety rule, is
`DRIVEPREP-SPEC-UBUNTU.md`.
