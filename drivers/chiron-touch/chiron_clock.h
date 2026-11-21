#ifndef CHIRON_CLOCK_H
#define CHIRON_CLOCK_H

#include "chiron_board.h"

#define CHIRON_CLOCK_WAIT_US 500u
#define CHIRON_CLOCK_WAIT_ITERATIONS 500u

enum chiron_clock_result {
    CHIRON_CLOCK_BAD_IO = -60, CHIRON_CLOCK_PROFILE = -61,
    CHIRON_CLOCK_WRITE = -62, CHIRON_CLOCK_TIMEOUT = -63,
    CHIRON_CLOCK_READBACK = -64, CHIRON_CLOCK_TIMER = -65,
    CHIRON_CLOCK_CHANGED = -66
};

enum chiron_clock_step {
    CC_IDENTITY, CC_SNAPSHOT, CC_PROFILE, CC_VOTE, CC_AHB_ON,
    CC_CONFIG, CC_UPDATE, CC_UPDATE_DONE, CC_BRANCH, CC_BRANCH_ON,
    CC_ROOT_ON, CC_FINAL, CC_DONE
};

enum chiron_clock_rollback {
    CC_ROLLBACK_NONE, CC_ROLLBACK_COMPLETE, CC_ROLLBACK_INCOMPLETE,
    CC_ROLLBACK_OWNER_LOST
};

struct chiron_clock_io {
    struct chiron_board_io board;
    /* All callbacks use board.context. Failures may have reached hardware. */
    int (*write32)(void *, uint64_t physical, uint32_t value);
    /* Full device memory barrier, including after a failed write callback. */
    void (*barrier)(void *);
    uint64_t (*now_us)(void *);
    void (*delay_us)(void *, uint32_t);
};

struct chiron_clock_report {
    /* Clock-only snapshots: no GPIO, GIC or QUP controller reads. */
    struct chiron_board_report before, after;
    /* CB_* bitsets. changed includes attempted writes, even on IO failure.
     * remaining records possibly owned changes not proven rolled back. */
    uint32_t changed, remaining;
    int result, step, rollback;
    int cleanup_result, cleanup_step, failed_register, cleanup_register;
};

/* Caller must serialize calls and keep firmware/other CPUs quiescent.
 * This function does not enable itself, parse boot args, or start touch.
 * Parent must gate it on explicit normal-touch or clock-probe activation;
 * the read-only probe must never call it. */
int chiron_clock_prepare(const struct chiron_clock_io *, struct chiron_clock_report *);

#endif
