#!/usr/bin/env python3
"""Apply the bounded MSM8998 delta to the fixed HackiOS source implementation."""

import argparse
import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent


# E1: read-only probe for the UFS handoff question.  Every value below is a
# register fact taken from the UFSHCI register map (include/ufs/ufshci.h) and
# from mainline gcc-msm8998.c; the physical windows come from the MSM8998 DT.
# No register is ever written, so the worst case is a bus fault, never a
# half-programmed controller.
UFS_PROBE_C = r'''
/* ---- chiron UFS handoff probe (E1): read-only, zero writes ---------------- */
extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
int PE_parse_boot_argn(const char *arg_string, void *arg_ptr, int arg_size);

static uint32_t
chiron_ufs_rd(vm_offset_t base, uint32_t off)
{
	return *(volatile uint32_t *)(base + off);
}

/*
 * Order is chosen by value of information, not by address order.
 *
 * XNU's phys window ends near 0x1bd8c000, and the loader only maps IO from
 * 0x01000000 upward, so GCC at 0x00100000 lies outside both. UFSHC at
 * 0x01da4000 lies inside the loader's IO span. Reading UFSHC first therefore
 * answers "can this kernel reach the UFS controller at all" without risking a
 * hang on an address that could be unmapped for reasons unrelated to UFS.
 *
 * A hang yields no log at all: the phone has no live serial, kernel output is
 * dumped only when the kernel returns to the loader. So every level below is a
 * separate boot, and no level may cover two unknowns.
 */
static void
chiron_ufs_probe(void)
{
	static int done;
	vm_offset_t gcc, hc, phy;
	uint32_t gdscr, cap, ver, hcs, hce, istat, utrlba, pcs;
	int level = 0;

	if (done) {
		return;
	}
	done = 1;

	PE_parse_boot_argn("chiron_ufs", &level, sizeof level);
	kprintf("chiron-ufs: E1 begin level=%d\n", level);

	/* level 0: control point. GICD is provably alive here -- the interrupts
	 * this very kernel runs on are routed through it. If even this hangs, the
	 * mapping path itself is broken and nothing further is interpretable. */
	if (level <= 0) {
		vm_offset_t gicd = ml_io_map(0x17a00000ULL, 0x10000ULL);
		uint32_t ctlr = chiron_ufs_rd(gicd, 0x0000);
		uint32_t typer = chiron_ufs_rd(gicd, 0x0004);
		kprintf("chiron-ufs: ctl gicd=%llx ctlr=%08x typer=%08x\n",
		    (unsigned long long)gicd, ctlr, typer);
		kprintf("chiron-ufs: E1 stop after control point\n");
		return;
	}

	/* level 1: the UFS host controller, inside the loader's IO span.
	 *   CAP reads all ones -> block not answering (power/clock removed);
	 *   CAP reads sane     -> controller alive, so look at link state.
	 * HCS DEVICE_PRESENT and UTRLRDY is the bit that decides whether a
	 * read-only block driver is a port job or a rewrite. */
	hc = ml_io_map(0x01da4000ULL, 0x2500ULL);
	kprintf("chiron-ufs: hc=%llx\n", (unsigned long long)hc);
	cap = chiron_ufs_rd(hc, 0x00);
	kprintf("chiron-ufs: cap=%08x\n", cap);
	ver = chiron_ufs_rd(hc, 0x08);
	kprintf("chiron-ufs: ver=%08x\n", ver);
	istat = chiron_ufs_rd(hc, 0x20);
	hce = chiron_ufs_rd(hc, 0x34);
	kprintf("chiron-ufs: is=%08x hce=%08x\n", istat, hce);
	hcs = chiron_ufs_rd(hc, 0x30);
	kprintf("chiron-ufs: hcs=%08x dp=%u utrlrdy=%u utmrrdy=%u uiccrdy=%u\n",
	    hcs, hcs & 1u, (hcs >> 1) & 1u, (hcs >> 2) & 1u, (hcs >> 3) & 1u);
	utrlba = chiron_ufs_rd(hc, 0x50);
	kprintf("chiron-ufs: utrlba=%08x\n", utrlba);

	if (level < 2) {
		kprintf("chiron-ufs: E1 stop after hc\n");
		return;
	}
	/* level 2: PHY PCS ready, only once the controller answered sanely. */
	if (cap != 0 && cap != 0xffffffffu && (cap & 0x1fu) != 0) {
		phy = ml_io_map(0x01da7000ULL, 0x1000ULL);
		pcs = chiron_ufs_rd(phy, 0xd60);
		kprintf("chiron-ufs: pcs_ready=%08x started=%u\n", pcs, pcs & 1u);
	} else {
		kprintf("chiron-ufs: phy skipped (controller silent)\n");
	}

	if (level < 3) {
		kprintf("chiron-ufs: E1 stop after phy\n");
		return;
	}
	/* level 3: GCC last. 0x00100000 is below the loader IO base 0x01000000,
	 * so a hang here stays ambiguous between CX collapsed and never mapped. */
	gcc = ml_io_map(0x00100000ULL, 0xb0000ULL);
	kprintf("chiron-ufs: gcc=%llx\n", (unsigned long long)gcc);
	gdscr = chiron_ufs_rd(gcc, 0x75004);
	kprintf("chiron-ufs: gdscr=%08x pwr=%u collapse=%u\n",
	    gdscr, gdscr & 0xfu, (gdscr >> 31) & 1u);
	kprintf("chiron-ufs: cbcr1 ahb=%08x axi=%08x unipro=%08x ice=%08x\n",
	    chiron_ufs_rd(gcc, 0x7500c), chiron_ufs_rd(gcc, 0x75008),
	    chiron_ufs_rd(gcc, 0x76008), chiron_ufs_rd(gcc, 0x7600c));
	kprintf("chiron-ufs: cbcr2 phyaux=%08x tx0=%08x rx0=%08x rx1=%08x aggre1=%08x\n",
	    chiron_ufs_rd(gcc, 0x76040), chiron_ufs_rd(gcc, 0x75010),
	    chiron_ufs_rd(gcc, 0x75014), chiron_ufs_rd(gcc, 0x7605c),
	    chiron_ufs_rd(gcc, 0x82028));
	kprintf("chiron-ufs: E1 end\n");
}
'''

GPU_PROBE_C = r'''
/* ---- chiron Adreno 540 probe: read-only, zero writes -------------------- */

/*
 * The GPU the driver in this tree was written for is an Adreno 642L (SC7280) whose firmware
 * runs on a GMU; this phone's is an Adreno 540 (a5xx, MSM8998) with a GPMU, so none of that
 * driver's addresses, firmware or power sequence apply here. What follows only reads, one
 * block per level, so a level that ends the run still says which address it was:
 *
 *   0xffffffff  no one answers (not powered, or the hypervisor refuses the access)
 *   0x00000000  there, but not opened
 *   anything else  alive -- worth writing a driver for
 *
 * Addresses are this phone's own device tree: qcom,kgsl-3d0@5000000 (0x40000),
 * arm,smmu-kgsl@5040000 (0x10000), qcom,gpucc@5065000 (0x9000, both GDSCs inside it).
 */
static void
chiron_gpu_words(const char *what, vm_offset_t base, uint32_t off, unsigned n)
{
	char line[200];
	size_t used = 0;
	int all_f = 1, all_0 = 1;

	used += (size_t)snprintf(line + used, sizeof line - used, "chiron-gpu: %s +%#x:", what, off);
	for (unsigned i = 0; i < n; i++) {
		uint32_t v = chiron_ufs_rd(base, off + i * 4);

		if (v != 0xffffffffu) {
			all_f = 0;
		}
		if (v != 0) {
			all_0 = 0;
		}
		used += (size_t)snprintf(line + used, sizeof line - used, " %08x", v);
	}
	if (all_f) {
		snprintf(line + used, sizeof line - used, "  <- all ones: no one answers");
	} else if (all_0) {
		snprintf(line + used, sizeof line - used, "  <- all zero: there, not powered");
	}
	kprintf("%s\n", line);
}

static void
chiron_gpu_probe(void)
{
	static int done;
	vm_offset_t gpucc, kgsl, smmu;
	int level = 0;

	if (done) {
		return;
	}
	done = 1;
	if (!PE_parse_boot_argn("chiron_gpu", &level, sizeof level)) {
		kprintf("chiron-gpu: not asked (chiron_gpu=1..5); no register is read\n");
		return;
	}
	if (level <= 0) {
		kprintf("chiron-gpu: chiron_gpu=%d; no register is read\n", level);
		return;
	}
	if (level > 5) {
		level = 5;
	}
	kprintf("chiron-gpu: Adreno 540 probe, levels 1..%d, read only\n", level);

	if (level >= 1) {
		kprintf("chiron-gpu: level 1: the GPU's clock controller\n");
		gpucc = ml_io_map((vm_offset_t)0x05065000ULL, 0x9000ULL);
		chiron_gpu_words("gpucc", gpucc, 0x0000, 8);
	}
	if (level >= 2) {
		kprintf("chiron-gpu: level 2: the two power switches inside it\n");
		chiron_gpu_words("cx", gpucc, 0x1004, 2);
		chiron_gpu_words("gx", gpucc, 0x1090, 2);
		chiron_gpu_words("gx-domain", gpucc, 0x0130, 1);
	}
	if (level >= 3) {
		kprintf("chiron-gpu: level 3: the 3D core's registers\n");
		kgsl = ml_io_map((vm_offset_t)0x05000000ULL, 0x40000ULL);
		chiron_gpu_words("kgsl", kgsl, 0x0000, 8);
	}
	if (level >= 4) {
		kprintf("chiron-gpu: level 4: the SMMU in front of it\n");
		smmu = ml_io_map((vm_offset_t)0x05040000ULL, 0x10000ULL);
		chiron_gpu_words("smmu", smmu, 0x0000, 8);
	}
	if (level >= 5) {
		kprintf("chiron-gpu: level 5: the GPMU, before the 3D core is powered\n");
		chiron_gpu_words("gpmu", kgsl, 0xa888, 4);
		chiron_gpu_words("gpmu", kgsl, 0xac60, 2);
	}
	kprintf("chiron-gpu: end after level %d\n", level);
}
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"source context changed: {old[:100]}")
    return text.replace(old, new, 1)


def loader_overlay(source, profile):
    path = source / "xnuloader/loader.c"
    text = path.read_text()
    for instruction in ("0xd500409f", "0xd500407f"):
        old = f".inst {instruction}"
        if text.count(old) != 2:
            raise ValueError("loader PAN/UAO return context changed")
        text = text.replace(old, ".inst 0xd503201f")
    text = replace_once(text, "static efi_handle IMAGE;", "static efi_handle IMAGE;\nstatic int chiron_mode;")
    text = replace_once(text, "\tefi_block_io *bio = find_log_partition();",
                        "\tefi_block_io *bio = chiron_mode ? NULL : find_log_partition();")
    text = replace_once(text, "#define MAX_MEM_SIZE        0x180000000ULL",
                        f"#define MAX_MEM_SIZE        0x{profile['kernel_window_limit']:x}ULL")
    text = replace_once(text, "#define RESIDENT_RD_RESERVE 0x30000000ULL",
                        f"#define RESIDENT_RD_RESERVE 0x{profile['resident_kernel_reserve']:x}ULL")
    text = replace_once(text, "int core_shift;                  /*",
                        "uint32_t virtual_timer_intid;\n\tint core_shift;                  /*")
    marker = "static const struct platform platform_redwood = {"
    block = """static const struct platform platform_chiron = {
\t.name = "chiron", .io_base = 0x01000000, .io_size = 0xff000000,
\t.gic_dist = 0x17a00000, .gic_redist = 0x17b00000,
\t.uart = 0, .psci_hvc = 0, .cores = 1, .core_shift = 0,
\t.virtual_timer_intid = 19, .fb_base = 0x9d400000,
};

"""
    text = replace_once(text, marker, block + marker)
    text = replace_once(text, "\t&platform_redwood,\n", "\t&platform_chiron,\n\t&platform_redwood,\n")
    text = replace_once(text, '\tdt_prop(&w, "reg", gic_reg, sizeof(gic_reg));',
                        '\tdt_prop(&w, "reg", gic_reg, sizeof(gic_reg));\n'
                        '\tdt_prop_u32(&w, "virtual-timer-intid", plat->virtual_timer_intid ? plat->virtual_timer_intid : 27);')
    text = replace_once(text, "\t\tif (PLAT == &platform_qemu_virt)\n\t\t\tramfb_phone_screen();",
                        "\t\tif (PLAT == &platform_chiron &&\n"
                        "\t\t    (SCREEN.width != 1080 || SCREEN.height != 2160 || SCREEN.stride != 4320)) {\n"
                        '\t\t\tprint("chiron: GOP mode differs from measured panel; refusing boot\\n");\n'
                        "\t\t\treturn 0x8000000000000003ULL;\n\t\t}\n"
                        "\t\tif (PLAT == &platform_qemu_virt) {\n"
                        "\t\t\tuint64_t midr = READ_SYSREG(MIDR_EL1);\n"
                        "\t\t\tif ((midr >> 24) != 0x41) {\n"
                        '\t\t\t\tprint("unknown phone GOP: refusing QEMU MMIO fallback\\n");\n'
                        "\t\t\t\treturn 0x8000000000000003ULL;\n\t\t\t}\n"
                        "\t\t\tramfb_phone_screen();\n\t\t}")
    text = replace_once(text, '\tprint("platform %s\\n", PLAT->name);',
                        '\tchiron_mode = (PLAT == &platform_chiron);\n\tprint("platform %s\\n", PLAT->name);')
    text = replace_once(text, "\tif (have_payload && pi.usb_mode && usb_start()) {",
                        "\tif (have_payload && pi.usb_mode) {\n"
                        "\t\tif (!usb_start()) {\n"
                        '\t\t\tprint("USB transport unavailable; kernel not started\\n");\n'
                        "\t\t\treturn 0x8000000000000003ULL;\n\t\t}")
    text = replace_once(text, "\tuint64_t cpu_mpidr[8] = { kernel_mpidr };",
                        "\tif (PLAT == &platform_chiron) {\n"
                        "\t\tif (kernel_mpidr != this_mpidr || more_cores) {\n"
                        '\t\t\tprint("chiron: first port is the measured boot CPU only\\n");\n'
                        "\t\t\treturn 0;\n\t\t}\n\t}\n"
                        "\tuint64_t cpu_mpidr[8] = { kernel_mpidr };")
    path.write_text(text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kernel_overlay(tree):
    make = tree / "makedefs/MakeInc.def"
    text = replace_once(
        make.read_text(),
        "MACHINE_FLAGS_ARM64_BCM2837 = -DARM64_BOARD_CONFIG_BCM2837 -Xclang -target-feature -Xclang +pan",
        "MACHINE_FLAGS_ARM64_BCM2837 = -DARM64_BOARD_CONFIG_BCM2837 -DCHIRON_PORT=1 -march=armv8-a -mno-outline-atomics",
    )
    make.write_text(text)
    board = tree / "pexpert/pexpert/arm64/BCM2837.h"
    board.write_text(replace_once(board.read_text(), "#define __ARM_PAN_AVAILABLE__     1",
                                  "#undef __ARM_PAN_AVAILABLE__"))
    gic = tree / "osfmk/arm64/gic_v3_irq.c"
    text = replace_once(gic.read_text(), "#define GIC_INTID_VTIMER        27",
                        UFS_PROBE_C + GPU_PROBE_C + "\n#define GIC_INTID_VTIMER        27")
    text = replace_once(text, "#define GIC_INTID_VTIMER        27",
                        "static uint32_t gic_vtimer_intid = 27;\n#define GIC_INTID_VTIMER        gic_vtimer_intid")
    text = replace_once(text, "\tuint32_t mine = 1u << GIC_INTID_VTIMER;",
                        "\tuint32_t mine;")
    text = replace_once(text, "\t\tuint64_t const *reg = prop;",
                        "\t\tuint64_t const *reg = prop;\n"
                        '\t\tif (SecureDTGetProperty(entry, "virtual-timer-intid", &prop, &prop_size) == kSuccess) {\n'
                        "\t\t\tif (prop_size != sizeof(uint32_t)) panic(\"gic: invalid timer property size\");\n"
                        "\t\t\tgic_vtimer_intid = *(uint32_t const *)prop;\n"
                        "\t\t\tif (gic_vtimer_intid < 16 || gic_vtimer_intid > 31) panic(\"gic: timer is not a PPI\");\n"
                        "\t\t}\n")
    text = replace_once(text, "\t/* System-register CPU interface */",
                        "\tmine = 1u << GIC_INTID_VTIMER;\n\n\t/* System-register CPU interface */")
    text = replace_once(text, "\tml_install_interrupt_handler(NULL, 0, NULL, gic_irq_handler, NULL);",
                        "\tml_install_interrupt_handler(NULL, 0, NULL, gic_irq_handler, NULL);\n"
                        '\tkprintf("chiron: build=ufs-probe-4 TCR=%llx SCTLR=%llx MMFR0=%llx MMFR1=%llx ISAR0=%llx MPIDR=%llx hwshift=%u timer=%u\\n",\n'
                        '\t    __builtin_arm_rsr64("TCR_EL1"), __builtin_arm_rsr64("SCTLR_EL1"),\n'
                        '\t    __builtin_arm_rsr64("ID_AA64MMFR0_EL1"), __builtin_arm_rsr64("ID_AA64MMFR1_EL1"),\n'
                        '\t    __builtin_arm_rsr64("ID_AA64ISAR0_EL1"), __builtin_arm_rsr64("MPIDR_EL1"), ARM_PGSHIFT, GIC_INTID_VTIMER);\n'
                        "\tchiron_ufs_probe();\n\tchiron_gpu_probe();")
    gic.write_text(text)
    return {str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (make, board, gic)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", choices=("loader", "kernel"))
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()
    profile = json.loads((HERE / "chiron-profile.json").read_text())
    result = loader_overlay(args.source, profile) if args.kind == "loader" else kernel_overlay(args.source)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "profile": profile, "kind": args.kind, "result": result,
        "hardware_observed": False,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
