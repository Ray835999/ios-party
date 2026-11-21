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
 * gfx3d_clk's CBCR (0x1098) is not just an enable bit. gpucc_msm8998_probe writes BIT13
 * before anything else -- "force periph logic on to avoid perf counter corruption" -- and
 * a branch whose peripheral logic is not forced on never clears its halt bit, whatever the
 * RCG behind it is doing. Every previous run wrote BIT0 alone, read 80000620 back, and
 * called the clock dead. BIT13 goes in with BIT0 from now on.
 */
#define CHIRON_GFX3D_ON          (0x2000u | 1u)

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
				 * gpupll0 is a Fabia PLL, and a Fabia PLL does not use the generic
				 * alpha register layout. From clk-alpha-pll.c, the FABIA table is:
				 *
				 *   MODE 0x00   L_VAL 0x04   USER_CTL 0x0c   CONFIG_CTL 0x14
				 *   CONFIG_CTL_U 0x18   TEST_CTL 0x1c   STATUS 0x24
				 *   OPMODE 0x2c   FRAC 0x38
				 *
				 * Every earlier attempt read 0x2c as if it were STATUS. It is OPMODE,
				 * and it reads 0 -- PLL_STANDBY. The PLL has never been started, which
				 * is why it never locked, which is why the branches never came out of
				 * halt. STATUS at 0x24 has never been read once on this machine, and its
				 * lock bit is BIT31, not BIT0 and not BIT16: the old test could not have
				 * reported a lock even if there had been one.
				 *
				 * The sequence below is the one alpha_pll_fabia_enable performs: hold
				 * standby, release reset (MODE BIT2 = PLL_RESET_N), set OPMODE to
				 * PLL_RUN, wait for STATUS BIT31, then unmask the outputs (USER_CTL
				 * 0..2) and drop the output gate (MODE BIT0 = PLL_OUTCTRL). A PLL that
				 * will not lock only leaves its status bit clear -- it cannot hang the
				 * bus, so there is nothing to be careful about here.
				 */
				if (clk >= 5) {
					uint32_t mode, status, user, frac;

					kprintf("chiron-gpu: fabia: mode=%08x opmode=%08x status=%08x user=%08x\n",
					    chiron_gpu_rd(gpucc, 0x0000), chiron_gpu_rd(gpucc, 0x002c),
					    chiron_gpu_rd(gpucc, 0x0024), chiron_gpu_rd(gpucc, 0x000c));
					kprintf("chiron-gpu: fabia: l=%08x frac=%08x cfg=%08x cfgu=%08x test=%08x\n",
					    chiron_gpu_rd(gpucc, 0x0004), chiron_gpu_rd(gpucc, 0x0038),
					    chiron_gpu_rd(gpucc, 0x0014), chiron_gpu_rd(gpucc, 0x0018),
					    chiron_gpu_rd(gpucc, 0x001c));

					/*
					 * gpupll0 does not take XO directly. gpucc routes it through the
					 * branch at 0x1020 first -- "Instead of going directly to the block,
					 * XO is routed through this branch" -- and in Linux that branch is
					 * CLK_IS_CRITICAL, precisely because nothing else in the block can
					 * work without it. It has never been enabled here, so every PLL
					 * start so far ran with no reference clock at all: correct config,
					 * OPMODE taking PLL_RUN, and a status word that stays zero because
					 * the lock detector has nothing to compare against.
					 */
					user = chiron_gpu_rd(gpucc, 0x1020);
					kprintf("chiron-gpu: fabia: cxo 1020=%08x (the block's ref clock)\n", user);
					if (!(user & 1u)) {
						chiron_gpu_wr(gpucc, 0x1020, user | 1u);
						delay(1000);
					}
					kprintf("chiron-gpu: fabia: cxo 1020=%08x after enable\n",
					    chiron_gpu_rd(gpucc, 0x1020));

					chiron_gpu_wr(gpucc, 0x002c, 0);
					/*
					 * alpha_pll_fabia_enable does not set RESET_N on its own. Its mask is
					 *
					 *     PLL_OUTCTRL | PLL_BYPASSNL | PLL_RESET_N
					 *
					 * and it writes all three at once. MODE bit1 is PLL_BYPASSNL -- bypass,
					 * active low -- and leaving it engaged keeps the VCO out of the loop no
					 * matter what OPMODE says, so the lock detector has nothing to lock to.
					 * Every run so far cleared standby and released reset while still in
					 * bypass, which is why STATUS bit31 never came up.
					 */
					mode = chiron_gpu_rd(gpucc, 0x0000);
					if ((mode & 6u) != 6u) {
						chiron_gpu_wr(gpucc, 0x0000, (mode | 6u) & ~1u);
					}
					kprintf("chiron-gpu: fabia: mode=%08x (bypass %u reset_n %u) opmode -> run\n",
					    chiron_gpu_rd(gpucc, 0x0000),
					    (chiron_gpu_rd(gpucc, 0x0000) >> 1) & 1u,
					    (chiron_gpu_rd(gpucc, 0x0000) >> 2) & 1u);
					chiron_gpu_wr(gpucc, 0x002c, 1);
					for (i = 0; i < 100; i++) {
						if (chiron_gpu_rd(gpucc, 0x0024) & (1u << 31)) {
							break;
						}
						delay(1000);
					}
					status = chiron_gpu_rd(gpucc, 0x0024);
					kprintf("chiron-gpu: fabia: ran %u ms, mode=%08x opmode=%08x status=%08x (%s)\n",
					    i, chiron_gpu_rd(gpucc, 0x0000), chiron_gpu_rd(gpucc, 0x002c),
					    status, (status & (1u << 31)) ? "LOCKED" : "no lock");
					if (status & (1u << 31)) {
						user = chiron_gpu_rd(gpucc, 0x000c);
						frac = chiron_gpu_rd(gpucc, 0x0038);
						if ((user & 7u) != 7u) {
							chiron_gpu_wr(gpucc, 0x000c, user | 7u);
						}
						mode = chiron_gpu_rd(gpucc, 0x0000);
						chiron_gpu_wr(gpucc, 0x0000, mode | 1u);
						kprintf("chiron-gpu: fabia: out on, mode=%08x user=%08x frac=%08x\n",
						    chiron_gpu_rd(gpucc, 0x0000),
						    chiron_gpu_rd(gpucc, 0x000c), frac);
					}
				}
				/*
				 * CMD_RCGR's two low bits are not what they look like. From clk-rcg2.c:
				 *
				 *   CMD_UPDATE  BIT0   a strobe the hardware clears by itself
				 *   CMD_ROOT_EN BIT1   the level that actually enables the root clock
				 *
				 * Every earlier attempt wrote BIT0 and read back 80000000 with ROOT_EN
				 * still clear, and concluded the write had been dropped. It had not: the
				 * update had been consumed, on a root that was never enabled. So enable
				 * the root for the 3D source and for the timer source before touching
				 * their branches, and print SRC_SEL from CFG_RCGR (0x1074, BIT8..10) --
				 * that is the only place that says which parent the RCG is counting.
				 */
				if (clk >= 5) {
					v = chiron_gpu_rd(gpucc, 0x1070);
					kprintf("chiron-gpu: rcg: 1070=%08x 1074=%08x root_en=%d src_sel=%u\n",
					    v, chiron_gpu_rd(gpucc, 0x1074), (v & 2u) ? 1 : 0,
					    (chiron_gpu_rd(gpucc, 0x1074) >> 8) & 7u);
					if (!(v & 2u)) {
						chiron_gpu_wr(gpucc, 0x1070, v | 2u);
					}
					v = chiron_gpu_rd(gpucc, 0x10b0);
					if (!(v & 2u)) {
						chiron_gpu_wr(gpucc, 0x10b0, v | 2u);
					}
					kprintf("chiron-gpu: rcg: root on, 1070=%08x 10b0=%08x\n",
					    chiron_gpu_rd(gpucc, 0x1070), chiron_gpu_rd(gpucc, 0x10b0));
				}
				/*
				 * CFG_RCGR holds the source and the divider, and every RCG in this block
				 * reads 0 there. pre_div 0 is not "divide by one": __clk_rcg2_configure_mnd
				 * puts f->pre_div straight into BIT0..4 of this word, and a divider of
				 * zero means the RCG produces no clock at all. That is why no CBCR halt
				 * bit has cleared on this machine -- including rbbmtimer's, whose parent
				 * is XO and has nothing to do with gpupll0.
				 *
				 * Program them the way that function does: pre_div into BIT0..4, SRC_SEL
				 * into BIT8..10, then pulse CMD_UPDATE and wait for the strobe to clear.
				 * rbbmtimer goes first, on XO at pre_div 1. Then gfx3d is put on XO at
				 * pre_div 3 before the PLL is asked for anything: if the branch runs on
				 * XO, the branch and its halt logic are fine and the PLL is all that is
				 * left to explain.
				 */
				if (clk >= 6) {
					uint32_t cfg;

					/*
					 * The branch has to be enabled before its halt bit can mean anything,
					 * and the 22:53 run shows exactly that: 1098 read 80000620 after the
					 * divider was programmed -- BIT0 clear, branch off, halt guaranteed.
					 * This block runs ahead of the branch-enable code further down, so it
					 * opens the gate itself first.
					 */
					chiron_gpu_wr(gpucc, 0x1098,
					    chiron_gpu_rd(gpucc, 0x1098) | CHIRON_GFX3D_ON);
					delay(1000);
					kprintf("chiron-gpu: cfg: 1098 enabled -> %08x\n",
					    chiron_gpu_rd(gpucc, 0x1098));

					/*
					 * Only 0x1074 is proven readable here: the 22:28 run aborted the
					 * kernel inside the first line that touched rbbmtimer's CFG_RCGR and
					 * the three words after it (fault_code 0x10, external abort), and
					 * kprintf evaluates every argument before printing, so that whole
					 * line vanished. Those addresses stay untouched -- gfx3d is the
					 * branch that matters and its CFG_RCGR is a word this driver has
					 * already read successfully.
					 *
					 * The divider is what was missing. __clk_rcg2_configure_mnd writes
					 * f->pre_div into BIT0..4, so the 0 that every RCG reads there is a
					 * divider of zero, and a branch behind a zero divider never gets a
					 * clock and never clears its halt bit. gfx3d_clk_src is given XO at
					 * pre_div 3 first, then gpupll0_out_even at the same pre_div, so the
					 * log separates "the branch works and the PLL does not" from "the
					 * branch itself never worked".
					 */
					cfg = chiron_gpu_rd(gpucc, 0x1074);
					kprintf("chiron-gpu: cfg: 1074=%08x before\n", cfg);
					cfg &= ~(0x1fu | (0x7u << 8) | (0x3u << 12) | (1u << 20));
					cfg |= 3u;
					chiron_gpu_wr(gpucc, 0x1074, cfg);
					kprintf("chiron-gpu: cfg: 1074=%08x after, XO pre_div 3\n",
					    chiron_gpu_rd(gpucc, 0x1074));
					chiron_gpu_wr(gpucc, 0x1070, chiron_gpu_rd(gpucc, 0x1070) | 1u);
					for (i = 0; i < 50; i++) {
						if (!(chiron_gpu_rd(gpucc, 0x1098) & (1u << 31))) {
							break;
						}
						delay(1000);
					}
					kprintf("chiron-gpu: cfg: gfx3d on XO 1098=%08x after %u ms (%s) 1070=%08x\n",
					    chiron_gpu_rd(gpucc, 0x1098), i,
					    (chiron_gpu_rd(gpucc, 0x1098) & (1u << 31)) ? "HALTED" : "RUNNING",
					    chiron_gpu_rd(gpucc, 0x1070));

					cfg = chiron_gpu_rd(gpucc, 0x1074);
					cfg &= ~(0x7u << 8);
					cfg |= (1u << 8);
					chiron_gpu_wr(gpucc, 0x1074, cfg);
					chiron_gpu_wr(gpucc, 0x1070, chiron_gpu_rd(gpucc, 0x1070) | 1u);
					for (i = 0; i < 50; i++) {
						if (!(chiron_gpu_rd(gpucc, 0x1098) & (1u << 31))) {
							break;
						}
						delay(1000);
					}
					kprintf("chiron-gpu: cfg: gfx3d on PLL 1098=%08x after %u ms (%s)\n",
					    chiron_gpu_rd(gpucc, 0x1098), i,
					    (chiron_gpu_rd(gpucc, 0x1098) & (1u << 31)) ? "HALTED" : "RUNNING");

					/* Leave it on whichever source runs, so GX at least has a clock. */
					if (chiron_gpu_rd(gpucc, 0x1098) & (1u << 31)) {
						cfg = chiron_gpu_rd(gpucc, 0x1074);
						cfg &= ~(0x7u << 8);
						chiron_gpu_wr(gpucc, 0x1074, cfg);
						chiron_gpu_wr(gpucc, 0x1070,
						    chiron_gpu_rd(gpucc, 0x1070) | 1u);
						delay(1000);
						kprintf("chiron-gpu: cfg: back to XO, 1098=%08x\n",
						    chiron_gpu_rd(gpucc, 0x1098));
					}
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
				if ((v & CHIRON_GFX3D_ON) != CHIRON_GFX3D_ON) {
					chiron_gpu_wr(gpucc, 0x1098, v | CHIRON_GFX3D_ON);
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
				/*
				 * Final look at the two branches and at the RCG that feeds them. What
				 * the earlier "is it protected" probe actually showed is now explained:
				 * 0x10b0 is a CMD_RCGR, and its BIT0 is a self-clearing UPDATE strobe,
				 * not a level -- writing it and reading back 80000000 means the strobe
				 * was consumed, not that the write was dropped. 0x1074 (CFG_RCGR, with
				 * SRC_SEL at BIT8..10) is where the branch's parent actually lives, and
				 * it has never been read. If the PLL locks and a branch is still HALTED,
				 * this word says whether the RCG is pointed at a dead parent.
				 */
				if (clk >= 5) {
					kprintf("chiron-gpu: after: cbcr 1098=%08x 10d0=%08x 1054=%08x rcg 1070=%08x 1074=%08x\n",
					    chiron_gpu_rd(gpucc, 0x1098), chiron_gpu_rd(gpucc, 0x10d0),
					    chiron_gpu_rd(gpucc, 0x1054), chiron_gpu_rd(gpucc, 0x1070),
					    chiron_gpu_rd(gpucc, 0x1074));
				}
			}
			if (pw >= 3) {
				/*
				 * gpu_gx_gdsc names GPU_GX_BCR (0x1090) as its reset, and the Linux gdsc
				 * driver does not just clear a reset line: gdsc_enable() pulses it with
				 * reset_control_assert() then reset_control_deassert(), because the domain's
				 * power switch will not close its PWR_ON until it has seen the edge. The
				 * 00:16 and 10:14 runs only ever wrote 0, and 0x1094 stayed at 08222000 --
				 * SW_COLLAPSE clear, PWR_ON never set. Assert, wait, deassert.
				 */
				chiron_gpu_wr(gpucc, 0x1090, 1);
				delay(1000);
				chiron_gpu_wr(gpucc, 0x1090, 0);
				delay(1000);
				kprintf("chiron-gpu: power: gx reset pulsed 1 -> 0, 1090=%08x\n",
				    chiron_gpu_rd(gpucc, 0x1090));
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
			/*
			 * Level 7: an ordering bug, and the last three words of the 3D source.
			 *
			 * Everything the clock block above did ran while CX was up (pw 2) but GX was
			 * still collapsed -- pw 3 comes after it. gfx3d_clk's branch lives in the GX
			 * domain, and a branch in a collapsed domain can be enabled and still report
			 * halted for ever. That is exactly what 80000620 said on every run, and it is
			 * why pointing the RCG at XO changed nothing. Now clamp, CX, GX and the BCR
			 * are all released, so the same two experiments are worth repeating: the halt
			 * bit finally means something.
			 *
			 * 0x1078/0x107c/0x1080 are gfx3d_clk_src's M, N and D -- what
			 * __clk_rcg2_configure_mnd programs when CFG_RCGR's mode selects MND. None of
			 * them has been read on this phone. One line per word on purpose: an external
			 * abort takes the whole line that caused it with it, but the lines already
			 * printed survive in the log.
			 */
			if (clk >= 7) {
				uint32_t cfg, st;

				/*
				 * The 00:16 run answered the M/N/D question by aborting on the first of
				 * the three. 0x1078 is not implemented on this RCG -- gfx3d_clk_src runs
				 * in dual-edge mode with a plain divider, so the words
				 * __clk_rcg2_configure_mnd would program were never built -- and reading
				 * it took an external abort (fault_code 0x10) before its own kprintf
				 * could print. None of 0x1078/0x107c/0x1080 is touched again.
				 */
				kprintf("chiron-gpu: retry: cx=%08x gx=%08x bcr=%08x clamp=%08x 1074=%08x 1098=%08x\n",
				    chiron_gpu_rd(gpucc, 0x1004), chiron_gpu_rd(gpucc, 0x1094),
				    chiron_gpu_rd(gpucc, 0x1090), chiron_gpu_rd(gpucc, 0x0130),
				    chiron_gpu_rd(gpucc, 0x1074), chiron_gpu_rd(gpucc, 0x1098));

				/* XO at pre_div 3 first: a source that needs nothing from any PLL. */
				cfg = chiron_gpu_rd(gpucc, 0x1074);
				cfg &= ~(0x1fu | (0x7u << 8) | (0x3u << 12) | (1u << 20));
				cfg |= 3u;
				chiron_gpu_wr(gpucc, 0x1074, cfg);
				chiron_gpu_wr(gpucc, 0x1070, chiron_gpu_rd(gpucc, 0x1070) | 3u);
				chiron_gpu_wr(gpucc, 0x1098, chiron_gpu_rd(gpucc, 0x1098) | CHIRON_GFX3D_ON);
				for (i = 0; i < 100; i++) {
					if (!(chiron_gpu_rd(gpucc, 0x1098) & (1u << 31))) {
						break;
					}
					delay(1000);
				}
				st = chiron_gpu_rd(gpucc, 0x1098);
				kprintf("chiron-gpu: retry: XO/pre3 1098=%08x after %u ms (%s) 1070=%08x\n",
				    st, i, (st & (1u << 31)) ? "HALTED" : "RUNNING",
				    chiron_gpu_rd(gpucc, 0x1070));

				/* Then gpupll0_out_even, now that bypass is out of the enable. */
				cfg = chiron_gpu_rd(gpucc, 0x1074);
				cfg &= ~(0x7u << 8);
				cfg |= (1u << 8);
				chiron_gpu_wr(gpucc, 0x1074, cfg);
				chiron_gpu_wr(gpucc, 0x1070, chiron_gpu_rd(gpucc, 0x1070) | 1u);
				for (i = 0; i < 100; i++) {
					if (!(chiron_gpu_rd(gpucc, 0x1098) & (1u << 31))) {
						break;
					}
					delay(1000);
				}
				st = chiron_gpu_rd(gpucc, 0x1098);
				kprintf("chiron-gpu: retry: PLL 1098=%08x after %u ms (%s)\n",
				    st, i, (st & (1u << 31)) ? "HALTED" : "RUNNING");

				/* rbbmtimer rides XO directly, so it is the clean control. */
				chiron_gpu_wr(gpucc, 0x10d0, chiron_gpu_rd(gpucc, 0x10d0) | 1u);
				delay(1000);
				st = chiron_gpu_rd(gpucc, 0x10d0);
				kprintf("chiron-gpu: retry: rbbmtimer 10d0=%08x (%s)\n",
				    st, (st & (1u << 31)) ? "HALTED" : "running");
				kprintf("chiron-gpu: retry: fabia mode=%08x opmode=%08x status=%08x user=%08x\n",
				    chiron_gpu_rd(gpucc, 0x0000), chiron_gpu_rd(gpucc, 0x002c),
				    chiron_gpu_rd(gpucc, 0x0024), chiron_gpu_rd(gpucc, 0x000c));
				/*
				 * And the only proof that GX is really up: the 3D core's own registers.
				 * If they still read zero, the halt bit means "no power", not "no clock".
				 * This is the last line of the block on purpose -- if the core is not
				 * powered its register window can take an external abort with it.
				 */
				kprintf("chiron-gpu: retry: kgsl %08x %08x %08x %08x\n",
				    chiron_gpu_rd(kgsl, 0x0000), chiron_gpu_rd(kgsl, 0x0004),
				    chiron_gpu_rd(kgsl, 0x0008), chiron_gpu_rd(kgsl, 0x000c));
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
        "\t/* The loader's own line does not end in a space, and the first token here does not\n"
        "\t * begin with one, so the separator is written explicitly. Without it the last switch\n"
        "\t * and the first chiron_* token fuse into a single word -- loader_return_after=300\n"
        "\t * followed by chiron_gpu=2 arrives as loader_return_after=300chiron_gpu=2, and\n"
        "\t * neither option ever reaches the kernel's parser. */\n"
        "\tif (chiron_args[0] != 0 && n != 0 && ba->commandLine[n - 1] != ' ' &&\n"
        "\t    n < BOOT_LINE_LENGTH - 1) {\n"
        "\t\tba->commandLine[n++] = ' ';\n"
        "\t}\n"
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
    # ... and the switch that hands the panel to userspace. loader_screen=0 is read before the
    # kernel starts, so from that point on nothing here draws over what iOS's display driver
    # writes. screen_note's single "starting the kernel" frame still goes up first -- it is the
    # only thing on the panel until userspace has something of its own to show.
    text = replace_once(text,
        '\tfor (const char *c = pi.cmdline; *c; c++) {\n\t\tif (starts_with(c, "loader_return_after=")) {',
        '\tfor (const char *c = pi.cmdline; *c; c++) {\n'
        '\t\tif (starts_with(c, "loader_screen=")) {\n'
        "\t\t\tscreen_off = c[14] == '0';\n"
        '\t\t\tcontinue;\n'
        '\t\t}\n'
        '\t\tif (starts_with(c, "loader_return_after=")) {')
    # the screen says what it is doing before the minutes of silence, not after them
    text = replace_once(text,
        "\tstruct payload_info pi;\n\n\tif (!parse_payload(&pi, 1) || pi.kernel == NULL) {",
        "\tstruct payload_info pi;\n\n"
        '\tscreen_note("starting the kernel: minutes of quiet while its window is cleaned");\n'
        "\tif (!parse_payload(&pi, 1) || pi.kernel == NULL) {")
    # --- a second payload ramdisk: the boot volume as md0 -----------------------------------
    # The flash recipes boot from a small boot volume that carries stage0 and the empty mount
    # points, with the image holding init, rc and the launchd jobs as the second disk: stage0
    # mounts that one on /redwood and runs its init out of it. The payload had no way to carry
    # two disks, so the small image always landed in md0 and stage0 had nothing to mount on
    # /redwood -- which is why that tree stayed empty and launchd never found its libraries.
    text = replace_once(text,
        "\tuint64_t resident_rd;           /* size of a root image the host put at the resident address */\n",
        "\tuint64_t resident_rd;           /* size of a root image the host put at the resident address */\n"
        "\tconst uint8_t *ramdisk2;        /* chiron: the boot volume, which becomes /dev/md0 */\n"
        "\tuint32_t ramdisk2_size;\n")
    text = replace_once(text,
        '\t\t} else if (str_eq(e->name, "usb")) {',
        '\t\t} else if (str_eq(e->name, "ramdisk2")) {\n'
        '\t\t\tpi->ramdisk2 = p + sizeof(*e);            /* the boot volume: it is the root, md0 */\n'
        '\t\t\tpi->ramdisk2_size = e->size;\n'
        '\t\t} else if (str_eq(e->name, "usb")) {')
    text = replace_once(text,
        "\tuint64_t rd1_pa = 0, rd1_size = 0;\n",
        "\tuint64_t rd1_pa = 0, rd1_size = 0;\n"
        "\t/*\n"
        "\t * chiron: two disks from the payload. The boot volume is laid down first and becomes\n"
        "\t * RAMDisk (md0); the payload's own ramdisk follows and becomes RAMDisk1 (md1). That is\n"
        "\t * the order the device tree below expects, and the order stage0 needs: it mounts md1\n"
        "\t * on /redwood before running /redwood/sbin/init.\n"
        "\t */\n"
        "\tif (pi.ramdisk2_size != 0) {\n"
        "\t\tuint64_t boot_size = (pi.ramdisk2_size + PAGE_SIZE_MAX - 1) & ~(PAGE_SIZE_MAX - 1);\n\n"
        "\t\trd1_pa = rd_pa;\n"
        "\t\trd1_size = boot_size;\n"
        "\t\trd_pa = rd_pa + boot_size;\n"
        "\t}\n")
    text = replace_once(text,
        "\tuint64_t dt_pa = rd1_size != 0 ? rd1_pa + rd1_size : rd_pa + rd_size;\n",
        "\tuint64_t dt_pa = rd1_pa + rd1_size > rd_pa + rd_size ?"
        " rd1_pa + rd1_size : rd_pa + rd_size;\n")
    text = replace_once(text,
        '\tif (rd1_size != 0) {\n'
        '\t\tprint("resident image at pa %x size %x: it is the root (md0), the payload ramdisk is md1\\n", rd1_pa, rd1_size);\n'
        '\t}\n',
        '\tif (rd1_size != 0) {\n'
        '\t\tif (!rd_resident && pi.ramdisk2_size != 0) {\n'
        '\t\t\tmemset((void *)rd1_pa, 0, rd1_size);\n'
        '\t\t\tmemcpy((void *)rd1_pa, pi.ramdisk2, pi.ramdisk2_size);\n'
        '\t\t}\n'
        '\t\tprint("boot volume at pa %x size %x (md0), the payload ramdisk is md1\\n", rd1_pa, rd1_size);\n'
        '\t}\n')
    path.write_text(text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


USERLAND_GATE_C = '''
/* ---- the gate that drops the frame -------------------------------------------------------- */

/*
 * finish_update's fifth block loads vtable+0xb8 and calls it; a zero answer jumps straight to
 * "return 0" and the rest of the function -- where the frame is handed on -- never runs. That is
 * what the phone did twice: is_ready answered 1 throughout, ignore_update_p stopped answering 1
 * after 35 calls, begin_update returned 0, render_update returned a surface list, finish_update
 * returned 0, and MiniIOMobileFramebuffer never saw a swap at all. So this one is asked as well,
 * and a refusal is turned into a yes.
 *
 * The slot is read at run time instead of being checked against a recorded address: the table
 * lives in read-only memory shared by every process, so the page is copied before it is written
 * and nothing about the original has to be known in advance.
 */
static uintptr_t (*orig_frame_gate)(void *display);
static unsigned frame_gate_said;

static uintptr_t
my_frame_gate(void *display)
{
	uintptr_t answer = orig_frame_gate != NULL ? orig_frame_gate(display) : 0;

	if (frame_gate_said < 4) {
		frame_gate_said++;
		say("redwood: the frame gate answers %#lx (flags %#llx, b233 %#x, current %p, ctx %p, px %p)\\n",
		    (unsigned long)answer, (unsigned long long)DISPLAY_FLAGS(display), DISPLAY_B233(display),
		    (void *)(*(uintptr_t *)((char *)(display) + 0x230)),
		    (void *)(*(uintptr_t *)((char *)(display) + 0x4a8)),
		    (void *)(*(uintptr_t *)((char *)(display) + 0x830)));
	}
	return answer != 0 ? answer : 1;
}

static void *
replace_slot(const char *what, uintptr_t slot, void *replacement)
{
	uintptr_t *entry = (uintptr_t *)(slot + slide);
	vm_address_t page = (vm_address_t)entry & ~(vm_address_t)(vm_page_size - 1);
	kern_return_t kr;
	void *original;

	kr = vm_protect(mach_task_self(), page, vm_page_size, FALSE,
	    VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
	if (kr != KERN_SUCCESS) {
		say("redwood: %s: cannot make the table writable (%d)\\n", what, kr);
		return NULL;
	}
	original = (void *)*entry;
	*entry = (uintptr_t)replacement;
	vm_protect(mach_task_self(), page, vm_page_size, FALSE, VM_PROT_READ);
	say("redwood: %s: the slot held %p, the gate holds it now\\n", what, original);
	return original;
}

'''

USERLAND_INJECT_C = '''
/* ---- a container path for the window server, where none comes back ---------------------------- */

/*
 * backboardd asks containermanagerd for the shared system container of its own bundle
 * (pathProviderForSystemContainerForCurrentProcess -> container_query_get_single_result). On this
 * phone the container manager dies about five seconds after it starts, on every boot -- the kernel
 * says where:
 *
 *   usertrap: containermanager[154] exception 1 code 0x1 0x8: pc 0x1cdf1674c (CoreFoundation +0x7a74c)
 *   usertrap:   called from 0x1cdf026a4 (CoreFoundation +0x666a4)
 *   usertrap:   called from 0x1cdf69a3c / Foundation +0xbb80c / ContainerManagerCommon +0x82270
 *
 * -- a CoreFoundation object that came back NULL and was then retained (`ldar x8, [x0, #8]` with
 * x0 = 0). So the query comes back XPC_INTERRUPTED_REPLY (52), the path is NULL, and backboardd
 * terminates itself:
 *
 *   Error getting shared system container for bundleID: com.apple.backboardd: 52
 *   *** Terminating app due to uncaught exception 'NSInvalidArgumentException', reason:
 *   '*** -[NSURL initFileURLWithPath:]: nil string parameter'
 *
 * Nothing else in the window server wants a container: the path is the folder it keeps its shader
 * cache in. So a URL asked for with no path at all gets a folder of ours instead.
 */
static const char container_dir[] =
    "/private/var/containers/Data/System/1E1D0B4E-0000-4000-8000-000000000001";

static id (*orig_init_file_url)(id, SEL, id);
static id (*orig_init_file_url_dir)(id, SEL, id, BOOL);

static void
make_one_directory(char *path, size_t from)
{
    size_t i;

    for (i = from; path[i] != 0; i++) {
        if (path[i] == '/') {
            path[i] = 0;
            mkdir(path, 0755);
            path[i] = '/';
        }
    }
    mkdir(path, 0755);
}

static void
make_container_dirs(void)
{
    static const char caches[] = "/Library/Caches";
    char path[sizeof(container_dir) + sizeof(caches)];

    strcpy(path, container_dir);
    make_one_directory(path, 1);
    strcat(path, caches);
    make_one_directory(path, sizeof(container_dir));
}

static id
string_for_path(const char *path)
{
    return ((id (*)(Class, SEL, const char *))objc_msgSend)(objc_getClass("NSString"),
        sel_registerName("stringWithUTF8String:"), path);
}

static id
my_init_file_url(id self, SEL selector, id path)
{
    if (path == NULL) {
        say("hakios: the window server asked for a container and got none: %s\\n", container_dir);
        path = string_for_path(container_dir);
    }
    return orig_init_file_url(self, selector, path);
}

static id
my_init_file_url_dir(id self, SEL selector, id path, BOOL is_directory)
{
    if (path == NULL) {
        say("hakios: the window server asked for a container and got none: %s\\n", container_dir);
        path = string_for_path(container_dir);
    }
    return orig_init_file_url_dir(self, selector, path, is_directory);
}

static void
container_path_fallback(void)
{
    Class cls = objc_getClass("NSURL");
    Method one, two;

    make_container_dirs();
    if (cls == NULL) {
        return;
    }
    if ((one = class_getInstanceMethod(cls, sel_registerName("initFileURLWithPath:"))) != NULL) {
        orig_init_file_url = (id (*)(id, SEL, id))method_setImplementation(one, (IMP)my_init_file_url);
    }
    if ((two = class_getInstanceMethod(cls, sel_registerName("initFileURLWithPath:isDirectory:"))) != NULL) {
        orig_init_file_url_dir = (id (*)(id, SEL, id, BOOL))method_setImplementation(two,
            (IMP)my_init_file_url_dir);
    }
    say("hakios: a container path with no answer becomes %s\\n", container_dir);
}

/*
 * containermanagerd dies on every boot with a SIGSEGV whose stack nobody prints: the kernel's
 * systrace hook (user_exception_trace) has no call site in this tree, and launchd's exception
 * handler takes the fault before any of our code would run. A Unix SIGSEGV handler is installed
 * before any of that, and it is first in line: it walks the frame pointers of the dying thread
 * and writes them out, then exits with the death code launchd expects. The addresses are the
 * same shared-cache ones the hang watch prints, so symaddr.py names them on the host.
 */
#include <signal.h>
#include <sys/ucontext.h>

static void
cm_segv_say(int sig, siginfo_t *info, void *ctx)
{
    ucontext_t *uc = ctx;
    uintptr_t pcs[14];
    unsigned depth = 0;
    char line[1024];
    int at;
    uintptr_t fp;

    pcs[depth++] = uc->uc_mcontext->__ss.__pc;
    if (uc->uc_mcontext->__ss.__lr != 0) {
        pcs[depth++] = uc->uc_mcontext->__ss.__lr;
    }
    fp = uc->uc_mcontext->__ss.__fp;
    while (depth < sizeof(pcs) / sizeof(pcs[0]) && fp != 0 && (fp & 15) == 0) {
        uintptr_t next = ((uintptr_t *)fp)[0], frame_lr = ((uintptr_t *)fp)[1];

        if (frame_lr == 0) {
            break;
        }
        pcs[depth++] = frame_lr;
        if (next <= fp) {
            break;
        }
        fp = next;
    }
    at = snprintf(line, sizeof(line), "hakios: containermanagerd crash: bad %p:", info ? info->si_addr : NULL);
    for (unsigned i = 0; i < depth && at < (int)sizeof(line) - 96; i++) {
        Dl_info di;

        if (dladdr((void *)pcs[i], &di) != 0 && di.dli_fname != NULL) {
            const char *image = strrchr(di.dli_fname, '/');

            at += di.dli_sname != NULL && pcs[i] - (uintptr_t)di.dli_saddr < 0x4000 ?
                snprintf(line + at, sizeof(line) - at, " %s", di.dli_sname) :
                snprintf(line + at, sizeof(line) - at, " %s+%#lx",
                image != NULL ? image + 1 : di.dli_fname, pcs[i] - (uintptr_t)di.dli_fbase);
        } else {
            at += snprintf(line + at, sizeof(line) - at, " %#lx", pcs[i]);
        }
    }
    say("%s\\n", line);
    _exit(139);                     /* 128 + SIGSEGV: the death launchd was expecting */
}

/*
 * Where the broken ICU handle comes from: the iOS volume is the root, but /private/var is a
 * memory filesystem, and /etc/localtime points through /var/db/timezone/zoneinfo/current -- into
 * that empty memory var. ICU builds a date format whose own calendar is missing, and the first
 * thing CFDateFormatterSetFormat does with it falls over at 0x8, past the point an error code
 * could be written. The memory var is writable, so the chain the links expect is rebuilt here,
 * and the environment is pinned to UTC so ICU never needs the file at all.
 */
__attribute__((constructor))
static void
cm_timezone_fix(void)
{
    const char *name = getprogname();

    if (name == NULL || strncmp(name, "containermanagerd", 17) != 0) {
        return;
    }
    setenv("TZ", "UTC", 1);
    mkdir("/var/db", 0755);
    mkdir("/var/db/timezone", 0755);
    mkdir("/var/db/timezone/zoneinfo", 0755);
    if (symlink("/usr/share/zoneinfo/UTC", "/var/db/timezone/zoneinfo/current") == 0) {
        say("hakios: containermanagerd: the current zone is answered from the real root\\n");
    }
}

__attribute__((constructor))
static void
cm_segv_install(void)
{
    const char *name = getprogname();
    struct sigaction sa;

    if (name == NULL || strncmp(name, "containermanagerd", 17) != 0) {
        return;
    }
    sa.sa_sigaction = cm_segv_say;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_SIGINFO;
    if (sigaction(SIGSEGV, &sa, NULL) == 0) {
        say("hakios: containermanagerd: a crash will be described\\n");
        return;
    }
    say("hakios: containermanagerd: the crash handler could not be installed\\n");
}

/*
 * What the SIGSEGV handler named: CFDateFormatterCreateISO8601Formatter builds the formatter
 * shell, CFDateFormatterSetFormat then hands the ICU date format inside it to udate_applyPattern
 * -- and that handle is NULL here, where ICU has no zone or locale database of its own to open
 * with, so the first field it reads past the handle is the 0x8 of the crash. The call is the one
 * place in this path that crosses from CoreFoundation into libicucore, which means it goes
 * through a stub, which means dyld's interpose reaches it where a hook on CFDateFormatter's own
 * internals would not. A null handle is answered with an error code instead of dereferenced --
 * the C convention ICU itself is written to -- and CoreFoundation then takes its own failure
 * path (CFRelease, return NULL), which the container-path fallback above is already there for.
 * The library is only ever loaded into backboardd and the container managers; backboardd's own
 * calls pass a real handle and go through untouched.
 */
/*
 * The originals behind these interposes are not reachable through dlsym, and that is what the
 * 17:45 round cost. An interpose section is what dyld consults for every lookup made in this
 * process, so RTLD_NEXT -- this library is the last image loaded -- has nothing later to offer,
 * and RTLD_DEFAULT hands back the replacement itself. The log said exactly that, once per
 * process, and then every process that took the null the wrapper returned died on SIGTRAP:
 * SpringBoard was restarted 178 times, backboardd 89, and the screen never changed again.
 *
 * What the cache does offer is where it was built to live. Below are the unslid addresses of the
 * four symbols, read out of this phone's own 20F66 iPhone10,3 cache (tools/dsctrie.py walks the
 * export tries; each was cross-checked against the LC_SYMTAB of its image), and the first
 * sixteen bytes of its code beside each one. cache_function adds the slide, checks that those
 * bytes are still what the cache holds there, and only then hands the address out: a cache from
 * some other build answers NULL instead of jumping into whatever happens to be at the address,
 * and original_behind then falls back to dlsym for the symbols that are not interposed.
 */
extern const void *_dyld_get_shared_cache_range(size_t *length);

#define UNSLID_XPC_REPLY_SYNC   0x1d0d27000UL    /* /usr/lib/system/libxpc.dylib */
#define UNSLID_CFDF_ISO         0x186ea9710UL    /* /System/Library/Frameworks/CoreFoundation.framework */
#define UNSLID_CFDF_CREATE      0x186e82c4cUL    /* /System/Library/Frameworks/CoreFoundation.framework */
#define UNSLID_UDAT_APPLY       0x18f297ea8UL    /* /usr/lib/libicucore.A.dylib */
#define UNSLID_CFDF_SETFORMAT   0x186e653d0UL    /* /System/Library/Frameworks/CoreFoundation.framework */

static const unsigned char SIG_XPC_REPLY_SYNC[16] = {
    0xff, 0x43, 0x01, 0xd1, 0xf8, 0x5f, 0x01, 0xa9, 0xf6, 0x57, 0x02, 0xa9, 0xf4, 0x4f, 0x03, 0xa9
};
static const unsigned char SIG_CFDF_ISO[16] = {
    0xff, 0x03, 0x02, 0xd1, 0xfc, 0x6f, 0x02, 0xa9, 0xfa, 0x67, 0x03, 0xa9, 0xf8, 0x5f, 0x04, 0xa9
};
static const unsigned char SIG_CFDF_CREATE[16] = {
    0xa8, 0x29, 0x2c, 0xf0, 0x08, 0x21, 0x21, 0x91, 0x04, 0x01, 0x40, 0xf9, 0xd6, 0xbc, 0xfe, 0x17
};
static const unsigned char SIG_UDAT_APPLY[16] = {
    0xff, 0x43, 0x02, 0xd1, 0xf4, 0x4f, 0x07, 0xa9, 0xfd, 0x7b, 0x08, 0xa9, 0xfd, 0x03, 0x02, 0x91
};
/* stp x22,x21,[sp,#-0x30]!; stp x20,x19,[sp,#0x10]; stp x29,x30,[sp,#0x20]; add x29,sp,#0x20
 * -- then "add x8, x0, #8" and a load through it, which is the crash at address 8. */
static const unsigned char SIG_CFDF_SETFORMAT[16] = {
    0xf6, 0x57, 0xbd, 0xa9, 0xf4, 0x4f, 0x01, 0xa9, 0xfd, 0x7b, 0x02, 0xa9, 0xfd, 0x83, 0x00, 0x91
};

static void *
cache_function(uintptr_t unslid, const unsigned char *signature)
{
    const uintptr_t base = 0x180000000UL;    /* where the cache was built to be, before the slide */
    size_t length = 0;
    const void *cache = _dyld_get_shared_cache_range(&length);
    uintptr_t address;

    if (cache == NULL || length == 0 || unslid < base || unslid >= base + length) {
        return NULL;
    }
    address = unslid + ((uintptr_t)cache - base);
    if (memcmp((const void *)address, signature, 16) != 0) {
        return NULL;
    }
    return (void *)address;
}

static void *
original_behind(uintptr_t unslid, const unsigned char *signature, const char *name, const void *replacement)
{
    void *symbol = cache_function(unslid, signature);

    if (symbol == NULL) {
        symbol = dlsym(RTLD_NEXT, name);
        if (symbol == NULL || symbol == replacement) {
            symbol = dlsym(RTLD_DEFAULT, name);
        }
        if (symbol == replacement) {
            symbol = NULL;
        }
    }
    if (symbol == NULL) {
        say("hakios: %s has nothing behind the interpose; the wrapper answers by itself\\n", name);
    }
    return symbol;
}

#define DYLD_INTERPOSE(_replacement, _replacee) \\
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \\
    _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = \\
        { (const void *)&_replacement, (const void *)&_replacee };

static int cm_answer_null_formats;      /* set in the container manager only */

static void (*orig_udate_apply_pattern)(void *format, int pattern_in_locale, const void *pattern,
    int length, int *status);

static void
my_udate_apply_pattern(void *format, int pattern_in_locale, const void *pattern, int length, int *status)
{
    if (orig_udate_apply_pattern == NULL ||
        (cm_answer_null_formats && format == NULL)) {
        say("hakios: udat_applyPattern with a null format; an error is answered\\n");
        if (status != NULL) {
            *status = 1;                /* U_ILLEGAL_ARGUMENT_ERROR */
        }
        return;
    }
    orig_udate_apply_pattern(format, pattern_in_locale, pattern, length, status);
}

extern void udat_applyPattern(void *format, int pattern_in_locale, const void *pattern, int length, int *status);

DYLD_INTERPOSE(my_udate_apply_pattern, udat_applyPattern)

/*
 * What this round's crash added: the handle handed to ICU was not null -- the pc landed inside
 * libicucore with real CFDateFormatterSetFormat frames below it -- so the ISO8601 path gives ICU
 * a handle whose own insides are broken (no zone data behind it) and ICU falls over past the
 * point any error code could be written. That path starts at
 * CFDateFormatterCreateISO8601Formatter, which the container manager calls across images, so the
 * interpose reaches it: in the container manager the formatter is rebuilt with
 * CFDateFormatterCreate (NoStyle, NoStyle) -- the same shell without the ISO8601 special case --
 * and any pattern call ICU cannot honour is answered with an error by the interpose above. The
 * other processes forward to the original untouched.
 */
static CFDateFormatterRef (*orig_cfdf_iso)(CFAllocatorRef, CFLocaleRef);
static CFDateFormatterRef (*cfdf_create)(CFAllocatorRef, CFLocaleRef, CFDateFormatterStyle, CFDateFormatterStyle);

static CFDateFormatterRef
my_cfdf_iso(CFAllocatorRef allocator, CFLocaleRef locale)
{
    if (orig_cfdf_iso == NULL) {
        return NULL;
    }
    if (!cm_answer_null_formats || cfdf_create == NULL) {
        return orig_cfdf_iso(allocator, locale);
    }
    CFDateFormatterRef formatter = cfdf_create(allocator, locale, kCFDateFormatterNoStyle, kCFDateFormatterNoStyle);
    say("hakios: containermanagerd: the ISO8601 formatter is rebuilt (%s), not taken through the crashing path\\n",
        formatter != NULL ? "a shell" : "null");
    return formatter;
}

DYLD_INTERPOSE(my_cfdf_iso, CFDateFormatterCreateISO8601Formatter)

/*
 * The rebuilt formatter can be null as well -- CFDateFormatterCreate fails too when the locale
 * data behind it is gone -- and the caller carries straight on into CFDateFormatterSetFormat.
 * That function's fourth instruction is "add x8, x0, #8" followed by a load through x8, so a
 * null formatter makes it read address 8: that is the crash the container manager has been
 * dying of, and the crash report names it exactly --
 *
 *     bad 0x8: CFDateFormatterSetFormat CFDateFormatterSetFormat CFDateFormatterCreateISO8601Formatter
 *
 * A null formatter has nothing to set a format on, so the call is answered instead of made.
 * The address was read out of this phone's 20F66 iPhone10,3 cache: CFDateFormatterCreateISO8601
 * calls it five times, and the sixteen bytes at the target are the ones recorded in
 * SIG_CFDF_SETFORMAT.
 */
extern void CFDateFormatterSetFormat(CFDateFormatterRef, CFStringRef);

static void (*orig_cfdf_setformat)(CFDateFormatterRef, CFStringRef);

static void
my_cfdf_setformat(CFDateFormatterRef formatter, CFStringRef format)
{
	if (formatter == NULL) {
		if (cm_answer_null_formats) {
			say("hakios: containermanagerd: a null formatter takes no format\\n");
		}
		return;
	}
	if (orig_cfdf_setformat != NULL) {
		orig_cfdf_setformat(formatter, format);
	}
}

DYLD_INTERPOSE(my_cfdf_setformat, CFDateFormatterSetFormat)

__attribute__((constructor))
static void
cm_icu_interpose(void)
{
    /*
     * The interpose itself is a section dyld scans, so it is in force in every process the
     * library lands in; the replacement is registered statically above. The original behind it
     * is resolved once here, for all of them, and only the container manager is allowed to
     * answer a null with an error: backboardd's calls carry a real handle and go through
     * untouched. The name is udat_ (UDateFormat's), not udate_ -- that older spelling is the
     * reason the first try found nothing at all.
     */
    orig_udate_apply_pattern = (void (*)(void *, int, const void *, int, int *))
        original_behind(UNSLID_UDAT_APPLY, SIG_UDAT_APPLY, "udat_applyPattern",
        (const void *)&my_udate_apply_pattern);
    orig_cfdf_iso = (CFDateFormatterRef (*)(CFAllocatorRef, CFLocaleRef))
        original_behind(UNSLID_CFDF_ISO, SIG_CFDF_ISO, "CFDateFormatterCreateISO8601Formatter",
        (const void *)&my_cfdf_iso);
    cfdf_create = (CFDateFormatterRef (*)(CFAllocatorRef, CFLocaleRef, CFDateFormatterStyle, CFDateFormatterStyle))
        original_behind(UNSLID_CFDF_CREATE, SIG_CFDF_CREATE, "CFDateFormatterCreate", NULL);
    orig_cfdf_setformat = (void (*)(CFDateFormatterRef, CFStringRef))
        original_behind(UNSLID_CFDF_SETFORMAT, SIG_CFDF_SETFORMAT, "CFDateFormatterSetFormat",
        (const void *)&my_cfdf_setformat);
    if (getprogname() != NULL && strncmp(getprogname(), "containermanagerd", 17) == 0) {
        cm_answer_null_formats = 1;
        say("hakios: containermanagerd: a null ICU format is answered, not dereferenced\\n");
    }
}

/*
 * What backboardd's own stacks named this round: its main thread sits in
 * xpc_connection_send_message_with_reply_sync on a connection to the container manager --
 * which dies on its ISO8601 formatter before answering, so the wait is unbounded. While the
 * main thread waits there it serves no other mach message, and one of those unanswered
 * messages is what SpringBoard's lock-holding thread waits for: the whole chain, from one
 * synchronous send. So in backboardd, a synchronous send to the container manager is answered
 * here and now with an empty dictionary -- every lookup in it misses, and the container code
 * takes the path it already takes when a container is missing. Every other connection, and
 * every other process, goes through untouched.
 */
/* No xpc/xpc.h here -- the SDK does not ship one; the ABI is pointers all the way down. */
extern const char *xpc_connection_get_name(void *);
extern void *xpc_dictionary_create(const char *const keys[], void *const values[], size_t count);

static void * (*orig_xpc_reply_sync)(void *, void *);
static int bb_answer_containers_here;
static unsigned bb_xpc_logged;

static void *
my_xpc_reply_sync(void *connection, void *message)
{
    const char *name;

    if (!bb_answer_containers_here) {
        return orig_xpc_reply_sync != NULL ? orig_xpc_reply_sync(connection, message) : NULL;
    }
    /* The phone drew the line for us, and it is much narrower than "never wait".
     *
     * The round that answered every synchronous send on this process killed the picture
     * outright: 24 sends to com.apple.runningboard came back empty, and each one was followed
     * within 200 microseconds by "[connection] Handshake failed with error: <(null)>" from
     * backboardd[13:282] -- one for one, every time. RunningBoard is what grants the activity
     * assertion backboardd waits on ("addOverlay(1-init 2): waiting for activity assertion"),
     * so an empty answer there means it never gets one, and the boot logo it had already put
     * up is never replaced. That is the opposite of progress.
     *
     * RunningBoard answers its own sends; it does not need this process to service a reply
     * first. Only the container manager has to be fenced off here: it dies on its ICU
     * formatter before answering, so a thread that waits on it waits for a process that is
     * already gone. Everything else goes through untouched. */
    name = connection != NULL ? xpc_connection_get_name(connection) : NULL;
    if (name == NULL || strstr(name, "containermanagerd") == NULL) {
        return orig_xpc_reply_sync != NULL ? orig_xpc_reply_sync(connection, message) : NULL;
    }
    if (bb_xpc_logged < 32) {
        bb_xpc_logged++;
        say("hakios: %s: %s answered empty, not waited for\\n",
            getprogname() != NULL ? getprogname() : "?", name);
    }
    return xpc_dictionary_create(NULL, NULL, 0);
}

extern void *xpc_connection_send_message_with_reply_sync(void *, void *);

DYLD_INTERPOSE(my_xpc_reply_sync, xpc_connection_send_message_with_reply_sync)

__attribute__((constructor))
static void
bb_xpc_interpose(void)
{
    orig_xpc_reply_sync = (void * (*)(void *, void *))
        original_behind(UNSLID_XPC_REPLY_SYNC, SIG_XPC_REPLY_SYNC,
        "xpc_connection_send_message_with_reply_sync", (const void *)&my_xpc_reply_sync);
    if (orig_xpc_reply_sync == NULL) {
        return;
    }
    if (getprogname() != NULL && strncmp(getprogname(), "backboardd", 10) == 0) {
        bb_answer_containers_here = 1;
        say("hakios: backboardd: a synchronous container send is answered, not waited on\\n");
    }
}

/* ---- what the shell's daemons do when their container is nowhere --------------------------- */

/*
 * Three deaths in the 22:11 run, all the same disease in different bodies:
 *
 *   iconservicesagent[14] Terminating ... '-[NSURL initFileURLWithPath:]: nil string parameter'
 *   pkd[18]  Terminating ... 'unable to locate user container: error 52'
 *   lsd[126] exited due to SIGABRT, service has crashed 4 times in a row, nothing printed
 *
 * The first is the exact nil the container fallback already answers for backboardd and
 * SpringBoard -- those two daemons simply never got the library's constructor to turn it on.
 * The second is the same query failing one layer deeper. The third said nothing because
 * libc++abi prints only when an NSException is in flight; a plain abort() leaves no reason,
 * and a shell whose LaunchServices daemon dies four times in a row has nothing to build a
 * desktop from.
 *
 * So the fallback is turned on for the daemons the shell starts, and every injected process
 * gains an abort handler that walks the frame chain the way the SIGSEGV one does: the next
 * round starts from the answer, not from the guess.
 */
static const char *const daemon_fallback_names[] = {
    "lsd", "pkd", "pluginkit", "iconservicesagent", NULL
};

__attribute__((constructor))
static void
daemon_container_fallback(void)
{
    const char *name = getprogname();
    unsigned i;

    if (name == NULL) {
        return;
    }
    for (i = 0; daemon_fallback_names[i] != NULL; i++) {
        if (strcmp(name, daemon_fallback_names[i]) == 0) {
            container_path_fallback();
            say("hakios: %s: the container fallback is on\\n", name);
            return;
        }
    }
}

static void
daemon_abort_say(int sig, siginfo_t *info, void *ctx)
{
    ucontext_t *uc = ctx;
    uintptr_t pcs[14];
    unsigned depth = 0;
    char line[1024];
    int at;
    uintptr_t fp;

    pcs[depth++] = uc->uc_mcontext->__ss.__pc;
    if (uc->uc_mcontext->__ss.__lr != 0) {
        pcs[depth++] = uc->uc_mcontext->__ss.__lr;
    }
    fp = uc->uc_mcontext->__ss.__fp;
    while (depth < sizeof(pcs) / sizeof(pcs[0]) && fp != 0 && (fp & 15) == 0) {
        uintptr_t next = ((uintptr_t *)fp)[0], frame_lr = ((uintptr_t *)fp)[1];

        if (frame_lr == 0) {
            break;
        }
        pcs[depth++] = frame_lr;
        if (next <= fp) {
            break;
        }
        fp = next;
    }
    at = snprintf(line, sizeof(line), "hakios: %s aborted: bad %p:",
        getprogname() != NULL ? getprogname() : "?", info ? info->si_addr : NULL);
    for (unsigned i = 0; i < depth && at < (int)sizeof(line) - 96; i++) {
        Dl_info di;

        if (dladdr((void *)pcs[i], &di) != 0 && di.dli_fname != NULL) {
            const char *image = strrchr(di.dli_fname, '/');

            at += di.dli_sname != NULL && pcs[i] - (uintptr_t)di.dli_saddr < 0x4000 ?
                snprintf(line + at, sizeof(line) - at, " %s", di.dli_sname) :
                snprintf(line + at, sizeof(line) - at, " %s+%#lx",
                image != NULL ? image + 1 : di.dli_fname, pcs[i] - (uintptr_t)di.dli_fbase);
        } else {
            at += snprintf(line + at, sizeof(line) - at, " %#lx", pcs[i]);
        }
    }
    say("%s\\n", line);
    _exit(128 + SIGABRT);           /* the death launchd was expecting */
}

__attribute__((constructor))
static void
daemon_abort_watch(void)
{
    struct sigaction sa;

    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = daemon_abort_say;
    sa.sa_flags = SA_SIGINFO;
    if (sigaction(SIGABRT, &sa, NULL) == 0) {
        say("hakios: an abort is described before it is taken\\n");
    }
}

/*
 * backboardd gets the same treatment the SpringBoard side gives its own process. When
 * SpringBoard's main thread waits on the BSCollectionFilter lock, the holder sits in this
 * process holding it across a mach message; these stacks say what this process was doing with
 * it. Every second turn, all of the threads.
 */
static void *
bb_hang_watch(void *argument)
{
    unsigned turn;

    for (turn = 1; turn <= 90; turn++) {
        sleep(20);
        if (turn % 2 != 0) {
            continue;
        }
        thread_act_array_t threads = NULL;
        mach_msg_type_number_t count = 0;
        thread_t self = mach_thread_self();
        uint64_t seconds = (uint64_t)turn * 20;
        unsigned i;

        if (task_threads(mach_task_self(), &threads, &count) != KERN_SUCCESS) {
            continue;
        }
        say("hakios: backboardd: %u threads at %llus\\n", (unsigned)count, seconds);
        for (i = 0; i < count && i < 24; i++) {
            arm_thread_state64_t state;
            mach_msg_type_number_t scount = ARM_THREAD_STATE64_COUNT;
            uintptr_t pcs[10];
            unsigned depth = 0;
            char line[1024];
            int at;
            unsigned j;

            if (threads[i] == self || thread_suspend(threads[i]) != KERN_SUCCESS) {
                continue;
            }
            if (thread_get_state(threads[i], ARM_THREAD_STATE64, (thread_state_t)&state, &scount) == KERN_SUCCESS) {
                uintptr_t fp = (uintptr_t)arm_thread_state64_get_fp(state);
                uintptr_t lr = (uintptr_t)arm_thread_state64_get_lr(state);

                pcs[depth++] = (uintptr_t)arm_thread_state64_get_pc(state);
                if (lr != 0) {
                    pcs[depth++] = lr;
                }
                while (depth < sizeof(pcs) / sizeof(pcs[0]) && (fp & 15) == 0) {
                    vm_size_t got = 0;
                    uintptr_t pair[2];
                    Dl_info validity;

                    if (vm_read_overwrite(mach_task_self(), (vm_address_t)fp, sizeof(pair),
                        (vm_address_t)&pair, &got) != KERN_SUCCESS) {
                        break;
                    }
                    if (pair[1] == 0 || dladdr((void *)pair[1], &validity) == 0 || validity.dli_fname == NULL) {
                        break;
                    }
                    pcs[depth++] = pair[1];
                    if (pair[0] <= fp) {
                        break;
                    }
                    fp = pair[0];
                }
            }
            thread_resume(threads[i]);
            at = snprintf(line, sizeof(line), "hakios: backboardd: thread %u at %llus:", i, seconds);
            for (j = 0; j < depth && at < (int)sizeof(line) - 96; j++) {
                Dl_info info;

                if (dladdr((void *)pcs[j], &info) != 0 && info.dli_fname != NULL) {
                    const char *image = strrchr(info.dli_fname, '/');

                    at += info.dli_sname != NULL && pcs[j] - (uintptr_t)info.dli_saddr < 0x4000 ?
                        snprintf(line + at, sizeof(line) - at, " %s", info.dli_sname) :
                        snprintf(line + at, sizeof(line) - at, " %s+%#lx",
                        image != NULL ? image + 1 : info.dli_fname, pcs[j] - (uintptr_t)info.dli_fbase);
                } else {
                    at += snprintf(line + at, sizeof(line) - at, " %#lx", pcs[j]);
                }
            }
            say("%s\\n", line);
        }
        for (i = 0; i < count; i++) {
            mach_port_deallocate(mach_task_self(), threads[i]);
        }
        vm_deallocate(mach_task_self(), (vm_address_t)threads, count * sizeof(thread_t));
    }
    return NULL;
}

__attribute__((constructor))
static void
bb_watch_install(void)
{
    pthread_t watcher;

    if (getprogname() == NULL || strncmp(getprogname(), "backboardd", 10) != 0) {
        return;
    }
    if (pthread_create(&watcher, NULL, bb_hang_watch, NULL) == 0) {
        pthread_detach(watcher);
        say("hakios: backboardd: every thread is looked at every 40 seconds\\n");
        return;
    }
    say("hakios: backboardd: no thread to look at the others with\\n");
}
'''


USERLAND_INJECT_SB_C = '''
/* ---- what SpringBoard waits for a container ------------------------------------------------ */

/*
 * SpringBoard asks containermanagerd for its own containers exactly the way backboardd does
 * (inject.c): when the container manager is not answering the query comes back with no path at
 * all, and -[NSURL initFileURLWithPath:] with a nil string throws. backboardd got a folder of
 * ours standing in for the one it wanted; SpringBoard, which is a different library
 * (libhakios.sb.dylib), had nothing, so it is given the same.
 */
static const char sb_container_dir[] =
    "/private/var/containers/Data/System/1E1D0B4E-0000-4000-8000-000000000001";

static id (*orig_sb_init_file_url)(id, SEL, id);
static id (*orig_sb_init_file_url_dir)(id, SEL, id, BOOL);

static void
sb_make_one_directory(char *path, size_t from)
{
	size_t i;

	for (i = from; path[i] != 0; i++) {
		if (path[i] == '/') {
			path[i] = 0;
			mkdir(path, 0755);
			path[i] = '/';
		}
	}
	mkdir(path, 0755);
}

static void
sb_make_container_dirs(void)
{
	static const char caches[] = "/Library/Caches";
	char path[sizeof(sb_container_dir) + sizeof(caches)];

	strcpy(path, sb_container_dir);
	sb_make_one_directory(path, 1);
	strcat(path, caches);
	sb_make_one_directory(path, sizeof(sb_container_dir));
}

static id
sb_string_for_path(const char *path)
{
	return ((id (*)(Class, SEL, const char *))objc_msgSend)(objc_getClass("NSString"),
	    sel_registerName("stringWithUTF8String:"), path);
}

static id
sb_init_file_url(id self, SEL selector, id path)
{
	if (path == NULL) {
		say("hakios: SpringBoard asked for a container and got none: %s\\n", sb_container_dir);
		path = sb_string_for_path(sb_container_dir);
	}
	return orig_sb_init_file_url(self, selector, path);
}

static id
sb_init_file_url_dir(id self, SEL selector, id path, BOOL is_directory)
{
	if (path == NULL) {
		say("hakios: SpringBoard asked for a container and got none: %s\\n", sb_container_dir);
		path = sb_string_for_path(sb_container_dir);
	}
	return orig_sb_init_file_url_dir(self, selector, path, is_directory);
}

static void
sb_container_path_fallback(void)
{
	Class cls = objc_getClass("NSURL");
	Method one, two;

	sb_make_container_dirs();
	if (cls == NULL) {
		say("hakios: SpringBoard: NSURL is not there; no container path can be stood in for\\n");
		return;
	}
	if ((one = class_getInstanceMethod(cls, sel_registerName("initFileURLWithPath:"))) != NULL) {
		orig_sb_init_file_url = (id (*)(id, SEL, id))method_setImplementation(one, (IMP)sb_init_file_url);
	}
	if ((two = class_getInstanceMethod(cls, sel_registerName("initFileURLWithPath:isDirectory:"))) != NULL) {
		orig_sb_init_file_url_dir = (id (*)(id, SEL, id, BOOL))method_setImplementation(two,
		    (IMP)sb_init_file_url_dir);
	}
	say("hakios: SpringBoard: a container path with no answer becomes %s\\n", sb_container_dir);
}

/* ---- where SpringBoard's main thread is, once every twenty seconds -------------------------- */

/*
 * Nothing in SpringBoard says a word between the end of this constructor and the first frame
 * CoreAnimation commits -- and on the phone that gap has lasted the whole run, with the window
 * server holding an Apple logo nothing replaces. So the main thread is stopped for a moment
 * every twenty seconds and its pc, lr and frame pointers are written down, symbolised the way
 * the stall watch does it (inject_sb.c's touch_lat=1): an address inside a shared cache image
 * comes out as "Image+offset". A stack whose top is mach_msg2_trap is a wait for a service; one
 * that keeps ending in the same two or three frames is a loop or a lock.
 */
static uintptr_t sb_stack_top, sb_stack_bottom;

/* The last synchronous send this process entered and did not come back from. The pass-through
 * counter used to stop at 32, which is exactly where the shell stalls on this phone, so the one
 * name that mattered was always the one that never got written. Keeping the last entry outside
 * the counter means the hang watch can name the service even past the log limit. */
static char sb_last_waited[128];
static unsigned sb_last_waited_id;
/* Only the main thread's send is the stall -- other threads enter and leave XPC constantly and
 * were overwriting the name the hang watch tried to read. The watch records its own thread port
 * here; the hook names a send only when it is the main thread's. */
static thread_t sb_main_thread;

static void
sb_hang_say(thread_t thread, uint64_t seconds)
{
	arm_thread_state64_t state;
	mach_msg_type_number_t count = ARM_THREAD_STATE64_COUNT;
	uintptr_t pcs[16];
	unsigned depth = 0;
	char line[1280];
	int at;

	if (thread_suspend(thread) != KERN_SUCCESS) {
		say("hakios: hang %llus: the main thread would not stop\\n", seconds);
		return;
	}
	if (thread_get_state(thread, ARM_THREAD_STATE64, (thread_state_t)&state, &count) == KERN_SUCCESS) {
		uintptr_t pc = (uintptr_t)arm_thread_state64_get_pc(state);
		uintptr_t lr = (uintptr_t)arm_thread_state64_get_lr(state);
		uintptr_t fp = (uintptr_t)arm_thread_state64_get_fp(state);

		pcs[depth++] = pc;
		if (lr != 0) {
			pcs[depth++] = lr;
		}
		while (depth < sizeof(pcs) / sizeof(pcs[0]) && (fp & 15) == 0) {
			/*
			 * The range check against the watcher's own stack rejected every other thread's
			 * frames outright -- thread 2's chain came back as a bare 0 because its stack
			 * lives elsewhere. Reading the pair through vm_read_overwrite cannot fault on a
			 * wrong fp, and dladdr on the recovered lr rejects garbage without one.
			 */
			vm_size_t got = 0;
			uintptr_t pair[2];

			if (vm_read_overwrite(mach_task_self(), (vm_address_t)fp, sizeof(pair),
			    (vm_address_t)&pair, &got) != KERN_SUCCESS) {
				break;
			}
			uintptr_t next = pair[0], frame_lr = pair[1];
			Dl_info validity;

			if (frame_lr == 0 || dladdr((void *)frame_lr, &validity) == 0 || validity.dli_fname == NULL) {
				break;
			}
			pcs[depth++] = frame_lr;
			if (next <= fp) {
				break;
			}
			fp = next;
		}
	}
	thread_resume(thread);
	at = snprintf(line, sizeof(line), "hakios: hang %llus:", seconds);
	for (unsigned i = 0; i < depth && at < (int)sizeof(line) - 96; i++) {
		Dl_info info;

		if (dladdr((void *)pcs[i], &info) != 0 && info.dli_fname != NULL) {
			const char *image = strrchr(info.dli_fname, '/');

			at += info.dli_sname != NULL && pcs[i] - (uintptr_t)info.dli_saddr < 0x4000 ?
			    snprintf(line + at, sizeof(line) - at, " %s", info.dli_sname) :
			    snprintf(line + at, sizeof(line) - at, " %s+%#lx",
			    image != NULL ? image + 1 : info.dli_fname, pcs[i] - (uintptr_t)info.dli_fbase);
		} else {
			at += snprintf(line + at, sizeof(line) - at, " %#lx", pcs[i]);
		}
	}
	say("%s\\n", line);
	if (sb_last_waited_id != 0) {
		say("hakios: hang %llus: last send #%u is still out to %s\\n",
		    seconds, sb_last_waited_id, sb_last_waited);
	}
}

/*
 * One thread's pc and lr, and its frame pointers while they stay inside the range this thread
 * was given -- which is only known for the main thread. Two addresses are enough to tell a wait
 * for a service (mach_msg2_trap) from a lock (__ulock_wait) from work in progress.
 */
static void
sb_one_thread(thread_t thread, unsigned index, uint64_t seconds)
{
	arm_thread_state64_t state;
	mach_msg_type_number_t count = ARM_THREAD_STATE64_COUNT;
	uintptr_t pcs[8];
	unsigned depth = 0;
	char line[1024];
	int at;

	if (thread_suspend(thread) != KERN_SUCCESS) {
		return;
	}
	if (thread_get_state(thread, ARM_THREAD_STATE64, (thread_state_t)&state, &count) == KERN_SUCCESS) {
		uintptr_t fp = (uintptr_t)arm_thread_state64_get_fp(state);
		uintptr_t lr = (uintptr_t)arm_thread_state64_get_lr(state);

		pcs[depth++] = (uintptr_t)arm_thread_state64_get_pc(state);
		if (lr != 0) {
			pcs[depth++] = lr;
		}
		while (depth < sizeof(pcs) / sizeof(pcs[0]) && (fp & 15) == 0) {
			/*
			 * The range check against the watcher's own stack rejected every other thread's
			 * frames outright -- thread 2's chain came back as a bare 0 because its stack
			 * lives elsewhere. Reading the pair through vm_read_overwrite cannot fault on a
			 * wrong fp, and dladdr on the recovered lr rejects garbage without one.
			 */
			vm_size_t got = 0;
			uintptr_t pair[2];

			if (vm_read_overwrite(mach_task_self(), (vm_address_t)fp, sizeof(pair),
			    (vm_address_t)&pair, &got) != KERN_SUCCESS) {
				break;
			}
			uintptr_t next = pair[0], frame_lr = pair[1];
			Dl_info validity;

			if (frame_lr == 0 || dladdr((void *)frame_lr, &validity) == 0 || validity.dli_fname == NULL) {
				break;
			}
			pcs[depth++] = frame_lr;
			if (next <= fp) {
				break;
			}
			fp = next;
		}
	}
	thread_resume(thread);
	at = snprintf(line, sizeof(line), "hakios: thread %u at %llus:", index, seconds);
	for (unsigned i = 0; i < depth && at < (int)sizeof(line) - 96; i++) {
		Dl_info info;

		if (dladdr((void *)pcs[i], &info) != 0 && info.dli_fname != NULL) {
			const char *image = strrchr(info.dli_fname, '/');

			at += info.dli_sname != NULL && pcs[i] - (uintptr_t)info.dli_saddr < 0x4000 ?
			    snprintf(line + at, sizeof(line) - at, " %s", info.dli_sname) :
			    snprintf(line + at, sizeof(line) - at, " %s+%#lx",
			    image != NULL ? image + 1 : info.dli_fname, pcs[i] - (uintptr_t)info.dli_fbase);
		} else {
			at += snprintf(line + at, sizeof(line) - at, " %#lx", pcs[i]);
		}
	}
	say("%s\\n", line);
}

static void
sb_every_thread(uint64_t seconds)
{
	thread_act_array_t threads = NULL;
	mach_msg_type_number_t count = 0;
	thread_t self = mach_thread_self();
	unsigned i;

	if (task_threads(mach_task_self(), &threads, &count) != KERN_SUCCESS) {
		return;
	}
	say("hakios: %u threads at %llus\\n", (unsigned)count, seconds);
	for (i = 0; i < count && i < 20; i++) {
		if (threads[i] != self) {
			sb_one_thread(threads[i], i, seconds);
		}
	}
	for (i = 0; i < count; i++) {
		mach_port_deallocate(mach_task_self(), threads[i]);
	}
	vm_deallocate(mach_task_self(), (vm_address_t)threads, count * sizeof(thread_t));
}

static void *
sb_hang_watch(void *argument)
{
	thread_t main_thread = (thread_t)(uintptr_t)argument;
	unsigned turn;

	sb_main_thread = main_thread;
	for (turn = 1; turn <= 40; turn++) {
		sleep(60);
		sb_hang_say(main_thread, (uint64_t)turn * 60);
		/*
		 * Every third turn, the other threads as well. When the main thread waits on an
		 * __ulock_wait inside BSCollectionFilter -- a lock that cannot come from another
		 * process -- the holder is one of ours, and which one it is only the stacks of the
		 * others can say.
		 */
		if (turn % 3 == 0) {
			sb_every_thread((uint64_t)turn * 20);
		}
	}
	return NULL;
}

static void
sb_watch_the_main_thread(void)
{
	pthread_t watcher;

	sb_stack_top = (uintptr_t)pthread_get_stackaddr_np(pthread_self());
	sb_stack_bottom = sb_stack_top - pthread_get_stacksize_np(pthread_self());
	if (pthread_create(&watcher, NULL, sb_hang_watch, (void *)(uintptr_t)mach_thread_self()) == 0) {
		pthread_detach(watcher);
		say("hakios: SpringBoard: the main thread is looked at every 20 seconds\\n");
		return;
	}
	say("hakios: SpringBoard: no thread to look at the main one with\\n");
}

/*
 * What the whole-thread stacks named: a thread of ours sits in a synchronous XPC round trip
 * and holds the BSCollectionFilter lock across it, so the main thread waits on the lock while
 * the thread waits on a reply that never comes. Whose reply only the connection's name can
 * say -- the first few sends are named in the log -- and the container manager is the one
 * known to die on its formatter before answering. So a synchronous send to it is answered here
 * and now with an empty dictionary, and every other connection goes through untouched.
 */
#define DYLD_INTERPOSE(_replacement, _replacee) \\
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \\
    _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = \\
        { (const void *)&_replacement, (const void *)&_replacee };

/* No xpc/xpc.h in this SDK; the ABI is pointers all the way down. */
extern const char *xpc_connection_get_name(void *);
extern void *xpc_dictionary_create(const char *const keys[], void *const values[], size_t count);

/*
 * The original is taken from the shared cache by its unslid address, checked against the first
 * sixteen bytes of the code that was there when the address was read -- dlsym is no help with
 * an interpose in force: it hands back the replacement itself.
 */
extern const void *_dyld_get_shared_cache_range(size_t *length);

#define UNSLID_XPC_REPLY_SYNC   0x1d0d27000UL    /* /usr/lib/system/libxpc.dylib */

static const unsigned char SIG_XPC_REPLY_SYNC[16] = {
    0xff, 0x43, 0x01, 0xd1, 0xf8, 0x5f, 0x01, 0xa9, 0xf6, 0x57, 0x02, 0xa9, 0xf4, 0x4f, 0x03, 0xa9
};

static void *
sb_cache_function(uintptr_t unslid)
{
    const uintptr_t base = 0x180000000UL;
    size_t length = 0;
    const void *cache = _dyld_get_shared_cache_range(&length);
    uintptr_t address;

    if (cache == NULL || length == 0 || unslid < base || unslid >= base + length) {
        return NULL;
    }
    address = unslid + ((uintptr_t)cache - base);
    if (memcmp((const void *)address, SIG_XPC_REPLY_SYNC, 16) != 0) {
        return NULL;
    }
    return (void *)address;
}

static void *
sb_original_behind(const void *replacement)
{
    void *symbol = sb_cache_function(UNSLID_XPC_REPLY_SYNC);

    if (symbol == NULL) {
        symbol = dlsym(RTLD_NEXT, "xpc_connection_send_message_with_reply_sync");
        if (symbol == NULL || symbol == replacement) {
            symbol = dlsym(RTLD_DEFAULT, "xpc_connection_send_message_with_reply_sync");
        }
        if (symbol == replacement) {
            symbol = NULL;
        }
    }
    if (symbol == NULL) {
        say("hakios: SpringBoard: the synchronous XPC send has nothing behind the interpose\\n");
    }
    return symbol;
}

static void * (*orig_sb_xpc_reply_sync)(void *, void *);
static unsigned sb_xpc_logged;
static unsigned sb_xpc_passed;

static void *
sb_xpc_reply_sync(void *connection, void *message)
{
    const char *name = connection != NULL ? xpc_connection_get_name(connection) : NULL;

    /* orig_sb_xpc_reply_sync is kept only to say whether the shared-cache lookup worked. Even when
     * it did not, the answer below is still an empty dictionary: returning NULL here would hand
     * the caller a protocol error at the exact moment it cannot handle one. */
    if (orig_sb_xpc_reply_sync == NULL && !sb_xpc_logged) {
        say("hakios: SpringBoard: the original send is unknown, answering empty anyway\\n");
    }
    /* Two names, no more, and both because the service is dead or cannot answer this thread.
     *
     * The all-sends-answered round proved the cost of over-answering: backboardd lost its
     * RunningBoard handshake one-for-one with every empty reply and stopped updating the boot
     * logo. The same rule applies here. cfprefsd is fenced because SpringBoard's own main
     * thread holds the BSCollectionFilter lock across the send (680 s of __ulock_wait proved
     * it); the container manager is fenced because it dies in its ICU formatter before
     * answering. A service that is alive and answers on its own -- MobileGestalt, RunningBoard,
     * LaunchServices -- is left alone; an empty dictionary to one of those is a wrong answer,
     * not a shortcut. */
    /* iconservices is the 21:20 stall: SpringBoard's #66/#67 sit 480 s on it and never come
     * back. Its agent starts (pid 14, fallback on) but as user/501 with no persona it cannot
     * register com.apple.iconservices, so no one is listening. Unlike lsd -- where an empty
     * dictionary became an NSException and an abort storm -- icon lookups have a real
     * "no icons yet" answer on a first boot, so an empty reply is the shape that path expects.
     * cfprefsd/containermanagerd/lsd.mapdb stay as they were. */
    if (name != NULL && (strstr(name, "cfprefsd") != NULL || strstr(name, "containermanagerd") != NULL
                         || strstr(name, "lsd.mapdb") != NULL || strstr(name, "com.apple.iconservices") != NULL)) {
        if (sb_xpc_logged < 32) {
            sb_xpc_logged++;
            say("hakios: SpringBoard: %s answered empty, not waited for\\n", name);
        }
        return xpc_dictionary_create(NULL, NULL, 0);
    }
    if (orig_sb_xpc_reply_sync == NULL) {
        return NULL;
    }
    /* The names that go through are written down too, and for one reason: on the phone the
     * main thread was in __ulock_wait on BSCollectionFilter while thread 2 sat in this very
     * call, and the log only carried the names that were answered -- so the one name that
     * mattered, the one being waited on, was the only thing missing.
     *
     * Each pass-through is numbered on the way in and numbered again on the way out. The last
     * number that is written once and never twice is the send that never came back: its name
     * is the service the lock-holding thread is stuck on, and that is the whole answer the next
     * round needs. */
    /* Every numbered send is written, not just the first 32: the shell on this phone stalls
     * after #32, so the old cut-off hid exactly the name the watch needed. The last one entered
     * is kept in sb_last_waited as well, and the hang watch prints it even after logging stops,
     * because a send that never returns never gets to clear it. */
    {
        unsigned id = ++sb_xpc_passed;
        thread_t self = mach_thread_self();
        int is_main = (sb_main_thread == 0 || self == sb_main_thread);

        if (is_main) {
            snprintf(sb_last_waited, sizeof(sb_last_waited), "%s",
                name != NULL ? name : "(anonymous)");
            sb_last_waited_id = id;
        }
        if (id <= 200) {
            say("hakios: SpringBoard: #%u %s waited for\\n", id,
                name != NULL ? name : "(anonymous)");
        }
        void *answer = orig_sb_xpc_reply_sync(connection, message);

        if (id <= 200) {
            say("hakios: SpringBoard: #%u came back\\n", id);
        }
        if (is_main) {
            sb_last_waited_id = 0;          /* only the main thread's return clears its own name */
        }
        mach_port_deallocate(mach_task_self(), self);
        return answer;
    }
}

extern void *xpc_connection_send_message_with_reply_sync(void *, void *);

DYLD_INTERPOSE(sb_xpc_reply_sync, xpc_connection_send_message_with_reply_sync)

__attribute__((constructor))
static void
sb_xpc_interpose(void)
{
    orig_sb_xpc_reply_sync = (void * (*)(void *, void *))
        sb_original_behind((const void *)&sb_xpc_reply_sync);
    if (orig_sb_xpc_reply_sync == NULL) {
        return;
    }
    /* Constructors run on the main thread before any XPC send, so pin it here rather than
     * waiting for the watcher: the first send (#1 runningboard) must be attributable too. */
    sb_main_thread = mach_thread_self();
	say("hakios: SpringBoard: synchronous XPC round trips are watched\\n");
}

/* ---- a nil member is skipped, not raised over ---------------------------------------------- */

/*
 * The abort that kept the shell off the screen in the 21:35 run, five seconds after pid 98 asked
 * the profile daemon for its profiles:
 *
 *   SpringBoard[98] *** Terminating app due to uncaught exception 'NSInvalidArgumentException',
 *   reason: '*** -[__NSSetM removeObject:]: object cannot be nil'
 *
 * The profile daemon is not on this phone (launchd: "failed lookup:
 * com.apple.managedconfiguration.profiled.public, error 3: No such process"), so the set that
 * came back was empty and something between the answer and the screen put nil where a member
 * belonged. Which caller is not worth guessing at: every collection mutator that raises over nil
 * sits on that path, so all of them skip nil instead. A missing entry costs a row on a screen;
 * an exception costs the shell, every five seconds, forever.
 */
#include <objc/runtime.h>

static IMP orig_nil_set_remove;
static IMP orig_nil_set_add;
static IMP orig_nil_array_add;
static IMP orig_nil_array_insert;
static IMP orig_nil_dict_set;
static IMP orig_nil_ordered_add;
static unsigned nil_skipped;

static void
sb_nil_note(const char *what)
{
	if (nil_skipped < 8) {
		nil_skipped++;
		say("hakios: SpringBoard: a nil %s was skipped, not raised over\\n", what);
	}
}

static void
sb_nil_set_remove(id self, SEL selector, id member)
{
	if (member == NULL) {
		sb_nil_note("set member (remove)");
		return;
	}
	((void (*)(id, SEL, id))orig_nil_set_remove)(self, selector, member);
}

static void
sb_nil_set_add(id self, SEL selector, id member)
{
	if (member == NULL) {
		sb_nil_note("set member (add)");
		return;
	}
	((void (*)(id, SEL, id))orig_nil_set_add)(self, selector, member);
}

static void
sb_nil_array_add(id self, SEL selector, id member)
{
	if (member == NULL) {
		sb_nil_note("array member (add)");
		return;
	}
	((void (*)(id, SEL, id))orig_nil_array_add)(self, selector, member);
}

static void
sb_nil_array_insert(id self, SEL selector, id member, unsigned long index)
{
	if (member == NULL) {
		sb_nil_note("array member (insert)");
		return;
	}
	((void (*)(id, SEL, id, unsigned long))orig_nil_array_insert)(self, selector, member, index);
}

static void
sb_nil_dict_set(id self, SEL selector, id value, id key)
{
	if (value == NULL || key == NULL) {
		sb_nil_note("dictionary entry");
		return;
	}
	((void (*)(id, SEL, id, id))orig_nil_dict_set)(self, selector, value, key);
}

static void
sb_nil_ordered_add(id self, SEL selector, id member)
{
	if (member == NULL) {
		sb_nil_note("ordered set member");
		return;
	}
	((void (*)(id, SEL, id))orig_nil_ordered_add)(self, selector, member);
}

static IMP
sb_nil_hook(const char *class_name, const char *selector, void *replacement)
{
	Class cls = objc_getClass(class_name);
	Method method;

	if (cls == NULL) {
		return NULL;
	}
	method = class_getInstanceMethod(cls, sel_registerName(selector));
	if (method == NULL) {
		return NULL;
	}
	return method_setImplementation(method, (IMP)replacement);
}

__attribute__((constructor))
static void
sb_nil_guard(void)
{
	orig_nil_set_remove = sb_nil_hook("__NSSetM", "removeObject:", (void *)sb_nil_set_remove);
	orig_nil_set_add = sb_nil_hook("__NSSetM", "addObject:", (void *)sb_nil_set_add);
	orig_nil_array_add = sb_nil_hook("__NSArrayM", "addObject:", (void *)sb_nil_array_add);
	orig_nil_array_insert = sb_nil_hook("__NSArrayM", "insertObject:atIndex:",
	    (void *)sb_nil_array_insert);
	orig_nil_dict_set = sb_nil_hook("__NSDictionaryM", "setObject:forKey:", (void *)sb_nil_dict_set);
	orig_nil_ordered_add = sb_nil_hook("__NSOrderedSetM", "addObject:", (void *)sb_nil_ordered_add);
	say("hakios: SpringBoard: nil survives the mutators (set %d/%d array %d/%d dict %d ordered %d)\\n",
	    orig_nil_set_remove != NULL, orig_nil_set_add != NULL,
	    orig_nil_array_add != NULL, orig_nil_array_insert != NULL,
	    orig_nil_dict_set != NULL, orig_nil_ordered_add != NULL);
}

/* ---- say why the shell aborts, instead of only that it did --------------------------------- */
/*
 * The 01:19 run killed SpringBoard every 26 s from pid 10, with a bare SIGABRT and no
 * Terminating exception to explain it -- the nil-guard is here, not in libredwood.dylib, so
 * the abort handler the daemons get never reached this process. Same frame walk, same exit code;
 * the next boot names the frame that actually raised it.
 */
static void
sb_abort_say(int sig, siginfo_t *info, void *ctx)
{
	ucontext_t *uc = ctx;
	uintptr_t pcs[16];
	unsigned depth = 0;
	char line[1100];
	int at;
	uintptr_t fp;

	pcs[depth++] = uc->uc_mcontext->__ss.__pc;
	if (uc->uc_mcontext->__ss.__lr != 0) {
		pcs[depth++] = uc->uc_mcontext->__ss.__lr;
	}
	fp = uc->uc_mcontext->__ss.__fp;
	while (depth < sizeof(pcs) / sizeof(pcs[0]) && fp != 0 && (fp & 15) == 0) {
		uintptr_t next = ((uintptr_t *)fp)[0], frame_lr = ((uintptr_t *)fp)[1];

		if (frame_lr == 0) {
			break;
		}
		pcs[depth++] = frame_lr;
		if (next <= fp) {
			break;
		}
		fp = next;
	}
	at = snprintf(line, sizeof(line), "hakios: SpringBoard aborted: bad %p:",
	    info ? info->si_addr : NULL);
	for (unsigned i = 0; i < depth && at < (int)sizeof(line) - 96; i++) {
		Dl_info di;

		if (dladdr((void *)pcs[i], &di) != 0 && di.dli_fname != NULL) {
			const char *image = strrchr(di.dli_fname, '/');

			at += di.dli_sname != NULL && pcs[i] - (uintptr_t)di.dli_saddr < 0x4000 ?
			    snprintf(line + at, sizeof(line) - at, " %s", di.dli_sname) :
			    snprintf(line + at, sizeof(line) - at, " %s+%#lx",
			    image != NULL ? image + 1 : di.dli_fname, pcs[i] - (uintptr_t)di.dli_fbase);
		} else {
			at += snprintf(line + at, sizeof(line) - at, " %#lx", pcs[i]);
		}
	}
	say("%s\\n", line);
	_exit(128 + SIGABRT);
}

__attribute__((constructor))
static void
sb_abort_watch(void)
{
	struct sigaction sa;

	memset(&sa, 0, sizeof(sa));
	sa.sa_sigaction = sb_abort_say;
	sa.sa_flags = SA_SIGINFO;
	if (sigaction(SIGABRT, &sa, NULL) == 0) {
		say("hakios: SpringBoard: an abort is described before it is taken\\n");
	}
}

/* ---- a tokenizer that cannot build its language data is not worth the shell --------------- */
/*
 * The 02:14 run died seven times, every time on the same stack:
 *
 *   abort -> liblangid.dylib+0x1d70 (_langid_env_create ->
 *   _langid_create_with_datapath_internal) -> NLTokenizerCreate ->
 *   NLStringTokenizerCreate -> CFStringTokenizerCreate
 *
 * liblangid aborts when it cannot open the language model its data path points at -- this
 * rootfs is the slim one and has none. Neither the frame gate nor the desktop needs word
 * tokenization, and a caller handed NULL from a tokenizer create takes its own no-tokenizer
 * path (that is the documented failure of these C creators). Answer NULL here instead of
 * letting liblangid abort the process on the way to a keyboard that will never be used.
 */
/* No parameter is read: CFRange is passed by value (two registers) and a wrong declaration
 * would shift every argument. An empty parameter list ignores what the caller passed. */
static void *my_sb_tokenizer_create(void)
{
	say("hakios: SpringBoard: a tokenizer has no language data; none is returned\\n");
	return NULL;
}

/* CFStringTokenizerCreate is declared by <CoreFoundation/CoreFoundation.h>, already included
 * here -- re-declaring it with a different prototype is a hard error. Its address is enough
 * for the interpose section; the real signature is never used on our side. */
DYLD_INTERPOSE(my_sb_tokenizer_create, CFStringTokenizerCreate)
'''


SPRINGBOARD_NOTIFY_SYNC = '''	notify_register_dispatch("com.redwood.unlock", &token, dispatch_get_main_queue(), ^(__unused int t) {
		unlock();
	});
	notify_register_dispatch("com.redwood.layers", &token, dispatch_get_main_queue(), ^(__unused int t) {
		layers();
	});
	notify_register_dispatch("com.redwood.switcher", &token, dispatch_get_main_queue(), ^(__unused int t) {
		switcher();
	});'''

SPRINGBOARD_NOTIFY_ASYNC = '''	/*
	 * The same three, off the constructor's thread.
	 *
	 * notify_register_dispatch is not a local call: it asks notifyd to hold the name and waits
	 * for the answer. notifyd does not answer on this phone -- the hang watch of the 17:49 run
	 * caught the constructor stopped there, the stack reading
	 *
	 *   mach_msg2_trap mach_msg2_internal mach_msg_overwrite mach_msg
	 *   notify_resume notify_resume notify_post redwood_springboard_init
	 *
	 * -- and a constructor that waits never returns, so UIApplicationMain is never called, the
	 * main run loop never turns, and the window server keeps its Apple logo up for the whole
	 * run. Registered from a queue of our own the wait costs nothing that is on the screen; the
	 * remote control those three names give up is worth the picture.
	 */
	dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
		int unlock_token = 0, layers_token = 0, switcher_token = 0;

		notify_register_dispatch("com.redwood.unlock", &unlock_token, dispatch_get_main_queue(), ^(__unused int t) {
			unlock();
		});
		notify_register_dispatch("com.redwood.layers", &layers_token, dispatch_get_main_queue(), ^(__unused int t) {
			layers();
		});
		notify_register_dispatch("com.redwood.switcher", &switcher_token, dispatch_get_main_queue(), ^(__unused int t) {
			switcher();
		});
	});'''


GL_BOOTSTRAP = '''# chiron: the window server needs the software OpenGL ES renderer and this runner has neither the
# switch nor the tools. CoreAnimation says "Failed to initialize Metal or OpenGLES contexts!" and
# draws nothing on this phone -- there is no GPU -- so GL=1 (tools/mk-mesa.sh: Mesa's softpipe
# behind src/eagl.c) is what makes a frame possible at all. mesa's build wants meson and ninja,
# which the workflow's apt list does not install.
if [ -z "${GL-}" ]; then
	GL=1
fi
if ! command -v meson >/dev/null 2>&1 || ! command -v ninja >/dev/null 2>&1; then
	python3 -m pip install --quiet --break-system-packages meson ninja 2>/dev/null ||
		python3 -m pip install --quiet meson ninja 2>/dev/null || true
	export PATH="$HOME/.local/bin:$PATH"
fi
'''


def userland_overlay():
    """Put the container-path fallback into the injection library, and switch the OpenGL ES
    renderer on, both inside the tree the image is built from.

    The kernel overlay runs from the project tree (build-chiron-kernel.sh cd's into it) and before
    userland/build.sh, so userland/src/inject.c and userland/build.sh here are the ones used.
    """
    path = Path("userland/src/inject.c")
    if not path.is_file():
        raise ValueError("userland/src/inject.c: not under %s" % Path.cwd())
    text = path.read_text()
    text = replace_once(text, "#include <string.h>\n#include <sys/sysctl.h>",
                        "#include <string.h>\n#include <sys/stat.h>\n#include <unistd.h>\n#include <sys/sysctl.h>")
    text = replace_once(text, "static void\ncontainer_manager_fixes(void)\n{",
                        USERLAND_INJECT_C.strip("\n")
                        + "\n\nstatic void\ncontainer_manager_fixes(void)\n{")
    text = replace_once(text, "\t\topengl_es();\n\t\twindow_server_fixes();",
                        "\t\topengl_es();\n\t\tcontainer_path_fallback();\n"
                        "\t\twindow_server_fixes();")
    # The gate finish_update consults before it hands the frame on: read at run time, answered
    # with a yes when the display says no. See USERLAND_GATE_C for the two runs that named it.
    text = replace_once(text,
        "#define DISPLAY_B233(display)   (*(uint8_t *)((char *)(display) + 0x233))",
        "#define DISPLAY_B233(display)   (*(uint8_t *)((char *)(display) + 0x233))\n"
        + USERLAND_GATE_C.strip("\n"))
    text = replace_once(text,
        '\torig_is_ready = replace_virtual("is_ready", VSLOT(VT_APPLE_CLCD_DISPLAY, 0xb0), 0x1882f0908UL, my_is_ready);',
        '\torig_is_ready = replace_virtual("is_ready", VSLOT(VT_APPLE_CLCD_DISPLAY, 0xb0), 0x1882f0908UL, my_is_ready);\n'
        '\torig_frame_gate = replace_slot("frame gate", VSLOT(VT_APPLE_CLCD_DISPLAY, 0xa8), my_frame_gate);')
    # One boot has to answer every question the display path can ask, because each boot costs
    # twenty minutes of shared-cache decompression. finish_update is the last place in the
    # server that decides whether a composed frame is handed on, so it prints the whole state
    # it works from -- before and after -- instead of only the value it returns.
    text = replace_once(text,
        "static uintptr_t\nmy_finish_update(void *display, void *update, unsigned flags)\n"
        "{\n"
        "\tuintptr_t r = orig_finish_update(display, update, flags);\n"
        "\n"
        "\tif (calls_finish++ < 6) {\n"
        '\t\tsay("redwood: finish_update(%#x) -> %#lx\\n", flags, r);\n'
        "\t}\n"
        "\treturn r;\n"
        "}",
        "static uintptr_t\nmy_finish_update(void *display, void *update, unsigned flags)\n"
        "{\n"
        "\tunsigned n = calls_finish++;\n"
        "\tuintptr_t r;\n"
        "\n"
        "\tif (n < 8) {\n"
        '\t\tsay("redwood: finish_update(%#x) #%u in : flags %#llx surf %p ctx %p px %p b233 %#x\\n",\n'
        "\t\t    flags, n, (unsigned long long)DISPLAY_FLAGS(display),\n"
        "\t\t    (void *)(*(uintptr_t *)((char *)(display) + 0x230)),\n"
        "\t\t    (void *)(*(uintptr_t *)((char *)(display) + 0x4a8)),\n"
        "\t\t    (void *)(*(uintptr_t *)((char *)(display) + 0x830)),\n"
        "\t\t    DISPLAY_B233(display));\n"
        "\t}\n"
        "\t/* The gate is installed and the log says so, yet it is never entered. Print the\n"
        "\t * table the display actually carries, so the next line says whether the entry we\n"
        "\t * wrote is the entry that will be read. */\n"
        "\tif (n < 3) {\n"
        "\t\tvoid **vt = *(void ***)display;\n"
        '\t\tsay("redwood: finish_update #%u: display %p vt %p slot_b8 %p ours %p slot_c0 %p\\n",\n'
        "\t\t    n, display, (void *)vt, vt[0xb8 / 8], (void *)my_frame_gate, vt[0xc0 / 8]);\n"
        "\t}\n"
        "\t/* And ask the gate once, by hand: whatever the answer is, it is the value the real\n"
        "\t * finish_update would have branched on. */\n"
        "\tif (n == 0 && orig_frame_gate != NULL) {\n"
        '\t\tsay("redwood: the gate asked by hand answers %#lx\\n",\n'
        "\t\t    (unsigned long)orig_frame_gate(display));\n"
        "\t}\n"
        "\tr = orig_finish_update(display, update, flags);\n"
        "\tif (n < 8) {\n"
        '\t\tsay("redwood: finish_update(%#x) #%u out: -> %#lx surf %p\\n",\n'
        "\t\t    flags, n, (unsigned long)r,\n"
        "\t\t    (void *)(*(uintptr_t *)((char *)(display) + 0x230)));\n"
        "\t}\n"
        "\treturn r;\n"
        "}")
    path.write_text(text)

    # SpringBoard aborts in liblangid when stat("/usr/share/langid/langid.inv") fails
    # (LanguageIdentifier.c:621, _open_and_mmap_for_reading).  The data file ships in this
    # image, but while the iOS volume is the root the image is bound on /redwood, so the
    # absolute path resolves to the volume's empty directory instead.  Cover that directory
    # with the image's copy using the very same bind_dir the launchd jobs already use.
    # This runs before switch_root, hence the /flash prefix; if the volume has no such
    # directory, make it a synthetic one first (as is done for /redwood and /fw).
    init = Path("userland/src/init.c")
    if not init.is_file():
        raise ValueError("userland/src/init.c: not under %s" % Path.cwd())
    init_text = init.read_text()
    init_text = replace_once(init_text,
        '\tcheck(bind_dir(in_image("/hal"), "/flash/System/Library/Audio/Plug-Ins/HAL"),\n'
        '\t    "our audio device, not Apple\'s plug-ins");',
        '\tcheck(bind_dir(in_image("/hal"), "/flash/System/Library/Audio/Plug-Ins/HAL"),\n'
        '\t    "our audio device, not Apple\'s plug-ins");\n'
        '\t/* liblangid aborts SpringBoard if it cannot stat its language data. */\n'
        '\t{\n'
        '\t\tstruct stat langid_st;\n'
        '\t\tif (stat("/flash/usr/share/langid", &langid_st) != 0) {\n'
        '\t\t\tsynthetic_dir("/flash/usr/share/langid");\n'
        '\t\t}\n'
        '\t\tcheck(bind_dir(in_image("/usr/share/langid"), "/flash/usr/share/langid"),\n'
        '\t\t    "our language data where liblangid reads it");\n'
        '\t}')
    init.write_text(init_text)

    # SpringBoard: the same container fallback it never had (its library is inject_sb.c, not
    # inject.c), and a stack of its main thread every twenty seconds, since nothing else in the
    # process says where it stopped.
    sb = Path("userland/src/inject_sb.c")
    if not sb.is_file():
        raise ValueError("userland/src/inject_sb.c: not under %s" % Path.cwd())
    sb_text = sb.read_text()
    sb_text = replace_once(sb_text, "#include <dlfcn.h>\n#include <fcntl.h>",
                           "#include <dlfcn.h>\n#include <fcntl.h>\n"
                           "#include <mach/mach.h>\n#include <pthread.h>")
    sb_text = replace_once(sb_text, "/* ---- methods of SpringBoard's classes, replaced",
                           USERLAND_INJECT_SB_C.strip("\n")
                           + "\n\n/* ---- methods of SpringBoard's classes, replaced")
    sb_text = replace_once(sb_text, "\tload_tweaks();\n",
                           "\tload_tweaks();\n"
                           "\tsb_container_path_fallback();\n"
                           "\tsb_watch_the_main_thread();\n")
    sb_text = replace_once(sb_text, SPRINGBOARD_NOTIFY_SYNC, SPRINGBOARD_NOTIFY_ASYNC)
    sb.write_text(sb_text)

    build = Path("userland/build.sh")

    # The interpose on udat_applyPattern takes the address of the original, which lives in
    # libicucore and is not linked here; keep that one reference unresolved at link time and
    # let dyld bind it at load.
    script2 = build.read_text()
    script2 = replace_once(script2,
        "$DYN -dynamiclib -install_name /redwood/lib/libredwood.dylib",
        "$DYN -dynamiclib -Wl,-U,_udat_applyPattern -Wl,-U,_CFDateFormatterSetFormat -Wl,-U,_xpc_connection_send_message_with_reply_sync -install_name /redwood/lib/libredwood.dylib")
    script2 = replace_once(script2,
        "$DYN -dynamiclib -install_name /redwood/lib/libredwood-sb.dylib",
        "$DYN -dynamiclib -Wl,-U,_xpc_connection_send_message_with_reply_sync -Wl,-U,_CFStringTokenizerCreate -install_name /redwood/lib/libredwood-sb.dylib")
    build.write_text(script2)
    built = None
    if build.is_file():
        script = build.read_text()
        if "chiron: the window server needs the software OpenGL ES" not in script:
            script = replace_once(script, "#!/bin/bash\n", "#!/bin/bash\n" + GL_BOOTSTRAP)
            build.write_text(script)
        built = "GL=%s" % ("1" if 'GL=1' in script else "?")
    return {"file": str(path), "container_path_fallback": text.count("container_path_fallback"),
            "springboard": str(sb), "springboard_container_path_fallback": sb_text.count("sb_container_path_fallback"),
            "springboard_hang_watch": sb_text.count("sb_watch_the_main_thread"),
            "init_langid_bind": init_text.count("our language data where liblangid reads it"),
            "build": built}


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
    # --- the display: who opens it, and whether anything ever reaches the panel -------------
    # presenterMain copies every swap into the boot framebuffer, and the first eight swaps log
    # themselves ("swap %u (#%u): surfaces ..."). No run so far has shown even one: the log
    # ends at start()'s "display 1125x2436" line and never says swap. Before guessing at the
    # renderer, ask the simpler question -- does anything open disp0 at all? A client that
    # never opens cannot swap, and the answer splits the problem in two.
    # The file is one of the tree's own new files: apply.sh copies xnu/files/. into the source
    # root, so files/iokit/Kernel/... arrives as iokit/Kernel/... -- not under a files/ prefix.
    fb = tree / "iokit/Kernel/MiniIOMobileFramebuffer.cpp"
    text = replace_once(fb.read_text(),
        "\tfLock = IOLockAlloc();\n"
        "\tfVSyncCall = thread_call_allocate(&IOMobileFramebufferUserClient::vsyncFired, this);\n"
        "\treturn fLock != NULL && fVSyncCall != NULL;",
        "\tfLock = IOLockAlloc();\n"
        "\tfVSyncCall = thread_call_allocate(&IOMobileFramebufferUserClient::vsyncFired, this);\n"
        '\tIOLog("MiniIOMobileFramebuffer: disp0 opened by a client\\n");\n'
        "\treturn fLock != NULL && fVSyncCall != NULL;")
    fb.write_text(text)
    # --- who execs what -------------------------------------------------------------------
    # Every daemon but SpringBoard stops at "service state: xpcproxy" and never reaches
    # "service spawned with pid", and memory: programs stays 2 -- so nothing but SpringBoard
    # ever became a process. Nothing in the launchd log can say whether a spawn reaches
    # execve at all, so say it here: the pid and the path at execve, and the same at
    # posix_spawn's entry. pid < 60 covers launchd's whole early set (SpringBoard 10,
    # backboardd 13); past that the log would only repeat itself.
    kexec = tree / "bsd/kern/kern_exec.c"
    text = replace_once(
        kexec.read_text(),
        "\tmemoryshot(VM_EXECVE, DBG_FUNC_NONE);\n\n\tmuap.fname = uap->fname;",
        "\tmemoryshot(VM_EXECVE, DBG_FUNC_NONE);\n"
        "\tif (p->p_pid < 60) {\n"
        "\t\tchar chiron_ex[80];\n"
        "\t\tsize_t chiron_ex_done = 0;\n"
        "\t\tif (copyinstr(uap->fname, chiron_ex, sizeof(chiron_ex), &chiron_ex_done) == 0) {\n"
        '\t\t\tkprintf("chiron-exec: pid %d %s\\n", p->p_pid, chiron_ex);\n'
        "\t\t} else {\n"
        '\t\t\tkprintf("chiron-exec: pid %d <unreadable>\\n", p->p_pid);\n'
        "\t\t}\n"
        "\t}\n\n"
        "\tmuap.fname = uap->fname;")
    text = replace_once(
        text,
        "\tproc_t p = ap;\n\tuser_addr_t pid = uap->pid;",
        "\tproc_t p = ap;\n\tuser_addr_t pid = uap->pid;\n"
        "\tif (ap->p_pid < 60) {\n"
        "\t\tchar chiron_sp[80];\n"
        "\t\tsize_t chiron_sp_done = 0;\n"
        "\t\tif (copyinstr(uap->path, chiron_sp, sizeof(chiron_sp), &chiron_sp_done) == 0) {\n"
        '\t\t\tkprintf("chiron-spawn: enter pid %d %s\\n", ap->p_pid, chiron_sp);\n'
        "\t\t} else {\n"
        '\t\t\tkprintf("chiron-spawn: enter pid %d <unreadable>\\n", ap->p_pid);\n'
        "\t\t}\n"
        "\t}")
    kexec.write_text(text)
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
    if args.kind == "kernel":
        result = dict(result, userland=userland_overlay())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "profile": profile, "kind": args.kind, "result": result,
        "hardware_observed": False,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
