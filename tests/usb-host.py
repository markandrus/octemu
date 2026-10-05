#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Scripted USB host driving the emulator's packet bench.

  octemu --usb-host /tmp/usbh.sock ... &
  tests/usb-host.py /tmp/usbh.sock enum   # reset + enumerate, print result
  tests/usb-host.py /tmp/usbh.sock msc    # enum + INQUIRY + TEST UNIT READY
  tests/usb-host.py /tmp/usbh.sock midi-recv [SECS]   # drain EP2 IN
  tests/usb-host.py /tmp/usbh.sock midi-send HEX...   # raw MIDI -> EP2 OUT

Speaks the line protocol of qemu/ot-board.c's bench (setup/in/out/reset/
speed). Transfers block until the guest primes the endpoint, so every step
runs under a deadline — a hang IS the failure signal, reported with the op
that stalled. Exit 0 only when the whole scenario passed.
"""
import socket, struct, sys, time


class Stall(Exception):
    """The device answered a control-stage IN/OUT with a STALL handshake."""


class Bench:
    def __init__(self, path, timeout=20.0):
        self.s = socket.create_connection if False else None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.timeout = timeout
        self.buf = b""

    def cmd(self, line, expect=None, timeout=None):
        self.sock.sendall(line.encode() + b"\n")
        return self.wait(expect or line.split()[0], timeout)

    def wait(self, prefix, timeout=None):
        deadline = time.time() + (timeout or self.timeout)
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = self.buf[:i].decode()
                self.buf = self.buf[i + 1:]
                if line.startswith("err"):
                    raise RuntimeError(f"bench: {line}")
                if line.startswith(prefix) or line == "ok":
                    return line
                continue                     # unrelated reply; keep reading
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"no reply to '{prefix}' in time — the "
                                   f"guest never completed the transfer")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError("bench closed the socket")
            self.buf += chunk

    # ---- USB primitives ----
    def reset(self):
        self.cmd("reset", "ok")

    def speed(self, hs):
        self.cmd(f"speed {'hs' if hs else 'fs'}", "ok")

    def setup(self, bm, breq, wval, widx, wlen):
        pkt = struct.pack("<BBHHH", bm, breq, wval, widx, wlen)
        self.cmd(f"setup {pkt.hex()}", "ok")

    def ep_in(self, ep, maxlen, timeout=None):
        r = self.cmd(f"in {ep} {maxlen}", f"in {ep}", timeout)
        parts = r.split()
        if len(parts) > 2 and parts[2] == "stall":
            raise Stall(f"EP{ep} IN stalled")
        return bytes.fromhex(parts[2]) if len(parts) > 2 else b""

    def ep_out(self, ep, data=b"", timeout=None):
        r = self.cmd(f"out {ep} {data.hex()}".rstrip(), f"out {ep}", timeout)
        parts = r.split()
        if len(parts) > 2 and parts[2] == "stall":
            raise Stall(f"EP{ep} OUT stalled")
        return int(parts[2])

    # ---- control transfers ----
    def ctrl_in(self, bm, breq, wval, widx, wlen):
        self.setup(bm, breq, wval, widx, wlen)
        data = self.ep_in(0, wlen)
        self.ep_out(0)                       # status stage: OUT ZLP
        return data

    def ctrl_nodata(self, bm, breq, wval, widx):
        self.setup(bm, breq, wval, widx, 0)
        self.ep_in(0, 64)                    # status stage: IN ZLP


def parse_config(cfg):
    """Yield (type, bytes) descriptors from a config blob."""
    i = 0
    while i + 2 <= len(cfg):
        ln, ty = cfg[i], cfg[i + 1]
        if ln < 2:
            break
        yield ty, cfg[i:i + ln]
        i += ln


def enumerate_device(b, hs=True):
    b.speed(hs)
    b.reset()
    time.sleep(0.3)
    dev = b.ctrl_in(0x80, 6, 0x0100, 0, 18)
    assert len(dev) == 18, f"device descriptor: got {len(dev)} bytes"
    vid, pid = struct.unpack("<HH", dev[8:12])
    b.ctrl_nodata(0x00, 5, 1, 0)             # SET_ADDRESS 1
    cfg9 = b.ctrl_in(0x80, 6, 0x0200, 0, 9)
    assert len(cfg9) == 9, f"config header: got {len(cfg9)} bytes"
    total = struct.unpack("<H", cfg9[2:4])[0]
    cfg = b.ctrl_in(0x80, 6, 0x0200, 0, total)
    assert len(cfg) == total, f"full config: got {len(cfg)}/{total} bytes"
    b.ctrl_nodata(0x00, 9, 1, 0)             # SET_CONFIGURATION 1
    ifaces = [d for t, d in parse_config(cfg) if t == 4]
    eps = [d for t, d in parse_config(cfg) if t == 5]
    print(f"enumerated: VID {vid:04x} PID {pid:04x}, config {total} bytes, "
          f"bNumInterfaces {cfg[4]}, {len(ifaces)} interface descriptors:")
    for d in ifaces:
        print(f"  interface {d[2]} alt {d[3]}: class {d[5]:02x}/{d[6]:02x}"
              f"/{d[7]:02x}, {d[4]} EPs")
    for d in eps:
        mx = struct.unpack('<H', d[4:6])[0]
        print(f"  EP {d[2]:02x} type {d[3] & 3} maxpkt {mx}")
    return dev, cfg


def msc_cbw(tag, datalen, in_dir, cb):
    return struct.pack("<4sIIBBB", b"USBC", tag, datalen,
                       0x80 if in_dir else 0, 0, len(cb)) + \
           cb + bytes(16 - len(cb))


def msc_test(b):
    print("MSC INQUIRY:")
    cbw = msc_cbw(1, 36, True, bytes([0x12, 0, 0, 0, 36, 0]))
    assert b.ep_out(1, cbw) == 31
    data = b.ep_in(1, 36)
    print(f"  {len(data)} bytes: {data[8:36].decode('ascii', 'replace')!r}")
    csw = b.ep_in(1, 13)
    assert csw[:4] == b"USBS", f"bad CSW: {csw.hex()}"
    print(f"  CSW status {csw[12]}")
    inquiry_ok = len(data) == 36 and csw[12] == 0

    print("MSC TEST UNIT READY:")
    cbw = msc_cbw(2, 0, False, bytes([0x00, 0, 0, 0, 0, 0]))
    assert b.ep_out(1, cbw) == 31
    csw = b.ep_in(1, 13)
    assert csw[:4] == b"USBS", f"bad CSW: {csw.hex()}"
    print(f"  CSW status {csw[12]} (nonzero = no medium, fine outside "
          f"DISK MODE)")
    return inquiry_ok


class Rsp:
    """Minimal RSP client for reading guest memory (the DIN FIFO head), so
    the RX conformance can assert what the decoder enqueued. ☠ Attaching
    HALTS the guest, so the ISR that decodes EP2 OUT can only run while the
    target is CONTINUED — every read halts briefly, reads, and resumes."""
    def __init__(self, port):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.connect(("127.0.0.1", port))
        self.buf = b""
        self.s.sendall(b"$?#3f")
        self._recv()
        self.s.sendall(b"$c#63")              # leave the guest running

    def _recv(self, timeout=5):
        self.s.settimeout(timeout)
        while True:
            i = self.buf.find(b"$")
            j = self.buf.find(b"#", i + 1) if i >= 0 else -1
            if i >= 0 and j >= 0 and len(self.buf) >= j + 3:
                pkt = self.buf[i + 1:j]
                self.buf = self.buf[j + 3:]
                self.s.sendall(b"+")
                return pkt.decode()
            self.buf += self.s.recv(4096)

    def read_u32(self, addr):
        self.s.sendall(b"\x03")               # interrupt -> stop reply
        self._recv()
        p = b"m%x,4" % addr
        self.s.sendall(b"$" + p + b"#%02x" % (sum(p) % 256))
        val = bytes.fromhex(self._recv())
        self.s.sendall(b"$c#63")              # resume
        return int.from_bytes(val, "big")     # ColdFire is big-endian


# name, USB-MIDI event packet bytes (CIN nibble + up to 3 MIDI bytes),
# expected DIN byte count the decoder should enqueue:
RX_CLASSES = [
    ("note-on",       bytes([0x09, 0x90, 0x3c, 0x64]), 3),
    ("note-off",      bytes([0x08, 0x80, 0x3c, 0x40]), 3),
    ("poly-at",       bytes([0x0a, 0xa0, 0x3c, 0x40]), 3),
    ("cc",            bytes([0x0b, 0xb0, 0x07, 0x7f]), 3),
    ("program",       bytes([0x0c, 0xc0, 0x05, 0x00]), 2),
    ("chan-press",    bytes([0x0d, 0xd0, 0x40, 0x00]), 2),
    ("pitch-bend",    bytes([0x0e, 0xe0, 0x00, 0x40]), 3),
    ("mtc-qf(F1)",    bytes([0x02, 0xf1, 0x20, 0x00]), 2),
    ("song-pos(F2)",  bytes([0x03, 0xf2, 0x10, 0x20]), 3),
    ("song-sel(F3)",  bytes([0x02, 0xf3, 0x05, 0x00]), 2),
    ("tune-req(F6)",  bytes([0x05, 0xf6, 0x00, 0x00]), 1),
    ("clock(F8)",     bytes([0x0f, 0xf8, 0x00, 0x00]), 1),
    ("start(FA)",     bytes([0x0f, 0xfa, 0x00, 0x00]), 1),
    ("stop(FC)",      bytes([0x0f, 0xfc, 0x00, 0x00]), 1),
    # SysEx F0 7E 7F 09 01 F7 as CIN4(start,3) + CIN7(end,3) = 6 bytes
    ("sysex-6",       bytes([0x04, 0xf0, 0x7e, 0x7f, 0x07, 0x09, 0x01, 0xf7]), 6),
]
FIFO_HEAD = 0x46100b80


def midi_conform(b, gdb_port):
    r = Rsp(gdb_port)
    ok = True
    for name, pkt, want in RX_CLASSES:
        before = r.read_u32(FIFO_HEAD)
        b.ep_out(2, pkt)
        time.sleep(0.15)
        after = r.read_u32(FIFO_HEAD)
        delta = (after - before) & 31            # 32-entry ring, wrapping head
        status = "ok" if delta == want else "MISMATCH"
        if delta != want:
            ok = False
        print(f"  RX {name:<14} enqueued {delta} bytes (want {want})  {status}")
    return ok


def probe_in(b, name, bm, breq, wval, widx, wlen):
    """Issue a control-IN request; report the reply or a stall/timeout."""
    try:
        b.setup(bm, breq, wval, widx, wlen)
        data = b.ep_in(0, wlen, timeout=3)
        try:
            b.ep_out(0, timeout=3)           # status stage
        except TimeoutError:
            pass
        print(f"  {name:<28} -> {len(data)}B {data.hex()}")
        return data
    except (TimeoutError, RuntimeError) as e:
        print(f"  {name:<28} -> STALL/unhandled ({type(e).__name__})")
        return None


def probe_nodata(b, name, bm, breq, wval, widx):
    try:
        b.setup(bm, breq, wval, widx, 0)
        b.ep_in(0, 64, timeout=3)            # status IN ZLP
        print(f"  {name:<28} -> ACK")
        return True
    except (TimeoutError, RuntimeError) as e:
        print(f"  {name:<28} -> STALL/unhandled ({type(e).__name__})")
        return False


def stdreq(b):
    """Measure which standard + string requests the composite answers."""
    enumerate_device(b)
    print("standard requests:")
    probe_in(b, "GET_STATUS(device)",      0x80, 0, 0, 0, 2)
    probe_in(b, "GET_CONFIGURATION",       0x80, 8, 0, 0, 1)
    probe_in(b, "GET_INTERFACE(if2)",      0x81, 10, 0, 2, 1)
    probe_nodata(b, "SET_INTERFACE(if2,0)", 0x01, 11, 0, 2)
    probe_in(b, "GET_STATUS(ep 0x82)",     0x82, 0, 0, 0x82, 2)
    probe_nodata(b, "CLEAR_FEATURE(ep2 halt)", 0x02, 1, 0, 0x02)
    probe_in(b, "GET_DESC device trunc(8)", 0x80, 6, 0x0100, 0, 8)
    print("string descriptors:")
    probe_in(b, "STRING 0 (langid)",       0x80, 6, 0x0300, 0, 255)
    probe_in(b, "STRING 1",                0x80, 6, 0x0301, 0x0409, 255)
    probe_in(b, "STRING 2",                0x80, 6, 0x0302, 0x0409, 255)
    probe_in(b, "STRING 3",                0x80, 6, 0x0303, 0x0409, 255)


def validate_descriptors(b, hs=True):
    """Independent structural check of the composite config against USB 2.0 +
    USB-MIDI 1.0 — a cross-read the firmware does not do itself.

    hs selects which speed's rules apply; it must match the speed the device
    was enumerated at, because some fields (iso bInterval) mean different
    things per speed."""
    _, cfg = enumerate_device(b, hs)
    errs = []
    descs = list(parse_config(cfg))
    # config header
    total = cfg[2] | cfg[3] << 8
    if total != len(cfg):
        errs.append(f"wTotalLength {total} != actual {len(cfg)}")
    if cfg[4] != sum(1 for t, _ in descs if t == 4):
        errs.append(f"bNumInterfaces {cfg[4]} != interface descriptors "
                    f"{sum(1 for t, _ in descs if t == 4)}")
    ifaces = {d[2]: d for t, d in descs if t == 4}
    if set(ifaces) != {0, 1, 2}:
        errs.append(f"interface numbers {sorted(ifaces)} != 0,1,2")
    # class codes: 0=MSC(08/06/50), 1=AudioControl(01/01), 2=MIDIStreaming(01/03)
    want = {0: (8, 6, 0x50), 1: (1, 1, 0), 2: (1, 3, 0)}
    for n, (c, s, p) in want.items():
        d = ifaces.get(n)
        if d and (d[5], d[6]) != (c, s):
            errs.append(f"interface {n} class {d[5]:02x}/{d[6]:02x} != "
                        f"{c:02x}/{s:02x}")
    # AC header (CS_INTERFACE 0x24, subtype 1) baInterfaceNr must list MS iface
    ac = [d for t, d in descs if t == 0x24 and len(d) > 2 and d[2] == 1]
    if not ac:
        errs.append("no AudioControl header (CS_INTERFACE/HEADER)")
    else:
        h = ac[0]
        n_if = h[7] if len(h) > 7 else 0
        linked = list(h[8:8 + n_if])
        if 2 not in linked:
            errs.append(f"AC header does not link MIDIStreaming iface 2 "
                        f"(links {linked})")
    # endpoints: EP 0x81/0x01 bulk (MSC), 0x02/0x82 bulk (MIDI)
    eps = {d[2]: d for t, d in descs if t == 5}
    for addr in (0x81, 0x01, 0x02, 0x82):
        d = eps.get(addr)
        if not d:
            errs.append(f"missing endpoint {addr:#x}")
        elif d[3] & 3 != 2:
            errs.append(f"endpoint {addr:#x} not bulk (type {d[3] & 3})")
    # class-specific MS bulk endpoint descriptors (CS_ENDPOINT 0x25) for EP2
    cs_eps = [d for t, d in descs if t == 0x25]
    if len(cs_eps) < 2:
        errs.append(f"expected 2 CS_ENDPOINT (MS) descriptors, got "
                    f"{len(cs_eps)}")
    # MIDI jacks: at least one IN (subtype 2) and one OUT (subtype 3)
    jacks_in = [d for t, d in descs if t == 0x24 and len(d) > 2 and d[2] == 2]
    jacks_out = [d for t, d in descs if t == 0x24 and len(d) > 2 and d[2] == 3]
    if not jacks_in or not jacks_out:
        errs.append(f"MIDI jacks in/out = {len(jacks_in)}/{len(jacks_out)}")
    if errs:
        print("DESCRIPTOR VALIDATION FAILED:")
        for e in errs:
            print("  ✗", e)
        return False
    print(f"descriptors valid: {len(cfg)}B, 3 interfaces "
          f"(MSC+AC+MS), 4 bulk EPs, {len(jacks_in)} IN / {len(jacks_out)} "
          f"OUT jacks, {len(cs_eps)} CS-endpoint descriptors")
    return True


def validate_audio(b, hs=True):
    """Structural check of the UAC2 audio function added to the usb-midi
    composite: its own IAD (protocol AF 2.0), an AudioControl 2.0 interface
    (header bcdADC 2.00, a Clock Source with a READ-ONLY frequency control,
    an Input Terminal and a USB-streaming Output Terminal wired to it), and an
    AudioStreaming 2.0 interface (alt 0 zero-bandwidth, alt 1 with the iso IN
    EP), Type I PCM 16-bit, 16 channels / 736 B / 500 us at high speed and
    2 channels / 180 B / 1 ms at full speed. The MSC + MIDI half must still be
    present. hs must match the speed the device was enumerated at: iso
    bInterval and the channel count both depend on it."""
    _, cfg = enumerate_device(b, hs)
    errs = []
    descs = list(parse_config(cfg))
    if (cfg[2] | cfg[3] << 8) != len(cfg):
        errs.append(f"wTotalLength {cfg[2] | cfg[3] << 8} != actual {len(cfg)}")
    ifaces = [d for t, d in descs if t == 4]
    # ☠ Assert bNumInterfaces against the descriptors actually present, not a
    # magic number. It has been 3, then 4, then 5 as the layout changed, and a
    # hardcoded count fails for the wrong reason every time.
    distinct = sorted({d[2] for d in ifaces})
    if cfg[4] != len(distinct):
        errs.append(f"bNumInterfaces {cfg[4]} != {len(distinct)} distinct "
                    f"interface descriptors {distinct}")
    # Interface Association Descriptors. macOS (usbaudiod) walks these to
    # decide what functions exist; with a single IAD spanning AC+MS+AS it
    # logged AUAErrorCode.noAudioFunctions and refused the device. Two
    # separate audio functions, one MIDI (1.0), one streaming (2.0).
    iads = [d for t, d in descs if t == 0x0b]
    if len(iads) != 2:
        errs.append(f"{len(iads)} IADs, want 2 (MIDI function + audio function)")
    else:
        for iad, want_first, proto, what in ((iads[0], 1, 0x00, "MIDI"),
                                             (iads[1], 3, 0x20, "audio")):
            if iad[2] != want_first or iad[3] != 2:
                errs.append(f"{what} IAD covers iface {iad[2]}+{iad[3]}, "
                            f"want {want_first}+2")
            if iad[4] != 0x01 or iad[5] != 0x00:
                errs.append(f"{what} IAD class/subclass {iad[4]:#04x}/"
                            f"{iad[5]:#04x} != 0x01/0x00")
            if iad[6] != proto:
                errs.append(f"{what} IAD bFunctionProtocol {iad[6]:#04x} != "
                            f"{proto:#04x}")
    byclass = {(d[5], d[6]) for d in ifaces}
    for cs, what in [((8, 6), "MSC"), ((1, 1), "AudioControl"),
                     ((1, 3), "MIDIStreaming")]:
        if cs not in byclass:
            errs.append(f"{what} interface {cs[0]:02x}/{cs[1]:02x} missing")
    for addr in (0x81, 0x01, 0x02, 0x82):
        if addr not in {d[2] for t, d in descs if t == 5}:
            errs.append(f"MSC/MIDI endpoint {addr:#x} missing")
    # ☠ Locate the interfaces by CLASS/PROTOCOL, never by number.
    as_num = next((d[2] for d in ifaces if d[5] == 1 and d[6] == 2), None)
    ac_num = next((d[2] for d in ifaces
                   if d[5] == 1 and d[6] == 1 and d[7] == 0x20), None)
    if as_num is None:
        errs.append("no AudioStreaming interface (class 1 subclass 2)")
        as_num = -1
    if ac_num is None:
        errs.append("no AudioControl 2.0 interface (class 1/1, protocol 0x20)")
        ac_num = -1
    as_alts = [d for d in ifaces if d[2] == as_num]
    if len(as_alts) != 2:
        errs.append(f"interface {as_num} has {len(as_alts)} alt settings (want 2)")
    for d in as_alts:
        if d[7] != 0x20:
            errs.append(f"iface{as_num} alt{d[3]} bInterfaceProtocol "
                        f"{d[7]:#04x} != 0x20 (IP 2.0)")
    alt0 = [d for d in as_alts if d[3] == 0]
    alt1 = [d for d in as_alts if d[3] == 1]
    if not alt0 or alt0[0][4] != 0:
        errs.append(f"iface{as_num} alt0 is not zero-bandwidth")
    if not alt1 or alt1[0][4] != 1:
        errs.append(f"iface{as_num} alt1 does not have exactly 1 endpoint")
    # class-specific descriptors, attributed to their interface
    cur = None
    ac_hdr = clock = in_term = out_term = None
    as_general = fmt = iso_ep = cs_ep = None
    for t, d in descs:
        if t == 4:
            cur = d[2]
        elif cur == ac_num and t == 0x24:
            if d[2] == 1:
                ac_hdr = d
            elif d[2] == 0x0A:
                clock = d
            elif d[2] == 2:
                in_term = d
            elif d[2] == 3:
                out_term = d
        elif cur == as_num and t == 0x24 and d[2] == 1:
            as_general = d
        elif cur == as_num and t == 0x24 and d[2] == 2:
            fmt = d
        elif cur == as_num and t == 5:
            iso_ep = d
        elif cur == as_num and t == 0x25:
            cs_ep = d
    nch = 16 if hs else 2
    if not ac_hdr:
        errs.append("AudioControl 2.0 has no HEADER")
    else:
        if len(ac_hdr) != 9 or (ac_hdr[3] | ac_hdr[4] << 8) != 0x0200:
            errs.append(f"AC header is not bcdADC 2.00 ({ac_hdr.hex()})")
        want = 9 + sum(len(x) for x in (clock, in_term, out_term) if x)
        got = ac_hdr[6] | ac_hdr[7] << 8
        if got != want:
            errs.append(f"AC header wTotalLength {got} != {want}")
    if not clock:
        errs.append("AudioControl has no CLOCK_SOURCE — a UAC2 host cannot "
                    "resolve the stream's clock")
    else:
        if len(clock) != 8:
            errs.append(f"CLOCK_SOURCE length {len(clock)} != 8")
        if clock[4] & 3 != 1:
            errs.append(f"CLOCK_SOURCE bmAttributes {clock[4]:#04x}: not an "
                        f"internal fixed clock")
        # ☠ frequency control READ-ONLY (0b01): a host-programmable clock
        # would oblige the device to accept SET_CUR, a control OUT with a
        # data stage the stock EP0 stack does not have.
        if clock[5] & 3 != 1:
            errs.append(f"CLOCK_SOURCE bmControls {clock[5]:#04x}: frequency "
                        f"control is not read-only")
    if not in_term:
        errs.append("AudioControl has no INPUT_TERMINAL")
    elif len(in_term) != 17:
        errs.append(f"INPUT_TERMINAL length {len(in_term)} != 17 (2.0 layout)")
    else:
        if clock and in_term[7] != clock[3]:
            errs.append(f"INPUT_TERMINAL bCSourceID {in_term[7]} != clock "
                        f"id {clock[3]}")
        if in_term[8] != nch:
            errs.append(f"INPUT_TERMINAL bNrChannels {in_term[8]} != {nch}")
    if not out_term:
        errs.append("AudioControl has no OUTPUT_TERMINAL")
    elif len(out_term) != 12:
        errs.append(f"OUTPUT_TERMINAL length {len(out_term)} != 12 (2.0 layout)")
    else:
        if (out_term[4] | out_term[5] << 8) != 0x0101:
            errs.append("OUTPUT_TERMINAL is not USB Streaming (0x0101)")
        if in_term and out_term[7] != in_term[3]:
            errs.append(f"OUTPUT_TERMINAL bSourceID {out_term[7]} does not "
                        f"name the INPUT_TERMINAL (id {in_term[3]})")
        if clock and out_term[8] != clock[3]:
            errs.append(f"OUTPUT_TERMINAL bCSourceID {out_term[8]} != clock "
                        f"id {clock[3]}")
    if not as_general:
        errs.append(f"no AS_GENERAL under AudioStreaming interface {as_num}")
    elif len(as_general) != 16:
        errs.append(f"AS_GENERAL length {len(as_general)} != 16 (2.0 layout)")
    else:
        if out_term and as_general[3] != out_term[3]:
            errs.append(f"AS_GENERAL bTerminalLink {as_general[3]} does not "
                        f"name the OUTPUT_TERMINAL (id {out_term[3]})")
        if as_general[5] != 1 or not (as_general[6] & 1):
            errs.append("AS_GENERAL is not Format Type I / PCM")
        if as_general[10] != nch:
            errs.append(f"AS_GENERAL bNrChannels {as_general[10]} != {nch}")
    if not fmt:
        errs.append(f"no FORMAT_TYPE under AudioStreaming interface {as_num}")
    elif len(fmt) != 6 or fmt[3] != 1 or fmt[4] != 2 or fmt[5] != 16:
        errs.append(f"FORMAT_TYPE_I 2.0 {fmt.hex()} != type I, 2-byte "
                    f"subslot, 16 bits")
    want_pkt, want_int = (736, 3) if hs else (180, 1)
    if not iso_ep:
        errs.append(f"no iso endpoint under iface{as_num}")
    else:
        if len(iso_ep) != 7:
            errs.append(f"iso EP descriptor length {len(iso_ep)} != 7")
        if iso_ep[2] != 0x83:
            errs.append(f"iso EP address {iso_ep[2]:#x} != 0x83")
        if iso_ep[3] & 3 != 1:
            errs.append(f"iso EP type {iso_ep[3] & 3} != 1 (isochronous)")
        if (iso_ep[3] >> 2) & 3 != 1:
            errs.append(f"iso EP sync type {(iso_ep[3] >> 2) & 3} != 1 "
                        f"(asynchronous)")
        mx = iso_ep[4] | iso_ep[5] << 8
        if mx != want_pkt:
            errs.append(f"iso EP wMaxPacketSize {mx} != {want_pkt}")
        # ☠ bInterval on an ISO endpoint means different things per speed:
        # 1 ms frames at full speed, an exponent over 125 us microframes at
        # high speed. 3 = 500 us, the poll that lets 16 channels fit one
        # 1024 B transaction; the bench ignores it, real hosts do not.
        if iso_ep[6] != want_int:
            errs.append(f"iso EP bInterval {iso_ep[6]} != {want_int} for "
                        f"{'high' if hs else 'full'} speed")
        if mx > (1024 if hs else 1023):
            errs.append(f"iso EP wMaxPacketSize {mx} exceeds one transaction")
    if not cs_ep:
        errs.append(f"no CS_ENDPOINT (audio iso EP) descriptor under iface{as_num}")
    elif len(cs_ep) != 8:
        errs.append(f"CS_ENDPOINT length {len(cs_ep)} != 8 (2.0 layout)")
    if errs:
        print("AUDIO DESCRIPTOR VALIDATION FAILED:")
        for e in errs:
            print("  ✗", e)
        return False
    print(f"audio descriptors valid: config {len(cfg)}B, {cfg[4]} interfaces, "
          f"{len(iads)} IADs, UAC2 iface{ac_num} AC (clock {clock[3]:#x}, "
          f"IT {in_term[3]:#x}, OT {out_term[3]:#x}) + iface{as_num} AS "
          f"alt0/alt1, Type I 16-bit {nch} ch, iso EP 0x83 {want_pkt}B "
          f"bInterval {want_int}")
    return True


def uac2_clock_requests(b, hs=True):
    """The class requests a UAC2 host issues to the clock source before it
    publishes a device, and one it must NOT get an answer to:

      GET RANGE CS_SAM_FREQ_CONTROL -> 1 subrange, 44100..44100 step 0
      GET CUR   CS_SAM_FREQ_CONTROL -> 44100
      GET CUR   CS_CLOCK_VALID      -> 1
      GET CUR   of an unsupported selector -> STALL (the stock handler)
      SET CUR   CS_SAM_FREQ_CONTROL 44100 -> data stage taken, status ACKed
                (the clock is fixed, but some UAC2 hosts SET the rate they
                have just read and give the device up if it STALLs)
      SET CUR   CS_SAM_FREQ_CONTROL 48000 -> the status stage STALLs, and
                EP0 answers the next request
      GET_MAX_LUN on interface 0    -> still answered (the shim only
                                        intercepts the audio AC interface)

    ☠ Without the bench's EP0 STALL model the last two would be a hang and a
    pass-by-timeout; with it a stall is an answer that can be asserted."""
    _, cfg = enumerate_device(b, hs)
    ifaces = [d for t, d in parse_config(cfg) if t == 4]
    ac_num = next(d[2] for d in ifaces if d[5] == 1 and d[6] == 1 and d[7] == 0x20)
    cur = None
    clk = None
    for t, d in parse_config(cfg):
        if t == 4:
            cur = d[2]
        elif cur == ac_num and t == 0x24 and d[2] == 0x0A:
            clk = d[3]
    widx = (clk << 8) | ac_num
    ok = True

    def expect(what, got, want):
        nonlocal ok
        if got == want:
            print(f"  ok: {what} -> {got.hex()}")
        else:
            print(f"  ✗ {what} -> {got.hex()!r}, want {want.hex()}")
            ok = False

    expect("RANGE sample frequency",
           b.ctrl_in(0xA1, 2, 1 << 8, widx, 14),
           struct.pack("<H", 1) + struct.pack("<III", 44100, 44100, 0))
    expect("CUR sample frequency",
           b.ctrl_in(0xA1, 1, 1 << 8, widx, 4), struct.pack("<I", 44100))
    expect("CUR clock valid", b.ctrl_in(0xA1, 1, 2 << 8, widx, 1), b"\x01")
    # a short wLength must be honoured (min(wLength, len))
    expect("CUR sample frequency, wLength 2",
           b.ctrl_in(0xA1, 1, 1 << 8, widx, 2), struct.pack("<H", 44100))
    try:
        got = b.ctrl_in(0xA1, 1, 7 << 8, widx, 4)
        print(f"  ✗ unsupported selector answered {got.hex()} instead of STALL")
        ok = False
    except Stall:
        print("  ok: unsupported clock selector -> STALL")
    try:
        got = b.ctrl_in(0xA1, 2, 1 << 8, (0x7f << 8) | ac_num, 14)
        print(f"  ✗ unknown entity answered {got.hex()} instead of STALL")
        ok = False
    except Stall:
        print("  ok: unknown entity -> STALL")

    def set_cur_freq(rate):
        """SET CUR of the clock's sample frequency: SETUP, the 4-byte OUT data
        stage, then the IN status stage. Returns "ACK", "status STALL",
        "data-stage STALL" or "data stage never accepted"."""
        b.setup(0x21, 1, 1 << 8, widx, 4)
        try:
            b.ep_out(0, struct.pack("<I", rate), timeout=10)
        except Stall:
            return "data-stage STALL"
        except TimeoutError:
            return "data stage never accepted"
        try:
            b.ep_in(0, 64)
            return "ACK"
        except Stall:
            return "status STALL"

    for rate, want in ((44100, "ACK"), (48000, "status STALL")):
        got = set_cur_freq(rate)
        if got == want:
            print(f"  ok: SET CUR sample frequency {rate} -> {got}")
        else:
            print(f"  ✗ SET CUR sample frequency {rate} -> {got}, want {want}")
            ok = False
            if got == "data stage never accepted":
                return False                 # EP0 is still waiting on it
        if rate == 44100:
            expect("CUR sample frequency after SET CUR 44100",
                   b.ctrl_in(0xA1, 1, 1 << 8, widx, 4), struct.pack("<I", 44100))
    expect("RANGE sample frequency after the rejected SET CUR",
           b.ctrl_in(0xA1, 2, 1 << 8, widx, 14),
           struct.pack("<H", 1) + struct.pack("<III", 44100, 44100, 0))
    lun = b.ctrl_in(0xA1, 0xFE, 0, 0, 1)
    if len(lun) == 1:
        print(f"  ok: MSC GET_MAX_LUN still answered ({lun.hex()})")
    else:
        print("  ✗ MSC GET_MAX_LUN no longer answered")
        ok = False
    return ok


def audio_get_interface(b, iface):
    """GET_INTERFACE on the given interface -> the alt setting byte."""
    d = b.ctrl_in(0x81, 10, 0, iface, 1)
    return d[0] if d else None


def audio_stream_iface(cfg):
    """The AudioStreaming interface number, read from the config.

    ☠ Never hardcode it. The descriptors were renumbered when they split into
    two audio functions, and every scenario that assumed "interface 3" then
    talked to the wrong interface: the shim ignored it, EP3 never came up, and
    the gates failed or — worse — went quiet in a way that looked like a pass.
    """
    i = 0
    while i < len(cfg):
        ln = cfg[i]
        if ln == 0:
            break
        if cfg[i + 1] == 4 and cfg[i + 5] == 1 and cfg[i + 6] == 2:
            return cfg[i + 2]
        i += ln
    sys.exit("no AudioStreaming interface (class 1 subclass 2) in the config")


def audio_set_interface(b, iface, alt):
    b.ctrl_nodata(0x01, 11, alt, iface)


def audio_stream_params(cfg, hs):
    """What the AudioStreaming interface DESCRIBES, read from the config so
    every scenario measures against the device's own claim: channels, bytes
    per frame, iso packet size, and the poll interval in ms (speed-dependent:
    bInterval is 1 ms frames at full speed, 2^(b-1) x 125 us microframes at
    high speed). Understands UAC1 (Format Type I carries the channel count)
    and UAC2 (AS_GENERAL carries it; the 2.0 Format Type I is 6 bytes)."""
    as_num = audio_stream_iface(cfg)
    cur = None
    ch = sub = maxpkt = binterval = None
    for t, d in parse_config(cfg):
        if t == 4:
            cur = d[2]
        elif cur == as_num and t == 0x24 and d[2] == 1 and len(d) == 16:
            ch = d[10]                               # UAC2 AS_GENERAL
        elif cur == as_num and t == 0x24 and d[2] == 2:
            if len(d) == 6:
                sub = d[4]                           # UAC2 Format Type I
            else:
                ch, sub = d[4], d[5]                 # UAC1 Format Type I
        elif cur == as_num and t == 5:
            maxpkt = d[4] | d[5] << 8
            binterval = d[6]
    if None in (ch, sub, maxpkt, binterval):
        sys.exit(f"AudioStreaming iface {as_num}: incomplete format "
                 f"(ch={ch} sub={sub} maxpkt={maxpkt} bInterval={binterval})")
    interval_ms = (2 ** (binterval - 1)) * 0.125 if hs else float(binterval)
    return {"iface": as_num, "channels": ch, "frame_bytes": ch * sub,
            "maxpkt": maxpkt, "interval_ms": interval_ms}


def check_cadence(sizes, frame_bytes=4, interval_ms=1.0):
    """The 44.1 kHz iso rate: every packet carries floor or ceil of
    44.1 x interval frames (44/45 per 1 ms, 22/23 per 500 us), and the
    AVERAGE rate is exactly 44.1 x interval frames per packet.

    ☠ This used to require every run of 10 packets to carry exactly 441 frames
    (9x44 + 45). That premise died with the rate servo: the payload now nudges
    the drain rate by +-0.1 frame/packet to track the host's clock, because a
    fixed 44.1 lets the device and host clocks drift apart until the ring
    overruns or underruns -- measured on hardware as bursts of discontinuities
    every few seconds. A fixed-pattern assertion would forbid the very
    correction that removes those clicks, so the rate is checked instead: the
    mean over the whole capture must be 44.1 within a tolerance far tighter
    than any audible drift, and no single packet may be a size the format does
    not allow.
    """
    ok = True
    nominal = 44.1 * interval_ms
    lo, hi = int(nominal), int(nominal) + 1
    # The servo's authority is +-0.1 frame PER PACKET at either speed (it
    # nudges the x100 accumulator step by +-10), so a saturated servo can
    # emit floor(nominal - 0.1) frames: 21 at high speed. ☠ The bench drains
    # the ring the instant a packet is queued, so here the servo is ALWAYS
    # saturated low; the gate can only check that the rate is sane, not that
    # the servo centres — a real host's pacing is a hardware measurement.
    allowed = tuple(n * frame_bytes for n in (lo - 1, lo, hi))
    bad = [s for s in sizes if s not in allowed]
    if bad:
        print(f"  ✗ {len(bad)}/{len(sizes)} packets not one of {allowed} B "
              f"(e.g. {bad[:5]})")
        ok = False
    frames = sum(sizes) // frame_bytes
    mean = frames / len(sizes) if sizes else 0
    tol = 0.15
    if abs(mean - nominal) > tol:
        print(f"  ✗ mean {mean:.3f} frames/packet, want {nominal:.2f} +- {tol:.3f}")
        ok = False
    print(f"  {len(sizes)} packets, sizes {sorted(set(sizes))}, mean "
          f"{mean:.3f} frames/packet {'(rate ok)' if ok else '- RATE WRONG'}")
    return ok


def coexist(b, rounds=20):
    """Interleave MSC transactions (EP1) with MIDI drain (EP2) on the bus,
    proving the ISR services both endpoints' completions without either
    corrupting the other. Returns (msc_ok, clocks, starts). The TX drop
    counter is checked by the caller over RSP (no gdb contention here)."""
    msc_ok = True
    clocks = starts = 0
    tag = 100
    for _ in range(rounds):
        # MSC INQUIRY on EP1
        tag += 1
        cbw = msc_cbw(tag, 36, True, bytes([0x12, 0, 0, 0, 36, 0]))
        try:
            if b.ep_out(1, cbw) != 31:
                msc_ok = False
            data = b.ep_in(1, 36, timeout=5)
            csw = b.ep_in(1, 13, timeout=5)
            if csw[:4] != b"USBS" or len(data) != 36:
                msc_ok = False
        except (TimeoutError, RuntimeError):
            msc_ok = False
        # MIDI drain on EP2 (a couple of packets)
        try:
            pk = b.ep_in(2, 64, timeout=2)
            for i in range(0, len(pk), 4):
                ev = pk[i:i + 4]
                if ev[:2] == b"\x0f\xf8":
                    clocks += 1
                elif ev[:2] == b"\x0f\xfa":
                    starts += 1
        except TimeoutError:
            pass
    return msc_ok, clocks, starts


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    b = Bench(sys.argv[1])
    scenario = sys.argv[2]
    if scenario == "enum":
        enumerate_device(b)
    elif scenario == "msc":
        enumerate_device(b)
        ok = msc_test(b)
        sys.exit(0 if ok else 1)
    elif scenario == "midi-recv":
        secs = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
        end = time.time() + secs
        while time.time() < end:
            try:
                pkts = b.ep_in(2, 64, timeout=max(0.5, end - time.time()))
            except TimeoutError:
                continue
            for i in range(0, len(pkts), 4):
                print("event:", pkts[i:i + 4].hex())
    elif scenario == "midi-send":
        raw = bytes.fromhex("".join(sys.argv[3:]))
        # USB-MIDI encode: channel-voice only (bench-side convenience)
        out = b""
        i = 0
        while i < len(raw):
            st = raw[i]
            n = 3 if (st & 0xF0) not in (0xC0, 0xD0) else 2
            if st >= 0xF8:
                out += bytes([0x0F, st, 0, 0])
                i += 1
                continue
            evt = raw[i:i + n]
            out += bytes([st >> 4]) + evt + bytes(4 - 1 - len(evt))
            i += n
        print(f"sending {len(out)} packet bytes to EP2 OUT")
        b.ep_out(2, out)
    elif scenario == "coexist":
        # MSC + MIDI interleaved, both must stay correct (enum + sync-gate
        # poke by the caller; the drop counter is checked by the caller).
        msc_ok, clocks, starts = coexist(b)
        print(f"MSC ok={msc_ok}  MIDI clocks={clocks} starts={starts}")
        sys.exit(0 if (msc_ok and clocks > 0) else 1)
    elif scenario == "stdreq":
        stdreq(b)
    elif scenario == "validate":
        sys.exit(0 if validate_descriptors(b) else 1)
    elif scenario == "audio-validate":
        # P1 gate: the composite enumerates with the UAC2 audio function
        # (AC + AS + iso EP), MSC + MIDI still intact. "fs" checks the
        # full-speed config (2 channels).
        sys.exit(0 if validate_audio(b, "fs" not in sys.argv[3:]) else 1)
    elif scenario == "audio-uac2-ctrl":
        # the clock-source class requests a UAC2 host needs, and the stalls
        sys.exit(0 if uac2_clock_requests(b, "fs" not in sys.argv[3:]) else 1)
    elif scenario == "audio-alt":
        # P2 gate: SET_INTERFACE(3, alt) brings EP3 up/down, observable through
        # GET_INTERFACE and the bench delivering (or not) an iso packet.
        hs = "fs" not in sys.argv[3:]
        _, cfg = enumerate_device(b, hs)
        fmt = audio_stream_params(cfg, hs)
        as_if = fmt["iface"]
        ok = True
        if audio_get_interface(b, as_if) != 0:
            print(f"  ✗ iface{as_if} does not start at alt 0"); ok = False
        audio_set_interface(b, as_if, 1)
        if audio_get_interface(b, as_if) != 1:
            print(f"  ✗ SET_INTERFACE({as_if},1) did not select alt 1"); ok = False
        else:
            print("  alt 1 selected")
        # With alt 1 up and the machine producing blocks, an EP3 IN completes.
        try:
            pk = b.ep_in(3, fmt["maxpkt"], timeout=5)
            print(f"  EP3 delivered {len(pk)} B on alt 1")
            if len(pk) == 0:
                print("  ✗ EP3 delivered an empty packet on alt 1"); ok = False
        except TimeoutError:
            print("  ✗ EP3 never delivered on alt 1"); ok = False
        audio_set_interface(b, as_if, 0)
        if audio_get_interface(b, as_if) != 0:
            print(f"  ✗ SET_INTERFACE({as_if},0) did not select alt 0"); ok = False
        else:
            print("  alt 0 selected (torn down)")
        # On alt 0 no further packets: the guest stops priming EP3.
        try:
            b.ep_in(3, fmt["maxpkt"], timeout=2)
            print("  ✗ EP3 still delivered after alt 0"); ok = False
        except TimeoutError:
            print("  EP3 idle on alt 0 (correct)")
        sys.exit(0 if ok else 1)
    elif scenario == "audio-cadence":
        # P3 gate: pull N packets on alt 1 and assert the 44/45-frame cadence.
        # argv: N [fs]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        hs = "fs" not in sys.argv[4:]
        _, cfg = enumerate_device(b, hs)
        fmt = audio_stream_params(cfg, hs)
        as_if = fmt["iface"]
        print(f"  stream: {fmt['channels']} ch, {fmt['frame_bytes']} B/frame, "
              f"maxpkt {fmt['maxpkt']}, poll {fmt['interval_ms']} ms "
              f"({'high' if hs else 'full'} speed)")
        audio_set_interface(b, as_if, 1)
        sizes = []
        for _ in range(n):
            pk = b.ep_in(3, fmt["maxpkt"], timeout=5)
            sizes.append(len(pk))
        audio_set_interface(b, as_if, 0)
        sys.exit(0 if check_cadence(sizes, fmt["frame_bytes"],
                                    fmt["interval_ms"]) else 1)
    elif scenario == "audio-stream":
        # P4 driver: pull packets on alt 1 into OUTFILE. Keep pulling until the
        # capture has caught a burst (>= a few thousand frames of real audio)
        # plus a tail, so the window is guaranteed to carry audio; hard cap at
        # MAXPKT. argv: OUTFILE [MAXPKT]
        # argv: OUTFILE [MAXPKT] [fs]. The capture is raw frames of the
        # described width (frame_bytes, printed); a frame counts as audio when
        # ANY channel is loud, so a burst on track 5 alone is still a burst.
        out = sys.argv[3]
        opts = sys.argv[4:]
        maxpkt = next((int(o) for o in opts if o.isdigit()), 60000)
        hs = "fs" not in opts
        # frames=N: keep capturing until N frames are in hand (a walk with
        # several bursts), instead of stopping after the first burst.
        want_frames = next((int(o[7:]) for o in opts if o.startswith("frames=")), 0)
        _, cfg = enumerate_device(b, hs)
        fmt = audio_stream_params(cfg, hs)
        as_if, fb = fmt["iface"], fmt["frame_bytes"]
        print(f"  stream: {fmt['channels']} ch, {fb} B/frame, maxpkt "
              f"{fmt['maxpkt']}, poll {fmt['interval_ms']} ms")
        audio_set_interface(b, as_if, 1)
        data = bytearray()
        nz = 0
        caught_at = None
        for i in range(maxpkt):
            pk = b.ep_in(3, fmt["maxpkt"], timeout=5)
            for off in range(0, len(pk) - fb + 1, fb):
                if any(abs(int.from_bytes(pk[off + c:off + c + 2], "little",
                                          signed=True)) > 64
                       for c in range(0, fb, 2)):
                    nz += 1
            data += pk
            if want_frames:
                if len(data) // fb >= want_frames:
                    break
                continue
            if caught_at is None and nz >= 4000:
                caught_at = len(data) // fb        # a burst is in hand
            if caught_at is not None and len(data) // fb - caught_at > 8000:
                break                              # got the burst + a tail
        audio_set_interface(b, as_if, 0)
        open(out, "wb").write(data)
        print(f"wrote {len(data)} B ({len(data)//fb} frames of {fb} B, "
              f"{nz} with audio) to {out}")
        sys.exit(0 if nz >= 2000 else 1)
    elif scenario == "enum-conform":
        # P3 gate: structural descriptor validity + the standard requests a
        # host issues, including CLEAR_FEATURE(ENDPOINT_HALT) on EP2.
        ok = validate_descriptors(b)
        print("standard requests:")
        # GET_STATUS(device) is stock-handled and returns 2 bytes (the exact
        # value is stock behaviour, unchanged from the shipped MSC firmware);
        # GET_CONFIGURATION/GET_INTERFACE have right answers to assert;
        # CLEAR_FEATURE(ep2) is the composite's own new obligation.
        checks = [
            ("GET_STATUS(device)",   len(probe_in(b, "GET_STATUS(device)", 0x80, 0, 0, 0, 2) or b"") == 2),
            ("GET_CONFIGURATION",    probe_in(b, "GET_CONFIGURATION", 0x80, 8, 0, 0, 1) == b"\x01"),
            ("GET_INTERFACE(if2)",   probe_in(b, "GET_INTERFACE(if2)", 0x81, 10, 0, 2, 1) == b"\x00"),
            ("SET_INTERFACE(if2,0)", probe_nodata(b, "SET_INTERFACE(if2,0)", 0x01, 11, 0, 2)),
            ("CLEAR_FEATURE(ep2)",   probe_nodata(b, "CLEAR_FEATURE(ep2 halt)", 0x02, 1, 0, 0x02)),
        ]
        for name, passed in checks:
            if not passed:
                print(f"  ✗ {name} failed")
                ok = False
        sys.exit(0 if ok else 1)
    elif scenario == "midi-conform":
        # RX conformance: enumerate at the given speed, then send every
        # MIDI message class in on EP2 OUT and assert the decoder enqueued
        # the right byte count into the DIN FIFO (read over RSP).
        hs = len(sys.argv) < 5 or sys.argv[4] != "fs"
        gdb_port = int(sys.argv[3])
        enumerate_device(b, hs=hs)
        print(f"RX conformance ({'HS' if hs else 'FS'}):")
        sys.exit(0 if midi_conform(b, gdb_port) else 1)
    else:
        print(f"unknown scenario {scenario}")
        sys.exit(2)


if __name__ == "__main__":
    main()
