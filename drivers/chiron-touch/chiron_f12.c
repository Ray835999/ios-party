/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Restricted subset of synaptics_dsx_force/synaptics_dsx_core.c at the pinned
 * MSM8998 commit. Original driver Copyright (C) 2012-2015 Synaptics Incorporated,
 * Copyright (C) 2018 XiaoMi, Inc. Register parsing is bounds checked here.
 */
#include "chiron_f12.h"

static int
cf_refuse(struct chiron_f12_reader *reader, const char *field, uint16_t address,
    const uint8_t *actual, size_t bytes, const char *expected)
{
    reader->refusal = (struct chiron_f12_refusal){
        .field = field, .expected = expected, .address = address
    };
    if (bytes > sizeof(reader->refusal.actual))
        bytes = sizeof(reader->refusal.actual);
    reader->refusal.bytes = (uint8_t)bytes;
    for (size_t i = 0; i < bytes; i++)
        reader->refusal.actual[i] = actual[i];
    return CHIRON_DSX_UNVERIFIED_LAYOUT;
}

static int
cf_read(struct chiron_f12_reader *reader, uint16_t base, unsigned offset,
    uint8_t *out, size_t length)
{
    if (!reader->read || (base >> 8) >= CHIRON_DSX_PAGES ||
        offset >= 0xffu - (base & 0xffu) ||
        length > 0xffu - (base & 0xffu) - offset) {
        uint8_t request[] = {(uint8_t)base, (uint8_t)(base >> 8),
            (uint8_t)offset, (uint8_t)(offset >> 8),
            (uint8_t)length, (uint8_t)(length >> 8)};
        return cf_refuse(reader, "register_read.bounds", base, request, sizeof(request),
            "base/offset/count (LE16) within supported pages and below page selector");
    }
    if (reader->read(reader->context, (uint16_t)(base + offset), out, length) != (int)length)
        return CHIRON_DSX_IO;
    return 0;
}

static unsigned
cf_present(const uint8_t *query5, unsigned control)
{
    return (query5[1 + control / 8] >> (control % 8)) & 1u;
}

static unsigned
cf_offset(const uint8_t *query5, unsigned control)
{
    unsigned result = 0;
    for (unsigned i = 0; i < control; i++)
        result += cf_present(query5, i);
    return result;
}

static int
cf_control23_size(const uint8_t *query5, const uint8_t *structure,
    size_t length, unsigned *size)
{
    size_t at = 0;
    /* Query6: register-size header, then 7-bit subpacket presence groups. */
    for (unsigned reg = 0; reg <= 23; reg++) {
        if (!cf_present(query5, reg))
            continue;
        if (at >= length)
            return CHIRON_DSX_UNVERIFIED_LAYOUT;
        uint8_t bytes = structure[at++];
        if (!bytes) {
            if (length - at < 2 || reg == 23)
                return CHIRON_DSX_UNVERIFIED_LAYOUT;
            at += 2; /* upstream's extended register-size header */
        }
        unsigned groups = 0, packets = 0;
        uint8_t bitmap;
        do {
            if (at >= length)
                return CHIRON_DSX_UNVERIFIED_LAYOUT;
            bitmap = structure[at++];
            if (reg == 23) {
                if (groups || (bitmap & 0x60u))
                    return CHIRON_DSX_UNVERIFIED_LAYOUT;
                packets = bitmap & 0x7fu;
            }
            groups++;
        } while (bitmap & 0x80u);
        if (reg == 23) {
            if ((packets & 3u) != 3u)
                return CHIRON_DSX_UNVERIFIED_LAYOUT;
            *size = 2 + ((packets >> 2) & 1u) + ((packets >> 3) & 1u) +
                ((packets >> 4) & 1u);
            if (bytes != *size)
                return CHIRON_DSX_UNVERIFIED_LAYOUT;
            return 0;
        }
    }
    return CHIRON_DSX_UNVERIFIED_LAYOUT;
}

static int
cf_validate(void *context, const struct chiron_dsx_functions *functions,
    struct chiron_dsx_layout *layout, int prepare)
{
    struct chiron_f12_reader *reader = context;
    uint8_t f01[21], build_id[3], status, device_control, q5_size, q8_size;
    uint8_t q5[9] = {0}, q6[64], q8[5] = {0};
    uint8_t control8[14], control23[5] = {0}, control28, control20[3];
    unsigned control23_size;
    int result;
    struct chiron_dsx_layout pending;
    *layout = (struct chiron_dsx_layout){0};
    if (reader)
        reader->refusal = (struct chiron_f12_refusal){0};
    if (!reader || !functions || functions->f01.number != 1 ||
        functions->f12.number != 0x12 || functions->f01.version ||
        functions->f12.version || reader->flip > 3) {
        if (reader)
            return cf_refuse(reader, "function descriptors/rotation", 0, NULL, 0,
                "F01=1, F12=0x12, versions=0, flip<=3");
        return CHIRON_DSX_UNVERIFIED_LAYOUT;
    }
#define CF_READ(base, offset, data, length) do { \
    result = cf_read(reader, base, offset, data, length); \
    if (result) return result; \
} while (0)
    CF_READ(functions->f01.data, 0, &status, 1);
    CF_READ(functions->f01.control, 0, &device_control, 1);
    int reset = prepare && status == 0x81;
    if (status != 0 && !reset)
        return cf_refuse(reader, "F01.Data0.status", functions->f01.data,
            &status, 1, "0x00");
    /* RMI4 3.2.1: CONFIGURED is write-only/read-zero; Data0 reports readiness. */
    if (device_control & 3u)
        return cf_refuse(reader, "F01.Control0.sleep", functions->f01.control,
            &device_control, 1, "(value & 0x03) == 0 (awake)");
    CF_READ(functions->f01.query, 0, f01, sizeof(f01));
    /* Upstream reads the build ID separately from the standard query burst. */
    CF_READ(functions->f01.query, 18, build_id, sizeof(build_id));
    uint32_t build = (uint32_t)build_id[0] | (uint32_t)build_id[1] << 8 |
        (uint32_t)build_id[2] << 16;
    if (f01[0] != 1)
        return cf_refuse(reader, "F01.Query0.manufacturer", functions->f01.query,
            f01, 1, "0x01");
    if (build != CHIRON_DSX_MEASURED_BUILD)
        return cf_refuse(reader, "F01.Query18.build", functions->f01.query + 18,
            build_id, sizeof(build_id), "little-endian build 2676394");
    CF_READ(functions->f12.query, 4, &q5_size, 1);
    if (q5_size < 5 || q5_size > sizeof(q5))
        return cf_refuse(reader, "F12.Query4.Query5_size", functions->f12.query + 4,
            &q5_size, 1, "5..9");
    CF_READ(functions->f12.query, 5, q5, q5_size);
    if (!cf_present(q5, 8) || !cf_present(q5, 20) ||
        !cf_present(q5, 23) || !cf_present(q5, 28))
        return cf_refuse(reader, "F12.Query5.control_presence", functions->f12.query + 5,
            q5, q5_size, "Ctrl8,20,23,28 present");
    if (!q5[0] || q5[0] > sizeof(q6))
        return cf_refuse(reader, "F12.Query5.Query6_size", functions->f12.query + 5,
            q5, 1, "1..64");
    CF_READ(functions->f12.query, 6, q6, q5[0]);
    result = cf_control23_size(q5, q6, q5[0], &control23_size);
    if (result)
        return cf_refuse(reader, "F12.Query6.Ctrl23_descriptor", functions->f12.query + 6,
            q6, q5[0], "valid size/subpacket encoding; Ctrl23 subpackets 0,1 present");
    CF_READ(functions->f12.query, 7, &q8_size, 1);
    if (q8_size < 2 || q8_size > sizeof(q8))
        return cf_refuse(reader, "F12.Query7.Query8_size", functions->f12.query + 7,
            &q8_size, 1, "2..5");
    CF_READ(functions->f12.query, 8, q8, q8_size);
    if (!(q8[1] & 2u)) /* Data1 must exist; Data0 determines its offset. */
        return cf_refuse(reader, "F12.Query8.Data1_presence", functions->f12.query + 8,
            q8, q8_size, "Data1 bit set");
    CF_READ(functions->f12.control, cf_offset(q5, 8), control8, sizeof(control8));
    CF_READ(functions->f12.control, cf_offset(q5, 20), control20, sizeof(control20));
    CF_READ(functions->f12.control, cf_offset(q5, 23), control23, control23_size);
    CF_READ(functions->f12.control, cf_offset(q5, 28), &control28, 1);
    /*
     * Vendor enables TYPE/X/Y/Z/WX/WY by writing 0xff to Ctrl28. Only the
     * first reset initialization may do this, after every layout check passes.
     * Optional Ctrl23 subpackets are described by Query6. Reject mappings
     * that relabel another object class as a finger.
     */
    if (control28 != 0xff && !reset)
        return cf_refuse(reader, "F12.Control28.report_enable",
            functions->f12.control + cf_offset(q5, 28), &control28, 1, "0xff");
    if (control20[2] != 0)
        return cf_refuse(reader, "F12.Control20.reporting_mode",
            functions->f12.control + cf_offset(q5, 20) + 2, control20 + 2, 1, "0x00");
    if (!(control23[0] & 1u))
        return cf_refuse(reader, "F12.Control23.finger_enable",
            functions->f12.control + cf_offset(q5, 23), control23, 1, "bit 0 set");
    if (!control23[1] || control23[1] > CHIRON_DSX_CONTACTS)
        return cf_refuse(reader, "F12.Control23.contacts",
            functions->f12.control + cf_offset(q5, 23) + 1, control23 + 1, 1, "1..10");
    for (unsigned i = 2; i < control23_size; i++)
        if (control23[i] != 0)
            return cf_refuse(reader, "F12.Control23.optional_subpacket",
                functions->f12.control + cf_offset(q5, 23) + i, control23 + i, 1, "0x00");
    uint16_t max_x = (uint16_t)(control8[0] | (uint16_t)control8[1] << 8);
    uint16_t max_y = (uint16_t)(control8[2] | (uint16_t)control8[3] << 8);
    unsigned offset = q8[1] & 1u;
    if (!max_x || !max_y ||
        offset + (size_t)control23[1] * 8 > 0xffu - (functions->f12.data & 0xffu))
        return cf_refuse(reader, "F12.Control8.geometry/Data1_bounds",
            functions->f12.control + cf_offset(q5, 8), control8, sizeof(control8),
            "nonzero max X/Y; all contact bytes below page selector");
    /* Status/ready must also remain valid after the control/query reads. */
    CF_READ(functions->f01.data, 0, &status, 1);
    if (status != (reset ? 0x81 : 0))
        return cf_refuse(reader, "F01.Data0.status_after_queries", functions->f01.data,
            &status, 1, reset ? "0x81 (unchanged reset/unconfigured)" : "0x00");
    pending = (struct chiron_dsx_layout){
        .f12 = functions->f12, .data1 = (uint16_t)(functions->f12.data + offset),
        .max_x = max_x, .max_y = max_y, .object_bytes = 8,
        .contacts = control23[1], .flip = reader->flip
    };
    if (q8_size >= 3 && (q8[2] & 0x80u)) {
        unsigned data15_offset = 0;
        for (unsigned i = 0; i < 15; i++)
            data15_offset += (q8[1 + i / 8] >> (i % 8)) & 1u;
        unsigned bytes = (control23[1] + 7) / 8;
        if (data15_offset + bytes > 0xffu - (functions->f12.data & 0xffu))
            return cf_refuse(reader, "F12.Query8.Data15_bounds", functions->f12.query + 8,
                q8, q8_size, "Data15 bytes below page selector");
        pending.data15 = (uint16_t)(functions->f12.data + data15_offset);
        pending.data15_bytes = (uint8_t)bytes;
    }
    if (reset) {
        if (!reader->write)
            return cf_refuse(reader, "F01.reset_initialization", functions->f01.control,
                NULL, 0, "explicit control-write transport");
        uint16_t report_address = (uint16_t)(functions->f12.control + cf_offset(q5, 28));
        if (control28 != 0xff) {
            reader->configuration_write_attempts++;
            if (reader->write(reader->context, report_address, 0xff))
                return CHIRON_DSX_IO;
            CF_READ(report_address, 0, &control28, 1);
            if (control28 != 0xff)
                return cf_refuse(reader, "F12.Control28.readback", report_address,
                    &control28, 1, "0xff");
        }
        /* Re-read before the vendor's read/modify/write; preserve all other bits. */
        CF_READ(functions->f01.data, 0, &status, 1);
        if (status != 0x81)
            return cf_refuse(reader, "F01.Data0.status_before_configure", functions->f01.data,
                &status, 1, "0x81");
        CF_READ(functions->f01.control, 0, &device_control, 1);
        if (device_control & 3u)
            return cf_refuse(reader, "F01.Control0.sleep_before_configure", functions->f01.control,
                &device_control, 1, "(value & 0x03) == 0 (awake)");
        uint8_t configured = device_control | 0x80u;
        reader->configuration_write_attempts++;
        if (reader->write(reader->context, functions->f01.control, configured))
            return CHIRON_DSX_IO;
        CF_READ(functions->f01.control, 0, &device_control, 1);
        if ((device_control & 0x7fu) != (configured & 0x7fu))
            return cf_refuse(reader, "F01.Control0.rw_readback", functions->f01.control,
                &device_control, 1, "read/write bits [6:0] unchanged; CONFIGURED reads zero");
        CF_READ(functions->f01.data, 0, &status, 1);
        if (status != 0)
            return cf_refuse(reader, "F01.Data0.status_after_configure", functions->f01.data,
                &status, 1, "0x00 (Unconfigured and reset status cleared)");
        return cf_validate(reader, functions, layout, 0);
    }
    *layout = pending;
#undef CF_READ
    return 0;
}

int
chiron_f12_validate(void *context, const struct chiron_dsx_functions *functions,
    struct chiron_dsx_layout *layout)
{
    return cf_validate(context, functions, layout, 0);
}

int
chiron_f12_prepare(void *context, const struct chiron_dsx_functions *functions,
    struct chiron_dsx_layout *layout)
{
    return cf_validate(context, functions, layout, 1);
}
