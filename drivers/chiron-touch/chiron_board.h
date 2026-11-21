#ifndef CHIRON_BOARD_H
#define CHIRON_BOARD_H

#include <stddef.h>
#include <stdint.h>
#include "chiron_touch_abi.h"

/* Pinned DT physical bases, GCC HWIO, pinctrl EAST tile and GICv3 offsets. */
#define CHIRON_GCC_BASE UINT64_C(0x00100000)
#define CHIRON_GCC_SIZE 0xb0000u
#define CHIRON_GPIO_BASE UINT64_C(0x03d57000) /* 0x03400000 + EAST + GPIO87*0x1000 */
#define CHIRON_GPIO_SIZE 0x3000u
#define CHIRON_GIC_BASE UINT64_C(0x17a00000)
#define CHIRON_GIC_SIZE 0x10000u
#define CHIRON_QUP_BASE UINT64_C(0x0c179000)
#define CHIRON_QUP_SIZE 0x600u

enum chiron_board_register {
    CB_BLSP_RESET, CB_AHB_VOTE, CB_AHB_CLOCK, CB_QUP_CLOCK, CB_ROOT_COMMAND,
    CB_ROOT_CONFIG, CB_SDA_MUX, CB_SCL_MUX, CB_RESET_CONFIG, CB_RESET_LEVEL,
    CB_QUP_IRQ_ENABLE, CB_QUP_STATE, CB_QUP_STATUS, CB_REGISTER_COUNT
};

enum chiron_board_issue {
    CB_READY = 0, CB_PLATFORM = -20, CB_READ_FAILED = -21,
    CB_CLOCK = -22, CB_PINMUX = -23, CB_RESET = -24, CB_IRQ = -25,
    CB_CONTROLLER = -26, CB_OWNERSHIP = -27
};

struct chiron_board_io {
    void *context;
    int (*property)(void *, const char *path, const char *name,
        const void **data, size_t *length);
    /* No write callback exists in this probe. Exact 32-bit device reads only. */
    int (*read32)(void *, uint64_t physical, uint32_t *value);
};

struct chiron_board_report {
    uint32_t handoff[4], observed, values[CB_REGISTER_COUNT];
    uint32_t expected_mask, expected_value;
    int issue, failed_register;
    uint8_t platform_valid, owner_assumed;
};

uint64_t chiron_board_register_address(unsigned);
const char *chiron_board_register_name(unsigned);
int chiron_board_identity(const struct chiron_board_io *, struct chiron_board_report *);
int chiron_board_probe(const struct chiron_board_io *, struct chiron_board_report *);
/* Rechecks retained clocks/pins/IRQ without requiring our own QUP to be idle. */
int chiron_board_retained(const struct chiron_board_io *, struct chiron_board_report *);

#endif
