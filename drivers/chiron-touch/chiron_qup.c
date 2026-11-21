/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Register protocol adapted from Qualcomm's i2c-msm-v2.c and header at
 * LineageOS/android_kernel_xiaomi_msm8998 7231883a368604f90806e167890e00f515a9927c.
 * Original protocol implementation: Copyright (c) 2014-2019 The Linux Foundation.
 */
#include "chiron_qup.h"

#define CQ_VALID 4u
#define CQ_MASTER 16u
#define CQ_BUS_ACTIVE 0x100u
#define CQ_LINES_HIGH (3u << 26)
#define CQ_STATUS_ERRORS 0x038000fcu
#define CQ_FIFO_ERRORS 0x3cu
#define CQ_INPUT_NOT_EMPTY 0x20u
#define CQ_OUTPUT_SERVICE 0x100u
#define CQ_INPUT_SERVICE 0x200u
#define CQ_OUTPUT_DONE 0x400u
#define CQ_INPUT_DONE 0x800u

static uint32_t
cq_read(struct chiron_qup *q, uint32_t offset)
{
    return q->io.read32(q->io.context, offset);
}

static void
cq_write(struct chiron_qup *q, uint32_t offset, uint32_t value)
{
    q->io.write32(q->io.context, offset, value);
    q->io.barrier(q->io.context);
}

static int
cq_owned(struct chiron_qup *q)
{
    return q->io.handoff_ready && q->io.handoff_ready(q->io.context) == 1;
}

static int
cq_wait_state(struct chiron_qup *q, unsigned state, int only_valid)
{
    uint64_t start = q->io.now_us(q->io.context);
    for (unsigned i = 0; i < 1500; i++) {
        if (q->io.now_us(q->io.context) - start >= 15000)
            break;
        uint32_t value = cq_read(q, CQ_STATE);
        if ((value & CQ_VALID) && (only_valid || (value & 3u) == state))
            return 0;
        q->io.delay_us(q->io.context, 10);
    }
    return CHIRON_QUP_TIMEOUT;
}

static int
cq_state(struct chiron_qup *q, unsigned state)
{
    int status = cq_wait_state(q, 0, 1);
    if (status)
        return status;
    cq_write(q, CQ_STATE, state);
    return cq_wait_state(q, state, 0);
}

static int
cq_error(struct chiron_qup *q)
{
    uint32_t status = cq_read(q, CQ_STATUS);
    if (status & 8u)
        return CHIRON_QUP_NACK;
    if (status & CQ_STATUS_ERRORS)
        return CHIRON_QUP_BUS_ERROR;
    if (cq_read(q, CQ_ERROR) & CQ_FIFO_ERRORS)
        return CHIRON_QUP_FIFO_ERROR;
    return 0;
}

static int
cq_fail(struct chiron_qup *q, int status)
{
    q->ready = 0;
    /*
     * Vendor error path resets the controller, not the panel. Do not write
     * after ownership is revoked or if the state register is invalid.
     * No bus-clear pulses, GPIO recovery, IRQ changes or retries.
     */
    if (cq_owned(q) && (cq_read(q, CQ_STATE) & CQ_VALID))
        cq_write(q, CQ_STATE, 0);
    return status;
}

int
chiron_qup_open(struct chiron_qup *q, const struct chiron_qup_io *io)
{
    static const uint32_t block_bytes[4] = {16, 16, 32, 0};
    uint32_t modes, status;
    int result;
    *q = (struct chiron_qup){0};
    if (!io)
        return CHIRON_QUP_BLOCKED;
    q->io = *io;
    if (!io->read32 || !io->write32 || !io->barrier || !io->delay_us ||
        !io->now_us || !cq_owned(q))
        return CHIRON_QUP_BLOCKED;
    status = cq_read(q, CQ_STATE);
    if ((status & (CQ_VALID | CQ_MASTER)) != (CQ_VALID | CQ_MASTER))
        return CHIRON_QUP_UNSUPPORTED;
    status = cq_read(q, CQ_STATUS);
    if ((status & (CQ_BUS_ACTIVE | 0x200u | CQ_STATUS_ERRORS)) ||
        (status & CQ_LINES_HIGH) != CQ_LINES_HIGH)
        return CHIRON_QUP_BUS_ERROR;
    modes = cq_read(q, CQ_IO_MODES);
    q->output_bytes = block_bytes[modes & 3u] * (2u << ((modes >> 2) & 3u));
    q->input_bytes = block_bytes[(modes >> 5) & 3u] * (2u << ((modes >> 7) & 3u));
    if (!q->input_bytes || !q->output_bytes)
        return CHIRON_QUP_UNSUPPORTED;
    /* Source: i2c_msm_qup_init(), then the per-transfer FIFO setup. */
    cq_write(q, CQ_SW_RESET, 1);
    result = cq_wait_state(q, 0, 0);
    if (result)
        return cq_fail(q, result);
    cq_write(q, CQ_CONFIG, 0x207); /* mini-core I2C, N=7 */
    cq_write(q, CQ_ERROR_ENABLE, CQ_FIFO_ERRORS);
    cq_write(q, CQ_OP_MASK, CQ_INPUT_SERVICE | CQ_OUTPUT_SERVICE);
    cq_write(q, CQ_MASTER_CONFIG, 1); /* version-two tags */
    if (!(cq_read(q, CQ_STATE) & CQ_MASTER))
        return cq_fail(q, CHIRON_QUP_UNSUPPORTED);
    q->ready = 1;
    return 0;
}

static int
cq_transfer(struct chiron_qup *q, const uint8_t *tags, size_t output,
    uint8_t *data, size_t input)
{
    uint8_t staged[80] = {0};
    uint32_t config, modes;
    int result;
    if (!q->ready || !cq_owned(q)) {
        q->ready = 0;
        return CHIRON_QUP_BLOCKED;
    }
    /* Like upstream choose_mode(), require strictly less than FIFO capacity.
     * Never split a touch snapshot or silently switch to BLOCK/DMA mode. */
    if (output >= q->output_bytes || (input && input + 2 >= q->input_bytes))
        return CHIRON_QUP_UNSUPPORTED;
    result = cq_error(q);
    if (result)
        return cq_fail(q, result);
    if (cq_read(q, CQ_STATUS) & (CQ_BUS_ACTIVE | 0x200u))
        return cq_fail(q, CHIRON_QUP_BUS_ERROR);
    result = cq_state(q, 0);
    if (result)
        return cq_fail(q, result);
    cq_write(q, CQ_MX_INPUT, 0);
    cq_write(q, CQ_MX_OUTPUT, 0);
    cq_write(q, CQ_MX_READ, input ? (uint32_t)input + 2 : 0);
    cq_write(q, CQ_MX_WRITE, (uint32_t)output);
    config = cq_read(q, CQ_CONFIG);
    config = (config & ~(0xf00u | 0x1fu | 0xc0u)) | 0x207u | (input ? 0 : 0x80u);
    cq_write(q, CQ_CONFIG, config);
    modes = cq_read(q, CQ_IO_MODES);
    cq_write(q, CQ_IO_MODES, (modes & ~0x1fc00u) | 0xc000u);
    cq_write(q, CQ_OP_MASK, 0);
    result = cq_state(q, 1);
    if (result)
        return cq_fail(q, result);
    cq_write(q, CQ_CLOCK, (14u << 16) | 28u); /* source's 400 kHz divider */
    result = cq_state(q, 3);
    if (result)
        return cq_fail(q, result);
    for (size_t at = 0; at < output; at += 4) {
        uint32_t word = 0;
        for (size_t b = 0; b < 4 && at + b < output; b++)
            word |= (uint32_t)tags[at + b] << (8 * b);
        cq_write(q, CQ_OUT_FIFO, word);
    }
    result = cq_state(q, 1);
    if (result)
        return cq_fail(q, result);
    /* Bounded polling replaces completion IRQs; GIC must stay masked. */
    unsigned done = 0;
    uint64_t start = q->io.now_us(q->io.context);
    for (unsigned i = 0; i < 5000; i++) {
        if (q->io.now_us(q->io.context) - start >= 500000)
            break;
        result = cq_error(q);
        if (result)
            return cq_fail(q, result);
        uint32_t op = cq_read(q, CQ_OPERATIONAL);
        done = input ? (op & CQ_INPUT_DONE) : (op & (CQ_OUTPUT_SERVICE | CQ_OUTPUT_DONE));
        if (op & (CQ_INPUT_SERVICE | CQ_OUTPUT_SERVICE))
            cq_write(q, CQ_OPERATIONAL, op & (CQ_INPUT_SERVICE | CQ_OUTPUT_SERVICE));
        if (done)
            break;
        q->io.delay_us(q->io.context, 100);
    }
    if (!done)
        return cq_fail(q, CHIRON_QUP_TIMEOUT);
    unsigned idle = 0;
    start = q->io.now_us(q->io.context);
    for (unsigned i = 0; i < 5000; i++) {
        if (q->io.now_us(q->io.context) - start >= 500000)
            break;
        result = cq_error(q);
        if (result)
            return cq_fail(q, result);
        if (!(cq_read(q, CQ_STATUS) & CQ_BUS_ACTIVE)) {
            idle = 1;
            break;
        }
        q->io.delay_us(q->io.context, 100);
    }
    if (!idle)
        return cq_fail(q, CHIRON_QUP_TIMEOUT);
    if (input) {
        /* The pinned driver discards exactly two input-tag bytes per read. */
        for (size_t at = 0; at < input + 2; at += 4) {
            if (!(cq_read(q, CQ_OPERATIONAL) & CQ_INPUT_NOT_EMPTY))
                return cq_fail(q, CHIRON_QUP_FIFO_ERROR);
            uint32_t word = cq_read(q, CQ_IN_FIFO);
            for (size_t b = 0; b < 4 && at + b < input + 2; b++)
                if (at + b >= 2)
                    staged[at + b - 2] = (uint8_t)(word >> (8 * b));
        }
        if (cq_read(q, CQ_OPERATIONAL) & CQ_INPUT_NOT_EMPTY)
            return cq_fail(q, CHIRON_QUP_FIFO_ERROR);
    }
    result = cq_error(q);
    if (!result)
        result = cq_state(q, 0);
    if (result)
        return cq_fail(q, result);
    for (size_t i = 0; i < input; i++)
        data[i] = staged[i];
    return input ? (int)input : 0;
}

int
chiron_qup_select_page(void *context, uint8_t slave, uint8_t page)
{
    struct chiron_qup *q = context;
    if (slave != 0x20 || page >= 5)
        return CHIRON_QUP_UNSUPPORTED;
    const uint8_t tags[6] = {0x81, 0x40, 0x83, 2, 0xff, page};
    return cq_transfer(q, tags, sizeof(tags), NULL, 0);
}

int
chiron_qup_read(void *context, uint8_t slave, uint8_t reg, uint8_t *data, size_t size)
{
    struct chiron_qup *q = context;
    if (slave != 0x20 || !data || !size || size > 80 || size > 0xffu - reg)
        return CHIRON_QUP_UNSUPPORTED;
    /* Pointer write without STOP, repeated START with read address, read+STOP. */
    const uint8_t tags[9] = {0x81, 0x40, 0x82, 1, reg, 0x81, 0x41, 0x87, (uint8_t)size};
    return cq_transfer(q, tags, sizeof(tags), data, size);
}

int
chiron_qup_write_byte(void *context, uint8_t slave, uint8_t reg, uint8_t value)
{
    struct chiron_qup *q = context;
    if (slave != 0x20 || reg == 0xff)
        return CHIRON_QUP_UNSUPPORTED;
    const uint8_t tags[6] = {0x81, 0x40, 0x83, 2, reg, value};
    return cq_transfer(q, tags, sizeof(tags), NULL, 0);
}
