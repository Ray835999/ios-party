/*
 * Getting the phone back to the bootloader from here, without the volume-key dance.
 *
 * Two commands, both over the fastboot this loader already speaks:
 *
 *   oem fastboot            write the Android bootloader control block ("bootonce-bootloader",
 *                           the one "adb reboot bootloader" writes) to "misc" and reset. ABL
 *                           reads misc on the way up and stops in fastboot once.
 *   flash:<partition>       write what was just downloaded straight to that GPT partition.
 *                           This is the one that matters: with it "fastboot flash boot ..."
 *                           works from this loader, and no key combination is needed at all.
 *   oem dump <p> <lba> <n>  stage raw blocks of a partition for "fastboot get_staged", so a
 *                           partition can be looked at without a host-side reader.
 *
 * Both go through the block driver the kernel's own writes use: write_blocks, flush_blocks,
 * read back, compare. Nothing is taken on trust.
 */

static uint64_t downloaded_len;         /* bytes the host last downloaded (0: none) */

/* the partition with this GPT name (ASCII; case is not part of a GPT name), or NULL */
static efi_block_io *
find_partition(const char *want)
{
	static const efi_guid part_guid = EFI_PARTITION_INFO_GUID;
	static const efi_guid blk_guid = EFI_BLOCK_IO_GUID;
	efi_handle *handles;
	uint64_t count;

	if (want == NULL || *want == 0 ||
	    EFI_ERROR(BS->locate_handle_buffer(ByProtocol, &part_guid, NULL, &count, &handles))) {
		return NULL;
	}
	for (uint64_t i = 0; i < count; i++) {
		efi_partition_info *info;
		efi_block_io *bio;
		unsigned k = 0;
		int same = 1;

		if (EFI_ERROR(BS->handle_protocol(handles[i], &part_guid, (void **)&info)) || info->type != 2) {
			continue;
		}
		for (; want[k] != 0; k++) {
			char16 c;

			if (k >= sizeof(info->name) / sizeof(info->name[0])) {
				same = 0;       /* longer than a GPT name can be */
				break;
			}
			c = info->name[k];
			if (c >= 'A' && c <= 'Z') {
				c = (char16)(c + ('a' - 'A'));
			}
			if (c != (char16)(unsigned char)want[k]) {
				same = 0;
				break;
			}
		}
		if (same && k < sizeof(info->name) / sizeof(info->name[0]) && info->name[k] != 0) {
			same = 0;               /* "boot" is not "boot_a" */
		}
		if (same && !EFI_ERROR(BS->handle_protocol(handles[i], &blk_guid, (void **)&bio)) && bio->media != NULL) {
			return bio;
		}
	}
	return NULL;
}

/*
 * The image the host downloaded, written to a partition by name. Chunks of 2 MB, each one
 * read back and compared before the next goes out, so a failure names the byte it happened at
 * instead of only saying that the flash was no good.
 */
static int
flash_partition(const char *name, const uint8_t *data, uint64_t len, const char **why)
{
	efi_block_io *bio;
	uint64_t bs, room, done;
	efi_status s;

	*why = NULL;
	bio = find_partition(name);
	if (bio == NULL) {
		*why = "no such partition";
		return 0;
	}
	bs = bio->media->block_size;
	room = (bio->media->last_block + 1) * bs;
	if (bs == 0 || (bs & (bs - 1)) != 0 || bs > 65536) {
		*why = "odd block size";
		return 0;
	}
	if (len == 0) {
		*why = "nothing downloaded";
		return 0;
	}
	if (len > room) {
		*why = "image is larger than the partition";
		print("flash: %s holds %d bytes, the image is %d\n", name, room, len);
		return 0;
	}
	print("flash: %s, %d bytes (partition %d), writing\n", name, len, room);
	for (done = 0; done < len; done += bs * 512) {
		uint64_t chunk = len - done > bs * 512 ? bs * 512 : len - done;

		chunk = (chunk + bs - 1) / bs * bs;     /* whole blocks: the tail is padded by what follows */
		flush_dcache((uint64_t)(uintptr_t)(data + done), (uint64_t)(uintptr_t)(data + done + chunk));
		s = bio->write_blocks(bio, bio->media->media_id, done / bs, chunk, (void *)(uintptr_t)(data + done));
		if (EFI_ERROR(s)) {
			*why = "the block driver refused a write";
			print("flash: write of %d bytes at %d: %x\n", chunk, done, s);
			return 0;
		}
		bio->flush_blocks(bio);
		s = bio->read_blocks(bio, bio->media->media_id, done / bs, chunk, block_buffer);
		if (EFI_ERROR(s)) {
			*why = "the block driver refused the read back";
			print("flash: read back of %d bytes at %d: %x\n", chunk, done, s);
			return 0;
		}
		flush_dcache((uint64_t)(uintptr_t)block_buffer, (uint64_t)(uintptr_t)block_buffer + chunk);
		if (memcmp(block_buffer, data + done, chunk) != 0) {
			*why = "read back DIFFERENT";
			print("flash: %d bytes at %d did not read back the same\n", chunk, done);
			return 0;
		}
		if ((done / (bs * 512)) % 4 == 0) {
			print("flash: %d of %d bytes in place\n", done + chunk, len);
		}
	}
	return 1;
}

/*
 * The Android bootloader control block: 2048 bytes at the start of "misc", the command in the
 * first 32. ABL reads it before it decides what to boot; "bootonce-bootloader" is what adb and
 * fastboot write for "reboot bootloader", and the bootloader clears it once it has acted on it.
 */
static int
write_bcb(const char *command, const char **why)
{
	static uint8_t want[32];
	static uint8_t block[4096] __attribute__((aligned(4096)));
	efi_block_io *bio;
	uint64_t bs;
	efi_status s;

	*why = NULL;
	bio = find_partition("misc");
	if (bio == NULL) {
		*why = "no misc partition";
		return 0;
	}
	bs = bio->media->block_size;
	if (bs == 0 || bs > sizeof(block)) {
		*why = "odd block size on misc";
		return 0;
	}
	memset(want, 0, sizeof(want));
	for (unsigned i = 0; i < sizeof(want) - 1 && command[i] != 0; i++) {
		want[i] = (uint8_t)command[i];
	}
	memset(block, 0, bs);           /* the status and recovery fields go too: only the command is meant */
	memcpy(block, want, sizeof(want));
	flush_dcache((uint64_t)(uintptr_t)block, (uint64_t)(uintptr_t)block + bs);
	s = bio->write_blocks(bio, bio->media->media_id, 0, bs, block);
	if (EFI_ERROR(s)) {
		*why = "the block driver refused the write";
		print("boot: writing misc: %x\n", s);
		return 0;
	}
	bio->flush_blocks(bio);
	memset(block, 0, bs);
	s = bio->read_blocks(bio, bio->media->media_id, 0, bs, block);
	if (EFI_ERROR(s)) {
		*why = "the block driver refused the read back";
		print("boot: reading misc back: %x\n", s);
		return 0;
	}
	flush_dcache((uint64_t)(uintptr_t)block, (uint64_t)(uintptr_t)block + bs);
	if (memcmp(block, want, sizeof(want)) != 0) {
		*why = "misc did not read back the same";
		return 0;
	}
	return 1;
}

/* --- commands ---------------------------------------------------------------------------- */

		} else if (starts_with(cmd, "flash:")) {
			const char *name = cmd + 6;
			const char *why = NULL;

			if (downloaded_len == 0) {
				fb_reply("FAIL", "nothing downloaded");
				return;
			}
			if (!flash_partition(name, (const uint8_t *)(uintptr_t)PAYLOAD_BASE, downloaded_len, &why)) {
				print("flash: %s: %s\n", name, why != NULL ? why : "failed");
				fb_reply("FAIL", why != NULL ? why : "the write failed");
				return;
			}
			print("flash: %s: %d bytes written, every one of them read back the same\n", name, downloaded_len);
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem dump")) {
			/* "oem dump <partition> <first block> <how many>": the raw blocks, staged for
			 * "fastboot get_staged".  It reads and writes nothing else, so it can be asked of a
			 * partition whose contents are not known -- which is how one finds out what a
			 * bootloader actually looks at. */
			char name[36];
			const char *p = cmd + 8;
			unsigned n = 0;
			uint64_t lba, count, bs, bytes;
			efi_block_io *bio;
			efi_status s;

			while (*p == ' ') {
				p++;
			}
			while (*p != 0 && *p != ' ' && n < sizeof(name) - 1) {
				name[n++] = *p++;
			}
			name[n] = 0;
			while (*p == ' ') {
				p++;
			}
			lba = parse_number(p, 10);
			while (*p >= '0' && *p <= '9') {
				p++;
			}
			while (*p == ' ') {
				p++;
			}
			count = parse_number(p, 10);
			if (name[0] == 0 || count == 0) {
				fb_reply("FAIL", "oem dump <partition> <lba> <blocks>");
				return;
			}
			bio = find_partition(name);
			if (bio == NULL) {
				fb_reply("FAIL", "no such partition");
				return;
			}
			bs = bio->media->block_size;
			bytes = count * bs;
			if (bs == 0 || bytes > BLOCK_BUFFER_SIZE || lba > bio->media->last_block ||
			    count > bio->media->last_block + 1 - lba) {
				fb_reply("FAIL", "out of range");
				return;
			}
			s = bio->read_blocks(bio, bio->media->media_id, lba, bytes, block_buffer);
			if (EFI_ERROR(s)) {
				print("dump: %s at %d: %x\n", name, lba, s);
				fb_reply("FAIL", "the block driver refused the read");
				return;
			}
			flush_dcache((uint64_t)(uintptr_t)block_buffer, (uint64_t)(uintptr_t)block_buffer + bytes);
			staged_data = (const char *)(uintptr_t)block_buffer;
			staged_len = bytes;
			print("dump: %s from block %d, %d bytes staged\n", name, lba, bytes);
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem fastboot") || starts_with(cmd, "reboot-bootloader")) {
			/* the bootloader's own way in: misc, then a reset. No key combination, no host. */
			const char *want = "bootonce-bootloader";
			const char *rest = starts_with(cmd, "reboot-bootloader") ? cmd + 17 : cmd + 12;
			const char *why = NULL;

			while (*rest == ' ') {
				rest++;
			}
			if (*rest != 0) {
				want = rest;    /* "oem fastboot boot-fastboot": the fastbootd one, if ever wanted */
			}
			if (!write_bcb(want, &why)) {
				print("boot: %s: %s\n", want, why != NULL ? why : "failed");
				fb_reply("FAIL", why != NULL ? why : "misc was not written");
				return;
			}
			print("boot: \"%s\" is in misc; the bootloader stops in fastboot\n", want);
			fb_reply("OKAY", "");
			BS->stall(200000);
			ST->rt->reset_system(0 /* EfiResetCold */, 0, 0, NULL);
