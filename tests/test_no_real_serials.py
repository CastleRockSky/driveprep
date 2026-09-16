"""No real hardware serial may reach this repository.

The repo is public. Real drive serials reached staged changes three times --
twice in new test files, once in a library docstring that had been committed
and public for weeks -- and each was caught only because someone happened to
scan before committing. This makes it structural.

HOW THIS WORKS, AND WHY IT IS NOT A BLOCKLIST
=============================================
A test that greps for the serials we want to keep out would have to CONTAIN
them, publishing in the guard exactly what the guard exists to prevent. So
this matches serial-SHAPED strings instead and requires each one to be on the
allowlist of values known to be synthetic.

Adding a value here is therefore a deliberate act: if a scan fails, the fix is
almost always to replace the string with a synthetic one, NOT to widen the
allowlist. Only add to ALLOWED when the value is provably fake or is a
published model number.
"""
from __future__ import annotations

import binascii
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent

# Directories whose contents we control. `build/` is generated from these and
# is not committed, so scanning it would only produce duplicate failures.
SCAN_DIRS = ("driveprep", "tests", "docs")
SCAN_FILES = ("README.md",)
SCAN_SUFFIXES = (".py", ".md", ".toml", ".txt", ".html", ".css")


# --------------------------------------------------------------------------
# What a drive serial looks like
# --------------------------------------------------------------------------

PATTERNS = {
    # WD ATA serials, e.g. WD-TESTAAAA0001. Real ones are longer than the
    # eight-character minimum and always carry the WD- prefix.
    "WD ATA serial": re.compile(r"\bWD-[A-Z0-9]{8,}\b"),

    # The dock and similar enclosures report a long all-digit serial.
    "enclosure serial": re.compile(r"\bDD\d{11}\b"),

    # Seagate and HGST: eight alphanumerics carrying at least one digit and at
    # least two letters, e.g. Z000TEST or 0TESTAA0. Tuned against every such
    # serial that has crossed this bench -- nine distinct shapes, letter-first
    # and digit-first, one to four digits -- without recording any of them.
    # Model numbers share the shape, which is why ST2000DM and WD40EZRZ are
    # allowlisted rather than the pattern being loosened.
    "Seagate/HGST serial": re.compile(
        r"\b(?=[A-Z0-9]{8}\b)(?=[A-Z0-9]*[0-9])(?=(?:[A-Z0-9]*[A-Z]){2})"
        r"[A-Z0-9]{8}\b"
    ),

    # Longer HGST serials: two letters, four digits, then six or more, e.g.
    # AA0000TESTAA.
    "HGST long serial": re.compile(r"\b[A-Z]{2}\d{4}[A-Z0-9]{6,}\b"),
}

# udev writes some enclosure serials hex-encoded. Handled separately because
# the decode is what makes it precise: a SHA or a git hash is the same shape
# but does not decode to a printable identifier.
HEX_RUN = re.compile(r"\b[0-9a-fA-F]{12,}\b")


# --------------------------------------------------------------------------
# Values that are known-synthetic or otherwise safe to publish
# --------------------------------------------------------------------------

ALLOWED = {
    # Synthetic serials used across the tests and the spec.
    "WD-TESTAAAA0001",
    "WD-TESTBBBB0002",
    "WD-TESTCCCC0003",
    "WD-WCC4N1234567",
    "TESTSERIAL01",
    "ENCL0000000",

    # Published MODEL numbers, which identify a product line and no device.
    "ST2000DM",
    "WD40EZRZ",
}

# One synthetic sample per vendor shape, used by the pattern tests below. They
# are allowlisted because they are fake; they exist so the shapes can be
# asserted without writing a single real serial into this file.
SHAPE_SAMPLES = {
    "WD-TESTAAAA0001",   # WD ATA serial
    "Z000TEST",          # Seagate, letter-first
    "0TESTAA0",          # WD white label, digit-first
    "AA0000TESTAA",      # HGST long form
    "DD00000000000",     # enclosure, DD + 11 digits
}
ALLOWED |= SHAPE_SAMPLES

# Hex strings whose decoded form is allowlisted are allowed too, but listing
# them explicitly keeps the failure message honest about which spelling was
# found.
ALLOWED_HEX = {
    "5445535453455249414c3031",  # TESTSERIAL01
    "575830303031",              # WX0001
}


def _decodes_to_identifier(hex_text: str) -> str | None:
    """The ASCII an even-length hex run decodes to, if it looks like a serial.

    udev's hex spelling of a serial always decodes to printable identifier
    characters. Hashes and digests do not, which is what keeps this from
    flagging every checksum in the documentation.
    """
    if len(hex_text) % 2:
        return None
    try:
        raw = binascii.unhexlify(hex_text)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if re.fullmatch(r"[A-Za-z0-9 _.-]{4,}", text) else None


def _files() -> list[Path]:
    found: list[Path] = []
    for directory in SCAN_DIRS:
        root = REPO / directory
        if not root.is_dir():
            continue
        found += [p for p in sorted(root.rglob("*"))
                  if p.is_file() and p.suffix in SCAN_SUFFIXES
                  and "__pycache__" not in p.parts]
    found += [REPO / name for name in SCAN_FILES if (REPO / name).is_file()]
    return found


def _findings() -> list[tuple[str, str, str, int]]:
    """(kind, value, relative path, line number) for every suspect string."""
    out = []
    for path in _files():
        rel = str(path.relative_to(REPO))
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover - unreadable file
            continue
        for number, line in enumerate(lines, 1):
            for kind, pattern in PATTERNS.items():
                for value in pattern.findall(line):
                    if value not in ALLOWED:
                        out.append((kind, value, rel, number))
            for value in HEX_RUN.findall(line):
                if value.lower() in {h.lower() for h in ALLOWED_HEX}:
                    continue
                decoded = _decodes_to_identifier(value)
                if decoded and decoded not in ALLOWED:
                    out.append((f"hex-encoded serial (decodes to {decoded!r})",
                                value, rel, number))
    return out


# --------------------------------------------------------------------------


def test_no_real_hardware_serials_in_the_repo():
    findings = _findings()
    if not findings:
        return
    report = "\n".join(
        f"  {path}:{number}\n      {kind}: {value}"
        for kind, value, path, number in findings
    )
    pytest.fail(
        "Serial-shaped strings found that are not known to be synthetic.\n\n"
        f"{report}\n\n"
        "This repository is public. If any of these is a real drive or\n"
        "enclosure serial, replace it with a synthetic value -- WD-TESTAAAA0001,\n"
        "ENCL0000000, TESTSERIAL01 (hex 5445535453455249414c3031).\n\n"
        "Only add to ALLOWED in this file when the value is provably fake or is\n"
        "a published model number. Widening the allowlist to silence a real\n"
        "serial defeats the entire guard."
    )


# --------------------------------------------------------------------------
# The guard's own behaviour. A scanner that cannot fail is worse than none,
# because it reports success over work it never did.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("serial", sorted(SHAPE_SAMPLES))
def test_the_patterns_catch_every_serial_shape(serial):
    """Every vendor shape this bench has seen must match some pattern.

    These are SYNTHETIC stand-ins carrying the real shapes. Writing the actual
    serials here -- which is what the first draft of this file did -- would
    have published eight of them to prove the guard works, and the guard duly
    failed on its own test data. That failure is the reason SHAPE_SAMPLES
    exists and is allowlisted.
    """
    assert any(p.fullmatch(serial) for p in PATTERNS.values()), \
        f"{serial} would slip past every pattern"


def test_the_hex_decoder_recognises_an_encoded_serial():
    assert _decodes_to_identifier("5445535453455249414c3031") == "TESTSERIAL01"


def test_the_hex_decoder_ignores_digests():
    """A SHA1 is the same shape and must not be flagged."""
    assert _decodes_to_identifier("da39a3ee5e6b4b0d3255bfef95601890afd80709") is None


def test_a_planted_serial_is_detected(tmp_path, monkeypatch):
    """End to end: the scan must fail when a real-shaped serial appears."""
    # Assembled at runtime so this file contains no serial-shaped literal of
    # its own -- otherwise the guard would flag its own regression test.
    leaked = "WD-" + "WCC" + "4M" + "9" * 7
    planted = tmp_path / "driveprep"
    planted.mkdir()
    (planted / "leak.py").write_text(
        f"# serial {leaked} pasted from a live drive\n", encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "REPO", tmp_path)

    findings = _findings()
    assert any(value == leaked for _, value, _, _ in findings), \
        "the scan reported clean over a file containing a real-shaped serial"


def test_the_scan_actually_reads_files():
    """Guards against a path bug silently scanning nothing."""
    assert len(_files()) > 20, "the scan found suspiciously few files"
    assert any(p.name == "inventory.py" for p in _files()), \
        "library code must be scanned, not just tests -- one leak was in a docstring"
