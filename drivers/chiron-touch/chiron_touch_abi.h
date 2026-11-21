#ifndef CHIRON_TOUCH_ABI_H
#define CHIRON_TOUCH_ABI_H

/* Four little-endian u32 cells in /chosen/chiron-touch-handoff. */
#define CHIRON_TOUCH_HANDOFF_VERSION 1u
#define CHIRON_TOUCH_DIRECT_BOOT_CPU 1u
#define CHIRON_TOUCH_OTHER_CPUS_OFF 0xfeu
#define CHIRON_TOUCH_FIRMWARE_RESIDENT 1u

#endif
