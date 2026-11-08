#!/usr/bin/env python3
"""Put the Adreno 540 probe (chiron, MSM8998) into a pinned HackiOS/XNU source tree.

The probe is appended to the end of upstream's bsd/dev/hakios.adreno.c, which is where
rd(), wr(), say(), hold(), ml_io_map() and the boot-argument parsing already are; it
brings no file of its own, so nothing has to be added to bsd/conf/files.

hakios_adreno_init() is called once while the system starts, before there is a root file
system. One line at its top calls the probe, and a declaration above it is what C needs
for a definition that comes later in the same file.

Every edit is checked against the exact context it replaces, so a tree that has moved on
is refused instead of being patched in the wrong place.
"""

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "chiron_gpu_probe.c"

# The one place the probe is called from: the definition, not a declaration of it.
ANCHOR = "void\nhakios_adreno_init(void)\n{\n"
CALL = (
    "void hakios_chiron_gpu_init(void);\n"
    "\n"
    "void\n"
    "hakios_adreno_init(void)\n"
    "{\n"
    "\thakios_chiron_gpu_init();\n"
    "\n"
)


def replace_once(text, old, new, what):
    if text.count(old) != 1:
        raise ValueError(f"{what}: expected exactly one {old[:60]!r}, found {text.count(old)}")
    return text.replace(old, new, 1)


def overlay(tree):
    tree = Path(tree)
    target = tree / "bsd/dev/hakios.adreno.c"
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"missing regular upstream driver: {target}")

    text = target.read_text()
    if "hakios_chiron_gpu_init" in text:
        raise ValueError("the probe is already in this tree; use a fresh one")

    probe = PROBE.read_text(encoding="utf-8")
    if "hakios_chiron_gpu_init(void)" not in probe:
        raise ValueError(f"the probe source has no entry point: {PROBE}")

    # 1. the call, at the top of the one function that runs before the root file system
    text = replace_once(text, ANCHOR, CALL, "hakios_adreno_init")

    # 2. the probe itself, at the end of the file it shares state with
    if not text.endswith("\n"):
        text += "\n"
    text += "\n/* ---- chiron (MSM8998) Adreno 540 probe, appended by ci/hackios/apply_chiron_gpu.py ---- */\n"
    text += probe

    target.write_text(text)
    return {
        "file": str(target.relative_to(tree)),
        "bytes": len(text.encode()),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, required=True, help="the XNU tree to patch (xnu/)")
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()

    result = overlay(args.source)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "schema": "chiron-gpu-probe-overlay-v1",
        "kind": "kernel",
        "status": "read-only-probe-never-run-on-the-phone",
        "boot_arg": "chiron_gpu=N (1..5); absent = run all and return to the loader; 0 = do nothing",
        "return_arg": "chiron_gpu_return=1",
        "writes": "none",
        "hardware_observed": False,
        "result": result,
    }, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
