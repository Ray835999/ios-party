/*
 * Bounded DSX groundwork. No MMIO, reset, regulator or firmware operations.
 * Layout validation and a checked native-I2C handoff are prerequisites.
 */
#ifndef CHIRON_DSX_H
#define CHIRON_DSX_H

#include <stddef.h>
#include <stdint.h>

#define CHIRON_DSX_ADDRESS 0x20
#define CHIRON_DSX_PAGES 5
#define CHIRON_DSX_CONTACTS 10
#define CHIRON_DSX_OBJECT_BYTES 8
#define CHIRON_DSX_DOWN 1
#define CHIRON_DSX_MOVE 2
#define CHIRON_DSX_UP 3

enum chiron_dsx_status {
    CHIRON_DSX_OK = 0,
    CHIRON_DSX_FRAME_CHANGED = 1, /* observer only: discarded snapshot, poll continues */
    CHIRON_DSX_NO_TRANSPORT = -1,
    CHIRON_DSX_IO = -2,
    CHIRON_DSX_UNKNOWN = -3,
    CHIRON_DSX_UNVERIFIED_LAYOUT = -4,
    CHIRON_DSX_BAD_FRAME = -5,
    CHIRON_DSX_INVALID = -6
};

struct chiron_dsx_function {
    uint16_t query, command, control, data;
    uint8_t number, version;
};

struct chiron_dsx_functions {
    struct chiron_dsx_function f01, f12;
};

/*
 * Only an audited platform validator may supply this after reading the actual
 * F12 query/control registers. PDT alone proves none of these fields. The format
 * describes the eight-byte object prefix used by the local Synaptics DXE;
 * multiple contiguous objects require independent query/control evidence.
 * max_x/max_y are inclusive sensor maxima, never framebuffer dimensions.
 */
struct chiron_dsx_layout {
    struct chiron_dsx_function f12;
    uint16_t data1, max_x, max_y;
    uint8_t object_bytes, contacts, flip; /* flip bit 0: X; bit 1: Y */
    uint16_t data15;
    uint8_t data15_bytes;
};

struct chiron_dsx_bus {
    void *context;
    /* Only the protocol's {0xff, page} selector write; 0 on exact success. */
    int (*select_page)(void *, uint8_t slave, uint8_t page);
    /* Combined one-byte register-pointer write + read. Return bytes or < 0. */
    int (*read)(void *, uint8_t slave, uint8_t reg, uint8_t *, size_t);
    /*
     * Called before every snapshot: check F01 readiness and the actual F12
     * query/control state. No fallback layout or boot-argument overrides.
     */
    int (*validate_layout)(void *, const struct chiron_dsx_functions *,
        struct chiron_dsx_layout *);
};

typedef void (*chiron_dsx_post)(void *, uint32_t type, uint32_t finger,
    uint32_t x, uint32_t y);

struct chiron_dsx;
enum chiron_dsx_observed {
    CHIRON_DSX_OBS_BEFORE = 1, CHIRON_DSX_OBS_FRAME = 2,
    CHIRON_DSX_OBS_AFTER = 4, CHIRON_DSX_OBS_F01 = 8
};
/* Observes existing reads only. Frame bytes are before Data15 filtering. */
typedef void (*chiron_dsx_observer)(const struct chiron_dsx *, const uint8_t *,
    size_t, uint16_t before, uint16_t after, uint8_t f01, unsigned observed, int status);

struct chiron_dsx_contact {
    uint32_t x, y;
    uint8_t down;
};

struct chiron_dsx {
    struct chiron_dsx_bus bus;
    chiron_dsx_post post;
    void *post_context;
    struct chiron_dsx_functions functions;
    struct chiron_dsx_layout layout;
    struct chiron_dsx_contact contact[CHIRON_DSX_CONTACTS];
    uint8_t discovered, ready, changed_frames, io_errors;
    chiron_dsx_observer observer;
};

void chiron_dsx_init(struct chiron_dsx *, const struct chiron_dsx_bus *,
    chiron_dsx_post, void *);
int chiron_dsx_discover(struct chiron_dsx *);
int chiron_dsx_start(struct chiron_dsx *);
int chiron_dsx_frame(struct chiron_dsx *, const uint8_t *, size_t);
int chiron_dsx_poll(struct chiron_dsx *);
/* Idempotent UPs at the last valid positions, followed by refusal to poll. */
void chiron_dsx_stop(struct chiron_dsx *);

#endif
