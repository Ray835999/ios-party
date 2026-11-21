/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Read-only probe from pinned clock-gcc-8998.c, msm-clocks-hwio-8998.h,
 * clock-local2.c, pinctrl-msm8998.c, chiron DT and existing GICv3 implementation.
 */
#include "chiron_board.h"

static const uint64_t cb_addresses[CB_REGISTER_COUNT] = {
    CHIRON_GCC_BASE + 0x17000, CHIRON_GCC_BASE + 0x52004,
    CHIRON_GCC_BASE + 0x17004, CHIRON_GCC_BASE + 0x21008,
    CHIRON_GCC_BASE + 0x21020, CHIRON_GCC_BASE + 0x21024,
    CHIRON_GPIO_BASE, CHIRON_GPIO_BASE + 0x1000,
    CHIRON_GPIO_BASE + 0x2000, CHIRON_GPIO_BASE + 0x2004,
    CHIRON_GIC_BASE + 0x110, /* ISENABLER[131 / 32]; SPI99 + 32 = 131 */
    CHIRON_QUP_BASE + 4, CHIRON_QUP_BASE + 0x404
};

static const char *const cb_names[CB_REGISTER_COUNT] = {
    "BLSP1_BCR", "APCS_BRANCH_VOTE", "BLSP1_AHB_CBCR", "QUP5_I2C_CBCR",
    "QUP5_I2C_CMD_RCGR", "QUP5_I2C_CFG_RCGR", "GPIO87_CFG", "GPIO88_CFG",
    "GPIO89_CFG", "GPIO89_IO", "GICD_ISENABLER4", "QUP_STATE", "QUP_I2C_STATUS"
};

uint64_t
chiron_board_register_address(unsigned index)
{
    return index < CB_REGISTER_COUNT ? cb_addresses[index] : 0;
}

const char *
chiron_board_register_name(unsigned index)
{
    return index < CB_REGISTER_COUNT ? cb_names[index] : "unknown";
}

static uint64_t
cb_le(const uint8_t *p, unsigned size)
{
    uint64_t result = 0;
    for (unsigned i = 0; i < size; i++)
        result |= (uint64_t)p[i] << (8 * i);
    return result;
}

static int
cb_property(const struct chiron_board_io *io, const char *path, const char *name,
    const uint8_t **bytes, size_t wanted)
{
    const void *data = NULL;
    size_t size = 0;
    if (!io->property || io->property(io->context, path, name, &data, &size) ||
        !data || size != wanted)
        return 0;
    *bytes = data;
    return 1;
}

int
chiron_board_identity(const struct chiron_board_io *io, struct chiron_board_report *r)
{
    const uint8_t *type, *ranges, *gic, *rotation, *handoff;
    static const uint8_t expected[7] = {'c', 'h', 'i', 'r', 'o', 'n', 0};
    *r = (struct chiron_board_report){.issue = CB_PLATFORM, .failed_register = -1};
    if (!io || !cb_property(io, "/arm-io", "device_type", &type, sizeof(expected)) ||
        !cb_property(io, "/arm-io", "ranges", &ranges, 24) ||
        !cb_property(io, "/arm-io/gic", "reg", &gic, 32) ||
        !cb_property(io, "/chosen", "display-rotation", &rotation, 4))
        return CB_PLATFORM;
    for (unsigned i = 0; i < sizeof(expected); i++)
        if (type[i] != expected[i])
            return CB_PLATFORM;
    if (cb_le(ranges, 8) != 0 || cb_le(ranges + 8, 8) != 0x01000000 ||
        cb_le(ranges + 16, 8) != 0xff000000 ||
        cb_le(gic, 8) != 0x16a00000 || cb_le(gic + 8, 8) != 0x10000 ||
        cb_le(gic + 16, 8) != 0x16b00000 || cb_le(gic + 24, 8) != 0x100000 ||
        cb_le(rotation, 4) != 0)
        return CB_PLATFORM;
    r->platform_valid = 1;
    if (cb_property(io, "/chosen", "chiron-touch-handoff", &handoff, 16)) {
        for (unsigned i = 0; i < 4; i++)
            r->handoff[i] = (uint32_t)cb_le(handoff + 4 * i, 4);
        r->owner_assumed = r->handoff[0] == CHIRON_TOUCH_HANDOFF_VERSION &&
            r->handoff[1] == CHIRON_TOUCH_DIRECT_BOOT_CPU &&
            r->handoff[2] == CHIRON_TOUCH_OTHER_CPUS_OFF &&
            r->handoff[3] == CHIRON_TOUCH_FIRMWARE_RESIDENT;
    }
    r->issue = CB_READY;
    return 0;
}

static int
cb_read_group(const struct chiron_board_io *io, struct chiron_board_report *r,
    unsigned first, unsigned last)
{
    for (unsigned i = first; i <= last; i++) {
        uint32_t value;
        if (!io->read32 || io->read32(io->context, cb_addresses[i], &value)) {
            r->issue = CB_READ_FAILED;
            r->failed_register = (int)i;
            return r->issue;
        }
        r->values[i] = value;
        r->observed |= 1u << i;
    }
    return 0;
}

static int
cb_check(struct chiron_board_report *r, unsigned index, uint32_t mask,
    uint32_t expected, int issue)
{
    if ((r->values[index] & mask) == expected)
        return 0;
    r->failed_register = (int)index;
    r->expected_mask = mask;
    r->expected_value = expected;
    r->issue = issue;
    return issue;
}

static int
cb_probe(const struct chiron_board_io *io, struct chiron_board_report *r, int idle)
{
    int status = chiron_board_identity(io, r);
    if (status)
        return status;
#define CB_READ(first, last) do { \
    status = cb_read_group(io, r, first, last); if (status) return status; \
} while (0)
#define CB_CHECK(index, mask, expected, issue) do { \
    status = cb_check(r, index, mask, expected, issue); if (status) return status; \
} while (0)
    /* Read always-on GCC first; never access QUP while its clocks are off. */
    CB_READ(CB_BLSP_RESET, CB_ROOT_CONFIG);
    CB_CHECK(CB_BLSP_RESET, 1u, 0, CB_CLOCK);
    CB_CHECK(CB_AHB_VOTE, 1u << 17, 1u << 17, CB_CLOCK);
    CB_CHECK(CB_AHB_CLOCK, 1u << 31, 0, CB_CLOCK);
    CB_CHECK(CB_QUP_CLOCK, 0x80000003u, 1u, CB_CLOCK);
    CB_CHECK(CB_ROOT_COMMAND, 0x800000f1u, 0, CB_CLOCK);
    /* XO source 0, half-integer divider encoding 1 (divide by 1), MND off. */
    CB_CHECK(CB_ROOT_CONFIG, 0x371fu, 1u, CB_CLOCK);
    CB_READ(CB_SDA_MUX, CB_RESET_LEVEL);
    /* pinctrl-msm8998: EAST GPIO87/88 function index 3 = blsp_i2c5. */
    CB_CHECK(CB_SDA_MUX, 0x3cu, 3u << 2, CB_PINMUX);
    CB_CHECK(CB_SCL_MUX, 0x3cu, 3u << 2, CB_PINMUX);
    CB_CHECK(CB_RESET_CONFIG, 0x23cu, 1u << 9, CB_RESET);
    CB_CHECK(CB_RESET_LEVEL, 3u, 3u, CB_RESET);
    CB_READ(CB_QUP_IRQ_ENABLE, CB_QUP_IRQ_ENABLE);
    CB_CHECK(CB_QUP_IRQ_ENABLE, 1u << 3, 0, CB_IRQ);
    /* A firmware CPU could gate QUP after the clock snapshot. Do not read
     * even STATE/STATUS until the direct-handoff ownership check passes. */
    if (!r->owner_assumed)
        return r->issue = CB_OWNERSHIP;
    if (idle) {
        CB_READ(CB_QUP_STATE, CB_QUP_STATUS);
        CB_CHECK(CB_QUP_STATE, 0x14u, 0x14u, CB_CONTROLLER);
        CB_CHECK(CB_QUP_STATE, 2u, 0, CB_CONTROLLER);
        CB_CHECK(CB_QUP_STATUS, 0x0f8003fcu, 3u << 26, CB_CONTROLLER);
    }
    r->issue = CB_READY;
#undef CB_READ
#undef CB_CHECK
    return 0;
}

int
chiron_board_probe(const struct chiron_board_io *io, struct chiron_board_report *r)
{
    return cb_probe(io, r, 1);
}

int
chiron_board_retained(const struct chiron_board_io *io, struct chiron_board_report *r)
{
    return cb_probe(io, r, 0);
}
