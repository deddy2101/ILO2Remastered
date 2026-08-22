"""HP iLO2 'DVC' remote-console video codec decoder.

Faithful port of the bit-oriented state machine in
com.hp.ilo2.remcons.cim (rc175p11.jar, class file dated 2016, banner string
"Version 20050808154652" — so this codec dates back to ~2005 RILOE-era
firmware and was carried forward unchanged into iLO2).

The wire format is a sequence of bytes; bits are consumed LSB-first out of
an accumulator, but each field is bit-reversed on the way out (a common
trick to make a hardware LSB-first shift register emit MSB-first fields).
The decoder is a 48-state graph: each state consumes a fixed number of bits
(bits_to_read[state]), and the value of those bits selects the next state
(next_0/next_1). Along the way, states build up 16x16 pixel blocks using a
17-entry LRU colour cache (so repeated colours cost ~log2(17) bits instead
of a full RGB444 triplet) plus various run-length / block-repeat states.

This is intentionally a close transliteration of the Java rather than a
"clean" rewrite: the state graph has no independent documentation anywhere,
so preserving the original shape makes it possible to diff against the
decompiled source when something doesn't decode right.
"""

# Decoder states (SIZE_OF_ALL = 48)
RESET = 0
START = 1
PIXELS = 2
PIXLRU1 = 3
PIXLRU0 = 4
PIXCODE1 = 5
PIXCODE2 = 6
PIXCODE3 = 7
PIXGREY = 8
PIXRGBR = 9
PIXRPT = 10
PIXRPT1 = 11
PIXRPTSTD1 = 12
PIXRPTSTD2 = 13
PIXRPTNSTD = 14
CMD = 15
CMD0 = 16
MOVEXY0 = 17
EXTCMD = 18
CMDX = 19
MOVESHORTX = 20
MOVELONGX = 21
BLKRPT = 22
EXTCMD1 = 23
FIRMWARE = 24
EXTCMD2 = 25
MODE0 = 26
TIMEOUT = 27
BLKRPT1 = 28
BLKRPTSTD = 29
BLKRPTNSTD = 30
PIXFAN = 31
PIXCODE4 = 32
PIXDUP = 33
BLKDUP = 34
PIXCODE = 35
PIXSPEC = 36
EXIT = 37
LATCHED = 38
MOVEXY1 = 39
MODE1 = 40
PIXRGBG = 41
PIXRGBB = 42
HUNT = 43
PRINT0 = 44
PRINT1 = 45
CORP = 46
MODE2 = 47

BITS_TO_READ = [0, 1, 1, 1, 1, 1, 2, 3, 4, 4, 1, 1, 3, 3, 8, 1, 1, 7, 1, 1,
                3, 7, 1, 1, 8, 1, 7, 0, 1, 3, 7, 1, 4, 0, 0, 0, 1, 0, 1, 7,
                7, 4, 4, 1, 8, 8, 1, 4]

NEXT_0 = [1, 2, 31, 2, 2, 10, 10, 10, 10, 41, 2, 33, 2, 2, 2, 16, 19, 39,
          22, 20, 1, 1, 34, 25, 46, 26, 40, 1, 29, 1, 1, 36, 10, 2, 1, 35,
          8, 37, 38, 1, 47, 42, 10, 43, 45, 45, 1, 1]

NEXT_1 = [1, 15, 3, 11, 11, 10, 10, 10, 10, 41, 11, 12, 2, 2, 2, 17, 18, 39,
          23, 21, 1, 1, 28, 24, 46, 27, 40, 1, 30, 1, 1, 35, 10, 2, 1, 35,
          9, 37, 38, 1, 47, 42, 10, 0, 45, 45, 24, 1]

GETMASK = [0, 1, 3, 7, 15, 31, 63, 127, 255]


def _build_reversal_tables():
    reversal = [0] * 256
    left = [0] * 256
    right = [0] * 256
    for n in range(256):
        n2 = 8  # index of first (rightmost, i.e. least-significant-first) set bit seen
        n3 = 8
        n4 = n
        n5 = 0
        for n6 in range(8):
            n5 <<= 1
            if n4 & 1:
                if n2 > n6:
                    n2 = n6
                n5 |= 1
                n3 = 7 - n6
            n4 >>= 1
        reversal[n] = n5
        right[n] = n2
        left[n] = n3
    return reversal, left, right


_REVERSAL, _LEFT, _RIGHT = _build_reversal_tables()


def _build_color_remap():
    table = [0] * 4096
    for n2 in range(4096):
        table[n2] = (n2 & 0xF00) * 4352 + (n2 & 0xF0) * 272 + (n2 & 0xF) * 17
    return table


_COLOR_REMAP = _build_color_remap()


class DvcDecoder:
    """Decodes one byte at a time. Call feed(byte) for every DVC-mode byte
    (post RC4-decrypt if encryption is active). Blocks are delivered via
    on_block(x, y, pixels) where pixels is a list of up to 256 0xRRGGBB ints
    for a 16x16 tile starting at (x, y) in the remote framebuffer.
    """

    def __init__(self, on_block=None, on_resize=None, on_status=None,
                 on_refresh_request=None, on_seize=None, on_change_key=None,
                 debug=False, log_fn=print):
        self.on_block = on_block or (lambda x, y, px: None)
        self.on_resize = on_resize or (lambda w, h: None)
        self.on_status = on_status or (lambda field, text: None)
        self.on_refresh_request = on_refresh_request or (lambda: None)
        self.on_seize = on_seize or (lambda: None)
        self.on_change_key = on_change_key or (lambda: None)
        self.debug = debug
        self.log = log_fn

        self.color_remap_table = _COLOR_REMAP

        self.dvc_cc_active = 0
        self.dvc_cc_color = [0] * 17
        self.dvc_cc_usage = [0] * 17
        self.dvc_cc_block = [0] * 17

        self.dvc_pixel_count = 0
        self.dvc_size_x = 0
        self.dvc_size_y = 0
        self.dvc_y_clipped = 0
        self.dvc_lastx = 0
        self.dvc_lasty = 0
        self.dvc_newx = 0
        self.dvc_newy = 0
        self.dvc_color = 0
        self.dvc_last_color = 0

        self.dvc_ib_acc = 0
        self.dvc_ib_bcnt = 0
        self.dvc_zero_count = 0
        self.dvc_decoder_state = RESET
        self.dvc_next_state = RESET
        self.dvc_pixcode = LATCHED
        self.dvc_code = 0

        self.block = [0] * 256
        self.dvc_red = 0
        self.dvc_green = 0
        self.dvc_blue = 0
        self.fatal_count = 0
        self.printchan = 0
        self.printstring = ""
        self.count_bytes = 0
        self.cmd_p_buff = [0] * 256
        self.cmd_p_count = 0
        self.cmd_last = 0
        self.framerate = 30
        self.timeout_count = -1
        self.dvc_process_inhibit = False
        self.video_detected = True

        self.screen_x = 1
        self.screen_y = 1

        self._initialized = False

    # ---- colour cache -----------------------------------------------
    def cache_reset(self):
        self.dvc_cc_active = 0

    def cache_lru(self, color):
        active = self.dvc_cc_active
        idx = 0
        hit = 0
        for i in range(active):
            if color == self.dvc_cc_color[i]:
                idx = i
                hit = 1
                break
            if self.dvc_cc_usage[i] == active - 1:
                idx = i
        usage_at_idx = self.dvc_cc_usage[idx]
        if not hit:
            if active < 17:
                idx = active
                usage_at_idx = active
                active += 1
                self.dvc_cc_active = active
                self.dvc_pixcode = (LATCHED if active < 2 else
                                     PIXLRU0 if active == 2 else
                                     PIXCODE1 if active == 3 else
                                     PIXCODE2 if active < 6 else
                                     PIXCODE3 if active < 10 else
                                     PIXCODE4)
                NEXT_1[31] = self.dvc_pixcode
            self.dvc_cc_color[idx] = color
        self.dvc_cc_block[idx] = 1
        for i in range(active):
            if self.dvc_cc_usage[i] < usage_at_idx:
                self.dvc_cc_usage[i] += 1
        self.dvc_cc_usage[idx] = 0
        return hit

    def cache_find(self, n):
        active = self.dvc_cc_active
        for i in range(active):
            if n == self.dvc_cc_usage[i]:
                color = self.dvc_cc_color[i]
                for k in range(active):
                    if self.dvc_cc_usage[k] < n:
                        self.dvc_cc_usage[k] += 1
                self.dvc_cc_usage[i] = 0
                self.dvc_cc_block[i] = 1
                return color
        return -1

    def cache_prune(self):
        n = self.dvc_cc_active
        i = 0
        while i < n:
            if self.dvc_cc_block[i] == 0:
                n -= 1
                self.dvc_cc_block[i] = self.dvc_cc_block[n]
                self.dvc_cc_color[i] = self.dvc_cc_color[n]
                self.dvc_cc_usage[i] = self.dvc_cc_usage[n]
                continue
            self.dvc_cc_block[i] -= 1
            i += 1
        self.dvc_cc_active = n
        self.dvc_pixcode = (LATCHED if n < 2 else
                             PIXLRU0 if n == 2 else
                             PIXCODE1 if n == 3 else
                             PIXCODE2 if n < 6 else
                             PIXCODE3 if n < 10 else
                             PIXCODE4)
        NEXT_1[31] = self.dvc_pixcode

    # ---- block/tile output --------------------------------------------
    def next_block(self, n):
        active_video = self.video_detected
        if (self.dvc_pixel_count != 0 and self.dvc_y_clipped > 0
                and self.dvc_lasty == self.dvc_size_y):
            fill = self.color_remap_table[0]
            for i in range(self.dvc_y_clipped, 256):
                self.block[i] = fill
        self.dvc_pixel_count = 0
        self.dvc_next_state = START
        x = self.dvc_lastx * 16
        y = self.dvc_lasty * 16
        while n != 0:
            if active_video:
                self.on_block(x, y, list(self.block))
            x += 16
            self.dvc_lastx += 1
            if self.dvc_lastx >= self.dvc_size_x:
                break
            n -= 1

    # ---- bit accumulator ------------------------------------------------
    def add_bits(self, c):
        self.dvc_ib_acc |= c << self.dvc_ib_bcnt
        self.dvc_ib_bcnt += 8
        self.dvc_zero_count += _RIGHT[c]
        if self.dvc_zero_count > 30:
            if self.debug:
                self.log(f"dvc: reset sequence detected at {self.count_bytes}")
            self.dvc_next_state = HUNT
            self.dvc_decoder_state = HUNT
            return 4
        if c != 0:
            self.dvc_zero_count = _LEFT[c]
        return 0

    def get_bits(self, n):
        if n == 1:
            self.dvc_code = self.dvc_ib_acc & 1
            self.dvc_ib_acc >>= 1
            self.dvc_ib_bcnt -= 1
            return 0
        if n == 0:
            return 0
        v = self.dvc_ib_acc & GETMASK[n]
        self.dvc_ib_bcnt -= n
        self.dvc_ib_acc >>= n
        v = _REVERSAL[v]
        self.dvc_code = v >> (8 - n)
        return 0

    def show_error(self, msg):
        if self.debug:
            self.log(f"dvc: {msg}: state {self.dvc_decoder_state} code {self.dvc_code}")
            self.log(f"dvc: error at byte count {self.count_bytes}")

    # ---- main bit-consuming loop ----------------------------------------
    def process_bits(self, c):
        self.add_bits(c)
        self.count_bytes += 1
        n = 0
        while n == 0:
            need = BITS_TO_READ[self.dvc_decoder_state]
            if need > self.dvc_ib_bcnt:
                return 0
            self.get_bits(need)
            self.dvc_next_state = (NEXT_0[self.dvc_decoder_state]
                                    if self.dvc_code == 0
                                    else NEXT_1[self.dvc_decoder_state])
            state = self.dvc_decoder_state
            broke = False

            if state in (PIXLRU1, PIXLRU0, PIXCODE1, PIXCODE2, PIXCODE3, PIXCODE4):
                if self.dvc_cc_active == 1:
                    code = self.dvc_cc_usage[0]
                elif state == PIXLRU0:
                    code = 0
                elif state == PIXLRU1:
                    code = 1
                else:
                    code = self.dvc_code + 1 if self.dvc_code != 0 else self.dvc_code
                self.dvc_code = code
                color = self.cache_find(code)
                if color == -1:
                    self.show_error(f"could not find color for LRU {code}, cache has {self.dvc_cc_active} colors")
                    self.dvc_next_state = LATCHED
                else:
                    self.dvc_last_color = self.color_remap_table[color]
                    if self.dvc_pixel_count >= 256:
                        self.dvc_next_state = LATCHED
                    else:
                        self.block[self.dvc_pixel_count] = self.dvc_last_color
                        self.dvc_pixel_count += 1

            elif state == PIXRPTSTD1:
                if self.dvc_code == 7:
                    self.dvc_next_state = PIXRPTNSTD
                elif self.dvc_code == 6:
                    self.dvc_next_state = PIXRPTSTD2
                else:
                    self.dvc_code += 2
                    for _ in range(self.dvc_code):
                        if self.dvc_pixel_count >= 256:
                            self.show_error("too many pixels in a block2")
                            self.dvc_next_state = LATCHED
                            broke = True
                            break
                        self.block[self.dvc_pixel_count] = self.dvc_last_color
                        self.dvc_pixel_count += 1

            elif state in (PIXRPTSTD2, PIXRPTNSTD):
                if state == PIXRPTSTD2:
                    self.dvc_code += 8
                for _ in range(self.dvc_code):
                    if self.dvc_pixel_count >= 256:
                        self.show_error("too many pixels in a block3")
                        self.dvc_next_state = LATCHED
                        broke = True
                        break
                    self.block[self.dvc_pixel_count] = self.dvc_last_color
                    self.dvc_pixel_count += 1

            elif state == PIXDUP:
                if self.dvc_pixel_count >= 256:
                    self.show_error("too many pixels in a block4")
                    self.dvc_next_state = LATCHED
                else:
                    self.block[self.dvc_pixel_count] = self.dvc_last_color
                    self.dvc_pixel_count += 1

            elif state in (START, PIXELS, PIXRPT, PIXRPT1, BLKRPT, BLKRPT1, PIXFAN, PIXSPEC):
                pass

            elif state == PIXCODE:
                self.dvc_next_state = self.dvc_pixcode

            elif state == PIXRGBR:
                self.dvc_red = self.dvc_code << 8

            elif state == PIXRGBG:
                self.dvc_green = self.dvc_code << 4

            elif state in (PIXGREY, PIXRGBB):
                if state == PIXGREY:
                    self.dvc_red = self.dvc_code << 8
                    self.dvc_green = self.dvc_code << 4
                self.dvc_blue = self.dvc_code
                color = self.dvc_red | self.dvc_green | self.dvc_blue
                hit = self.cache_lru(color)
                if hit:
                    self.show_error(f"unexpected hit: color {color:04X}")
                    self.dvc_next_state = LATCHED
                else:
                    self.dvc_last_color = self.color_remap_table[color]
                    if self.dvc_pixel_count >= 256:
                        self.dvc_next_state = LATCHED
                    else:
                        self.block[self.dvc_pixel_count] = self.dvc_last_color
                        self.dvc_pixel_count += 1

            elif state in (MOVEXY0, MODE0):
                self.dvc_newx = self.dvc_code
                if state == MOVEXY0 and self.dvc_newx > self.dvc_size_x:
                    self.dvc_newx = 0

            elif state == MOVEXY1:
                self.dvc_newy = self.dvc_code & 0x7F
                self.dvc_lastx = self.dvc_newx
                self.dvc_lasty = self.dvc_newy
                if self.dvc_lasty > self.dvc_size_y:
                    self.dvc_lasty = 0

            elif state == MOVESHORTX:
                self.dvc_code = self.dvc_lastx + self.dvc_code + 1
                self.dvc_lastx = self.dvc_code & 0x7F
                if self.dvc_lastx > self.dvc_size_x:
                    self.dvc_lastx = 0

            elif state == MOVELONGX:
                self.dvc_lastx = self.dvc_code & 0x7F
                if self.dvc_lastx > self.dvc_size_x:
                    self.dvc_lastx = 0

            elif state == TIMEOUT:
                if self.timeout_count == self.count_bytes - 1:
                    self.show_error(f"double timeout at {self.count_bytes}, remaining bits {self.dvc_ib_bcnt & 7}")
                    self.dvc_next_state = LATCHED
                if self.dvc_ib_bcnt & 7:
                    self.get_bits(self.dvc_ib_bcnt & 7)
                self.timeout_count = self.count_bytes

            elif state == FIRMWARE:
                if self.cmd_p_count != 0:
                    self.cmd_p_buff[self.cmd_p_count - 1] = self.cmd_last
                self.cmd_p_count += 1
                self.cmd_last = self.dvc_code

            elif state == CORP:
                if self.dvc_code == 0:
                    self._dispatch_firmware_command()

            elif state == PRINT0:
                self.printchan = self.dvc_code
                self.printstring = ""

            elif state == PRINT1:
                if self.dvc_code != 0:
                    self.printstring += chr(self.dvc_code)
                else:
                    if self.printchan in (1, 2):
                        self.on_status(2 + self.printchan, self.printstring)
                    elif self.printchan == 3 and self.debug:
                        self.log(f"dvc print: {self.printstring}")
                    elif self.printchan == 4:
                        self.on_status("text", self.printstring)
                    self.dvc_next_state = START

            elif state in (CMD, CMD0, EXTCMD, CMDX, EXTCMD1, EXTCMD2):
                pass

            elif state == RESET:
                self.cache_reset()
                self.dvc_pixel_count = 0
                self.dvc_lastx = 0
                self.dvc_lasty = 0
                self.dvc_red = 0
                self.dvc_green = 0
                self.dvc_blue = 0
                self.fatal_count = 0
                self.timeout_count = -1
                self.cmd_p_count = 0

            elif state == LATCHED:
                if self.fatal_count == 11680:
                    self.on_refresh_request()
                self.fatal_count += 1
                if self.fatal_count == 120000:
                    self.on_refresh_request()
                if self.fatal_count == 12000000:
                    self.on_refresh_request()
                    self.fatal_count = 41

            elif state == BLKDUP:
                self.next_block(1)

            elif state == BLKRPTSTD:
                self.dvc_code += 2
                self.next_block(self.dvc_code)

            elif state == BLKRPTNSTD:
                self.next_block(self.dvc_code)

            elif state == MODE1:
                self.dvc_size_x = self.dvc_newx
                self.dvc_size_y = self.dvc_code

            elif state == MODE2:
                self.dvc_lastx = 0
                self.dvc_lasty = 0
                self.dvc_pixel_count = 0
                self.cache_reset()
                self.screen_x = self.dvc_size_x * 16
                self.screen_y = self.dvc_size_y * 16 + self.dvc_code
                self.video_detected = self.screen_x != 0 and self.screen_y != 0
                self.dvc_y_clipped = 256 - 16 * self.dvc_code if self.dvc_code > 0 else 0
                if not self.video_detected:
                    self.on_status("text", "No Video")
                    self.screen_x = 640
                    self.screen_y = 100
                else:
                    self.on_resize(self.screen_x, self.screen_y)

            elif state == HUNT:
                if self.dvc_next_state != self.dvc_decoder_state:
                    self.dvc_ib_bcnt = 0
                    self.dvc_ib_acc = 0
                    self.dvc_zero_count = 0
                    self.count_bytes = 0

            elif state == EXIT:
                return 1

            if not broke:
                if self.dvc_next_state == PIXELS and self.dvc_pixel_count == 256:
                    self.next_block(1)
                    self.cache_prune()

            if (self.dvc_decoder_state == self.dvc_next_state
                    and self.dvc_decoder_state not in (HUNT, LATCHED, PRINT1)):
                # dead state, force a resync like the Java client does
                return 6
            self.dvc_decoder_state = self.dvc_next_state
        return n

    def _dispatch_firmware_command(self):
        cmd = self.cmd_last
        if cmd == 1:
            self.dvc_next_state = EXIT
        elif cmd == 2:
            self.dvc_next_state = PRINT0
        elif cmd == 3:
            fps = self.cmd_p_buff[0] if self.cmd_p_count else 0
            self.framerate = fps
            self.on_status("framerate", fps)
        elif cmd in (4, 5):
            pass
        elif cmd == 6:
            self.on_status("text", "Video suspended")
            self.screen_x = 640
            self.screen_y = 100
        elif cmd in (7, 8):
            pass  # terminal-services launch/stop: not applicable to this client
        elif cmd == 9:
            if self.dvc_ib_bcnt & 7:
                self.get_bits(self.dvc_ib_bcnt & 7)
            self.on_change_key()
        elif cmd == 10:
            self.on_seize()
        self.cmd_p_count = 0

    # ---- entry point ------------------------------------------------
    def feed(self, byte_val: int):
        if not self._initialized:
            self._initialized = True
        if not self.dvc_process_inhibit:
            status = self.process_bits(byte_val)
        else:
            status = 0
        if status != 0:
            if self.debug:
                self.log(f"dvc: exit status {status} at block ({self.dvc_lastx},{self.dvc_lasty}) count {self.count_bytes}")
            self.dvc_decoder_state = LATCHED
            self.dvc_next_state = LATCHED
            self.fatal_count = 0
            self.on_refresh_request()
