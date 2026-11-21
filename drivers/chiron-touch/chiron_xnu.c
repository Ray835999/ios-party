/*
 * Replaces only redwood_goodix.c in an explicitly selected chiron scratch tree.
 * Keep the existing init symbol so the shared input queue needs no new ABI.
 */
#if !defined(CHIRON_PORT)
#error "The chiron touch overlay requires the CHIRON_PORT kernel build."
#endif

#include <kern/clock.h>
#include <kern/debug.h>
#include <kern/sched_prim.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <sys/redwood_input.h>
#include <sys/systm.h>

#include "chiron_dsx.h"
#include "chiron_touch_core.inc"
#include "chiron_touch_qup.inc"
#include "chiron_touch_f12.inc"
#include "chiron_touch_native.inc"
#include "chiron_touch_board.inc"
#include "chiron_touch_clock.inc"

_Static_assert(CHIRON_DSX_DOWN == REDWOOD_TOUCH_DOWN, "touch DOWN ABI");
_Static_assert(CHIRON_DSX_MOVE == REDWOOD_TOUCH_MOVE, "touch MOVE ABI");
_Static_assert(CHIRON_DSX_UP == REDWOOD_TOUCH_UP, "touch UP ABI");

extern kern_return_t thread_terminate(thread_t thread);
extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern void redwood_tl(const char *, ...);

static struct chiron_dsx chiron_touch;
static struct chiron_native chiron_native;
static struct chiron_board_io chiron_board_io;
static struct chiron_board_report chiron_board_report;
static struct chiron_clock_report chiron_clock_report;
static int chiron_clock_writes_allowed;
static uint32_t chiron_touch_mode;
static unsigned chiron_capture_polls, chiron_capture_records;
static uint64_t chiron_capture_last_us;
static struct {
    uint64_t physical;
    uint32_t size;
    vm_offset_t mapped;
} chiron_regions[] = {
    {CHIRON_GCC_BASE, CHIRON_GCC_SIZE, 0},
    {CHIRON_GPIO_BASE, CHIRON_GPIO_SIZE, 0},
    {CHIRON_GIC_BASE, CHIRON_GIC_SIZE, 0},
    {CHIRON_QUP_BASE, CHIRON_QUP_SIZE, 0}
};

static void
chiron_io_barrier(void *context)
{
    (void)context;
#if defined(__aarch64__)
    __asm__ volatile ("dmb sy" ::: "memory");
#else
    __sync_synchronize(); /* host syntax/contract tests */
#endif
}

static void
chiron_touch_wait(uint32_t interval, uint32_t scale)
{
    /* This worker holds no lock; always use a scheduler wait, never a spin. */
    if (assert_wait_timeout((event_t)&chiron_touch, THREAD_UNINT, interval,
        scale) == THREAD_WAITING)
        (void)thread_block(THREAD_CONTINUE_NULL);
}

static void
chiron_touch_sleep(void)
{
    chiron_touch_wait(20, NSEC_PER_MSEC);
}

static int
chiron_property(void *context, const char *path, const char *name,
    const void **data, size_t *size)
{
    DTEntry entry;
    unsigned int length;
    (void)context;
    if (SecureDTLookupEntry(NULL, path, &entry) != kSuccess ||
        SecureDTGetProperty(entry, name, data, &length) != kSuccess)
        return -1;
    *size = length;
    return 0;
}

static vm_offset_t
chiron_map_register(uint64_t physical)
{
    if (!chiron_board_report.platform_valid || (physical & 3u))
        return 0;
    for (unsigned i = 0; i < sizeof(chiron_regions) / sizeof(chiron_regions[0]); i++) {
        if (physical < chiron_regions[i].physical ||
            physical - chiron_regions[i].physical > chiron_regions[i].size - 4)
            continue;
        if (!chiron_regions[i].mapped)
            chiron_regions[i].mapped = ml_io_map((vm_offset_t)chiron_regions[i].physical,
                chiron_regions[i].size);
        if (!chiron_regions[i].mapped)
            return 0;
        return chiron_regions[i].mapped + (vm_offset_t)(physical - chiron_regions[i].physical);
    }
    return 0;
}

static int
chiron_physical_read(void *context, uint64_t physical, uint32_t *value)
{
    (void)context;
    vm_offset_t address = chiron_map_register(physical);
    if (!address)
        return -1;
    chiron_io_barrier(NULL);
    *value = *(volatile uint32_t *)address;
    chiron_io_barrier(NULL);
    return 0;
}

static uint32_t
chiron_qup_read32(void *context, uint32_t offset)
{
    uint32_t value = UINT32_MAX;
    (void)context;
    if (offset < CHIRON_QUP_SIZE && !(offset & 3u))
        (void)chiron_physical_read(NULL, CHIRON_QUP_BASE + offset, &value);
    return value;
}

static void
chiron_qup_write32(void *context, uint32_t offset, uint32_t value)
{
    (void)context;
    if (offset >= CHIRON_QUP_SIZE || (offset & 3u))
        return;
    vm_offset_t address = chiron_map_register(CHIRON_QUP_BASE + offset);
    if (address)
        *(volatile uint32_t *)address = value;
}

static int
chiron_retained(void *context)
{
    (void)context;
    return chiron_board_retained(&chiron_board_io, &chiron_board_report) == 0;
}

static void
chiron_delay_us(void *context, uint32_t interval)
{
    (void)context;
    chiron_touch_wait(interval, NSEC_PER_USEC);
}

static uint64_t
chiron_now_us(void *context)
{
    uint64_t ns;
    (void)context;
    absolutetime_to_nanoseconds(mach_absolute_time(), &ns);
    return ns / 1000;
}

static void
chiron_clock_delay_us(void *context, uint32_t interval)
{
    uint64_t start = mach_absolute_time();
    (void)context;
    /* The GCC source uses udelay(1); scheduler sleeps can exceed 500 us. */
    for (unsigned i = 0; i < 4096; i++) {
        uint64_t now = mach_absolute_time(), ns;
        if (now < start)
            return;
        absolutetime_to_nanoseconds(now - start, &ns);
        if (ns >= (uint64_t)interval * NSEC_PER_USEC)
            return;
    }
}

static int
chiron_clock_write32(void *context, uint64_t physical, uint32_t value)
{
    struct chiron_board_report identity;
    (void)context;
    if (!chiron_clock_writes_allowed ||
        chiron_board_identity(&chiron_board_io, &identity) || !identity.owner_assumed)
        return -1;
    if (physical != chiron_board_register_address(CB_AHB_VOTE) &&
        physical != chiron_board_register_address(CB_QUP_CLOCK) &&
        physical != chiron_board_register_address(CB_ROOT_COMMAND) &&
        physical != chiron_board_register_address(CB_ROOT_CONFIG))
        return -1;
    vm_offset_t mapped = chiron_map_register(physical);
    if (!mapped)
        return -1;
    chiron_io_barrier(NULL);
    *(volatile uint32_t *)mapped = value;
    chiron_io_barrier(NULL);
    return 0;
}

static void
chiron_probe_report(void)
{
    printf("chiron touch: retained probe status=%d failed=%d observed=%x handoff=%u/%u/%x/%u\n",
        chiron_board_report.issue, chiron_board_report.failed_register,
        (unsigned)chiron_board_report.observed,
        (unsigned)chiron_board_report.handoff[0], (unsigned)chiron_board_report.handoff[1],
        (unsigned)chiron_board_report.handoff[2], (unsigned)chiron_board_report.handoff[3]);
    for (unsigned i = 0; i < CB_REGISTER_COUNT; i++)
        if (chiron_board_report.observed & (1u << i))
            printf("chiron touch: %s [%llx]=%08x\n", chiron_board_register_name(i),
                (unsigned long long)chiron_board_register_address(i),
                (unsigned)chiron_board_report.values[i]);
    if (chiron_board_report.failed_register >= 0) {
        unsigned index = (unsigned)chiron_board_report.failed_register;
        if (chiron_board_report.issue == CB_READ_FAILED)
            printf("chiron touch: cannot map/read prerequisite %s\n",
                chiron_board_register_name(index));
        else
            printf("chiron touch: prerequisite %s requires mask=%08x expected=%08x observed=%08x\n",
                chiron_board_register_name(index), (unsigned)chiron_board_report.expected_mask,
                (unsigned)chiron_board_report.expected_value,
                (unsigned)chiron_board_report.values[index]);
    }
    if (chiron_board_report.issue == CB_OWNERSHIP)
        printf("chiron touch: resident UEFI ownership unconfirmed: direct boot-CPU handoff "
            "and seven other affinities confirmed OFF are required; probe only\n");
}

static void
chiron_post(void *context, uint32_t type, uint32_t finger, uint32_t x, uint32_t y)
{
    (void)context;
    redwood_touch_post(type, finger, x, y);
}

static int
chiron_native_transport(struct chiron_dsx_bus *bus)
{
    int status = chiron_board_probe(&chiron_board_io, &chiron_board_report);
    chiron_probe_report();
    if (chiron_touch_mode == 2)
        printf("chiron touch: read-only probe requested; no controller writes\n");
    if (status || chiron_touch_mode == 2)
        return status ? status : CHIRON_DSX_NO_TRANSPORT;
    struct chiron_qup_io io = {
        NULL, chiron_retained, chiron_qup_read32, chiron_qup_write32,
        chiron_io_barrier, chiron_delay_us, chiron_now_us
    };
    printf("chiron touch: retained registers pass; assuming boot-CPU firmware suspension "
        "excludes UEFI I2C clients (Boot Services remain resident)\n");
    /* Chiron's measured DT has no swap/x-flip/y-flip properties. */
    return chiron_native_bind(&chiron_native, &io, 0, bus);
}

static void
chiron_touch_probe_stop(void)
{
    chiron_board_io = (struct chiron_board_io){NULL, chiron_property, chiron_physical_read};
    int status = chiron_board_probe(&chiron_board_io, &chiron_board_report);
    chiron_probe_report();
    printf("CHIRON TOUCH PROBE: read-only, no transport or worker; stopping before GUI\n");
    /*
     * Include the snapshot in the panic itself for a photograph. The observed
     * bitmask marks valid cells; unread cells remain zero placeholders.
     */
    panic("CHIRON TOUCH PROBE READ ONLY status=%d bad=%d obs=%x\n"
        "owner=%u/%u/%x/%u\n"
        "GCC bcr=%x vote=%x ahb=%x qup=%x cmd=%x cfg=%x\n"
        "GPIO87=%x GPIO88=%x resetcfg=%x resetio=%x\n"
        "GIC en4=%x QUP state=%x status=%x\n"
        "obs marks valid cells; unread=0. NO CONTROLLER WRITES.",
        status, chiron_board_report.failed_register, (unsigned)chiron_board_report.observed,
        (unsigned)chiron_board_report.handoff[0], (unsigned)chiron_board_report.handoff[1],
        (unsigned)chiron_board_report.handoff[2], (unsigned)chiron_board_report.handoff[3],
        (unsigned)chiron_board_report.values[CB_BLSP_RESET],
        (unsigned)chiron_board_report.values[CB_AHB_VOTE],
        (unsigned)chiron_board_report.values[CB_AHB_CLOCK],
        (unsigned)chiron_board_report.values[CB_QUP_CLOCK],
        (unsigned)chiron_board_report.values[CB_ROOT_COMMAND],
        (unsigned)chiron_board_report.values[CB_ROOT_CONFIG],
        (unsigned)chiron_board_report.values[CB_SDA_MUX],
        (unsigned)chiron_board_report.values[CB_SCL_MUX],
        (unsigned)chiron_board_report.values[CB_RESET_CONFIG],
        (unsigned)chiron_board_report.values[CB_RESET_LEVEL],
        (unsigned)chiron_board_report.values[CB_QUP_IRQ_ENABLE],
        (unsigned)chiron_board_report.values[CB_QUP_STATE],
        (unsigned)chiron_board_report.values[CB_QUP_STATUS]);
}

static int
chiron_prepare_clocks(void)
{
    int status;
    chiron_clock_report = (struct chiron_clock_report){
        .failed_register = -1, .cleanup_register = -1
    };
    chiron_board_io = (struct chiron_board_io){NULL, chiron_property, chiron_physical_read};
    status = chiron_board_identity(&chiron_board_io, &chiron_board_report);
    if (!status) {
        struct chiron_clock_io io = {
            chiron_board_io, chiron_clock_write32, chiron_io_barrier,
            chiron_now_us, chiron_clock_delay_us
        };
        chiron_clock_writes_allowed = 1;
        status = chiron_clock_prepare(&io, &chiron_clock_report);
        chiron_clock_writes_allowed = 0;
    } else {
        chiron_clock_report.result = status;
        chiron_clock_report.before = chiron_board_report;
        chiron_clock_report.after = chiron_board_report;
    }
    printf("chiron touch: clock prepare result=%d step=%d reg=%d changed=%x remaining=%x "
        "rollback=%d cleanup=%d/%d/%d\n",
        status, chiron_clock_report.step, chiron_clock_report.failed_register,
        (unsigned)chiron_clock_report.changed, (unsigned)chiron_clock_report.remaining,
        chiron_clock_report.rollback, chiron_clock_report.cleanup_result,
        chiron_clock_report.cleanup_step, chiron_clock_report.cleanup_register);
    if (status) {
        chiron_board_report = chiron_clock_report.after;
        chiron_board_report.issue = status;
        chiron_board_report.failed_register = -1;
    }
    return status;
}

static void
chiron_touch_clock_probe_stop(void)
{
    int status = chiron_prepare_clocks();
    if (!status)
        (void)chiron_board_probe(&chiron_board_io, &chiron_board_report);
    chiron_probe_report();
    printf("CHIRON TOUCH CLOCK PROBE: GCC clocks only; no GPIO/regulator/QUP writes or worker\n");
    panic("CHIRON TOUCH CLOCK PROBE result=%d step=%d reg=%d changed=%x left=%x\n"
        "rollback=%d cleanup=%d/%d/%d before_obs=%x after_obs=%x\n"
        "board=%d bad=%d obs=%x owner=%u/%u/%x/%u\n"
        "BEFORE vote=%x ahb=%x qup=%x cmd=%x cfg=%x\n"
        "AFTER vote=%x ahb=%x qup=%x cmd=%x cfg=%x\n"
        "GPIO87=%x GPIO88=%x resetcfg=%x resetio=%x\n"
        "GIC en4=%x QUP state=%x status=%x\n"
        "GCC CLOCK WRITES REQUESTED. NO TOUCH TRANSACTIONS.",
        status, chiron_clock_report.step, chiron_clock_report.failed_register,
        (unsigned)chiron_clock_report.changed, (unsigned)chiron_clock_report.remaining,
        chiron_clock_report.rollback, chiron_clock_report.cleanup_result,
        chiron_clock_report.cleanup_step, chiron_clock_report.cleanup_register,
        (unsigned)chiron_clock_report.before.observed,
        (unsigned)chiron_clock_report.after.observed, chiron_board_report.issue,
        chiron_board_report.failed_register, (unsigned)chiron_board_report.observed,
        (unsigned)chiron_board_report.handoff[0], (unsigned)chiron_board_report.handoff[1],
        (unsigned)chiron_board_report.handoff[2], (unsigned)chiron_board_report.handoff[3],
        (unsigned)chiron_clock_report.before.values[CB_AHB_VOTE],
        (unsigned)chiron_clock_report.before.values[CB_AHB_CLOCK],
        (unsigned)chiron_clock_report.before.values[CB_QUP_CLOCK],
        (unsigned)chiron_clock_report.before.values[CB_ROOT_COMMAND],
        (unsigned)chiron_clock_report.before.values[CB_ROOT_CONFIG],
        (unsigned)chiron_clock_report.after.values[CB_AHB_VOTE],
        (unsigned)chiron_clock_report.after.values[CB_AHB_CLOCK],
        (unsigned)chiron_clock_report.after.values[CB_QUP_CLOCK],
        (unsigned)chiron_clock_report.after.values[CB_ROOT_COMMAND],
        (unsigned)chiron_clock_report.after.values[CB_ROOT_CONFIG],
        (unsigned)chiron_board_report.values[CB_SDA_MUX],
        (unsigned)chiron_board_report.values[CB_SCL_MUX],
        (unsigned)chiron_board_report.values[CB_RESET_CONFIG],
        (unsigned)chiron_board_report.values[CB_RESET_LEVEL],
        (unsigned)chiron_board_report.values[CB_QUP_IRQ_ENABLE],
        (unsigned)chiron_board_report.values[CB_QUP_STATE],
        (unsigned)chiron_board_report.values[CB_QUP_STATUS]);
}

static int
chiron_prepare_pinmux(uint32_t *changed)
{
    *changed = 0;
    int status = chiron_prepare_clocks();
    if (!status) {
        for (unsigned reg = CB_SDA_MUX; reg <= CB_SCL_MUX; reg++) {
            status = chiron_board_probe(&chiron_board_io, &chiron_board_report);
            if (status != CB_PINMUX && status != CB_RESET && status != CB_READY)
                break;
            if (!chiron_board_report.owner_assumed) {
                status = CB_OWNERSHIP;
                break;
            }
            /* Pinned pinctrl-msm8998: function 3, mux bits [5:2].
             * Accept only passive GPIO or existing I2C; preserve pull/drive. */
            int supported = 1;
            for (unsigned pin = CB_SDA_MUX; pin <= CB_SCL_MUX; pin++) {
                uint32_t mode = chiron_board_report.values[pin] & 0x23cu;
                if (mode != 0 && mode != 12)
                    supported = 0;
            }
            if (!supported) {
                status = CB_PINMUX;
                break;
            }
            uint64_t physical = chiron_board_register_address(reg);
            uint32_t value;
            if (chiron_physical_read(NULL, physical, &value)) {
                status = CB_READ_FAILED;
                break;
            }
            if ((value & 0x23cu) == 12)
                continue;
            if ((value & 0x23cu) != 0) {
                status = CB_PINMUX;
                break;
            }
            struct chiron_board_report identity;
            if (chiron_board_identity(&chiron_board_io, &identity) ||
                !identity.owner_assumed) {
                status = CB_OWNERSHIP;
                break;
            }
            vm_offset_t mapped = chiron_map_register(physical);
            if (!mapped) {
                status = CB_READ_FAILED;
                break;
            }
            *changed |= 1u << reg;
            chiron_io_barrier(NULL);
            *(volatile uint32_t *)mapped = (value & ~0x3cu) | 12u;
            chiron_io_barrier(NULL);
            if (chiron_physical_read(NULL, physical, &value) || (value & 0x23cu) != 12) {
                status = CB_PINMUX;
                break;
            }
            status = 0;
        }
        if (!status || status == CB_RESET)
            status = chiron_board_probe(&chiron_board_io, &chiron_board_report);
    }
    return status;
}

static void
chiron_touch_pinmux_probe_stop(void)
{
    uint32_t changed;
    int status = chiron_prepare_pinmux(&changed);
    chiron_probe_report();
    printf("CHIRON TOUCH PINMUX PROBE result=%d changed=%x; no reset/regulator/QUP writes or worker\n",
        status, (unsigned)changed);
    panic("CHIRON TOUCH PINMUX PROBE result=%d changed=%x board=%d bad=%d obs=%x\n"
        "GPIO87=%x GPIO88=%x resetcfg=%x resetio=%x\n"
        "GCC AND I2C5 MUX WRITES REQUESTED. NO TOUCH TRANSACTIONS.",
        status, (unsigned)changed, chiron_board_report.issue,
        chiron_board_report.failed_register, (unsigned)chiron_board_report.observed,
        (unsigned)chiron_board_report.values[CB_SDA_MUX],
        (unsigned)chiron_board_report.values[CB_SCL_MUX],
        (unsigned)chiron_board_report.values[CB_RESET_CONFIG],
        (unsigned)chiron_board_report.values[CB_RESET_LEVEL]);
}

static void
chiron_layout_refusal_report(void)
{
    const struct chiron_dsx_functions *f = &chiron_touch.functions;
    const struct chiron_f12_refusal *r = &chiron_native.reader.refusal;
    printf("chiron touch: PDT discovered=%u F01 query=%x control=%x data=%x "
        "F12 query=%x control=%x data=%x\n", chiron_touch.discovered,
        (unsigned)f->f01.query, (unsigned)f->f01.control, (unsigned)f->f01.data,
        (unsigned)f->f12.query, (unsigned)f->f12.control, (unsigned)f->f12.data);
    if (!r->field)
        return;
    printf("chiron touch: F12 refusal field=%s address=%x actual=",
        r->field, (unsigned)r->address);
    for (unsigned i = 0; i < r->bytes; i++)
        printf("%02x%s", (unsigned)r->actual[i], i + 1 == r->bytes ? "" : " ");
    printf(" expected=%s; sensor configuration write attempts=%u\n",
        r->expected, (unsigned)chiron_native.reader.configuration_write_attempts);
}

static void
chiron_transport_report(void)
{
    printf("chiron touch: transport input_fifo=%u output_fifo=%u ready=%u last_op=%u address=%x request=%u result=%d\n",
        (unsigned)chiron_native.qup.input_bytes, (unsigned)chiron_native.qup.output_bytes,
        (unsigned)chiron_native.qup.ready, (unsigned)chiron_native.last_operation,
        (unsigned)chiron_native.last_address, (unsigned)chiron_native.last_bytes,
        chiron_native.last_result);
}

static void
chiron_capture_report(const struct chiron_dsx *device, const uint8_t *frame,
    size_t bytes, uint16_t before, uint16_t after, uint8_t f01,
    unsigned observed, int status)
{
    static const char hex[] = "0123456789abcdef";
    char raw[CHIRON_DSX_CONTACTS * CHIRON_DSX_OBJECT_BYTES * 2 + 1];
    uint64_t now = chiron_now_us(NULL);
    unsigned active = 0;
    chiron_capture_polls++;
    if (chiron_capture_records >= 64 || (!status && chiron_capture_records &&
        now - chiron_capture_last_us < 5000000))
        return;
    chiron_capture_last_us = now;
    chiron_capture_records++;
    if (bytes > sizeof(raw) / 2)
        bytes = sizeof(raw) / 2;
    for (size_t i = 0; i < bytes; i++) {
        raw[2 * i] = hex[frame[i] >> 4];
        raw[2 * i + 1] = hex[frame[i] & 15u];
    }
    raw[2 * bytes] = 0;
    for (unsigned i = 0; i < device->layout.contacts; i++)
        if (device->contact[i].down)
            active |= 1u << i;
    redwood_tl("chiron touch: capture poll=%u obs=%x result=%d before=%04x after=%04x f01=%02x active=%x bytes=%u at_us=%llu\n",
        chiron_capture_polls, observed, status, (unsigned)before, (unsigned)after,
        (unsigned)f01, active, (unsigned)bytes, (unsigned long long)now);
    redwood_tl("chiron touch: raw=%s\n", raw);
}

static void
chiron_touch_thread(void *parameter, wait_result_t result)
{
    int status;
    struct chiron_dsx_bus bus = {0};
    (void)parameter;
    (void)result;
    if (chiron_touch_mode == 1) {
        uint32_t changed;
        status = chiron_prepare_pinmux(&changed);
        printf("chiron touch: pinmux prepare result=%d changed=%x\n",
            status, (unsigned)changed);
        if (status) {
            chiron_probe_report();
            printf("chiron touch: board preparation refused (%d); stopped before QUP\n", status);
            thread_terminate(current_thread());
            return;
        }
    }
    status = chiron_native_transport(&bus);
    if (status) {
        chiron_transport_report();
        printf("chiron touch: transport refused (%d); no pin/reset/regulator writes\n", status);
        thread_terminate(current_thread());
        return;
    }
    chiron_transport_report();
    chiron_dsx_init(&chiron_touch, &bus, chiron_post, NULL);
    status = chiron_dsx_start(&chiron_touch);
    if (status) {
        chiron_transport_report();
        chiron_layout_refusal_report();
        printf("chiron touch: discovery/layout refused (%d)\n", status);
        thread_terminate(current_thread());
        return;
    }
    printf("chiron touch: reset initialization verified; sensor configuration write attempts=%u\n",
        (unsigned)chiron_native.reader.configuration_write_attempts);
    uint32_t capture = 0;
    if (PE_parse_boot_argn("chiron_touch_capture", &capture, sizeof(capture)) && capture == 1) {
        chiron_touch.observer = chiron_capture_report;
        redwood_tl("chiron touch: capture layout f01=%x data1=%x data15=%x bitmap_bytes=%u contacts=%u max=%ux%u\n",
            (unsigned)chiron_touch.functions.f01.data, (unsigned)chiron_touch.layout.data1,
            (unsigned)chiron_touch.layout.data15, (unsigned)chiron_touch.layout.data15_bytes,
            (unsigned)chiron_touch.layout.contacts, (unsigned)chiron_touch.layout.max_x,
            (unsigned)chiron_touch.layout.max_y);
    }
    printf("chiron touch: verified DSX layout, polling every 20 ms\n");
    for (;;) {
        /* Thread context is required by redwood_touch_post's mutex/queue. */
        status = chiron_dsx_poll(&chiron_touch);
        if (status) {
            chiron_transport_report();
            chiron_layout_refusal_report();
            chiron_probe_report();
            printf("chiron touch: report failed (%d), all fingers released; stopped\n", status);
            thread_terminate(current_thread());
            return;
        }
        chiron_touch_sleep();
    }
}

void
redwood_goodix_init(void)
{
    uint32_t enabled = 0, probe = 0, clocks = 0, pinmux = 0;
    thread_t thread;
    int status;
    /* Probe takes priority even if the payload also requests normal touch. */
    if (PE_parse_boot_argn("chiron_touch_probe", &probe, sizeof(probe)) && probe == 1) {
        chiron_touch_probe_stop();
        return;
    }
    if (PE_parse_boot_argn("chiron_touch_clock_probe", &clocks, sizeof(clocks)) && clocks == 1) {
        chiron_touch_clock_probe_stop();
        return;
    }
    if (PE_parse_boot_argn("chiron_touch_pinmux_probe", &pinmux, sizeof(pinmux)) && pinmux == 1) {
        chiron_touch_pinmux_probe_stop();
        return;
    }
    if (!PE_parse_boot_argn("chiron_touch", &enabled, sizeof(enabled)) ||
        (enabled != 1 && enabled != 2))
        return;
    chiron_touch_mode = enabled;
    chiron_board_io = (struct chiron_board_io){NULL, chiron_property, chiron_physical_read};
    status = chiron_board_identity(&chiron_board_io, &chiron_board_report);
    if (status) {
        printf("chiron touch: platform refused before MMIO (%d), hardware untouched\n", status);
        return;
    }
    if (kernel_thread_start(chiron_touch_thread, NULL, &thread) == KERN_SUCCESS)
        thread_deallocate(thread);
    else
        printf("chiron touch: failed to start polling thread\n");
}
