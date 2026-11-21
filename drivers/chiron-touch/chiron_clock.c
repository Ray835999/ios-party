/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Bounded subset of clock-gcc-8998.c / clock-local2.c at LineageOS commit
 * 7231883a368604f90806e167890e00f515a9927c. See the clock evidence document.
 * Original clock protocols: Copyright (c) 2012-2017, 2020,
 * The Linux Foundation. All rights reserved.
 * No reset, force-root, PLL, RPM, pinctrl or QUP controller writes.
 */
#include "chiron_clock.h"

#define CC_VOTE_BIT (1u << 17)
#define CC_STATUS 0xf0000000u
#define CC_OFF 0x80000000u
#define CC_CFG_MASK 0x371fu
#define CC_HID_MASK 0x71fu
#define CC_CMD_ALLOWED 0x800000f3u
#define CC_DIRTY 0xf0u

struct cc_state {
    const struct chiron_clock_io *io;
    struct chiron_clock_report *r;
    uint64_t last_time;
    int lost;
};

static int
cc_owned(struct cc_state *s)
{
    struct chiron_board_report identity;
    if (s->lost)
        return CB_OWNERSHIP;
    int result = chiron_board_identity(&s->io->board, &identity);
    for (unsigned i = 0; i < 4; i++)
        s->r->after.handoff[i] = identity.handoff[i];
    s->r->after.platform_valid = identity.platform_valid;
    s->r->after.owner_assumed = identity.owner_assumed;
    if (result || !identity.owner_assumed) {
        s->lost = 1;
        return result ? result : CB_OWNERSHIP;
    }
    return 0;
}

static int
cc_read(struct cc_state *s, unsigned reg, uint32_t *value)
{
    if (s->io->board.read32(s->io->board.context,
            chiron_board_register_address(reg), value)) {
        s->r->failed_register = (int)reg;
        return CB_READ_FAILED;
    }
    s->r->after.observed |= 1u << reg;
    s->r->after.values[reg] = *value;
    return 0;
}

static int
cc_snapshot(struct cc_state *s)
{
    int result = cc_owned(s);
    if (result)
        return result;
    for (unsigned i = CB_BLSP_RESET; i <= CB_ROOT_CONFIG; i++) {
        uint32_t value;
        if ((result = cc_read(s, i, &value)))
            return result;
    }
    return cc_owned(s);
}

static int
cc_on(uint32_t value)
{
    return (value & CC_STATUS) == 0 || (value & CC_STATUS) == 0x20000000u;
}

static int
cc_mismatch(struct cc_state *s, unsigned reg, int result)
{
    s->r->failed_register = (int)reg;
    return result;
}

/* A matching active clock is accepted without writes. Only an idle/off
 * branch with the captured reset/XO profile (or configured XO) can change. */
static int
cc_profile(struct cc_state *s, int *active)
{
    const uint32_t *v = s->r->after.values;
    *active = 0;
    if (v[CB_BLSP_RESET] & 1u)
        return cc_mismatch(s, CB_BLSP_RESET, CHIRON_CLOCK_PROFILE);
    if (v[CB_ROOT_COMMAND] & ~CC_OFF)
        return cc_mismatch(s, CB_ROOT_COMMAND, CHIRON_CLOCK_PROFILE);
    uint32_t cfg = v[CB_ROOT_CONFIG] & CC_CFG_MASK;
    if (cfg != 0 && cfg != 1)
        return cc_mismatch(s, CB_ROOT_CONFIG, CHIRON_CLOCK_PROFILE);
    if (!cc_on(v[CB_AHB_CLOCK]) &&
        (v[CB_AHB_CLOCK] & CC_STATUS) != CC_OFF)
        return cc_mismatch(s, CB_AHB_CLOCK, CHIRON_CLOCK_PROFILE);
    if ((v[CB_AHB_VOTE] & CC_VOTE_BIT) && !cc_on(v[CB_AHB_CLOCK]))
        return cc_mismatch(s, CB_AHB_CLOCK, CHIRON_CLOCK_PROFILE);
    if ((v[CB_QUP_CLOCK] & 3u) == 0 &&
        (v[CB_QUP_CLOCK] & CC_STATUS) == CC_OFF &&
        v[CB_ROOT_COMMAND] == CC_OFF)
        return 0;
    if ((v[CB_QUP_CLOCK] & 3u) != 1 || !cc_on(v[CB_QUP_CLOCK]))
        return cc_mismatch(s, CB_QUP_CLOCK, CHIRON_CLOCK_PROFILE);
    if (v[CB_ROOT_COMMAND])
        return cc_mismatch(s, CB_ROOT_COMMAND, CHIRON_CLOCK_PROFILE);
    if (cfg != 1)
        return cc_mismatch(s, CB_ROOT_CONFIG, CHIRON_CLOCK_PROFILE);
    if (!(v[CB_AHB_VOTE] & CC_VOTE_BIT))
        return cc_mismatch(s, CB_AHB_VOTE, CHIRON_CLOCK_PROFILE);
    if (!cc_on(v[CB_AHB_CLOCK]))
        return cc_mismatch(s, CB_AHB_CLOCK, CHIRON_CLOCK_PROFILE);
    *active = 1;
    return 0;
}

/* Re-read the target and identity immediately before every RMW. Never restore
 * a whole saved register: other BLSP users may own unrelated vote bits. */
static int
cc_rmw(struct cc_state *s, unsigned reg, uint32_t mask, uint32_t old,
    uint32_t replacement, int command)
{
    uint32_t value, reset;
    int result = cc_owned(s);
    if (result)
        return result;
    if ((result = cc_read(s, CB_BLSP_RESET, &reset)))
        return result;
    if (reset & 1u)
        return cc_mismatch(s, CB_BLSP_RESET, CHIRON_CLOCK_CHANGED);
    if ((result = cc_read(s, reg, &value)))
        return result;
    if ((value & mask) != old ||
        (command && (value & ~CC_CMD_ALLOWED)))
        return cc_mismatch(s, reg, CHIRON_CLOCK_CHANGED);
    if ((result = cc_owned(s)))
        return result;
    uint32_t next = (value & ~mask) | replacement;
    s->r->changed |= 1u << reg;
    s->r->remaining |= 1u << reg;
    result = s->io->write32(s->io->board.context,
        chiron_board_register_address(reg), next);
    s->io->barrier(s->io->board.context);
    if (result)
        return cc_mismatch(s, reg, CHIRON_CLOCK_WRITE);
    if ((result = cc_owned(s)) || (result = cc_read(s, reg, &value)))
        return result;
    /* UPDATE self-clears; its completion is checked separately. */
    if ((!command && (value & mask) != replacement) ||
        (command && ((value & 2u) || (value & ~CC_CMD_ALLOWED))))
        return cc_mismatch(s, reg, CHIRON_CLOCK_READBACK);
    return 0;
}

enum cc_wait { CC_WAIT_AHB, CC_WAIT_BRANCH_ON, CC_WAIT_BRANCH_OFF,
    CC_WAIT_UPDATE, CC_WAIT_UPDATE_IDLE, CC_WAIT_ROOT_ON, CC_WAIT_ROOT_OFF };

static int
cc_time(struct cc_state *s, uint64_t start)
{
    uint64_t now = s->io->now_us(s->io->board.context);
    if (now < s->last_time)
        return CHIRON_CLOCK_TIMER;
    s->last_time = now;
    return now - start >= CHIRON_CLOCK_WAIT_US ? CHIRON_CLOCK_TIMEOUT : 0;
}

static int
cc_wait(struct cc_state *s, enum cc_wait kind)
{
    uint64_t start = s->io->now_us(s->io->board.context);
    if (start < s->last_time)
        return CHIRON_CLOCK_TIMER;
    s->last_time = start;
    unsigned reg = kind == CC_WAIT_AHB ? CB_AHB_CLOCK :
        (kind == CC_WAIT_BRANCH_ON || kind == CC_WAIT_BRANCH_OFF) ?
        CB_QUP_CLOCK : CB_ROOT_COMMAND;
    s->r->failed_register = (int)reg;
    for (unsigned i = 0; i < CHIRON_CLOCK_WAIT_ITERATIONS; i++) {
        uint32_t value;
        int result = cc_time(s, start);
        if (result || (result = cc_owned(s)) ||
            (result = cc_read(s, reg, &value)) ||
            (result = cc_time(s, start)))
            return result;
        int done;
        if (kind == CC_WAIT_AHB || kind == CC_WAIT_BRANCH_ON)
            done = cc_on(value);
        else if (kind == CC_WAIT_BRANCH_OFF)
            done = (value & CC_STATUS) == CC_OFF;
        else {
            if ((value & ~CC_CMD_ALLOWED) || (value & 2u))
                return CHIRON_CLOCK_CHANGED;
            done = kind == CC_WAIT_UPDATE ? !(value & (CC_DIRTY | 1u)) :
                kind == CC_WAIT_UPDATE_IDLE ? !(value & 1u) :
                kind == CC_WAIT_ROOT_ON ? !(value & CC_OFF) : !!(value & CC_OFF);
        }
        if (done) {
            if ((result = cc_owned(s)))
                return result;
            return cc_time(s, start);
        }
        s->io->delay_us(s->io->board.context, 1);
    }
    return CHIRON_CLOCK_TIMEOUT;
}

/* Revalidate all six GCC registers at stage boundaries. In particular a
 * clock which regressed after a successful halt check must not be ignored. */
static int
cc_off_stage(struct cc_state *s, uint32_t cfg)
{
    int active, result = cc_snapshot(s);
    if (result || (result = cc_profile(s, &active)))
        return result;
    const uint32_t *v = s->r->after.values;
    if (active)
        return cc_mismatch(s, CB_QUP_CLOCK, CHIRON_CLOCK_CHANGED);
    if (!(v[CB_AHB_VOTE] & CC_VOTE_BIT))
        return cc_mismatch(s, CB_AHB_VOTE, CHIRON_CLOCK_CHANGED);
    if (!cc_on(v[CB_AHB_CLOCK]))
        return cc_mismatch(s, CB_AHB_CLOCK, CHIRON_CLOCK_CHANGED);
    if ((v[CB_ROOT_CONFIG] & CC_CFG_MASK) != cfg)
        return cc_mismatch(s, CB_ROOT_CONFIG, CHIRON_CLOCK_CHANGED);
    return 0;
}

static int
cc_update(struct cc_state *s)
{
    /* Source set_rate_hid -> rcg_update_config. No force-root for this RCG:
     * non_local_children/non_local_control_timeout/force_enable_rcgr are 0. */
    return cc_rmw(s, CB_ROOT_COMMAND, 3u, 0, 1u, 1);
}

static int
cc_cleanup(struct cc_state *s)
{
    uint32_t value;
    int result;
#define CC_CLEAN(step_, expr) do { \
    s->r->cleanup_step = (step_); \
    if ((result = (expr))) return result; \
} while (0)
    CC_CLEAN(CC_IDENTITY, cc_owned(s));
    if (s->r->remaining & (1u << CB_QUP_CLOCK)) {
        CC_CLEAN(CC_BRANCH, cc_read(s, CB_QUP_CLOCK, &value));
        if ((value & 3u) == 1) {
            /* branch_clk_disable: clear only branch enable, barrier, HALT. */
            CC_CLEAN(CC_BRANCH, cc_rmw(s, CB_QUP_CLOCK, 3u, 1u, 0, 0));
        } else if (value & 3u)
            return CHIRON_CLOCK_CHANGED;
        CC_CLEAN(CC_BRANCH_ON, cc_wait(s, CC_WAIT_BRANCH_OFF));
        CC_CLEAN(CC_ROOT_ON, cc_wait(s, CC_WAIT_ROOT_OFF));
        s->r->remaining &= ~(1u << CB_QUP_CLOCK);
    }
    if (s->r->remaining & ((1u << CB_ROOT_CONFIG) | (1u << CB_ROOT_COMMAND))) {
        /* Never cancel or overwrite a live UPDATE transaction. If it is
         * stuck, leave the owned vote/config recorded as incomplete. */
        CC_CLEAN(CC_UPDATE_DONE, cc_wait(s, CC_WAIT_UPDATE_IDLE));
        CC_CLEAN(CC_BRANCH, cc_read(s, CB_QUP_CLOCK, &value));
        if ((value & 3u) || (value & CC_STATUS) != CC_OFF)
            return CHIRON_CLOCK_CHANGED;
        CC_CLEAN(CC_ROOT_ON, cc_wait(s, CC_WAIT_ROOT_OFF));
        CC_CLEAN(CC_CONFIG, cc_read(s, CB_ROOT_CONFIG, &value));
        uint32_t original = s->r->before.values[CB_ROOT_CONFIG] & CC_HID_MASK;
        if ((value & CC_CFG_MASK) == 1) {
            CC_CLEAN(CC_CONFIG, cc_rmw(s, CB_ROOT_CONFIG,
                CC_HID_MASK, 1u, original, 0));
        } else if ((value & CC_CFG_MASK) != original)
            return CHIRON_CLOCK_CHANGED;
        /* Re-latch even if a failed write left CFG looking unchanged. */
        CC_CLEAN(CC_UPDATE, cc_update(s));
        CC_CLEAN(CC_UPDATE_DONE, cc_wait(s, CC_WAIT_UPDATE));
        CC_CLEAN(CC_ROOT_ON, cc_wait(s, CC_WAIT_ROOT_OFF));
        CC_CLEAN(CC_CONFIG, cc_read(s, CB_ROOT_CONFIG, &value));
        if ((value & CC_CFG_MASK) != original)
            return CHIRON_CLOCK_READBACK;
        s->r->remaining &= ~((1u << CB_ROOT_CONFIG) | (1u << CB_ROOT_COMMAND));
    }
    if (s->r->remaining & (1u << CB_AHB_VOTE)) {
        CC_CLEAN(CC_VOTE, cc_read(s, CB_AHB_VOTE, &value));
        if (value & CC_VOTE_BIT)
            CC_CLEAN(CC_VOTE, cc_rmw(s, CB_AHB_VOTE,
                CC_VOTE_BIT, CC_VOTE_BIT, 0, 0));
        /* local_vote_clk_disable has no halt wait: other voters may keep
         * AHB on. Never write the shared BLSP1 AHB CBCR or BCR. */
        s->r->remaining &= ~(1u << CB_AHB_VOTE);
    }
#undef CC_CLEAN
    return 0;
}

int
chiron_clock_prepare(const struct chiron_clock_io *io, struct chiron_clock_report *r)
{
    int result, active;
    if (!r)
        return CHIRON_CLOCK_BAD_IO;
    *r = (struct chiron_clock_report){.result = CHIRON_CLOCK_BAD_IO,
        .failed_register = -1, .cleanup_register = -1};
    if (!io || !io->board.property || !io->board.read32 || !io->write32 ||
        !io->barrier || !io->now_us || !io->delay_us)
        return r->result;
    struct cc_state s = {.io = io, .r = r};
    result = chiron_board_identity(&io->board, &r->before);
    r->after = r->before;
    if (result || !r->before.owner_assumed) {
        r->result = result ? result : CB_OWNERSHIP;
        return r->result;
    }
#define CC_RUN(step_, expr) do { \
    r->step = (step_); \
    if ((result = (expr))) goto failed; \
} while (0)
    CC_RUN(CC_SNAPSHOT, cc_snapshot(&s));
    r->before = r->after;
    CC_RUN(CC_PROFILE, cc_profile(&s, &active));
    if (!active) {
        if (!(r->before.values[CB_AHB_VOTE] & CC_VOTE_BIT))
            CC_RUN(CC_VOTE, cc_rmw(&s, CB_AHB_VOTE,
                CC_VOTE_BIT, 0, CC_VOTE_BIT, 0));
        CC_RUN(CC_AHB_ON, cc_wait(&s, CC_WAIT_AHB));
        uint32_t cfg = r->before.values[CB_ROOT_CONFIG] & CC_CFG_MASK;
        CC_RUN(CC_CONFIG, cc_off_stage(&s, cfg));
        if (cfg != 1) {
            CC_RUN(CC_CONFIG, cc_rmw(&s, CB_ROOT_CONFIG, CC_HID_MASK, cfg, 1, 0));
            CC_RUN(CC_UPDATE, cc_update(&s));
            CC_RUN(CC_UPDATE_DONE, cc_wait(&s, CC_WAIT_UPDATE));
            CC_RUN(CC_UPDATE_DONE, cc_wait(&s, CC_WAIT_ROOT_OFF));
        }
        CC_RUN(CC_BRANCH, cc_off_stage(&s, 1));
        CC_RUN(CC_BRANCH, cc_rmw(&s, CB_QUP_CLOCK, 3u, 0, 1u, 0));
        CC_RUN(CC_BRANCH_ON, cc_wait(&s, CC_WAIT_BRANCH_ON));
        CC_RUN(CC_ROOT_ON, cc_wait(&s, CC_WAIT_ROOT_ON));
    }
    CC_RUN(CC_FINAL, cc_snapshot(&s));
    CC_RUN(CC_FINAL, cc_profile(&s, &active));
    if (!active) {
        result = CHIRON_CLOCK_CHANGED;
        goto failed;
    }
    r->step = CC_DONE;
    r->failed_register = -1;
    r->result = 0;
    return 0;
failed:
    r->result = result;
    int failed_register = r->failed_register;
    if (r->remaining) {
        r->cleanup_result = cc_cleanup(&s);
        r->cleanup_register = r->cleanup_result ? r->failed_register : -1;
        r->rollback = s.lost ? CC_ROLLBACK_OWNER_LOST :
            r->cleanup_result ? CC_ROLLBACK_INCOMPLETE : CC_ROLLBACK_COMPLETE;
    }
    /* Best-effort final GCC snapshot; never replace the primary failure.
     * After ownership loss the sticky guard prevents further MMIO/writes. */
    (void)cc_snapshot(&s);
    r->failed_register = failed_register;
    return r->result;
#undef CC_RUN
}
