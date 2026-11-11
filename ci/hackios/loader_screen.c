/*
 * Live screen: the kernel's log, drawn on the phone while the kernel runs.
 *
 * The kernel publishes its log through struct kernel_live (XNU's loader_return.c): a virtual
 * address, how much has been written, and the ring it wraps into.  This core is awake beside the
 * kernel for the whole run -- that is how the storage is served -- so the same text can be put
 * where a person can look at it, with no cable and no host.
 *
 * UEFI's console is NOT used for this.  print() stops going to it before the kernel starts
 * (screen_quiet), on purpose: boot services are not to be called while the kernel runs and
 * reprograms the interrupt controller.  So the glyphs below are ours, and every pixel is a plain
 * store into the framebuffer the loader was handed.
 *
 * Cost is kept honest: the whole screen is redrawn at most once a second, only when the log has
 * actually grown, and the redraw calls block_poll() as it goes so a refresh cannot delay the
 * kernel's storage requests by more than a few lines' worth of pixels.
 */

#define SCR_SCALE   2                       /* a font pixel is 2x2 screen pixels */
#define SCR_CW      (6 * SCR_SCALE)         /* character cell: 5 wide + 1 space */
#define SCR_CH      (8 * SCR_SCALE)         /*                 7 tall + 1 space */
#define SCR_COLS    89                      /* 89 * 12 + 6 + 10 = 1072 of 1080 */
#define SCR_ROWS    126                     /* (2160 - 2 title rows) / 16 */
#define SCR_TOP     (2 * SCR_CH)            /* the two rows at the top are the loader's own */

static char scr_text[SCR_ROWS][SCR_COLS + 1];
static int scr_head, scr_count, scr_col;
static uint32_t scr_last_len;
static uint64_t scr_last_draw, scr_last_log, scr_last_beat;
static int scr_drawn;

static void scr_line_begin(void)
{
	int at;

	if (scr_count < SCR_ROWS) {
		at = (scr_head + scr_count) % SCR_ROWS;
		scr_count++;
	} else {
		at = scr_head;
		scr_head = (scr_head + 1) % SCR_ROWS;
	}
	memset(scr_text[at], 0, sizeof(scr_text[at]));
	scr_col = 0;
}

static void scr_reset(void)
{
	scr_head = 0;
	scr_count = 0;
	scr_col = 0;
	for (int i = 0; i < SCR_ROWS; i++) {
		scr_text[i][0] = 0;
	}
}

static void scr_feed(char c)
{
	int last;

	if (c == '\r') {
		return;
	}
	if (c == '\n') {
		scr_line_begin();
		return;
	}
	if (c == '\t') {
		c = ' ';
	}
	if (c < 32 || c > 126) {
		c = '.';
	}
	if (scr_count == 0 || scr_col >= SCR_COLS) {
		scr_line_begin();
	}
	last = (scr_head + scr_count - 1) % SCR_ROWS;
	scr_text[last][scr_col++] = c;
	scr_text[last][scr_col] = 0;
}

static void scr_fill(int x, int y, int w, int h, uint32_t colour)
{
	uint64_t pitch = SCREEN.stride / 4;
	uint32_t *row = (uint32_t *)(uintptr_t)SCREEN.base + (uint64_t)y * pitch + (uint64_t)x;

	for (int j = 0; j < h; j++, row += pitch) {
		for (int i = 0; i < w; i++) {
			row[i] = colour;
		}
	}
}

static void scr_text_at(int x, int y, const char *s, uint32_t colour)
{
	for (; *s != 0; s++, x += SCR_CW) {
		int c = (unsigned char)*s;
		const uint8_t *g;

		if (c < 32 || c > 126) {
			c = '?';
		}
		g = font5x7[c - 32];
		for (int r = 0; r < 7; r++) {
			for (int b = 0; b < 5; b++) {
				if (g[r] & (0x10 >> b)) {
					scr_fill(x + b * SCR_SCALE, y + r * SCR_SCALE, SCR_SCALE, SCR_SCALE, colour);
				}
			}
		}
	}
}

static char *scr_num(char *p, uint64_t v)
{
	char t[24];
	int n = 0;

	do {
		t[n++] = (char)('0' + (int)(v % 10));
		v /= 10;
	} while (v != 0);
	while (n > 0) {
		*p++ = t[--n];
	}
	return p;
}

static char *scr_str(char *p, const char *s)
{
	while (*s != 0) {
		*p++ = *s++;
	}
	return p;
}

static void scr_draw(const struct kernel_live *live, uint64_t bytes)
{
	char head[SCR_COLS + 1];
	char *p = head;

	p = scr_str(p, "XNULOADER  beat ");
	p = scr_num(p, live->heartbeat);
	p = scr_str(p, " (");
	p = scr_num(p, live->heartbeat * 10);
	p = scr_str(p, "s)  log ");
	p = scr_num(p, bytes);
	p = scr_str(p, " bytes  last ");
	p = scr_num(p, (uint64_t)scr_count);
	p = scr_str(p, " lines");
	*p = 0;

	scr_fill(0, 0, SCREEN.width, SCR_TOP, 0x00000000);
	scr_text_at(SCR_CW / 2, SCR_CH / 2, head, 0xffa0e0ff);
	for (int i = 0; i < scr_count; i++) {
		int at = (scr_head + i) % SCR_ROWS;

		scr_text_at(SCR_CW / 2, SCR_TOP + i * SCR_CH, scr_text[at], 0xffffffff);
		if (block_enabled && (i & 31) == 31) {
			block_poll();           /* the storage is served in the middle of the redraw */
		}
	}
	/* whatever was below the last line belongs to a longer log */
	if (scr_count < SCR_ROWS) {
		scr_fill(0, SCR_TOP + scr_count * SCR_CH, SCREEN.width,
		    SCREEN.height - (SCR_TOP + scr_count * SCR_CH), 0x00000000);
	}
}

/*
 * Two lines, before the kernel: the loader then spends minutes cleaning the kernel's window
 * with nothing to say, and a screen that still shows the old menu looks exactly like a hang.
 */
static void
screen_note(const char *text)
{
	if (SCREEN.base == 0) {
		return;
	}
	scr_fill(0, 0, SCREEN.width, SCREEN.height, 0x00000000);
	scr_text_at(SCR_CW / 2, SCR_CH / 2, "XNULOADER", 0xffa0e0ff);
	scr_text_at(SCR_CW / 2, SCR_CH / 2 + 2 * SCR_CH, text, 0xffffffff);
}

/*
 * Once a second, and only when there is something new: the log has grown, or the kernel's
 * heartbeat has moved.  132 KB of log costs about a millisecond to lay out and a few to draw;
 * a kernel that has gone quiet still shows a heartbeat that ticks, which is the difference
 * between "working" and "gone".
 */
static void
screen_live_tick(const struct kernel_live *live, uint64_t slide)
{
	const char *log = NULL;
	uint32_t len = 0, ring_at = 0;
	uint64_t head = 0, ring = 0, n = 0, now = READ_SYSREG(CNTVCT_EL0);
	int wrapped = 0;

	if (SCREEN.base == 0 || live == NULL) {
		return;
	}
	if (scr_drawn && live->heartbeat == scr_last_beat && live->log == scr_last_log &&
	    (live->log == 0 || *(volatile uint32_t *)(live->log_len + slide) == scr_last_len)) {
		return;
	}
	if (scr_drawn && (now - scr_last_draw) < READ_SYSREG(CNTFRQ_EL0)) {
		return;
	}
	if (live->log != 0) {
		log = (const char *)(uintptr_t)(live->log + slide);
		len = *(volatile uint32_t *)(live->log_len + slide);
		ring_at = *(volatile uint32_t *)(live->log_ring_at + slide);
		wrapped = *(volatile uint8_t *)(live->log_wrapped + slide);
		head = live->log_head;
		ring = live->log_ring;
		n = len;
		if (n > head + ring) {
			n = head + ring;
		}
	}
	scr_reset();
	if (log != NULL) {
		if (!wrapped || ring_at >= ring) {
			for (uint64_t i = 0; i < n; i++) {
				scr_feed(log[i]);
			}
		} else {
			for (uint64_t i = 0; i < head; i++) {
				scr_feed(log[i]);
			}
			for (uint64_t i = 0; i < ring - ring_at; i++) {
				scr_feed(log[head + ring_at + i]);
			}
			for (uint64_t i = 0; i < ring_at; i++) {
				scr_feed(log[head + i]);
			}
		}
	}
	scr_last_draw = now;
	scr_last_len = len;
	scr_last_log = live->log;
	scr_last_beat = live->heartbeat;
	scr_drawn = 1;
	scr_draw(live, n);
}
