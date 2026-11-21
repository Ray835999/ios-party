#ifndef CHIRON_NATIVE_H
#define CHIRON_NATIVE_H

#include "chiron_qup.h"
#include "chiron_f12.h"

struct chiron_native {
    struct chiron_qup qup;
    struct chiron_f12_reader reader;
    uint8_t initialization_attempted;
    uint8_t page, last_operation; /* 0 page select, 1 read, 2 control write, 3 open */
    uint16_t last_address;
    size_t last_bytes;
    int last_result;
};

/*
 * Called by the retained-state board provider after its probe passes.
 * flip follows the measured chiron DT defaults; hardware remains experimental.
 */
int chiron_native_bind(struct chiron_native *, const struct chiron_qup_io *,
    uint8_t flip, struct chiron_dsx_bus *);

#endif
