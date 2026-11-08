/*
 * 小米 MIX 2（chiron，MSM8998）的 Adreno 540：只读探针，第一步。
 *
 * 这个文件由 ci/hackios/apply_chiron_gpu.py 追加到上游 hackios 的 bsd/dev/hakios.adreno.c 末尾，
 * 并在 hakios_adreno_init() 的第一行被调用，所以它能用那份文件的 rd()/wr()/say()/hold()/
 * 结构体 block 与 ml_io_map。它不写任何寄存器。
 *
 * 为什么先只读：hackios 现有那份驱动是 POCO X5 Pro 的 Adreno 642L（SC7280，a660 核 + GMU），
 * 而这个手机是 Adreno 540（a5xx，GPMU 而不是 GMU）。两者的地址、固件、上电顺序都不同。在一台
 * 每次验证只有五分钟、而且进得去系统就拿不到日志的手机上，先把"哪些块读得到、读到的是什么"
 * 记下来，比照着另一台手机的表格去写，风险低得多。这一步的结论决定后面还有没有路：
 *
 *   读得回 0xFFFFFFFF —— 总线没人应答（块没上电，或者被 XPU/hypervisor 拦下）
 *   读得回 0x00000000 —— 块在，但没被打开（多半是 GDSC 关着、时钟没开）
 *   读得回别的值     —— 块活着，后面才有得谈
 *
 * 地址全部来自这台手机自己的设备树（boot.img 里那份 DTB 的 /soc 节点），不是别处的表：
 *
 *   qcom,kgsl-3d0@5000000   reg 0x5000000+0x40000，irq 300，qcom,chipid 0x5040001
 *   arm,smmu-kgsl@5040000   reg 0x5040000+0x10000，irq 329/330/331
 *   qcom,gpucc@5065000      reg 0x5065000+0x9000（qcom,gpucc-8998-v2）
 *   qcom,gdsc@5066004       CX：base 0x5066004，hw_ctrl_addr 0x5066008
 *   qcom,gdsc@5066094       GX：base 0x5066094，domain_addr 0x5065130，sw_reset 0x5066090
 *
 * 两个 GDSC 的寄存器都落在 GPUCC 那 0x9000 里，所以只映射一次，按偏移读。
 *
 * Boot-args:
 *   chiron_gpu=N        做到第 N 步（1..5）。不给就不碰任何一个寄存器。
 *   chiron_gpu_return=1 做完回 loader，这样这次运行之后还能把日志取回来。
 *   chiron_gpu_pause=N  步与步之间停 N 毫秒，默认 500：给 loader 记下刚说的那行留时间。
 *
 * 状态：从未在这台手机上运行过。它只读，所以最坏的结果是总线不应答或者系统在它跑完之后
 * 崩掉——两种情况都会在 loader 的日志里留下一行，说明是哪一步、哪个地址。
 */

#define CHIRON_GPU_STEPS 5
#define CHIRON_GPUCC_PHYS   0x05065000u     /* qcom,gpucc@5065000 */
#define CHIRON_GPUCC_SIZE   0x9000u
#define CHIRON_SMMU_PHYS    0x05040000u     /* arm,smmu-kgsl@5040000 */
#define CHIRON_SMMU_SIZE    0x10000u
#define CHIRON_KGSL_PHYS    0x05000000u     /* qcom,kgsl-3d0@5000000 */
#define CHIRON_KGSL_SIZE    0x40000u

/* GPUCC 里的两个电源域，偏移相对 GPUCC 的基址 */
#define CHIRON_GDSC_CX      (0x05066004u - CHIRON_GPUCC_PHYS)   /* 0x1004 */
#define CHIRON_GDSC_CX_CTRL (0x05066008u - CHIRON_GPUCC_PHYS)   /* 0x1008 */
#define CHIRON_GDSC_GX      (0x05066094u - CHIRON_GPUCC_PHYS)   /* 0x1094 */
#define CHIRON_GDSC_GX_RST  (0x05066090u - CHIRON_GPUCC_PHYS)   /* 0x1090 */
#define CHIRON_GX_DOMAIN    (0x05065130u - CHIRON_GPUCC_PHYS)   /* 0x0130 */

/* GDSC 的状态字段（Linux drivers/clk/qcom/gdsc.c、gdsc.h）：低四位是状态 */
#define GDSC_POWER_ON       0x0u
#define GDSC_POWER_OFF      0x4u
#define GDSC_POWER_ON_WAIT  0x8u
#define GDSC_POWER_RET      0x10u
#define GDSC_STATUS_MASK    0x1fu

static struct block chiron_gpucc = { "chiron-gpucc", CHIRON_GPUCC_PHYS, CHIRON_GPUCC_SIZE };
static struct block chiron_smmu  = { "chiron-smmu",  CHIRON_SMMU_PHYS,  CHIRON_SMMU_SIZE };
static struct block chiron_kgsl  = { "chiron-kgsl",  CHIRON_KGSL_PHYS,  CHIRON_KGSL_SIZE };

static const char *
chiron_gdsc_state(uint32_t value)
{
	switch (value & GDSC_STATUS_MASK) {
	case GDSC_POWER_ON:      return "on";
	case GDSC_POWER_OFF:     return "off";
	case GDSC_POWER_ON_WAIT: return "coming up";
	case GDSC_POWER_RET:     return "retention";
	default:                 return "?";
	}
}

/* 一行说清一个块从偏移 off 起的 n 个寄存器；全 F 与全 0 单独点名，因为那是两种不同的坏。 */
static void
chiron_dump(struct block *b, uint32_t off, unsigned n)
{
	char line[256];
	size_t used = 0;

	need(b);
	for (unsigned i = 0; i < n; i++) {
		used += snprintf(line + used, sizeof(line) - used, " %08x", rd(b->base + off + i * 4));
		if (used >= sizeof(line) - 12) {
			break;
		}
	}
	say("chiron-gpu:   %s+%#05x:%s\n", b->name, off, line);
}

static void
chiron_gpu_probe_step(uint32_t n)
{
	switch (n) {
	case 1:
		/*
		 * GPUCC：时钟控制器本体。这里读得到东西，说明这半边总线通；读回全 F 说明
		 * 这个块现在根本不可达（GDSC 关着，或者被拦）。GCC 那边的 gcc_gpu_cfg_ahb_clk
		 * 是这块寄存器的接口时钟，它没开的时候读写会挂。
		 */
		say("chiron-gpu: step 1: the GPU's clock controller (gpucc 0x%08x, 0x%x bytes)\n",
		    CHIRON_GPUCC_PHYS, CHIRON_GPUCC_SIZE);
		chiron_dump(&chiron_gpucc, 0x0000, 8);      /* GCC_GPU_CC 的头几个 */
		chiron_dump(&chiron_gpucc, 0x1000, 4);      /* 电源域那一带 */
		break;
	case 2:
		/*
		 * 两个电源域各自的状态。这是 a540 能不能被叫醒的关键：CX 管总线接口与 SMMU，
		 * GX 管 3D 核。它们现在应该是 loader 或 Android 关掉后的样子。
		 * 只读状态位，不写 EN_REST_WAIT/EN_FEW_WAIT，也不碰 SW_RESET。
		 */
		say("chiron-gpu: step 2: the two power domains of the GPU\n");
		{
			uint32_t cx = rd(chiron_gpucc.base + CHIRON_GDSC_CX);
			uint32_t gx = rd(chiron_gpucc.base + CHIRON_GDSC_GX);
			uint32_t ctrl = rd(chiron_gpucc.base + CHIRON_GDSC_CX_CTRL);
			uint32_t rst = rd(chiron_gpucc.base + CHIRON_GDSC_GX_RST);
			uint32_t dom = rd(chiron_gpucc.base + CHIRON_GX_DOMAIN);

			say("chiron-gpu:   CX  %#010x -> %s; hw_ctrl %#010x\n", cx, chiron_gdsc_state(cx), ctrl);
			say("chiron-gpu:   GX  %#010x -> %s; sw_reset %#010x; domain %#010x\n",
			    gx, chiron_gdsc_state(gx), rst, dom);
		}
		break;
	case 3:
		/*
		 * SMMU：ARM 的 SMMU-v2（qcom,smmu-v2）。IDR0 是架构给的，读回来应该不是 0
		 * 也不是全 F；它自己说的版本在 IDR7 的低四位。这块要是读不动，后面给 GPU
		 * 建地址空间就无从谈起。
		 */
		say("chiron-gpu: step 3: the SMMU in front of the GPU (0x%08x)\n", CHIRON_SMMU_PHYS);
		chiron_dump(&chiron_smmu, 0x0000, 8);       /* SMMU_IDR0..IDR7 */
		{
			uint32_t idr0 = rd(chiron_smmu.base + 0x00);
			uint32_t idr7 = rd(chiron_smmu.base + 0x1c);
			say("chiron-gpu:   IDR0 %#010x: %s; SMMU arch %u.%u\n", idr0,
			    (idr0 == 0xffffffffu || idr0 == 0) ? "NOT ANSWERING" : "answers",
			    (idr7 >> 4) & 0xf, idr7 & 0xf);
		}
		break;
	case 4:
		/*
		 * kgsl-3d0 的头几个寄存器，也就是 RBBM 那一带。a5xx 的 RBBM 在 0x0 起：
		 * 0x00 是 RBBM_HW_VERSION。读到合理的版本号，说明 GPU 的整个 3D 窗口
		 * 至少在这一层是通的（哪怕 GX 还没上电）。
		 */
		say("chiron-gpu: step 4: the GPU itself, first words (kgsl-3d0 0x%08x)\n", CHIRON_KGSL_PHYS);
		chiron_dump(&chiron_kgsl, 0x0000, 8);
		break;
	case 5:
		/*
		 * 命令处理器与 GPMU 的状态各表。偏移取自 Linux kgsl 的 a5xx_reg.h：
		 * GPMU 的两条电源/时钟状态在 0xA88B 与 0xA88D。这些只有在 GX 上电之后
		 * 才有意义，现在读只是把"上电前它们长什么样"记下来，供之后对照。
		 */
		say("chiron-gpu: step 5: the command processor and the GPMU, before power\n");
		chiron_dump(&chiron_kgsl, 0x0a88b & ~3u, 4);        /* GPMU_SP/RBCCU 的状态那一带 */
		chiron_dump(&chiron_kgsl, 0x0ac61 & ~3u, 2);        /* GPMU 电压中断状态 */
		break;
	default:
		break;
	}
	say("chiron-gpu: step %u: done\n", n);
	hold(false);
}

void
hakios_chiron_gpu_init(void)
{
	uint32_t last = 0, ret = 0, gap = 500;

	/*
	 * 没有 boot-arg 的时候也跑：这个探针的目的就是"编出来、上机、看日志"，所以不让
	 * 传递 boot-arg 这件事挡在前面。要它别跑就写 chiron_gpu=0。
	 */
	if (!PE_parse_boot_argn("chiron_gpu", &last, sizeof(last))) {
		last = CHIRON_GPU_STEPS;
		ret = 1;                /* 跑完回 loader，这次运行的日志才取得回来 */
	} else if (last == 0) {
		return;
	}
	if (last > CHIRON_GPU_STEPS) {
		last = CHIRON_GPU_STEPS;
	}
	PE_parse_boot_argn("chiron_gpu_return", &ret, sizeof(ret));
	PE_parse_boot_argn("chiron_gpu_pause", &gap, sizeof(gap));

	step = 1;
	newest_step = last;
	pause_ms = gap;                 /* 全局的：hold() 读的是它 */
	say("chiron-gpu: Adreno 540 probe, steps 1 to %u, read only; nothing is written\n", last);
	say("chiron-gpu: addresses from this phone's own device tree: kgsl 0x%08x smmu 0x%08x gpucc 0x%08x\n",
	    CHIRON_KGSL_PHYS, CHIRON_SMMU_PHYS, CHIRON_GPUCC_PHYS);

	for (uint32_t n = 1; n <= last; n++) {
		step = n;
		chiron_gpu_probe_step(n);
	}

	say("chiron-gpu: probe over after step %u\n", last);
	if (ret) {
		say("chiron-gpu: back to the loader (chiron_gpu_return)\n");
		loader_return();
	}
}
