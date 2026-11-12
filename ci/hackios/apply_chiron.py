#!/usr/bin/env python3
"""Apply the bounded MSM8998 delta to the fixed HackiOS source implementation."""

import argparse
import hashlib
import json
import sys
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

#define CHIRON_GCC_PHYS     0x00100000ULL
#define CHIRON_GCC_SIZE     0x90000ULL
#define CHIRON_GPUCC_PHYS   0x05065000ULL
#define CHIRON_GPUCC_SIZE   0x9000ULL
#define CHIRON_KGSL_PHYS    0x05000000ULL
#define CHIRON_KGSL_SIZE    0x40000ULL
#define CHIRON_SMMU_PHYS    0x05040000ULL
#define CHIRON_SMMU_SIZE    0x10000ULL

/* gcc-msm8998.c: the branches that let the GPU's configuration port answer. */
#define CHIRON_GCC_GPU_AHB       0x71004u   /* gcc_gpu_cfg_ahb_clk, BIT0 enable, halt_check SKIP */
#define CHIRON_GCC_GPU_BIMC_SRC  0x7100cu   /* gcc_gpu_bimc_gfx_src_clk */
#define CHIRON_GCC_GPU_BIMC      0x71010u   /* gcc_gpu_bimc_gfx_clk */
#define CHIRON_GCC_GPU_SNOC      0x71018u   /* gcc_gpu_snoc_dvm_gfx_clk */

/*
 * The GPU of this phone is an Adreno 540 (a5xx, MSM8998, GPMU); the driver in this tree is
 * another phone's Adreno 642L (SC7280, a660, GMU), so none of its addresses, firmware or
 * power sequence apply. This probe reads -- with one deliberate exception, at level 2.
 *
 * WHY LEVEL 2 WRITES. The GPUCC (0x05065000) and the 3D core's configuration port
 * (0x05000000) both sit behind gcc_gpu_cfg_ahb_clk, and XBL leaves the GPU tree off. A block
 * whose bus clock is off does not answer all ones: it holds the bus, and the phone stops
 * without printing anything more. That is what the previous six-level run did -- it read
 * 0x05065000 with that clock off, the phone went away, and the cable never came back. The UFS
 * controller was in exactly this state (its first register read reset the phone twice) until
 * its gate was turned on. So level 2 reads GCC's four GPU branches, sets BIT0 on the one that
 * carries the GPU's configuration bus, and reads it back; levels 3 and up then talk to a bus
 * that answers.
 *
 * Offsets are mainline's, not invented:
 *   drivers/clk/qcom/gcc-msm8998.c    gcc_gpu_cfg_ahb_clk 0x71004, gcc_gpu_bimc_gfx_src
 *     0x7100c, gcc_gpu_bimc_gfx 0x71010, gcc_gpu_snoc_dvm_gfx 0x71018 (all BIT0)
 *   drivers/clk/qcom/gpucc-msm8998.c  gpu_cx_gdsc gdscr 0x1004 hw_ctrl 0x1008;
 *     gpu_gx_gdsc gdscr 0x1094, clamp_io 0x0130, GPU_GX_BCR 0x1090, cxc 0x1098
 *
 * 0xffffffff means no one answers; 0x00000000 means the block is there but not opened;
 * anything else is alive -- worth writing a driver for. Printing one line per word means the
 * last line in the log names the word that hung, so a run that dies still produces evidence.
 *
 * Addresses of the blocks are this phone's own device tree: qcom,kgsl-3d0@5000000 (0x40000),
 * arm,smmu-kgsl@5040000 (0x10000), qcom,gpucc@5065000 (0x9000, both GDSCs inside it).
 */
static uint32_t
chiron_gpu_rd(vm_offset_t base, uint32_t off)
{
	return *(volatile uint32_t *)(base + off);
}

static void
chiron_gpu_wr(vm_offset_t base, uint32_t off, uint32_t v)
{
	*(volatile uint32_t *)(base + off) = v;
}

/* one line per word: the last line printed names the word that hung the phone */
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
	vm_offset_t gcc, gpucc, kgsl, smmu;
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
	kprintf("chiron-gpu: Adreno 540 probe, levels 1..%d, the clock before the read\n", level);

	/*
	 * The interrupt controller this very kernel is running on: ml_io_map and the read path
	 * are provably good here. If level 1 answers, a later level that stops the phone is the
	 * block's fault, not the probe's.
	 */
	if (level >= 1) {
		kprintf("chiron-gpu: level 1: control point, the interrupt controller\n");
		chiron_gpu_words("gicd", ml_io_map((vm_offset_t)0x17a00000ULL, 0x10000ULL), 0x0000, 4);
	}
	/*
	 * Level 2: the GPU's bus interface clock. Read the four GCC branches, then set BIT0 on
	 * gcc_gpu_cfg_ahb_clk -- the single write in this probe, and the reason the blocks in
	 * levels 3 and up can be read at all. halt_check is SKIP for this branch, so there is no
	 * halt bit to wait for; the read-back says what the register holds now.
	 */
	if (level >= 2) {
		uint32_t ahb;

		gcc = ml_io_map((vm_offset_t)CHIRON_GCC_PHYS, (vm_size_t)CHIRON_GCC_SIZE);
		kprintf("chiron-gpu: level 2: the GPU's bus clock, GCC %#llx -> %llx\n",
		    CHIRON_GCC_PHYS, (unsigned long long)gcc);
		chiron_gpu_words("gcc-ahb", gcc, CHIRON_GCC_GPU_AHB, 1);
		chiron_gpu_words("gcc-bimc-src", gcc, CHIRON_GCC_GPU_BIMC_SRC, 1);
		chiron_gpu_words("gcc-bimc", gcc, CHIRON_GCC_GPU_BIMC, 1);
		chiron_gpu_words("gcc-snoc", gcc, CHIRON_GCC_GPU_SNOC, 1);
		ahb = chiron_gpu_rd(gcc, CHIRON_GCC_GPU_AHB);
		if ((ahb & 1u) == 0) {
			kprintf("chiron-gpu: gcc_gpu_cfg_ahb_clk is off; setting BIT0 (the one write)\n");
			chiron_gpu_wr(gcc, CHIRON_GCC_GPU_AHB, ahb | 1u);
			kprintf("chiron-gpu:   after the write: %08x (BIT0 %u)\n",
			    chiron_gpu_rd(gcc, CHIRON_GCC_GPU_AHB),
			    chiron_gpu_rd(gcc, CHIRON_GCC_GPU_AHB) & 1u);
		} else {
			kprintf("chiron-gpu: gcc_gpu_cfg_ahb_clk was already on; nothing written\n");
		}
	}
	/* Level 3: the GPU's own clock controller -- answers only once level 2 has run. */
	if (level >= 3) {
		gpucc = ml_io_map((vm_offset_t)CHIRON_GPUCC_PHYS, (vm_size_t)CHIRON_GPUCC_SIZE);
		kprintf("chiron-gpu: level 3: GPUCC %#llx -> %llx\n",
		    CHIRON_GPUCC_PHYS, (unsigned long long)gpucc);
		chiron_gpu_words("gpucc-cx-bcr", gpucc, 0x1000, 1);
		chiron_gpu_words("gpucc-cx", gpucc, 0x1004, 2);         /* gdscr, hw_ctrl */
		chiron_gpu_words("gpucc-gx-bcr", gpucc, 0x1090, 2);     /* GPU_GX_BCR, GX gdscr */
		chiron_gpu_words("gpucc-gfx3d", gpucc, 0x1098, 1);      /* gfx3d_clk, GX's cxc */
		chiron_gpu_words("gpucc-gx-clamp", gpucc, 0x0130, 1);
	}
	if (level >= 4) {
		kprintf("chiron-gpu: level 4: the 3D core's registers, kgsl %#llx\n", CHIRON_KGSL_PHYS);
		kgsl = ml_io_map((vm_offset_t)CHIRON_KGSL_PHYS, (vm_size_t)CHIRON_KGSL_SIZE);
		chiron_gpu_words("kgsl", kgsl, 0x0000, 8);
	}
	if (level >= 5) {
		kprintf("chiron-gpu: level 5: the SMMU in front of it, %#llx\n", CHIRON_SMMU_PHYS);
		smmu = ml_io_map((vm_offset_t)CHIRON_SMMU_PHYS, (vm_size_t)CHIRON_SMMU_SIZE);
		chiron_gpu_words("smmu", smmu, 0x0000, 8);
	}
	if (level >= 6) {
		kprintf("chiron-gpu: level 6: the GPMU, before the 3D core is powered\n");
		chiron_gpu_words("gpmu", kgsl, 0xa888, 4);
		chiron_gpu_words("gpmu", kgsl, 0xac60, 2);
	}
	/*
	 * Powering the domain, which is where a driver actually begins. Both GDSCs in GPUCC
	 * read 00222001: BIT0 (SW_COLLAPSE) set, BIT31 (PWR_ON) clear, i.e. collapsed. Linux's
	 * gdsc driver brings one up by clearing SW_COLLAPSE and waiting for PWR_ON to come
	 * back, after releasing CLAMP_IO or the core stays in a clamp. The 3D core's registers
	 * then stop reading as zeroes.
	 *
	 * Opt in with chiron_gpu_pwron=N, so a run that only reads never writes:
	 *   1  release CLAMP_IO
	 *   2  CX on   (GX sits inside CX, so this order matters)
	 *   3  GX on
	 *   4  release GPU_GX_BCR
	 */
	{
		int pw = 0, clk = 0;

		(void)PE_parse_boot_argn("chiron_gpu_clock", &clk, sizeof clk);
		if (PE_parse_boot_argn("chiron_gpu_pwron", &pw, sizeof pw) && pw > 0) {
			uint32_t v, i;

			gpucc = ml_io_map((vm_offset_t)CHIRON_GPUCC_PHYS, (vm_size_t)CHIRON_GPUCC_SIZE);
			kgsl = ml_io_map((vm_offset_t)CHIRON_KGSL_PHYS, (vm_size_t)CHIRON_KGSL_SIZE);
			kprintf("chiron-gpu: power %d: before bcr=%08x clamp=%08x cx=%08x gx=%08x cxc=%08x\n",
			    pw, chiron_gpu_rd(gpucc, 0x1090), chiron_gpu_rd(gpucc, 0x0130),
			    chiron_gpu_rd(gpucc, 0x1004), chiron_gpu_rd(gpucc, 0x1094),
			    chiron_gpu_rd(gpucc, 0x1098));
			if (pw >= 1) {
				chiron_gpu_wr(gpucc, 0x0130, 0);
				kprintf("chiron-gpu: power: clamp -> %08x\n",
				    chiron_gpu_rd(gpucc, 0x0130));
			}
			if (pw >= 2) {
				v = chiron_gpu_rd(gpucc, 0x1004);
				if (v & 1u) {
					chiron_gpu_wr(gpucc, 0x1004, v & ~1u);
				}
				for (i = 0; i < 200; i++) {
					if (chiron_gpu_rd(gpucc, 0x1004) & (1u << 31)) {
						break;
					}
					delay(1000);
				}
				kprintf("chiron-gpu: power: cx -> %08x after %u ms (%s)\n",
				    chiron_gpu_rd(gpucc, 0x1004), i,
				    (chiron_gpu_rd(gpucc, 0x1004) & (1u << 31)) ? "on" : "still off");
			}
			/*
			 * The 3D core's own clock, which is what the GX domain is built around.
			 * gpucc's gfx3d_clk_src is an RCG at 0x1070 with exactly one legal parent
			 * (gpupll0_out_even, pre_div 3), and gfx3d_clk is the branch at 0x1098 that
			 * fed the reading 80000620 -- BIT0 clear (disabled), BIT31 set (halted).
			 *
			 * Enabling a branch is a plain CBCR write. If the PLL or the RCG behind it
			 * is not running the halt bit simply never clears, and the read-back says
			 * so; nothing here can hang the bus. The PLL and RCG values are printed
			 * first so the next step is decided from what they actually are, not guessed.
			 */
			if (clk >= 1) {
				kprintf("chiron-gpu: clock: pll 0000=%08x 0004=%08x 0008=%08x 000c=%08x 0010=%08x 0018=%08x\n",
				    chiron_gpu_rd(gpucc, 0x0000), chiron_gpu_rd(gpucc, 0x0004),
				    chiron_gpu_rd(gpucc, 0x0008), chiron_gpu_rd(gpucc, 0x000c),
				    chiron_gpu_rd(gpucc, 0x0010), chiron_gpu_rd(gpucc, 0x0018));
				kprintf("chiron-gpu: clock: rcg 1030=%08x 1070=%08x 10b0=%08x 1100=%08x\n",
				    chiron_gpu_rd(gpucc, 0x1030), chiron_gpu_rd(gpucc, 0x1070),
				    chiron_gpu_rd(gpucc, 0x10b0), chiron_gpu_rd(gpucc, 0x1100));
				kprintf("chiron-gpu: clock: cbcr 1054=%08x 1098=%08x 10d0=%08x 1124=%08x\n",
				    chiron_gpu_rd(gpucc, 0x1054), chiron_gpu_rd(gpucc, 0x1098),
				    chiron_gpu_rd(gpucc, 0x10d0), chiron_gpu_rd(gpucc, 0x1124));
				/*
				 * gpupll0 is a Fabia PLL: STATUS lives at 0x2c, not 0x18, and USER_CTL
				 * BIT0 is what holds it in or out of reset. The first run never read
				 * 0x2c, so whether the PLL is even locked is still unknown -- and
				 * nothing downstream can run if it is not. Pulse the reset bit and
				 * print both words either side, rather than assume.
				 */
				if (clk >= 2) {
					kprintf("chiron-gpu: pll: status 0x2c=%08x user 0x0c=%08x before\n",
					    chiron_gpu_rd(gpucc, 0x002c), chiron_gpu_rd(gpucc, 0x000c));
					chiron_gpu_wr(gpucc, 0x000c, chiron_gpu_rd(gpucc, 0x000c) & ~1u);
					delay(10);
					chiron_gpu_wr(gpucc, 0x000c, chiron_gpu_rd(gpucc, 0x000c) | 1u);
					for (i = 0; i < 100; i++) {
						if (chiron_gpu_rd(gpucc, 0x002c) & (1u << 16)) {
							break;
						}
						delay(1000);
					}
					kprintf("chiron-gpu: pll: status 0x2c=%08x user 0x0c=%08x after %u ms (%s)\n",
					    chiron_gpu_rd(gpucc, 0x002c), chiron_gpu_rd(gpucc, 0x000c), i,
					    (chiron_gpu_rd(gpucc, 0x002c) & (1u << 16)) ? "locked" : "no lock");
				}
				/*
				 * All four RCGs read 80000000 -- BIT31 set on every one of them, which
				 * is why enabling a branch below never cleared its halt bit. Whether
				 * that bit is a root-off flag or a halt status, clearing it is the only
				 * thing that can make the tree run, and SRC_SEL is set to 0 (CXO, which
				 * is always on) with DIV 1 so the clock does not depend on gpupll0 being
				 * right on the first attempt.
				 */
				if (clk >= 3) {
					v = chiron_gpu_rd(gpucc, 0x1070);
					kprintf("chiron-gpu: rcg: 0x1070 before %08x\n", v);
					v &= ~((1u << 31) | (0x7u << 8) | 0x1fu);
					v |= 1u;
					chiron_gpu_wr(gpucc, 0x1070, v);
					delay(100);
					kprintf("chiron-gpu: rcg: 0x1070 after %08x\n",
					    chiron_gpu_rd(gpucc, 0x1070));
				}
				v = chiron_gpu_rd(gpucc, 0x10d0);
				if (!(v & 1u)) {
					chiron_gpu_wr(gpucc, 0x10d0, v | 1u);
				}
				for (i = 0; i < 100; i++) {
					if (!(chiron_gpu_rd(gpucc, 0x10d0) & (1u << 31))) {
						break;
					}
					delay(1000);
				}
				kprintf("chiron-gpu: clock: rbbmtimer -> %08x after %u ms (%s)\n",
				    chiron_gpu_rd(gpucc, 0x10d0), i,
				    (chiron_gpu_rd(gpucc, 0x10d0) & (1u << 31)) ? "HALTED" : "running");
				v = chiron_gpu_rd(gpucc, 0x1098);
				if (!(v & 1u)) {
					chiron_gpu_wr(gpucc, 0x1098, v | 1u);
				}
				for (i = 0; i < 100; i++) {
					if (!(chiron_gpu_rd(gpucc, 0x1098) & (1u << 31))) {
						break;
					}
					delay(1000);
				}
				kprintf("chiron-gpu: clock: gfx3d -> %08x after %u ms (%s)\n",
				    chiron_gpu_rd(gpucc, 0x1098), i,
				    (chiron_gpu_rd(gpucc, 0x1098) & (1u << 31)) ? "HALTED" : "running");
			}
			if (pw >= 3) {
				v = chiron_gpu_rd(gpucc, 0x1094);
				if (v & 1u) {
					chiron_gpu_wr(gpucc, 0x1094, v & ~1u);
				}
				for (i = 0; i < 200; i++) {
					if (chiron_gpu_rd(gpucc, 0x1094) & (1u << 31)) {
						break;
					}
					delay(1000);
				}
				kprintf("chiron-gpu: power: gx -> %08x after %u ms (%s)\n",
				    chiron_gpu_rd(gpucc, 0x1094), i,
				    (chiron_gpu_rd(gpucc, 0x1094) & (1u << 31)) ? "on" : "still off");
			}
			if (pw >= 4) {
				chiron_gpu_wr(gpucc, 0x1090, 0);
			}
			kprintf("chiron-gpu: power: after bcr=%08x clamp=%08x cx=%08x gx=%08x cxc=%08x\n",
			    chiron_gpu_rd(gpucc, 0x1090), chiron_gpu_rd(gpucc, 0x0130),
			    chiron_gpu_rd(gpucc, 0x1004), chiron_gpu_rd(gpucc, 0x1094),
			    chiron_gpu_rd(gpucc, 0x1098));
			chiron_gpu_words("kgsl-after", kgsl, 0x0000, 8);
		}
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
    text = replace_once(text, "static efi_handle IMAGE;",
                        "static efi_handle IMAGE;\nstatic int chiron_mode;\n"
                        "static char chiron_args[192];   /* chiron: the payload's chiron_* tokens, kept for the kernel */")
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
    # --- the way back to the bootloader, and flashing without it ---------------------------
    # Two commands over the fastboot this loader already speaks:
    #   oem fastboot      write "bootonce-bootloader" to misc -- the bootloader control block
    #                     adb and fastboot write for "reboot bootloader" -- and reset.  ABL
    #                     reads misc on the way up and stops in fastboot once.
    #   flash:<part>      write what was just downloaded to that GPT partition, each 2 MB read
    #                     back and compared.  With it "fastboot flash boot ..." works from this
    #                     loader, and the volume-key dance is not needed again.
    snippet = (HERE / "loader_flash.c").read_text()
    helpers, commands = snippet.split("/* --- commands ---", 1)
    commands = commands.split("*/", 1)[1].strip("\n")
    text = replace_once(text,
        "static uint8_t block_buffer[BLOCK_BUFFER_SIZE] __attribute__((aligned(0x2000)));",
        "static uint8_t block_buffer[BLOCK_BUFFER_SIZE] __attribute__((aligned(0x2000)));\n"
        + helpers.rstrip("\n") + "\n")
    # --- chiron: let a kernel-side option actually reach the kernel -------------------------
    # Evidence from the phone (2026-11-12 19:43, logs/cap/gpuL1u.txt): the kernel printed
    #
    #     cmdline: debug=0x8 cpus=1 rd=md0
    #
    # while the payload's cmdline entry held "... chiron_gpu=1". This loader builds the
    # kernel's command line out of what it parsed for itself (debug=, cpus=, rd=,
    # hv_wdt_pet=) and then stops; the payload's own entry is a list of switches for the
    # loader, and nothing in it goes on. So every kernel-side option written there --
    # chiron_gpu among them -- never arrived, and the Adreno probe's first line on the phone
    # was always "chiron-gpu: not asked". Tokens named chiron_* are now copied verbatim,
    # after everything the loader writes itself, so the kernel can see them.
    pass_through = (
        "\t/* chiron: hand kernel-side options through. The tokens were copied out right after\n"
        "\t * the payload was parsed (see chiron_args above): the payload itself lives inside a\n"
        "\t * region the kernel's own image overwrites before this point, so reading pi.cmdline\n"
        "\t * here would find zeroes. */\n"
        "\tfor (const char *c = chiron_args; *c != 0 && n < BOOT_LINE_LENGTH - 1; c++) {\n"
        "\t\tba->commandLine[n++] = *c;\n"
        "\t}\n"
        "\tba->commandLine[n] = 0;\n"
    )
    anchor = None
    for candidate in ('\tint have_payload = parse_payload(&pi, 0);',
                      '\tint have_payload = parse_payload(&pi, 0);\n'):
        if text.count(candidate) == 1:
            anchor = candidate
            break
    if anchor is None:
        raise ValueError("chiron: the loader's parse_payload call moved; cannot stage options")
    copy_out = (
        "\t/*\n"
        "\t * chiron: copy the tokens meant for the kernel out of the payload right now. The\n"
        "\t * payload sits in memory that the kernel's own image is loaded over later, so by the\n"
        "\t * time the kernel's command line is assembled pi.cmdline points at zeroes. That is\n"
        "\t * exactly why the first attempt handed the kernel nothing while the payload's entry\n"
        "\t * plainly held chiron_gpu=2. The buffer itself is a file-scope static, because the\n"
        "\t * code that appends it lives further down and outside whatever block this is in.\n"
        "\t */\n"
        "\t{\n"
        "\t\tunsigned w = 0;\n\n"
        "\t\tfor (const char *c = pi.cmdline; *c != 0; ) {\n"
        "\t\t\tconst char *e = c;\n\n"
        "\t\t\twhile (*e != 0 && *e != ' ') {\n"
        "\t\t\t\te++;\n"
        "\t\t\t}\n"
        "\t\t\tif (e - c > 7 && c[0] == 'c' && c[1] == 'h' && c[2] == 'i' && c[3] == 'r' &&\n"
        "\t\t\t    c[4] == 'o' && c[5] == 'n' && c[6] == '_') {\n"
        "\t\t\t\tif (w != 0 && w < sizeof(chiron_args) - 1) {\n"
        "\t\t\t\t\tchiron_args[w++] = ' ';\n"
        "\t\t\t\t}\n"
        "\t\t\t\tfor (const char *p = c; p < e && w < sizeof(chiron_args) - 1; p++) {\n"
        "\t\t\t\t\tchiron_args[w++] = *p;\n"
        "\t\t\t\t}\n"
        "\t\t\t}\n"
        "\t\t\tc = e;\n"
        "\t\t\twhile (*c == ' ') {\n"
        "\t\t\t\tc++;\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t\tchiron_args[w] = 0;\n"
        "\t}\n"
    )
    text = replace_once(text, anchor, anchor + "\n" + copy_out)

    # ... and the copy goes onto the kernel's line, after everything the loader writes itself
    cmd_anchor = None
    for candidate in ('\tprint("cmdline: %s\\n", ba->commandLine);',
                      '\tprint("cmdline: %s", ba->commandLine);'):
        if text.count(candidate) == 1:
            cmd_anchor = candidate
            break
    if cmd_anchor is None:
        raise ValueError("chiron: the loader's cmdline print moved; cannot hand options through")
    text = replace_once(text, cmd_anchor, pass_through + cmd_anchor)
    text = replace_once(text, '\t\t} else if (starts_with(cmd, "upload")) {',
        commands + '\n\t\t} else if (starts_with(cmd, "upload")) {')
    text = replace_once(text,
        '\t\t\t\tif (to_ramdisk) {\n\t\t\t\t\tresident_rd_size = total;\n\t\t\t\t}\n\t\t\t\tfb_reply("OKAY", "");',
        '\t\t\t\tif (to_ramdisk) {\n\t\t\t\t\tresident_rd_size = total;\n\t\t\t\t}\n'
        '\t\t\t\tdownloaded_len = to_ramdisk ? 0 : total;   /* what a "flash:" may write */\n'
        '\t\t\t\tfb_reply("OKAY", "");')
    text = replace_once(text,
        '\t\t    starts_with(cmd, "oem sound") || starts_with(cmd, "oem clearscreen") || starts_with(cmd, "oem memmap"))) {',
        '\t\t    starts_with(cmd, "oem sound") || starts_with(cmd, "oem clearscreen") || starts_with(cmd, "oem memmap") ||\n'
        '\t\t    starts_with(cmd, "flash:") || starts_with(cmd, "oem fastboot") || starts_with(cmd, "oem dump"))) {')
    text = replace_once(text,
                        '\t\t} else if (starts_with(cmd, "oem run") || starts_with(cmd, "oem selftest")) {\n\t\t\tint selftest =',
                        '\t\t} else if (starts_with(cmd, "oem run") || starts_with(cmd, "oem selftest")) {\n'
                        '\t\t\tif (downloaded_len != 0) {\n'
                        '\t\t\t\tfb_reply("FAIL", "a download took the payload\'s memory: reboot first");\n'
                        '\t\t\t\treturn;\n'
                        '\t\t\t}\n'
                        '\t\t\tint selftest =')
    # --- the live screen: the kernel's log, drawn on the phone ------------------------------
    # The kernel publishes its log through struct kernel_live, and this core is awake beside it
    # for the whole run -- so the text can be put where a person can look at it, with no cable.
    # UEFI's console is not used (print() stops going to it before the run, on purpose: boot
    # services must not be called while the kernel reprograms the interrupt controller), so the
    # glyphs are ours and every pixel is a plain store into the framebuffer the loader was given.
    # The font is generated from the string art in loader_screen_font.py: one source of truth.
    sys.path.insert(0, str(HERE))
    from loader_screen_font import emit as emit_font

    screen = (HERE / "loader_screen.c").read_text().rstrip("\n")
    text = replace_once(text, "} SCREEN;",
                        "} SCREEN;\n" + emit_font() + "\n" + screen + "\n")
    text = replace_once(text,
        "\tuint64_t sound;                 /* (a virtual address too) the same for a recording of the sound */\n};",
        "\tuint64_t sound;                 /* (a virtual address too) the same for a recording of the sound */\n};\n\n"
        "static void screen_live_tick(const struct kernel_live *live, uint64_t slide);")
    text = replace_once(text, "\t\tusb_poll_command();",
                        "\t\tusb_poll_command();\n\t\tscreen_live_tick(live, slide);")
    # the screen says what it is doing before the minutes of silence, not after them
    text = replace_once(text,
        "\tstruct payload_info pi;\n\n\tif (!parse_payload(&pi, 1) || pi.kernel == NULL) {",
        "\tstruct payload_info pi;\n\n"
        '\tscreen_note("starting the kernel: minutes of quiet while its window is cleaned");\n'
        "\tif (!parse_payload(&pi, 1) || pi.kernel == NULL) {")
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
    # --- APFS: say what a compressed file looks like to the reader --------------------------
    # In the ramdisk, /sbin/init (52 KB, a single 64 KB piece) execs while /share/pattern.bin
    # (300 KB, five pieces) reads back wrong, so the difference is the multi-piece path.  The
    # data is not at fault: decompressing the same image on the host gives 300000 bytes that
    # match the generator byte for byte.  The failure is silent as well -- the length of every
    # piece checks out -- so nothing distinguishes a bad piece table from a bad piece read
    # without seeing the bytes.  Only files of 200 KB and up are reported, once per read of
    # their first byte, so the log stays readable.
    apfs_hits = sorted(tree.rglob("apfs_lib.c")) if tree.exists() else []
    print("apply_chiron: apfs_lib.c candidates: %s" % [str(p.relative_to(tree)) for p in apfs_hits],
          flush=True)
    if not apfs_hits:
        raise SystemExit("apply_chiron: no apfs_lib.c under %s" % tree)
    apfs = apfs_hits[0]
    text = replace_once(
        apfs.read_text(),
        "\t*out = 0;\n\t(void)ino;\n\tif (cf->piece_count == 0 || cf->piece_off == NULL) {\n\t\treturn EIO;\n\t}",
        "\t*out = 0;\n\t(void)ino;\n"
        "\tif (off == 0 && cf->size >= 200000) {\n"
        '\t\tapfs_log("cfile %llu: %llu bytes, type %u, rsrc %llu of %llu, %u pieces, read %llu+%zu\\n",\n'
        "\t\t    (unsigned long long)cf->obj_id, (unsigned long long)cf->size, cf->type,\n"
        "\t\t    (unsigned long long)cf->rsrc_id, (unsigned long long)cf->rsrc_size,\n"
        "\t\t    cf->piece_count, (unsigned long long)off, len);\n"
        "\t}\n"
        "\tif (cf->piece_count == 0 || cf->piece_off == NULL) {\n"
        '\t\tapfs_log("cfile %llu: no piece table\\n", (unsigned long long)cf->obj_id);\n'
        "\t\treturn EIO;\n"
        "\t}")
    text = replace_once(
        text,
        "\tif (error == 0) {\n\t\tcf->ra_expect = ((off + len) % APFS_DECMPFS_BLOCK_SIZE == 0)",
        "\tif (off == 0 && cf->size >= 200000 && *out >= 16) {\n"
        '\t\tapfs_log("cfile %llu: first %02x %02x %02x %02x %02x %02x %02x %02x (%zu bytes, %d)\\n",\n'
        "\t\t    (unsigned long long)cf->obj_id, buf[0], buf[1], buf[2], buf[3],\n"
        "\t\t    buf[4], buf[5], buf[6], buf[7], *out, error);\n"
        "\t}\n"
        "\tif (error == 0) {\n\t\tcf->ra_expect = ((off + len) % APFS_DECMPFS_BLOCK_SIZE == 0)")
    # The one place every read of a compressed file passes through, before the branch that
    # picks attr / resource fork / plain.  A file that never reaches read_cfile_rsrc has
    # already been decided here, so this line says which shape the reader believed in --
    # and, when cfile_init failed, the error it failed with.
    text = replace_once(
        text,
        "\terror = apfs_cfile_init(vol, ino, cf);\n\tif (error == 0) {\n\t\tif (!cf->compressed) {",
        "\terror = apfs_cfile_init(vol, ino, cf);\n"
        "\tif (off == 0) {\n"
        '\t\tapfs_log("read_file %llu: len %zu ino.size %llu -> err %d, comp %d, type %u, '\
        'sz %llu, rsrc %d, pieces %u, table %llu of %llu\\n",\n'
        "\t\t    (unsigned long long)ino->obj_id, len, (unsigned long long)ino->size, error,\n"
        "\t\t    cf->compressed, cf->type, (unsigned long long)cf->size, cf->in_rsrc,\n"
        "\t\t    cf->piece_count, (unsigned long long)cf->table_base,\n"
        "\t\t    (unsigned long long)cf->rsrc_size);\n"
        "\t}\n"
        "\tif (error == 0) {\n\t\tif (!cf->compressed) {")
    apfs.write_text(text)
    # --- APFS: a compressed file is not "0 bytes", so do not read it as one -----------------
    # The symptom is exact and silent: every read(2) of the boot image returns 0 bytes with no
    # error, while the kernel's own exec of /sbin/init (apfs_read_file(), which takes the size
    # from the decmpfs header) works.  Both read paths here -- apfs_read_cached() through the
    # page cache, and apfs_read_direct() past it -- start with the same gate:
    #
    #     if (uio_resid(uio) > 0 && (uint64_t)uio_offset(uio) < size) { ...read... }
    #
    # so with an_size 0 they copy nothing and return 0.  an_size is filled in by apfs_vget()
    # from apfs_inode_size(); for a decmpfs file the inode's own size is 0, so if that call
    # did not take, the vnode is "0 bytes" for ever.  Ask again, once per file (offset 0),
    # BEFORE either path is chosen -- earlier attempts sat inside one of them and never ran --
    # and say every number involved, so one boot settles which of them is wrong.
    vnops = apfs.parent / "apfs_vnops.c"
    repair = (
        "\t\tuint64_t real = 0;\n"
        "\t\tint e2 = apfs_inode_size(&am->am_vol, &ino, &real);\n"
    )
    text = replace_once(
        vnops.read_text(),
        "\tif (uio_offset(uio) < 0) {\n\t\treturn EINVAL;\n\t}\n\tif (!am->am_ubc_read || !UBCINFOEXISTS(vp)) {",
        "\tif (uio_offset(uio) < 0) {\n"
        "\t\treturn EINVAL;\n"
        "\t}\n"
        "\tino = APFS_VP(vp)->an_ino;\n"
        "\tif (uio_offset(uio) == 0) {\n"
        + repair +
        '\t\tapfs_log("read %llu: size %llu, an_size %llu, mode %d, ubc %d, info %d, ask %d, real %llu\\n",\n'
        "\t\t    (unsigned long long)ino.obj_id, (unsigned long long)ino.size,\n"
        "\t\t    (unsigned long long)APFS_VP(vp)->an_size, (int)ino.mode, (int)am->am_ubc_read,\n"
        "\t\t    (int)(UBCINFOEXISTS(vp) ? 1 : 0), e2, (unsigned long long)real);\n"
        "\t\tif (APFS_VP(vp)->an_size == 0 && e2 == 0 && real != 0) {\n"
        "\t\t\tAPFS_VP(vp)->an_size = real;\n"
        "\t\t}\n"
        "\t}\n"
        "\tif (!am->am_ubc_read || !UBCINFOEXISTS(vp)) {")
    # and the same, in the path past the page cache: it has its own copy of the gate
    text = replace_once(
        text,
        "\tino = APFS_VP(vp)->an_ino;\n\tsize = APFS_VP(vp)->an_size;\n\tt0 = apfs_ticks();\n"
        "\tif (uio_resid(uio) > 0 && (uint64_t)uio_offset(uio) < size) {",
        "\tino = APFS_VP(vp)->an_ino;\n\tsize = APFS_VP(vp)->an_size;\n"
        "\tif (size == 0 && uio_offset(uio) == 0) {\n"
        + repair +
        '\t\tapfs_log("read_direct %llu: size %llu, mode %d, ask %d, real %llu\\n",\n'
        "\t\t    (unsigned long long)ino.obj_id, (unsigned long long)ino.size, (int)ino.mode, e2,\n"
        "\t\t    (unsigned long long)real);\n"
        "\t\tif (e2 == 0 && real != 0) {\n"
        "\t\t\tAPFS_VP(vp)->an_size = real;\n"
        "\t\t\tsize = real;\n"
        "\t\t}\n"
        "\t}\n"
        "\tt0 = apfs_ticks();\n\tif (uio_resid(uio) > 0 && (uint64_t)uio_offset(uio) < size) {")
    # --- APFS: where does a user-space read stop? -------------------------------------------
    # The answer so far: an_size was RIGHT (52280 for /sbin/init), ubc and ubc-info were both 1,
    # so the page-cache path is taken and the gate passes.  Yet not one read entry line appeared
    # for /etc/motd -- a read at offset 0 prints unconditionally -- so the read never arrives.
    # Either the name lookup fails, or open does.  Filtered to that one file, these lines settle
    # it: which branch of the name cache answered, and whether open was ever reached.
    text = replace_once(
        text,
        "\t*ap->a_vpp = NULLVP;\n\tif (!vnode_isdir(dvp)) {",
        "\t*ap->a_vpp = NULLVP;\n"
        "\tint is_motd = cnp->cn_namelen == 4 && cnp->cn_nameptr[0] == 'm' && cnp->cn_nameptr[1] == 'o' &&\n"
        "\t    cnp->cn_nameptr[2] == 't' && cnp->cn_nameptr[3] == 'd';\n"
        "\tif (is_motd) {\n"
        '\t\tapfs_log("lookup: motd in ino %llu\\n", (unsigned long long)dn->an_id);\n'
        "\t}\n"
        "\tif (!vnode_isdir(dvp)) {")
    text = replace_once(
        text,
        "\tif (error == -1) {\n\t\tos_atomic_inc(&am->am_lookup_positive_hits, relaxed);\n\t\treturn 0;\n\t}",
        "\tif (error == -1) {\n"
        '\t\tif (is_motd) {\n\t\t\tapfs_log("lookup motd: the name cache had it\\n");\n\t\t}\n'
        "\t\tos_atomic_inc(&am->am_lookup_positive_hits, relaxed);\n\t\treturn 0;\n\t}")
    text = replace_once(
        text,
        "\tif (error == ENOENT) {\n\t\tos_atomic_inc(&am->am_lookup_negative_hits, relaxed);\n\t\treturn ENOENT;\n\t}",
        "\tif (error == ENOENT) {\n"
        '\t\tif (is_motd) {\n\t\t\tapfs_log("lookup motd: a NEGATIVE cache entry\\n");\n\t\t}\n'
        "\t\tos_atomic_inc(&am->am_lookup_negative_hits, relaxed);\n\t\treturn ENOENT;\n\t}")
    text = replace_once(
        text,
        "\tapfs_lock(am);\n\tif (dn->an_id >= APFS_SYNTH_BASE) {",
        '\tif (is_motd) {\n\t\tapfs_log("lookup motd: the cache did not answer, asking the volume\\n");\n\t}\n'
        "\tapfs_lock(am);\n\tif (dn->an_id >= APFS_SYNTH_BASE) {")
    text = replace_once(
        text,
        "apfs_vnop_open(struct vnop_open_args *ap)\n{\n\tif (ap->a_mode & (FWRITE | O_TRUNC | O_CREAT)) {",
        "apfs_vnop_open(struct vnop_open_args *ap)\n{\n"
        "\tif (APFS_VP(ap->a_vp)->an_ino.obj_id == 83) {\n"
        '\t\tapfs_log("open motd: mode %d\\n", ap->a_mode);\n'
        "\t}\n"
        "\tif (ap->a_mode & (FWRITE | O_TRUNC | O_CREAT)) {")
    vnops.write_text(text)
    return {str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (make, board, gic, apfs, vnops)}


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
