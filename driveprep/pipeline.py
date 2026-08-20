"""The per-drive pipeline (spec 7).

Phases, in order. Each writes its result to the drive's state file before the
next begins, so a crash is resumable.

  0 inventory and eligibility        no writes
  1 SMART snapshot: before           no writes
  2 SMART short self-test (gate)     no writes
  3 confirmation gate                batch-wide, owned by the supervisor
  4 zero fill                        DESTRUCTIVE
  5 full-surface verification read   no writes
  6 SMART extended self-test         no writes
  7 SMART after + delta, kernel log  no writes
  8 grade, render HTML, render PNG   no writes

Phases 0-2 run for every drive in the batch BEFORE the single batch-wide
confirmation at phase 3, so the operator confirms once with full information
including short-test results, then leaves.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import SCHEMA_VERSION, TOOL_NAME, __version__
from . import blockio, grade as grading, identity as ident, inventory as inv
from . import kernlog, log, report as reporting, safety, smart, state as st
from . import thermal

_log = log.get("pipeline")


class DriveAborted(RuntimeError):
    """This drive cannot continue. The rest of the batch is unaffected."""


class DriveInterrupted(RuntimeError):
    """A signal arrived. Checkpoint written; the drive grades INCOMPLETE."""


class DrivePipeline:
    def __init__(self, disk: inv.Disk, drive_state: st.DriveState, config: dict,
                 options, stop_flag=None):
        self.disk = disk
        self.state = drive_state
        self.config = config
        self.options = options
        self.stop_flag = stop_flag

        io_cfg = config.get("io", {})
        self.pass_cfg = blockio.PassConfig(
            chunk_bytes=options.chunk_size or io_cfg.get("chunk_bytes",
                                                         blockio.DEFAULT_CHUNK),
            logical_block_bytes=disk.logical_block_bytes,
            physical_block_bytes=disk.physical_block_bytes,
            max_recorded_ranges=io_cfg.get("max_recorded_ranges", 1000),
            narrow_error_ranges=io_cfg.get("narrow_error_ranges", True),
            progress_interval_s=config.get("checkpoint", {})
                .get("progress_interval_s", 1.0),
        )
        self.checkpoint_interval = config.get("checkpoint", {}).get("interval_s", 30)
        self.smart_before: smart.SmartResult | None = None
        self.smart_after: smart.SmartResult | None = None
        self.kernel_events = kernlog.KernelEvents()
        self.thermal_state = thermal.ThermalState()
        self._active_guard: thermal.ThermalGuard | None = None

    # -- helpers -----------------------------------------------------------

    def _should_stop(self) -> bool:
        return bool(self.stop_flag and self.stop_flag.is_set())

    def has_failed(self) -> bool:
        """A FAIL condition already found by the passes themselves."""
        findings = self.state.verify_findings
        return bool(findings.read_errors or findings.nonzero_ranges)

    def _stop_requested(self) -> bool:
        """Signal, or --stop-on-fail once the outcome is already decided.

        Past the first read error the grade is FAIL and nothing later can
        change it; the remaining hours only refine HOW MANY bad sectors there
        are. That map is worth having by default -- "3 bad sectors in one 30 GB
        band" and "bad sectors throughout" are different products -- so this is
        opt-in, for triaging a pile you are happy to scrap.
        """
        if self._should_stop():
            return True
        if getattr(self.options, "stop_on_fail", False) and self.has_failed():
            if not self.state.stopped_on_fail:
                self.state.stopped_on_fail = True
                _log.error(
                    "%s: --stop-on-fail -- a FAIL condition was found, "
                    "abandoning the rest of this pass. The bad-sector map will "
                    "be incomplete and the report will say so.", self.disk.id,
                )
            return True
        return False

    def _check_stop(self) -> None:
        if self._should_stop():
            raise DriveInterrupted("interrupted by signal")

    def _progress(self, done, total, inst, mean):
        # Fold the live thermal reading into the checkpoint on every tick,
        # so an interrupted run does not lose the peak it already saw.
        if self._active_guard is not None:
            self._observe_thermal(self._active_guard.state)
        self.state.phase_offset = done
        self.state.write_progress(done, total, inst, mean,
                                  self.pass_cfg.progress_interval_s)
        self.state.checkpoint(interval_s=self.checkpoint_interval)

    # -- phases ------------------------------------------------------------

    def phase1_smart_before(self) -> None:
        self.state.enter_phase(st.PHASE_SMART_BEFORE)
        self.smart_before = smart.probe(
            self.disk.dev_path, self.disk.bus_type, self.disk.size_bytes,
            scan_hint=getattr(self.options, "scan_hint", {}).get(self.disk.dev_path),
        )
        self.state.smartctl_d_type = self.smart_before.d_type
        self.state.smart_available = self.smart_before.available

        if self.smart_before.available:
            self.state.ata_serial = self.smart_before.serial or ""
            # Persisted so the child process and any later resume can still
            # compute the before/after delta; holding it only in memory meant
            # it was lost at every process boundary.
            self.state.smart_before_data = self.smart_before.data
            self._archive_smart(self.smart_before, "smart-before.txt")
        else:
            _log.warning(
                "%s: SMART is not available through this drive's bridge. The "
                "grade will cap at CAUTION. Shucking this drive and connecting "
                "it to SATA would yield full SMART data.", self.disk.id,
            )
        self.state.checkpoint(force=True)

    def smart_gate(self) -> list[str]:
        """FAIL conditions already visible in the pre-erase snapshot (spec 6.3).

        The short self-test gate exists so a bad drive is not erased for eight
        hours before being failed. The same argument applies to SMART itself: a
        drive that already reports pending sectors, offline-uncorrectables or a
        FAILED health status is going to grade FAIL whatever the surface read
        says, so writing to it first buys nothing.

        Deliberately reuses the ordinary rubric rather than a second list of
        thresholds. Erase and verify are marked not-performed, so the
        conditions that depend on them contribute nothing here and only the
        genuinely pre-known failures can fire.

        Returns the FAIL reasons, or an empty list to proceed.
        """
        if not self.state.smart_available or not self.state.smart_before_data:
            return []

        before = smart.SmartResult(available=True,
                                   d_type=self.state.smartctl_d_type,
                                   data=self.state.smart_before_data)
        probe = {
            "smart": before.to_json(),
            "self_tests": {"short": {"run": False, "status": "not_run"},
                           "extended": {"run": False, "status": "not_run"}},
            "erase": {"performed": False},
            "verify": {"performed": False},
            "run_conditions": {"kernel_events": {}},
            "flags": {},
        }
        result = grading.evaluate(probe, self.config)
        if result.value != grading.FAIL:
            return []

        reasons = [r for r in result.reasons if r.startswith("FAIL")]
        _log.error(
            "%s: SMART already shows a failing condition before any writing. "
            "Skipping the erase -- this drive would grade FAIL whatever the "
            "surface read found, so there is nothing to gain from eight hours "
            "of writing to it. It will be reported FAIL and left UNERASED.",
            self.disk.id,
        )
        for reason in reasons:
            _log.error("  %s", reason)
        self.state.failed_reason = "; ".join(reasons)
        self.state.checkpoint(force=True)
        return reasons

    def phase2_short_test(self) -> bool:
        """Returns True if the drive may proceed to the erase.

        A failed short test aborts the drive before spending hours erasing it.
        This is the highest-value optimization in the pipeline and the main
        reason phase 2 exists. The drive is skipped, the batch token is
        unaffected, and its report carries erase.performed = false.
        """
        self.state.enter_phase(st.PHASE_SHORT_TEST)
        if not self.state.smart_available:
            self.state.short_test = smart.SelfTestResult(
                run=False, status="smart_unavailable").to_json()
            self.state.checkpoint(force=True)
            return True

        cfg = self.config.get("selftest", {})
        estimate = self._polling_estimate("short")
        result = smart.run_selftest(
            self.disk.dev_path, self.state.smartctl_d_type, "short",
            poll_interval_s=cfg.get("short_poll_s", 30),
            estimated_minutes=estimate,
            overrun_warn_factor=cfg.get("overrun_warn_factor", 1.5),
            no_progress_factor=cfg.get("no_progress_factor", 3.0),
            should_stop=self._should_stop,
        )
        self.state.short_test = result.to_json()
        self.state.checkpoint(force=True)

        if result.status in ("completed_without_error", "inconclusive",
                             "smart_unavailable", "interrupted"):
            return True
        if result.status.startswith("could_not_start"):
            _log.warning("%s: could not start the short self-test (%s); "
                         "continuing", self.disk.id, result.status)
            return True

        _log.error(
            "%s: short self-test reported %s. Skipping the erase entirely -- "
            "this drive is not worth 8 hours. It will be reported FAIL and "
            "left UNERASED.", self.disk.id, result.status,
        )
        self.state.failed_reason = f"short self-test: {result.status}"
        return False

    def _polling_estimate(self, kind: str) -> int | None:
        """The drive's own polling-time estimate for a self-test, in minutes.

        Must fall back to the PERSISTED before-snapshot, not to None. Phase 1
        runs in the parent (spec 7) so `self.smart_before` is None in the child
        that actually runs the self-tests. Returning None there makes
        run_selftest() default to a 5-minute estimate, which sets the
        no-progress deadline to 15 minutes -- and a six-hour extended test does
        not move remaining_percent within 15 minutes, so a perfectly healthy
        drive gets declared "inconclusive" 20 minutes in and graded CAUTION for
        it. That defeats the whole point of phase 6.
        """
        if self.smart_before and self.smart_before.available:
            value = self.smart_before.selftest_polling_minutes.get(kind)
            if value:
                return value
        if self.state.smart_before_data:
            rehydrated = smart.SmartResult(
                available=True, d_type=self.state.smartctl_d_type,
                data=self.state.smart_before_data)
            return rehydrated.selftest_polling_minutes.get(kind)
        return None

    def _start_offset_for(self, phase: int) -> int:
        """Where this phase should begin.

        A carried-over offset is ONLY valid when the checkpoint is already in
        the phase being entered. Reusing it across a phase transition made
        phase 5 start where phase 4 finished -- at the end of the device -- so
        the verification pass read only the final tail chunk. Worse,
        verify_zero() sets bytes_done = start_offset on entry, so the run then
        reported the full capacity as verified: a positive, fabricated
        "all bytes read back as zero" claim on a drive that was never read.

        That is the exact failure this tool exists to make impossible, so the
        rule is explicit: resume within a phase, always restart a new one.
        """
        if self.state.phase == phase:
            return self.state.resume_offset(self.pass_cfg.chunk_bytes)
        return 0

    def phase4_erase(self) -> None:
        self.state.enter_phase(st.PHASE_ERASE,
                               self._start_offset_for(st.PHASE_ERASE))
        self.state.erase_started_utc = self.state.erase_started_utc or log.utcstamp()
        self._run_pass(write=True)
        self.state.erase_finished_utc = log.utcstamp()
        self.state.erase_performed = True
        self.state.checkpoint(force=True)
        safety.reread_partition_table(self.disk)

    def phase5_verify(self) -> None:
        self.state.enter_phase(st.PHASE_VERIFY,
                               self._start_offset_for(st.PHASE_VERIFY))
        self.state.verify_started_utc = self.state.verify_started_utc or log.utcstamp()
        self._run_pass(write=False)
        self.state.verify_finished_utc = log.utcstamp()

        # Coverage assertion. verify.performed drives a positive claim on the
        # report -- "confirmed all N bytes read back as zero" -- so it is only
        # set when the pass actually reached the end of the device. Anything
        # short means the claim is not supported, whatever the reason, and the
        # report must not make it.
        covered = self.state.verify_findings.bytes_done
        if covered >= self.disk.size_bytes:
            self.state.verify_performed = True
        else:
            self.state.verify_performed = False
            _log.error(
                "%s: verification covered only %d of %d bytes (%.4f%%). NOT "
                "recording this as a completed verification -- the report will "
                "not claim the erase was confirmed.",
                self.disk.id, covered, self.disk.size_bytes,
                100 * covered / max(self.disk.size_bytes, 1),
            )
        self.state.checkpoint(force=True)

    def _run_pass(self, write: bool) -> None:
        """Run one full-device pass, surviving disconnects (spec 9).

        Findings accumulate across reconnects and across resumes. They are
        checkpointed continuously, because a resumed run that reports clean on a
        drive that had already failed is the single most dangerous bug
        available in this design.
        """
        findings = (self.state.erase_findings if write
                    else self.state.verify_findings)
        io_cfg = self.config.get("io", {})
        max_reconnects = io_cfg.get("reconnect_max_per_phase", 3)
        wait_s = io_cfg.get("reconnect_wait_s", 60)
        reconnects = 0

        while True:
            self._check_stop()
            offset = self.state.resume_offset(self.pass_cfg.chunk_bytes)
            if offset >= self.disk.size_bytes:
                return

            try:
                with safety.guarded_open(
                    self.disk, self.state.identity, write=write,
                    test_mode=self.options.test_mode,
                    output_root=Path(self.options.output_root),
                ) as (fd, kname):
                    self.state.kernel_name = kname
                    self.state.locators.open_epoch(kname)
                    self.state.checkpoint(force=True)

                    blockio.check_size_agreement(fd, self.disk.size_bytes)

                    guard = thermal.ThermalGuard(
                        self.disk.dev_path, self.state.smartctl_d_type,
                        self.config.get("thermal", {}),
                        phase_allows_pause=True,
                    ) if self.state.smart_available else None

                    self._active_guard = guard
                    if guard:
                        guard.start()
                    try:
                        runner = blockio.zero_fill if write else blockio.verify_zero
                        runner(
                            fd, self.disk.size_bytes, self.pass_cfg,
                            start_offset=offset, findings=findings,
                            progress=self._progress,
                            should_stop=self._stop_requested,
                            pause_gate=guard.wait_if_paused if guard else None,
                        )
                    finally:
                        if guard:
                            guard.stop()
                            self._absorb_thermal(guard.state)
                        self._active_guard = None
                    self.state.locators.close_current()
                    self.state.phase_offset = findings.bytes_done
                    self.state.checkpoint(force=True)

                if self.thermal_state.aborted:
                    raise DriveAborted(self.thermal_state.abort_reason
                                       or "thermal abort")
                return

            except blockio.DeviceVanishedError as exc:
                reconnects += 1
                findings.disconnects += 1
                self.state.disconnects += 1
                self.state.phase_offset = findings.bytes_done
                self.state.locators.close_current()
                self.state.checkpoint(force=True)
                if reconnects > max_reconnects:
                    raise DriveAborted(
                        f"device disconnected {reconnects} times in one phase; "
                        f"giving up. {exc}"
                    ) from exc
                _log.warning(
                    "%s: %s. Checkpointed at %d bytes; waiting up to %d s for "
                    "the device to return.", self.disk.id, exc,
                    findings.bytes_done, wait_s,
                )
                if not self._await_return(wait_s):
                    raise DriveAborted(
                        f"device did not return within {wait_s} s"
                    ) from exc

    def _await_return(self, wait_s: int) -> bool:
        """Wait for the drive to come back, identified by its tuple.

        The kernel name will very likely have changed, and on a different USB
        port the H:C:T:L will too, so the search is by identity rather than by
        name.
        """
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self._should_stop():
                return False
            time.sleep(2)
            for candidate in inv.scan():
                if candidate.identity == self.state.identity:
                    _log.info(
                        "%s: device returned as %s%s", self.disk.id,
                        candidate.kname,
                        "" if candidate.kname == self.disk.kname
                        else " (kernel name changed)",
                    )
                    self.disk = candidate
                    return True
        return False

    def _observe_thermal(self, tstate: thermal.ThermalState) -> None:
        """Idempotent live update, safe to call on every progress tick.

        Spec 9.1 lists max temperature among the findings the checkpoint must
        carry. Absorbing only at end-of-pass meant a run killed three hours
        into an erase lost the peak it had already seen, so the resumed run
        would understate it -- and a drive that actually reached 58 C could
        come back PASS instead of CAUTION. Same failure shape as findings not
        surviving a resume.

        Only the max is updated here; paused_seconds accumulates and would
        double-count if added repeatedly.
        """
        if tstate.max_temp_c is not None:
            self.thermal_state.max_temp_c = max(
                self.thermal_state.max_temp_c or 0, tstate.max_temp_c)
            self.state.max_temp_c = self.thermal_state.max_temp_c

    def _absorb_thermal(self, tstate: thermal.ThermalState) -> None:
        """End-of-pass absorb. Adds the accumulating counters exactly once."""
        self._observe_thermal(tstate)
        self.thermal_state.paused_seconds += tstate.paused_seconds
        self.state.thermal_pause_s = self.thermal_state.paused_seconds
        self.thermal_state.monitoring_possible &= tstate.monitoring_possible
        if tstate.aborted:
            self.thermal_state.aborted = True
            self.thermal_state.abort_reason = tstate.abort_reason
            self.state.thermally_aborted = True

    def phase6_extended_test(self) -> None:
        self.state.enter_phase(st.PHASE_EXTENDED_TEST)
        if getattr(self.options, "stop_on_fail", False) and self.has_failed():
            _log.info("%s: already failed; skipping the extended self-test",
                      self.disk.id)
            self.state.extended_test = smart.SelfTestResult(
                run=False, status="skipped_already_failed").to_json()
            self.state.checkpoint(force=True)
            return
        if self.options.skip_extended_test:
            self.state.skipped_extended_test = True
            self.state.extended_test = smart.SelfTestResult(
                run=False, status="skipped").to_json()
            self.state.checkpoint(force=True)
            return
        if not self.state.smart_available:
            self.state.extended_test = smart.SelfTestResult(
                run=False, status="smart_unavailable").to_json()
            self.state.checkpoint(force=True)
            return

        cfg = self.config.get("selftest", {})
        estimate = self._polling_estimate("extended")

        # Phase 6 cannot be throttled by withholding host I/O -- the self-test
        # runs on the drive itself -- so the guard aborts instead of pausing.
        guard = thermal.ThermalGuard(
            self.disk.dev_path, self.state.smartctl_d_type,
            self.config.get("thermal", {}),
            phase_allows_pause=False,
            on_abort=lambda _r: smart.abort_selftest(
                self.disk.dev_path, self.state.smartctl_d_type),
        )
        guard.start()
        try:
            result = smart.run_selftest(
                self.disk.dev_path, self.state.smartctl_d_type, "long",
                poll_interval_s=cfg.get("extended_poll_s", 300),
                estimated_minutes=estimate,
                overrun_warn_factor=cfg.get("overrun_warn_factor", 1.5),
                no_progress_factor=cfg.get("no_progress_factor", 3.0),
                should_stop=lambda: self._should_stop() or guard.aborted,
            )
        finally:
            guard.stop()
            self._absorb_thermal(guard.state)

        if guard.state.aborted and result.status != "completed_without_error":
            result = smart.SelfTestResult(
                run=True, status="inconclusive", duration_s=result.duration_s)
        self.state.extended_test = result.to_json()
        self.state.checkpoint(force=True)

    def phase7_smart_after(self) -> None:
        self.state.enter_phase(st.PHASE_SMART_AFTER)
        if self.state.smart_available:
            self.smart_after = smart.refresh(self.disk.dev_path,
                                             self.state.smartctl_d_type)
            self._archive_smart(self.smart_after, "smart-after.txt")

        self.kernel_events = kernlog.sweep(
            self.state.locators, self.state.run_started_utc or log.utcstamp())
        kernlog.write_log_file(self.kernel_events,
                               self.state.output_dir / "kernel-log.txt")
        self.state.kernel_events = self.kernel_events.to_json()
        self.state.checkpoint(force=True)

    def _archive_smart(self, result: smart.SmartResult, name: str) -> None:
        """Raw smartctl -x output is what a skeptical buyer will ask to see."""
        chunks = [result.text or ""]
        for section, text in (result.logs or {}).items():
            chunks.append(f"\n\n===== smartctl -l {section} =====\n{text}")
        (self.state.output_dir / name).write_text("".join(chunks), encoding="utf-8")

    # -- report ------------------------------------------------------------

    def build_report(self, interrupted: bool = False,
                     too_many_disconnects: bool = False) -> dict:
        self.state.enter_phase(st.PHASE_REPORT)
        disk = self.disk
        # Rehydrate the before-snapshot when this object did not run phase 1
        # itself -- it runs in the parent, before the confirmation gate.
        before = self.smart_before
        if before is None and self.state.smart_before_data:
            before = smart.SmartResult(
                available=True, d_type=self.state.smartctl_d_type,
                data=self.state.smart_before_data)
        after = self.smart_after or before

        erase_findings = self.state.erase_findings
        verify_findings = self.state.verify_findings

        erase_duration = _elapsed(self.state.erase_started_utc,
                                  self.state.erase_finished_utc)
        verify_duration = _elapsed(self.state.verify_started_utc,
                                   self.state.verify_finished_utc)

        smart_json = (after.to_json() if after
                      else {"available": False, "overall_health": None,
                            "power_on_hours": None, "power_cycles": None,
                            "attributes": [], "probe_log": []})
        if before and after and before is not after:
            smart_json["before_after_delta"] = smart.delta(before, after)
        else:
            smart_json["before_after_delta"] = []

        erase_block = {
            "performed": self.state.erase_performed,
            "method": "single_pass_zero",
            "standard_alignment": "NIST SP 800-88 Rev.1 Clear",
            "bytes_written": erase_findings.bytes_done,
            "started_utc": self.state.erase_started_utc,
            "finished_utc": self.state.erase_finished_utc,
            "duration_s": erase_duration,
            **{f"throughput_{k}": v for k, v in
               blockio.summarize_throughput(erase_findings.throughput_samples).items()},
        }
        if not self.state.erase_performed:
            erase_block = {
                "performed": False,
                "not_performed_reason": self.state.failed_reason
                or self.state.incomplete_reason
                or "the erase phase did not run",
                "method": None, "standard_alignment": None,
                "bytes_written": None, "started_utc": None,
                "finished_utc": None, "duration_s": None,
                "throughput_mean_mbs": None, "throughput_min_mbs": None,
                "throughput_max_mbs": None,
            }

        if self.state.verify_performed:
            clean = (not verify_findings.read_errors
                     and not verify_findings.nonzero_ranges)
            verify_block = {
                "performed": True,
                "bytes_read": verify_findings.bytes_done,
                "nonzero_ranges": [r.to_json() for r in verify_findings.nonzero_ranges],
                "read_error_ranges": [r.to_json() for r in
                                      verify_findings.read_error_ranges],
                "read_errors": verify_findings.read_errors,
                "duration_s": verify_duration,
                "ranges_truncated": verify_findings.ranges_truncated,
                "stopped_on_fail": self.state.stopped_on_fail,
                "result": "all_zero_no_errors" if clean else "errors_found",
            }
        else:
            verify_block = {
                "performed": False,
                "bytes_read": verify_findings.bytes_done or None,
                "nonzero_ranges": [r.to_json() for r in
                                   verify_findings.nonzero_ranges],
                "read_error_ranges": [r.to_json() for r in
                                      verify_findings.read_error_ranges],
                "read_errors": verify_findings.read_errors or None,
                "duration_s": verify_duration,
                "ranges_truncated": verify_findings.ranges_truncated,
                "stopped_on_fail": self.state.stopped_on_fail,
                "result": None,
            }

        report = {
            "schema_version": SCHEMA_VERSION,
            "report_id": f"DP-{_datestamp(self.state.run_started_utc)}-{disk.id}",
            "batch_id": self.state.batch_id,
            "tool": {"name": TOOL_NAME, "version": __version__},
            "generated_utc": log.utcstamp(),
            "seller_name": self.options.seller_name or "",
            "drive": {
                "by_id": disk.by_id,
                "kernel_name_at_run": self.state.kernel_name or disk.kname,
                "vendor": _vendor(before, disk),
                "model": (before.model if before and before.available
                          else disk.model),
                "family": None,
                "enclosure_serial": _masked(disk.serial, self.options.mask_serial),
                "ata_serial": _masked(self.state.ata_serial,
                                      self.options.mask_serial),
                "firmware": before.firmware if before and before.available else None,
                "capacity_bytes": disk.size_bytes,
                "capacity_label": disk.capacity_label,
                "logical_block_bytes": disk.logical_block_bytes,
                "physical_block_bytes": disk.physical_block_bytes,
                "rotation_rpm": (before.rotation_rate
                                 if before and before.available else None),
                "form_factor": (before.form_factor
                                if before and before.available else None),
                "bus_type": disk.bus_type,
                "enclosure": disk.model,
                "smartctl_device_type": self.state.smartctl_d_type,
                "sysfs_rotational": disk.sysfs_rotational,
                "identity": (self.state.identity.to_json()
                             if self.state.identity else None),
                "locator_epochs": self.state.locators.to_json(),
            },
            "smart": smart_json,
            "self_tests": {
                "short": self.state.short_test or {"run": False,
                                                   "status": "not_run"},
                "extended": self.state.extended_test or {"run": False,
                                                         "status": "not_run"},
            },
            "erase": erase_block,
            "verify": verify_block,
            "run_conditions": {
                "temp_max_c": self.thermal_state.max_temp_c,
                "thermal_pause_s": round(self.thermal_state.paused_seconds),
                "thermal_abort_reason": self.thermal_state.abort_reason,
                "disconnects": self.state.disconnects,
                "kernel_events": self.kernel_events.to_json(),
            },
            "flags": {
                "skipped_extended_test": self.state.skipped_extended_test,
                "smart_via_bridge_unavailable": not self.state.smart_available,
                "thermally_aborted": self.thermal_state.aborted,
                "interrupted": interrupted,
                "too_many_disconnects": too_many_disconnects,
            },
        }
        report["grade"] = grading.evaluate(report, self.config).to_json()
        return report

    def emit(self, report: dict) -> None:
        out = self.state.output_dir
        st.atomic_write_json(out / "report.json", report)
        html_path = reporting.render_html(report, out / "report.html")
        reporting.render_png(html_path, out / "report.png")
        # The two-page document that goes in the box with the drive.
        bundle = reporting.render_print_bundle(report, out / "report-print.html")
        reporting.render_pdf(bundle, out / "report.pdf")
        self.state.checkpoint(force=True)
        _log.info("%s: graded %s", self.disk.id, report["grade"]["value"])
        for reason in report["grade"]["reasons"]:
            _log.info("  %s", reason)


def _elapsed(start: str | None, end: str | None) -> int | None:
    from datetime import datetime
    if not (start and end):
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return int((datetime.strptime(end, fmt)
                    - datetime.strptime(start, fmt)).total_seconds())
    except ValueError:
        return None


def _datestamp(stamp: str | None) -> str:
    return (stamp or log.utcstamp())[:10].replace("-", "")


def _vendor(result, disk) -> str:
    model = (result.model if result and result.available else disk.model) or ""
    first = model.split()[0] if model else ""
    known = {"WDC": "Western Digital", "ST": "Seagate", "HGST": "HGST",
             "TOSHIBA": "Toshiba", "SAMSUNG": "Samsung", "HITACHI": "Hitachi"}
    for key, name in known.items():
        if model.upper().startswith(key):
            return name
    return first or "Hard disk drive"


def _masked(serial: str | None, mask: bool) -> str | None:
    """--mask-serial redacts the middle, keeping enough to identify the unit."""
    if not serial or not mask or len(serial) <= 6:
        return serial
    keep = 3
    return f"{serial[:keep]}{'*' * (len(serial) - 2 * keep)}{serial[-keep:]}"
