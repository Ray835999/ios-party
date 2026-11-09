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

#define CHIRON_GPU_STEPS 6

/*
 * The GPU the driver in this tree was written for is an Adreno 642L (SC7280) whose firmware
 * runs on a GMU; this phone's is an Adreno 540 (a5xx, MSM8998) with a GPMU, so none of that
 * driver's addresses, firmware or power sequence apply here. What follows only reads:
 *
 *   0xffffffff  no one answers (not powered, or the hypervisor refuses the access)
 *   0x00000000  there, but not opened
 *   anything else  alive -- worth writing a driver for
 *
 * Level 1 is a control point. A block whose clock is off does not have to answer all ones: it
 * can hold the bus, and then the phone stops without printing anything more. Printing one line
 * per word means the last line in the log names the word that hung -- so a run that dies still
 * produces evidence instead of just dying.
 *
 * Addresses are this phone's own device tree: qcom,kgsl-3d0@5000000 (0x40000),
 * arm,smmu-kgsl@5040000 (0x10000), qcom,gpucc@5065000 (0x9000, both GDSCs inside it).
 */
static void
chiron_gpu_words(const char *what, vm_offset_t base, uint32_t off, unsigned n)
{
	for (unsigned i = 0; i < n; i++) {
		uint32_t v = chiron_ufs_rd(base, off + i * 4);

		kprintf("chiron-gpu: %s +%#x = %08x%s\n", what, off + i * 4, v,
		    v == 0xffffffffu ? "  <- no one answers" :
		    (v == 0 ? "  <- zero, not opened" : ""));
	}
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
		kprintf("chiron-gpu: not asked (chiron_gpu=1..%d); no register is read\n",
		    CHIRON_GPU_STEPS);
		return;
	}
	if (level <= 0) {
		kprintf("chiron-gpu: chiron_gpu=%d; no register is read\n", level);
		return;
	}
	if (level > CHIRON_GPU_STEPS) {
		level = CHIRON_GPU_STEPS;
	}
	kprintf("chiron-gpu: Adreno 540 probe, levels 1..%d, read only\n", level);

	/*
	 * The interrupt controller this very kernel is running on: ml_io_map and the read path
	 * are provably good here. If level 1 answers, a later level that stops the phone is the
	 * block's fault, not the probe's.
	 */
	if (level >= 1) {
		kprintf("chiron-gpu: level 1: control point, the interrupt controller\n");
		chiron_gpu_words("gicd", ml_io_map((vm_offset_t)0x17a00000ULL, 0x10000ULL), 0x0000, 4);
	}
	if (level >= 2) {
		kprintf("chiron-gpu: level 2: the GPU's clock controller\n");
		gpucc = ml_io_map((vm_offset_t)0x05065000ULL, 0x9000ULL);
		chiron_gpu_words("gpucc", gpucc, 0x0000, 8);
	}
	if (level >= 3) {
		kprintf("chiron-gpu: level 3: the two power switches inside it\n");
		chiron_gpu_words("cx", gpucc, 0x1004, 2);
		chiron_gpu_words("gx", gpucc, 0x1090, 2);
		chiron_gpu_words("gx-domain", gpucc, 0x0130, 1);
	}
	if (level >= 4) {
		kprintf("chiron-gpu: level 4: the 3D core's registers\n");
		kgsl = ml_io_map((vm_offset_t)0x05000000ULL, 0x40000ULL);
		chiron_gpu_words("kgsl", kgsl, 0x0000, 8);
	}
	if (level >= 5) {
		kprintf("chiron-gpu: level 5: the SMMU in front of it\n");
		smmu = ml_io_map((vm_offset_t)0x05040000ULL, 0x10000ULL);
		chiron_gpu_words("smmu", smmu, 0x0000, 8);
	}
	if (level >= 6) {
		kprintf("chiron-gpu: level 6: the GPMU, before the 3D core is powered\n");
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
    # chiron: the old "first port is the measured boot CPU only" guard is gone -- it returned 0
    # whenever cmdline had cpus=/cpumask= (more_cores), which blocks every SMP run. Chiron SMP is
    # validated (chiron smp: ABI v1 validated 2 CPUs) and the smp overlay checks the cores itself.
    # chiron: the real ufshc address is 1da4000 -- this phone's Android DTB says ufshc@1da4000
    # (and contains no 1d84000 at all; that is an SDM660 address). Reading 1d84000 hits an
    # unmapped page: the loader's hc_init reset the phone twice there, and the kernel's native
    # driver reset it just the same (soft2/soft3, cpus=2 and cpus=1 alike). Fix the address.
    text = replace_once(text, '#define HC_PA              0x1d84000ULL        /* ufshc@1d84000, "ufs_mem", in this phone\'s Android device tree */',
                        '#define HC_PA              0x1da4000ULL        /* ufshc@1da4000, "hc", in this phone\'s Android DTB (was wrongly 1d84000) */')
    # chiron: the loader cannot touch the UFS host controller's registers at all -- the first
    # read (hc_init's UTMLBA, confirmed twice on the phone with cpus=1 and cpus=2) resets the
    # phone before the log can be fetched. In soft mode the loader never touches them: hc_known
    # stays 0 so hc_take_back no-ops and hc_list_is_uefis() returns true without a read, the
    # controller's list is left exactly as UEFI keeps it, and the kernel's own driver notes
    # UEFI's list address itself (the offer below records uefi 0:0 to say just that).
    text = replace_once(text, "static int hc_known;                    /* UEFI's list address is kept here: check it, put it back */",
                        "static int hc_known;                    /* UEFI's list address is kept here: check it, put it back */\n"
                        "static int hc_soft;                    /* chiron: the controller's registers reset the phone when read;\n"
                        "                                          UEFI's list is noted by the kernel instead */")
    text = replace_once(text, "\tif (!hc_requested) {\n\t\treturn 0;\n\t}\n\tlo = hc_rd(HC_UTRLBA);",
                        "\tif (!hc_requested) {\n\t\treturn 0;\n\t}\n"
                        "\tif (hc_soft) {\n"
                        "\t\t/* chiron: reading the controller's registers resets the phone before the log can be\n"
                        "\t\t * fetched, so the loader never touches them: UEFI's list stays in, and the kernel\n"
                        "\t\t * notes its address itself (the offer below records uefi 0:0 to say exactly that). */\n"
                        "\t\thc_uefi_lo = 0;\n"
                        "\t\thc_uefi_hi = 0;\n"
                        "\t\thc_known = 0;\n"
                        '\t\tprint("ufs: soft host-controller mode: the controller\'s registers are not touched here\\n");\n'
                        "\t\treturn 1;\n"
                        "\t}\n"
                        "\tlo = hc_rd(HC_UTRLBA);")
    text = replace_once(text, "\tif (ufs_requested >= 2) {\n\t\thc_init();              /* (ufs_native=: UEFI's transfer list kept, before anything reads) */\n\t}",
                        "\thc_soft = (PLAT == &platform_chiron);\n"
                        "\tif (ufs_requested >= 2) {\n\t\thc_init();              /* (ufs_native=: UEFI's transfer list kept, before anything reads) */\n\t}")
    text = replace_once(text, "\tif (hc_known && hc_list_is_uefis() && hc_rd(HC_UTRLDBR) == 0 && (hc_rd(HC_HCE) & 1)) {",
                        "\tif (hc_soft ? hc_requested\n"
                        "\t    : (hc_known && hc_list_is_uefis() && hc_rd(HC_UTRLDBR) == 0 && (hc_rd(HC_HCE) & 1))) {")
    # chiron: trace the hand-off. With bootcpu= the run stops dead right after "jumping to" and
    # the log cannot say where (both core 1 and core 2 behave the same, so it is the path, not a
    # core). Between that print and the kernel sit flush_log (UEFI boot services), wdt_set_ms
    # (SMC to the hypervisor), loader_save_ctx, a whole-memory clean, and PSCI CPU_ON. Each step
    # now leaves a line, and flush_log is skipped there so a hang inside it cannot hide the rest.
    text = replace_once(text,
        "\tprint(\"jumping to %x\\n\", entry_va + slide);\n\tflush_log();",
        "\tprint(\"jumping to %x\\n\", entry_va + slide);\n"
        "\tprint(\"chiron dbg: 1 hand over (flush_log skipped)\\n\");")
    text = replace_once(text, "\twdt_set_ms(60000);",
        "\twdt_set_ms(60000);\n"
        "\tprint(\"chiron dbg: 2 wdt set\\n\");")
    text = replace_once(text, "\tif (loader_save_ctx() == 0) {",
        "\tprint(\"chiron dbg: 3 save ctx\\n\");\n"
        "\tif (loader_save_ctx() == 0) {\n"
        "\t\tprint(\"chiron dbg: 4 save ctx ok\\n\");")
    text = replace_once(text,
        "\t\tif (kernel_mpidr != this_mpidr) {\n\t\t\tint64_t status = run_on_other_cpu(",
        "\t\tprint(\"chiron dbg: 5 run other cpu %x\\n\", kernel_mpidr);\n"
        "\t\tif (kernel_mpidr != this_mpidr) {\n\t\t\tint64_t status = run_on_other_cpu(")
    text = replace_once(text,
        "\tstatus = psci_call(PSCI_CPU_ON, mpidr, (uint64_t)(uintptr_t)&other_cpu_entry, (uint64_t)(uintptr_t)&other_cpu_start);",
        "\tprint(\"chiron dbg: 6 psci cpu_on %x\\n\", mpidr);\n"
        "\tstatus = psci_call(PSCI_CPU_ON, mpidr, (uint64_t)(uintptr_t)&other_cpu_entry, (uint64_t)(uintptr_t)&other_cpu_start);\n"
        "\tprint(\"chiron dbg: 7 psci returned %d\\n\", status);")
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
    # chiron soft host-controller: the loader cannot read the controller's registers, so it
    # offers the native area with uefi 0:0 -- "unnoted". At probe time the loader has touched
    # nothing, so the list now in the controller IS UEFI's: note it here, once, and every
    # switch-in/put-back below compares against the captured address as usual.
    ufshc = tree / "bsd/dev/redwood_ufshc.c"
    text = replace_once(ufshc.read_text(),
                        "\tbase = hc_read(UFSHC_UTRLBA);\n\tbaseu = hc_read(UFSHC_UTRLBAU);\n\tdb = hc_read(UFSHC_UTRLDBR);",
                        "\tbase = hc_read(UFSHC_UTRLBA);\n\tbaseu = hc_read(UFSHC_UTRLBAU);\n"
                        "\tif (loader->uefi_lo == 0 && loader->uefi_hi == 0) {\n"
                        "\t\t/* soft host-controller mode (chiron): the loader never touches the controller's\n"
                        "\t\t * registers, so it could not note UEFI's list (it offers 0:0). The list now in\n"
                        "\t\t * the controller is UEFI's, and this kernel notes it itself. */\n"
                        '\t\thc_say("ufs: native: the loader noted no UEFI\'s list; taking the one in the controller: %08x:%08x\\n",\n'
                        "\t\t    baseu, base);\n"
                        "\t\thc_loader.uefi_lo = base;\n"
                        "\t\thc_loader.uefi_hi = baseu;\n"
                        "\t}\n"
                        "\tdb = hc_read(UFSHC_UTRLDBR);")
    text = replace_once(text, "} else if (base != loader->uefi_lo || baseu != loader->uefi_hi) {",
                        "} else if (base != hc_loader.uefi_lo || baseu != hc_loader.uefi_hi) {")
    # chiron: the controller's power domain (GCC UFS_GDSC @ 0x1075004) and its gates are off at
    # this point in the boot -- the loader's first register read reset the phone twice, and the
    # GPU probe proved the pattern on this phone: power the domain, turn the gates on, read.
    # Opt-in via ufs_pwron=1 so nothing else changes.
    text = replace_once(text, "bool\nufshc_probe(const struct ufshc_loader *loader, bool only)\n{",
        "#define CHIRON_GCC_PA 0x100000ULL\n\n"
        "static volatile uint32_t *chiron_gcc;\n\n"
        "static uint32_t\nchiron_gcc_rd(uint32_t off)\n{\n\treturn chiron_gcc[off / 4];\n}\n\n"
        "static void\nchiron_gcc_wr(uint32_t off, uint32_t v)\n{\n\tchiron_gcc[off / 4] = v;\n}\n\n"
        "/* chiron: power the UFS domain and turn its gates on before the controller is read\n"
        " * (ufs_pwron=1). GCC offsets per gcc-msm8998.c: gdscr 0x75004, ufs reset 0x75000,\n"
        " * ufs_axi src cmd_rcgr 0x75018, unipro src cmd_rcgr 0x76028, gates axi 0x75008,\n"
        " * ahb 0x7500c, unipro 0x76008, ice 0x7600c, phy_aux 0x76040, rx_symbol_1 0x7605c,\n"
        " * aggre1_ufs_axi 0x82028, clkref 0x88004, tx/rx symbol 0x75010/14 (skip-status).\n"
        " * Linux clk-branch semantics: BIT31 (CLK_OFF) 1 = halted, BIT0 (CLK_ENABLE) 1 = on;\n"
        " * RCG2 cmd_rcgr BIT0 = ROOT_EN. soft4 measured every UFS gate halted (0x8000xxxx)\n"
        " * and both RCG roots off: XBL powers UFS down for fastboot, so the controller's\n"
        " * first register read external-aborts until the tree is re-powered. */\n"
        "static void chiron_gates_on(const uint32_t *gates, unsigned n);\n\n"
        "static void\nchiron_ufs_power_on(void)\n{\n"
        "\tunsigned on = 0;\n"
        "\tstatic const uint32_t g_core[] = { 0x75008, 0x7500c, 0x76008 };\n"
        "\tstatic const uint32_t g_noc[] = { 0x82028, 0x88004 };\n"
        "\tstatic const uint32_t g_rest[] = { 0x7600c, 0x76040, 0x7605c, 0x75010, 0x75014 };\n\n"
        "\tif (!PE_parse_boot_argn(\"ufs_pwron\", &on, sizeof(on)) || !on) {\n\t\treturn;\n\t}\n"
        "\tchiron_gcc = (volatile uint32_t *)ml_io_map(CHIRON_GCC_PA, 0x90000);\n"
        '\t\thc_say("ufs: native: GCC mapped %p\\n", (void *)chiron_gcc);\n'
        '\t\thc_say("ufs: native: bcr %08x\\n", chiron_gcc_rd(0x75000));\n'
        '\t\thc_say("ufs: native: gdscr %08x\\n", chiron_gcc_rd(0x75004));\n'
        '\t\thc_say("ufs: native: axi cmd %08x cfg %08x\\n", chiron_gcc_rd(0x75018), chiron_gcc_rd(0x7501c));\n'
        '\t\thc_say("ufs: native: unipro cmd %08x\\n", chiron_gcc_rd(0x76028));\n'
        '\t\thc_say("ufs: native: gates before axi %08x ahb %08x unipro %08x\\n",\n'
        "\t\t    chiron_gcc_rd(0x75008), chiron_gcc_rd(0x7500c), chiron_gcc_rd(0x76008));\n"
        "\tif (!(chiron_gcc_rd(0x75004) & (1u << 31))) {\n"
        "\t\tchiron_gcc_wr(0x75004, chiron_gcc_rd(0x75004) & ~1u);\n"
        "\t\tfor (unsigned i = 0; i < 200 && !(chiron_gcc_rd(0x75004) & (1u << 31)); i++) {\n"
        "\t\t\tdelay(1000);\n\t\t}\n"
        '\t\thc_say("ufs: native: gdscr after power-on %08x (bit31 %s)\\n", chiron_gcc_rd(0x75004),\n'
        "\t\t    (chiron_gcc_rd(0x75004) & (1u << 31)) ? \"on\" : \"OFF\");\n"
        "\t}\n"
        "\tif (on >= 1) {\n"
        "\t\tchiron_gcc_wr(0x75018, chiron_gcc_rd(0x75018) | 1u);\n"
        '\t\thc_say("ufs: native: axi root -> %08x\\n", chiron_gcc_rd(0x75018));\n'
        "\t}\n"
        "\tif (on >= 2) {\n"
        "\t\tchiron_gcc_wr(0x76028, chiron_gcc_rd(0x76028) | 1u);\n"
        '\t\thc_say("ufs: native: unipro root -> %08x\\n", chiron_gcc_rd(0x76028));\n'
        "\t}\n"
        "\tif (on >= 3) {\n\t\tchiron_gates_on(g_core, sizeof(g_core) / sizeof(g_core[0]));\t}\n"
        "\tif (on >= 4) {\n\t\tchiron_gates_on(g_noc, sizeof(g_noc) / sizeof(g_noc[0]));\t}\n"
        "\tif (on >= 5) {\n\t\tchiron_gates_on(g_rest, sizeof(g_rest) / sizeof(g_rest[0]));\t}\n"
        "}\n\n"
        "static void\nchiron_gates_on(const uint32_t *gates, unsigned n)\n{\n"
        "\tfor (unsigned g = 0; g < n; g++) {\n"
        "\t\tuint32_t v = chiron_gcc_rd(gates[g]);\n"
        "\t\tif (!(v & 1)) {\n\t\t\tchiron_gcc_wr(gates[g], v | 1u);\n\t\t}\n"
        "\t\tfor (unsigned i = 0; i < 100 && (chiron_gcc_rd(gates[g]) & (1u << 31)); i++) {\n"
        "\t\t\tdelay(1000);\n\t\t}\n"
        '\t\t\thc_say("ufs: native: GCC gate 0x%x -> %08x (%s)\\n", gates[g], chiron_gcc_rd(gates[g]),\n'
        "\t\t\t    (chiron_gcc_rd(gates[g]) & (1u << 31)) ? \"HALTED\" : \"running\");\n"
        "\t}\n}\n\n"
        "bool\nufshc_probe(const struct ufshc_loader *loader, bool only)\n{")
    text = replace_once(text, "\thc = ml_io_map(UFSHC_PHYS, UFSHC_SIZE);",
                        "\tchiron_ufs_power_on();\n\thc = ml_io_map(UFSHC_PHYS, UFSHC_SIZE);")
    # chiron: fix the controller address -- the phone's Android DTB says ufshc@1da4000 (0x2500);
    # 1d84000 is an SDM660 address and is not in this DTB at all. Reading it is a bus hang:
    # the kernel's native driver reset the phone on it in soft2/soft3.
    text = replace_once(text, '#define UFSHC_PHYS          0x1d84000ULL        /* ufshc@1d84000, "ufs_mem", in the phone\'s Android device tree */',
                        '#define UFSHC_PHYS          0x1da4000ULL        /* ufshc@1da4000, "hc", in the phone\'s Android DTB (was wrongly 1d84000) */')
    text = replace_once(text, "#define UFSHC_SIZE          0x3000",
                        "#define UFSHC_SIZE          0x2500")
    ufshc.write_text(text)
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
