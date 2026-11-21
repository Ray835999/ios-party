#ifndef CHIRON_F12_H
#define CHIRON_F12_H

#include "chiron_dsx.h"

#define CHIRON_DSX_MEASURED_BUILD 2676394u

struct chiron_f12_refusal {
    const char *field;
    const char *expected;
    uint16_t address;
    uint8_t bytes;
    uint8_t actual[64];
};

struct chiron_f12_reader {
    void *context;
    /* Exact absolute-register read, returns byte count or a negative error. */
    int (*read)(void *, uint16_t, uint8_t *, size_t);
    uint8_t flip;
    struct chiron_f12_refusal refusal;
    /* Exact single-byte write; only the validated F01 Control0/F12 Ctrl28. */
    int (*write)(void *, uint16_t, uint8_t);
    uint8_t configuration_write_attempts;
};

/* Read-only validation of a restricted profile of the pinned vendor F12 ABI. */
int chiron_f12_validate(void *, const struct chiron_dsx_functions *,
    struct chiron_dsx_layout *);
/* First discovery only: initialize the measured 0x81 reset/unconfigured state. */
int chiron_f12_prepare(void *, const struct chiron_dsx_functions *,
    struct chiron_dsx_layout *);

#endif
