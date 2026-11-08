#!/usr/bin/env python3
"""Opt-in, fail-closed MIX2 SMP overlays. Apply after base/USB loader overlays."""

import argparse
import hashlib
import json
from pathlib import Path
import re


HERE = Path(__file__).resolve().parent
CPU_COUNTS = (1, 2, 4, 8)
MPIDRS = (0, 1, 2, 3, 256, 257, 258, 259)


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"chiron SMP source context changed: {old[:100]}")
    return text.replace(old, new, 1)


def command_line(cpu_count):
    if type(cpu_count) is not int or cpu_count not in CPU_COUNTS:
        raise ValueError("chiron SMP supports only 1, 2, 4, or 8 CPUs")
    return f"cpus={cpu_count}"


def _publish(root, edits):
    """Validate all contexts before publishing any edits to a scratch source tree."""
    for relative, text in edits.items():
        target = root / relative
        if not target.parent.is_dir():
            raise ValueError(f"missing source directory: {target.parent}")
        if target.exists() and relative.endswith(".inc") and target.read_text() != text:
            raise ValueError(f"refusing to overwrite different SMP include: {target}")
    for relative, text in edits.items():
        (root / relative).write_text(text)
    return {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in edits}


def loader_overlay(source_repo: Path, cpu_count: int) -> dict:
    command_line(cpu_count)
    source_repo = Path(source_repo)
    text = (source_repo / "xnuloader/loader.c").read_text()
    if "CHIRON_SMP_LIMIT" in text:
        raise ValueError("chiron SMP loader overlay already applied; use a fresh scratch tree")
    # Accept either the base guard or the USB-adjusted guard. Nothing else is guessed.
    guards = (
        "\tif (PLAT == &platform_chiron) {\n"
        "\t\tif (kernel_mpidr != this_mpidr || more_cores) {\n",
        "\tif (PLAT == &platform_chiron) {\n"
        "\t\tmore_cores = 0;\n"
        "\t\tif (kernel_mpidr != this_mpidr) {\n",
    )
    matched = [g for g in guards if text.count(g) == 1]
    if len(matched) != 1:
        raise ValueError("apply base/USB overlays before SMP; unknown single-core guard")
    old_guard = matched[0] + (
        '\t\t\tprint("chiron: first port is the measured boot CPU only\\n");\n'
        "\t\t\treturn 0;\n\t\t}\n\t}\n"
    )
    text = replace_once(
        text, old_guard,
        "\tif (PLAT == &platform_chiron) {\n"
        "\t\tint final_count;\n"
        "\t\tif (!chiron_smp_parse(ba->commandLine, &final_count) ||\n"
        "\t\t    final_count != chiron_cpu_count) {\n"
        '\t\t\tprint("chiron smp: final boot arguments changed CPU selection\\n");\n'
        "\t\t\treturn 0;\n\t\t}\n\t}\n",
    )
    text = replace_once(
        text, "\t.uart = 0, .psci_hvc = 0, .cores = 1, .core_shift = 0,",
        f"\t.uart = 0, .psci_hvc = 0, .cores = {cpu_count}, .core_shift = 0,",
    )
    text = replace_once(
        text, "static int64_t\nrun_on_other_cpu(",
        f'#define CHIRON_SMP_LIMIT {cpu_count}\n#include "chiron_smp_loader.inc"\n\n'
        "static int64_t\nrun_on_other_cpu(",
    )
    text = replace_once(
        text, "READ_SYSREG(MPIDR_EL1) & 0xFFFFFF, kernel_mpidr = this_mpidr;",
        "READ_SYSREG(MPIDR_EL1) & (PLAT == &platform_chiron ? 0xff00ffffffULL : 0xffffffULL), "
        "kernel_mpidr = this_mpidr;",
    )
    text = replace_once(
        text, "\tif (battery_requested) {\n\t\tcharger_start();",
        "\tuint64_t chiron_cpu_mpidr[8] = { 0 };\n\tint chiron_cpu_count = 0;\n"
        "\tif (PLAT == &platform_chiron &&\n"
        "\t    !chiron_smp_prepare(pi.cmdline, this_mpidr, kernel_mpidr, kern_lo, kern_hi,\n"
        "\t        chiron_cpu_mpidr, &chiron_cpu_count)) return 0;\n"
        "\tif (battery_requested) {\n\t\tcharger_start();",
    )
    text = replace_once(
        text, '\tdt_prop_str(&w, "psci-method", plat->psci_hvc ? "hvc" : "smc");',
        '\tdt_prop_str(&w, "psci-method", plat->psci_hvc ? "hvc" : "smc");\n'
        '\tif (plat == &platform_chiron) dt_prop_u32(&w, "chiron-smp-cpu-count", chiron_cpu_count);',
    )
    text = replace_once(
        text, "\tif (more_cores) {\n\t\tfor (int distance",
        "\tif (plat == &platform_chiron) {\n"
        "\t\tcpu_count = chiron_cpu_count;\n"
        "\t\tfor (int i = 0; i < cpu_count; i++) cpu_mpidr[i] = chiron_cpu_mpidr[i];\n"
        "\t} else if (more_cores) {\n\t\tfor (int distance",
    )
    result = _publish(source_repo, {
        "xnuloader/loader.c": text,
        "xnuloader/chiron_smp_loader.inc": (HERE / "chiron_smp_loader.inc").read_text(),
    })
    return {"schema": "chiron-smp-overlay-v1", "kind": "loader", "cpu_limit": cpu_count,
            "mpidrs": list(MPIDRS[:cpu_count]), "command_line": command_line(cpu_count),
            "kernel_rebuild_required": cpu_count > 1, "hardware_observed": False,
            "result": result}


def kernel_overlay(xnu_tree: Path, strict_firmware_return=False) -> dict:
    xnu_tree = Path(xnu_tree)
    board = "pexpert/pexpert/arm64/BCM2837.h"
    config = "pexpert/pexpert/arm64/board_config.h"
    machine = "osfmk/arm64/machine_routines.c"
    psci = "osfmk/arm64/psci_smp.c"
    gic = "osfmk/arm64/gic_v3_irq.c"
    text = (xnu_tree / board).read_text()
    if "CHIRON_SMP" in text:
        raise ValueError("chiron SMP kernel overlay already applied; use a fresh scratch tree")
    if "#undef __ARM_PAN_AVAILABLE__" not in text:
        raise ValueError("apply CHIRON_PORT kernel overlay before SMP")
    if "-DCHIRON_PORT=1" not in (xnu_tree / "makedefs/MakeInc.def").read_text():
        raise ValueError("missing CHIRON_PORT build flags")
    text = replace_once(text, "#define BCM2837\n",
                        "#define BCM2837\n#if defined(CHIRON_PORT)\n#define CHIRON_SMP 1\n#endif\n")
    edits = {board: text}
    text = (xnu_tree / config).read_text()
    begin = text.index("#ifdef ARM64_BOARD_CONFIG_BCM2837")
    end = text.index("#endif  /* ARM64_BOARD_CONFIG_BCM2837 */", begin)
    section = text[begin:end]
    if not re.search(r"^#define MAX_CPUS\s+8(?:\s|$)", section, re.M):
        raise ValueError("BCM2837 must provide eight CPU slots")
    section = replace_once(
        section, "#define MAX_CPU_CLUSTERS               1",
        "#if defined(CHIRON_SMP)\n#define MAX_CPU_CLUSTERS               2\n"
        "#else\n#define MAX_CPU_CLUSTERS               1\n#endif",
    )
    edits[config] = text[:begin] + section + text[end:]
    text = (xnu_tree / machine).read_text()
    # Only topology bookkeeping uses Aff1. HAS_CLUSTER itself stays undefined:
    # enabling it also enables Apple-only CPU_OVRD instructions in start.s.
    for context in (
        "static uint8_t cluster_initialized = 0;",
        "\t\tuint32_t phys_cluster_id = MPIDR_CLUSTER_ID(cpu->phys_id);",
        "\t\tif (MPIDR_CPU_ID(cpu->phys_id) > cluster_max_cpu_phys_id[phys_cluster_id]) {",
        "\t/*\n\t * Build the cluster offset array, ensuring that the region reserved",
        "\tthis_cpu_datap->cluster_master = !OSTestAndSet",
    ):
        text = replace_once(text, "#if HAS_CLUSTER\n" + context,
                            "#if HAS_CLUSTER || defined(CHIRON_SMP)\n" + context)
    text = replace_once(
        text, "\tpset = pset_find(in_processor_info->cluster_id, NULL);",
        "#if defined(CHIRON_SMP) && CONFIG_SCHED_CLUTCH && !CONFIG_SCHED_EDGE\n"
        "\t/* Clutch's SMP bucket statistics share pset0's lock across physical clusters. */\n"
        "\tpset = &pset0;\n"
        "#else\n"
        "\tpset = pset_find(in_processor_info->cluster_id, NULL);\n"
        "#endif",
    )
    edits[machine] = text
    start = (xnu_tree / "osfmk/arm64/start.s").read_text()
    if "#if HAS_CLUSTER || defined(GENERIC_PSCI_SMP)" not in start or "#0xFFFF" not in start:
        raise ValueError("reset vector must preserve Aff1 and Aff0")
    text = (xnu_tree / psci).read_text()
    text = replace_once(text, "static const char *\npsci_error(",
                        '#if defined(CHIRON_SMP)\n#include "chiron_smp_kernel.inc"\n#endif\n\n'
                        "static const char *\npsci_error(")
    text = replace_once(text, "\t\tpsci_smp_hvc = 1;\n\t}\n}",
                        "\t\tpsci_smp_hvc = 1;\n\t}\n"
                        "#if defined(CHIRON_SMP)\n\tchiron_smp_validate_topology();\n#endif\n}")
    text = replace_once(
        text, "\tstatus = psci_call(PSCI_CPU_ON, phys_id, entry, 0);",
        "#if defined(CHIRON_SMP)\n\tchiron_smp_check_start(cpu, phys_id);\n#endif\n"
        "\tstatus = psci_call(PSCI_CPU_ON, phys_id, entry, 0);",
    )
    text = replace_once(
        text, "\tif (status != 0) {\n",
        "\tif (status != 0) {\n#if defined(CHIRON_SMP)\n"
        '\t\tif (chiron_smp_expected_cpus) panic("chiron smp: CPU_ON failed cpu %d (%lld)", cpu, status);\n'
        "#endif\n",
    )
    text = replace_once(
        text, "\treturn KERN_FAILURE;\n}\n\n/*",
        "#if defined(CHIRON_SMP)\n"
        '\tif (chiron_smp_expected_cpus) panic("chiron smp: cpu %d failed to join scheduler (stage %u)", cpu, stage);\n'
        "#endif\n\treturn KERN_FAILURE;\n}\n\n/*",
    )
    if strict_firmware_return:
        text = replace_once(
            text, "if (state == PSCI_AFFINITY_OFF || state < 0) {",
            "if (state == PSCI_AFFINITY_OFF) {")
        text = replace_once(
            text,
            "\t\tif (os_atomic_load(&psci_smp_cpu_stage[cpu], acquire) != PSCI_SMP_CPU_OFF &&\n"
            "\t\t    psci_call(PSCI_AFFINITY_INFO, cdp->cpu_phys_id, 0, 0) != PSCI_AFFINITY_OFF) {\n"
            '\t\t\tpsci_smp_note(" did not stop: it is left running\\n", cpu);\n\t\t}',
            "\t\tif (psci_call(PSCI_AFFINITY_INFO, cdp->cpu_phys_id, 0, 0) != PSCI_AFFINITY_OFF) {\n"
            '\t\t\tpsci_smp_note(" OFF unconfirmed: refusing return to resident firmware\\n", cpu);\n'
            "\t\t\tfor (;;) __builtin_arm_wfi();\n\t\t}")
    edits[psci] = text
    text = (xnu_tree / gic).read_text()
    if "virtual-timer-intid" not in text:
        raise ValueError("apply chiron timer overlay before SMP")
    text = replace_once(
        text, "\tuint64_t aff0 = phys_id & 0xff, aff1 = (phys_id >> 8) & 0xff;",
        "\tuint64_t aff0 = phys_id & 0xff, aff1 = (phys_id >> 8) & 0xff;\n"
        "#if defined(CHIRON_SMP)\n"
        '\tif ((phys_id & ~0xffffu) || aff0 >= 16 || sgi > 15) panic("chiron smp: unsupported SGI target");\n'
        "#endif",
    )
    text = replace_once(
        text, "\t\twhile (gic_read32(gicr + GICR_WAKER) & GICR_WAKER_CHILDASLEEP) {\n",
        "#if defined(CHIRON_SMP)\n"
        '\t\tuint64_t wake_start = __builtin_arm_rsr64("CNTVCT_EL0");\n'
        '\t\tuint64_t wake_ticks = __builtin_arm_rsr64("CNTFRQ_EL0");\n'
        "#endif\n"
        "\t\twhile (gic_read32(gicr + GICR_WAKER) & GICR_WAKER_CHILDASLEEP) {\n"
        "#if defined(CHIRON_SMP)\n"
        '\t\t\tif (!wake_ticks || __builtin_arm_rsr64("CNTVCT_EL0") - wake_start >= wake_ticks)\n'
        '\t\t\t\tpanic("chiron smp: redistributor wake timed out cpu %d", cpu);\n'
        "#endif\n",
    )
    edits[gic] = text
    clutch = "osfmk/kern/sched_clutch.c"
    text = (xnu_tree / clutch).read_text()
    text = replace_once(
        text,
        "\t/* Thread stealing is not enabled for single cluster clutch scheduler platforms */\n"
        "\treturn THREAD_NULL;",
        "#if defined(CHIRON_SMP)\n"
        "\t/* thread_select expects the pset lock dropped even when no thread is stolen. */\n"
        "\tpset_unlock(pset);\n"
        "#endif\n"
        "\t/* Thread stealing is not enabled for single cluster clutch scheduler platforms */\n"
        "\treturn THREAD_NULL;",
    )
    edits[clutch] = text
    edits["osfmk/arm64/chiron_smp_kernel.inc"] = (HERE / "chiron_smp_kernel.inc").read_text()
    return {"schema": "chiron-smp-overlay-v1", "kind": "kernel", "cpu_limit": 8,
            "strict_firmware_return": strict_firmware_return,
            "count_source": "/chosen/chiron-smp-cpu-count", "mpidrs": list(MPIDRS),
            "kernel_rebuild_required": True, "hardware_observed": False,
            "result": _publish(xnu_tree, edits)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("loader", "kernel"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cpu-count", type=int, choices=CPU_COUNTS, default=1)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = (loader_overlay(args.source, args.cpu_count) if args.kind == "loader"
              else kernel_overlay(args.source))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
