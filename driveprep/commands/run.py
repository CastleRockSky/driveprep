"""`list` and `run`: inventory, then the full pipeline behind the gate."""

from __future__ import annotations

from pathlib import Path

from .. import blockio, grade as grading, inventory as inv, log, runs, safety
from . import batch

_log = log.get("cli")


def cmd_list(options) -> int:
    disks = inv.scan()
    batch.evaluate_all(disks, options)

    print(f"\n{len(disks)} block device(s) found\n")
    for disk in disks:
        mark = "ELIGIBLE" if disk.eligible else "INELIGIBLE"
        tag = " [INTERNAL BUS]" if disk.is_internal_bus and disk.eligible else ""
        print(f"  {disk.id}")
        print(f"    {mark}{tag}  {disk.kname}  {disk.capacity_label}  "
              f"{disk.bus_type}  {disk.model or '(no model)'}")
        if disk.sysfs_rotational is not None:
            print(f"    sysfs rotational: {disk.sysfs_rotational} (advisory only)")
        for reason in disk.ineligible_reasons:
            print(f"      - {reason}")
        print()
    return 0


def cmd_run(options) -> int:
    config = grading.load_config()
    disks = inv.scan()
    batch.evaluate_all(disks, options)
    selected = batch.select_targets(disks, options)

    refused = [d for d in selected if not d.eligible]
    if refused:
        print("\nREFUSED -- these targets are ineligible:\n")
        for disk in refused:
            print(f"  {disk.id}")
            for reason in disk.ineligible_reasons:
                print(f"    - {reason}")
        print()
        return 2

    if not selected:
        print("No eligible drives selected. Nothing to do.")
        return 0

    output_root = Path(options.output_root)
    try:
        states = runs.prepare_states(selected, options, output_root)
    except runs.OutputCollision as exc:
        print(f"REFUSED -- {exc}")
        return 2

    # Per-drive locks, held for the WHOLE pipeline (spec 8.1). O_EXCL only
    # covers phases 4-5; phases 0-3 hold no descriptor at all, so without this
    # a second instance can plan the same drive, pass the gate, and overwrite
    # this run's checkpoint -- destroying accumulated findings -- before its own
    # O_EXCL open finally fails. Acquired in the parent and inherited by the
    # forked children, which is what keeps them held to phase 8.
    return batch.run_locked(
        selected, states,
        lambda: _run_batch(selected, states, config, options, output_root))


def _run_batch(selected, states, config, options, output_root) -> int:
    if not selected:
        print("Every selected drive is locked by another DrivePrep instance.")
        return 2

    # Phase 0 is complete. The token is computed HERE, over the eligible set
    # that we actually hold locks for, and before any self-test runs (spec
    # 4.4). Computing it before locking would print a token covering a drive
    # this run is not going to touch.
    token = safety.compute_token(selected)

    # Phases 1 and 2 run HERE, before the gate (spec 7): the operator confirms
    # once with full information, including short-test results, then leaves.
    # Running them inside the child instead would confirm first and test after,
    # which defeats the point of phase 2 being a gate the operator sees.
    _run_pre_gate_phases(selected, states, config, options)

    estimate = _estimate_batch(selected, states, config, options)
    manifest = safety.Manifest(
        disks=selected, token=token,
        total_bytes=sum(d.size_bytes for d in selected),
        estimate_seconds=estimate,
    )

    return batch.confirm_and_run(manifest, selected, states, config, options,
                                 output_root)


def _run_pre_gate_phases(disks, states, config, options) -> None:
    """Phase 1 for every drive, and phase 2 as well under --execute (spec 7).

    Plan mode deliberately stops after phase 1: the short self-test takes
    minutes per drive and perturbs the drive's self-test log, and since the
    token comes from the phase-0 set it has no bearing on what plan mode shows
    (spec 4.4).
    """
    from .. import pipeline as pipe

    for disk in disks:
        pipeline = pipe.DrivePipeline(disk, states[disk.id], config, options)
        try:
            pipeline.phase1_smart_before()
        except Exception as exc:  # noqa: BLE001 - never lose the batch to one drive
            _log.warning("%s: SMART snapshot failed (%s); continuing",
                         disk.id, exc)
            # Otherwise phase 2 went on to run smartctl with no -d type, and
            # the report claimed SMART data it never had.
            states[disk.id].smart_available = False

        # A drive already failing on SMART is skipped before the short test:
        # no point spending two minutes, let alone eight hours, on it.
        try:
            failures = pipeline.smart_gate()
        except Exception as exc:  # noqa: BLE001 - same rule as phase 1
            # Outside the try, one drive's malformed SMART data ended phases
            # 1-2 for every drive after it. The short test still gates it.
            _log.warning("%s: SMART health gate could not be evaluated (%s); "
                         "continuing to the short test", disk.id, exc)
            failures = []
        if failures:
            disk.short_test_status = "SKIPPED -- SMART already failing"
            disk.skip_erase = True
            continue

        if not options.execute:
            disk.short_test_status = "not run (plan mode)"
            continue

        try:
            if pipeline.phase2_short_test():
                status = (states[disk.id].short_test or {}).get("status")
                disk.short_test_status = (status or "not run").replace("_", " ")
            else:
                disk.short_test_status = "FAILED -- this drive will be SKIPPED"
                disk.skip_erase = True
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: short self-test failed to run (%s); continuing",
                         disk.id, exc)
            disk.short_test_status = "could not run"


def _estimate_batch(disks, states, config, options) -> float | None:
    """Read-only duration estimate (spec 7).

    Derived from a 1 GB sequential READ from the middle of each device.
    Nothing is written: phase 3 is the confirmation gate, so any calibration
    write before it would put bytes on the platters -- including LBA 0 --
    before the operator has confirmed.

    Two corrections learned from a real 2 x 4 TB batch that was estimated at
    21.7 h and took 28.6 h:

      * The probes run CONCURRENTLY, matching --jobs. Run one at a time they
        measure uncontended throughput, but the actual passes share a bus and a
        bridge; two 4 TB verifies came in at 12.6 h each against an 8 h
        uncontended prediction. Measuring under contention beats applying a
        made-up factor to a clean number.
      * The extended self-test is taken from the drive's OWN polling estimate
        rather than assumed to cost another full pass. That figure is known
        exactly, and on those drives it was 7.9 h against a 12.6 h pass.

    Still approximate, and knowingly so: the probe reads, while phase 4 writes,
    and on these drives writing ran faster than reading. Using the read rate
    for both makes the erase leg pessimistic, which is the safer direction for
    a number an operator plans their day around.
    """
    from concurrent.futures import ThreadPoolExecutor

    def probe(disk):
        """Returns (disk, seconds-per-full-pass) or (disk, None)."""
        cfg = blockio.PassConfig(
            chunk_bytes=options.chunk_size
            or config.get("io", {}).get("chunk_bytes", blockio.DEFAULT_CHUNK),
            logical_block_bytes=disk.logical_block_bytes,
            physical_block_bytes=disk.physical_block_bytes,
        )
        try:
            with safety.guarded_open(
                disk, disk.identity, write=False,
                test_mode=options.test_mode,
                output_root=Path(options.output_root),
            ) as (fd, _kname):
                return disk, blockio.estimate_duration(fd, disk.size_bytes, cfg)
        except (safety.SafetyError, OSError) as exc:
            _log.debug("could not estimate %s: %s", disk.id, exc)
            return disk, None

    workers = max(1, min(getattr(options, "jobs", 1), len(disks)))

    # Measure BOTH ends rather than pick one and pretend to precision.
    # Uncontended is what a lone drive achieves; contended is what N drives
    # sharing a bus achieve. The real run sits between them, because phase 4
    # writes (which contend less here) and phase 5 reads (which contend more).
    # A single number was 16% out whichever endpoint it used; a range that
    # actually contains the answer is more use to someone planning a day.
    # Keyed by disk.id, not the Disk itself: Disk is a dataclass, so it
    # generates __eq__ and is therefore unhashable.
    solo = {d.id: v for d, v in (probe(d) for d in disks)}
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            together = {d.id: v for d, v in pool.map(probe, disks)}
    else:
        together = solo

    low = high = 0.0
    for disk in disks:
        fast, slow = solo.get(disk.id), together.get(disk.id)
        if not fast or not slow:
            continue
        extended = (0.0 if options.skip_extended_test
                    else _extended_test_seconds(states.get(disk.id), slow))
        # Drives run concurrently, so the batch takes as long as its slowest.
        low = max(low, fast * 2 + extended)
        high = max(high, slow * 2 + extended)
    if not high:
        return None
    return (low, high) if high > low * 1.05 else high


def _extended_test_seconds(state, one_pass: float) -> float:
    """The drive's own estimate for phase 6, falling back to a pass-equivalent.

    Floored by capacity for the same reason phase 6's stall deadline is: a
    drive that claims a one-minute surface scan is describing nothing real,
    and believing it made the manifest quote an hour for a 32-hour batch.
    """
    if state is not None and state.smart_before_data:
        from .. import smart
        minutes = smart.SmartResult(
            available=True, d_type=state.smartctl_d_type,
            data=state.smart_before_data).selftest_polling_minutes.get("extended")
        floor = smart.extended_test_floor_minutes(state.capacity_bytes)
        minutes = max(minutes or 0, floor)
        if minutes:
            return minutes * 60
    return one_pass

