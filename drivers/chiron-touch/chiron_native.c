#include "chiron_native.h"

static int
cn_select(void *context, uint8_t slave, uint8_t page)
{
    struct chiron_native *native = context;
    native->last_operation = 0;
    native->last_address = (uint16_t)((page << 8) | 0xffu);
    native->last_bytes = 1;
    int result = chiron_qup_select_page(&native->qup, slave, page);
    native->last_result = result;
    if (!result)
        native->page = page;
    return result;
}

static int
cn_read(void *context, uint8_t slave, uint8_t reg, uint8_t *data, size_t size)
{
    struct chiron_native *native = context;
    native->last_operation = 1;
    native->last_address = (uint16_t)((native->page << 8) | reg);
    native->last_bytes = size;
    int result = chiron_qup_read(&native->qup, slave, reg, data, size);
    native->last_result = result;
    return result;
}

static int
cn_absolute_read(void *context, uint16_t address, uint8_t *data, size_t size)
{
    int result = cn_select(context, 0x20, (uint8_t)(address >> 8));
    if (result)
        return result;
    return cn_read(context, 0x20, (uint8_t)address, data, size);
}

static int
cn_absolute_write(void *context, uint16_t address, uint8_t value)
{
    struct chiron_native *native = context;
    int result = cn_select(context, 0x20, (uint8_t)(address >> 8));
    if (result)
        return result;
    native->last_operation = 2;
    native->last_address = address;
    native->last_bytes = 1;
    result = chiron_qup_write_byte(&native->qup, 0x20, (uint8_t)address, value);
    native->last_result = result;
    return result;
}

static int
cn_validate(void *context, const struct chiron_dsx_functions *functions,
    struct chiron_dsx_layout *layout)
{
    struct chiron_native *native = context;
    if (!native->initialization_attempted) {
        native->initialization_attempted = 1;
        return chiron_f12_prepare(&native->reader, functions, layout);
    }
    return chiron_f12_validate(&native->reader, functions, layout);
}

int
chiron_native_bind(struct chiron_native *native, const struct chiron_qup_io *io,
    uint8_t flip, struct chiron_dsx_bus *bus)
{
    *native = (struct chiron_native){0};
    *bus = (struct chiron_dsx_bus){0};
    if (flip > 3)
        return CHIRON_DSX_UNVERIFIED_LAYOUT;
    native->last_operation = 3;
    int result = chiron_qup_open(&native->qup, io);
    native->last_result = result;
    if (result)
        return result;
    native->reader = (struct chiron_f12_reader){
        .context = native, .read = cn_absolute_read, .flip = flip,
        .write = cn_absolute_write
    };
    *bus = (struct chiron_dsx_bus){native, cn_select, cn_read, cn_validate};
    return 0;
}
