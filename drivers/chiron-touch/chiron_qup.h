/*
 * Native QUP v2 FIFO protocol, derived from the pinned Qualcomm register
 * definitions and documented sequence. No GCC/TLMM/regulator programming.
 */
#ifndef CHIRON_QUP_H
#define CHIRON_QUP_H

#include <stddef.h>
#include <stdint.h>

enum chiron_qup_register {
    CQ_CONFIG = 0x000, CQ_STATE = 0x004, CQ_IO_MODES = 0x008,
    CQ_SW_RESET = 0x00c, CQ_OPERATIONAL = 0x018, CQ_ERROR = 0x01c,
    CQ_ERROR_ENABLE = 0x020, CQ_OP_MASK = 0x028,
    CQ_MX_OUTPUT = 0x100, CQ_OUT_FIFO = 0x110, CQ_MX_WRITE = 0x150,
    CQ_MX_INPUT = 0x200, CQ_MX_READ = 0x208, CQ_IN_FIFO = 0x218,
    CQ_CLOCK = 0x400, CQ_STATUS = 0x404, CQ_MASTER_CONFIG = 0x408
};

enum chiron_qup_result {
    CHIRON_QUP_BLOCKED = -10,
    CHIRON_QUP_UNSUPPORTED = -11,
    CHIRON_QUP_TIMEOUT = -12,
    CHIRON_QUP_NACK = -13,
    CHIRON_QUP_BUS_ERROR = -14,
    CHIRON_QUP_FIFO_ERROR = -15
};

struct chiron_qup_io {
    void *context;
    /*
     * Platform prerequisite, not a boot argument: exclusive bus ownership,
     * QUP IRQ masked, live iface clock and 19.2 MHz QUP input clock, GPIO87/88 mux, panel
     * rails/reset retained at handoff, and the correct MMIO mapping established.
     * The chiron provider documents resident-UEFI boot-CPU suspension rather
     * than claiming ExitBootServices, which this loader does not execute.
     * Must remain true throughout a transaction. Missing/false => no MMIO.
     */
    int (*handoff_ready)(void *);
    uint32_t (*read32)(void *, uint32_t offset);
    void (*write32)(void *, uint32_t offset, uint32_t value);
    void (*barrier)(void *);
    /* Thread context; may yield. Must return; no locks/IRQ masking across waits. */
    void (*delay_us)(void *, uint32_t);
    /* Monotonic elapsed microseconds, separate from requested delay lengths. */
    uint64_t (*now_us)(void *);
};

struct chiron_qup {
    struct chiron_qup_io io;
    uint32_t input_bytes, output_bytes;
    uint8_t ready;
};

/* Single owner/thread. An error poisons the session; no automatic recovery. */
int chiron_qup_open(struct chiron_qup *, const struct chiron_qup_io *);
/* DSX bus callbacks, restricted to addr 0x20 and supported register pages. */
int chiron_qup_select_page(void *, uint8_t slave, uint8_t page);
int chiron_qup_read(void *, uint8_t slave, uint8_t reg, uint8_t *, size_t);
/* One register byte + STOP; the F12 initializer selects the two control addresses. */
int chiron_qup_write_byte(void *, uint8_t slave, uint8_t reg, uint8_t value);

#endif
