/* Included by psci_smp.c after psci_call(), only in CHIRON_SMP builds. */
static const volatile uint64_t chiron_smp_abi[2] __attribute__((aligned(8), used)) = {
	0x31504d534e524843ULL, 0xceafb2acb1adb7bcULL
};
static uint32_t chiron_smp_expected_cpus;
static const uint32_t chiron_smp_mpidrs[8] = { 0, 1, 2, 3, 256, 257, 258, 259 };

static void
chiron_smp_validate_topology(void)
{
	DTEntry chosen;
	const void *property;
	unsigned int size;
	uint32_t count;
	const ml_topology_info_t *topology = ml_get_topology_info();

	/* A kernel built with this overlay still accepts the single-core/QEMU ADT. */
	if (SecureDTLookupEntry(NULL, "/chosen", &chosen) != kSuccess ||
	    SecureDTGetProperty(chosen, "chiron-smp-cpu-count", &property, &size) != kSuccess) return;
	if (size != sizeof(count)) panic("chiron smp: bad count size");
	count = *(const uint32_t *)property;
	if ((count != 1 && count != 2 && count != 4 && count != 8) ||
	    topology->num_cpus != count || topology->boot_cpu != &topology->cpus[0] ||
	    psci_smp_hvc != 0 || chiron_smp_abi[0] != 0x31504d534e524843ULL ||
	    chiron_smp_abi[1] != 0xceafb2acb1adb7bcULL) {
		panic("chiron smp: unsupported topology or PSCI conduit");
	}
	for (uint32_t cpu = 0; cpu < count; cpu++) {
		if (topology->cpus[cpu].phys_id != chiron_smp_mpidrs[cpu] ||
		    topology->cpus[cpu].cluster_core_id != (chiron_smp_mpidrs[cpu] & 0xff) ||
		    topology->cpus[cpu].die_cluster_id != (chiron_smp_mpidrs[cpu] >> 8)) {
			panic("chiron smp: unexpected MPIDR mapping for cpu %u", cpu);
		}
	}
	chiron_smp_expected_cpus = count;
	printf("chiron smp: ABI v1 validated %u CPUs\n", count);
}

static void
chiron_smp_check_start(int cpu, uint32_t phys_id)
{
	if (chiron_smp_expected_cpus == 0) return;
	if (cpu <= 0 || (uint32_t)cpu >= chiron_smp_expected_cpus ||
	    phys_id != chiron_smp_mpidrs[cpu]) {
		panic("chiron smp: CPU_ON target outside selected topology");
	}
	int64_t state = psci_call(PSCI_AFFINITY_INFO, phys_id, 0, 0);
	if (state != PSCI_AFFINITY_OFF) {
		panic("chiron smp: CPU_ON MPIDR 0x%x not OFF (%lld)", phys_id, state);
	}
}
