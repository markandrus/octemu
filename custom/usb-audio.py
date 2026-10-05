#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""usb-audio.py — a UAC2 audio-streaming IN endpoint added to the usb-midi
composite: at high speed the eight tracks as sixteen channels (track N's L/R
on channels 2N-1/2N, post-FX, pre-fader), at full speed their stereo sum, all
44.1 kHz 16-bit.

Unlike usb-midi (which revived dormant firmware), there is NO audio-streaming
code in the OS, and the new code (four grown config descriptors + the EP3 iso
servicing + a 36 KB audio ring) is far larger than the ~408 B of image free
space usb-midi leaves. So the payload lives on the CF card as /USBAUDIO.BIN
and is loaded at runtime into flex-heap pages that stage2 takes out of the
firmware's free list (--heap-reserve), where nothing else will ever be placed.

Pieces assembled here:

- custom/coldfire/usb-audio.s (the payload, position-independent via a
  relocation table): stage2 runtime installer, the SET/GET_INTERFACE + EP0
  clock-request + usb_isr + descriptor-clamp shims, the per-block producer,
  the iso packet builder, and the four grown UAC2 configs (generated below as
  an assembly include). Written to the card as /USBAUDIO.BIN.
- custom/coldfire/usb-audio-tramp.s (trampoline, linked into the 408 B image
  slack after usb-midi's overlay): hooks fs_card_detect_poll, and once the
  card is mounted loads the payload, invalidates the I-cache, and jsr's
  stage2.
- custom/coldfire/usb-audio-{alloc,report,guard}.s: the page allocator, the
  on-screen status reporter, and the image-resident hook guard, each in its
  own measured free zone.

Only a few in-image writes land here, all expect()-guarded: those blobs in
their free zones and the 6-byte jmp into fs_card_detect_poll. Everything else
the payload needs is installed AT RUNTIME by stage2 (into image code, but only
after the card blob is confirmed loaded — so an image without the card file
is exactly the stock usb-midi composite).

Usage (the Makefile passes the shipping flags, USBAUDIO_FLAGS):
  custom/usb-audio.py --in IMAGE.os --out IMAGE.os [--payload out/USBAUDIO.BIN]
                      --heap-reserve N --load-base ADDR --dma-at ADDR
"""
import argparse
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 0x40000400
# ☠ HARDWARE-MEASURED, not canary-derived. 0x48001000 was wrong: on a real
# unit, writes past ~0x48003000 destroy image code, and 0x48010000 is so badly
# occupied that the corruption takes out the exception screen's own display.
# 0x49000000 write-verified clean for 256 KB on hardware
# (cf/memtest-probe.s). ☠ Re-measure before moving this again.
SCRATCH = 0x49000000                    # payload load address in SDRAM scratch
PAYLOAD_HDR = 28                        # self-describing header, see below
# Header v2, all big-endian longwords:
#   +0  magic 'OTPL'
#   +4  total length (header + blob + relocation table)
#   +8  checksum (sum of every longword, minus this field, equals this field)
#   +12 entry OFFSET from the load base (not an absolute address any more:
#       the loader picks the address, so the payload cannot know it)
#   +16 relocation table offset from the load base
#   +20 relocation entry count
#   +24 link base — the address the code was linked at, so the loader can
#       compute the fixup delta without a build-time constant of its own
PAYLOAD_MAGIC = 0x4F54504C              # 'OTPL'
PAYLOAD_MAX = 0x10000                   # largest card blob the trampoline loads
# The image free slack after usb-midi's overlay (the free zone at 0x400d24d0;
# usb-midi occupies up to 0x400d2b44, leaving 408 B to 0x400d2cdc).
ZONE_TRAMP = 0x400D2B44
ZONE_END = 0x400D2CDC
ASM = os.path.join(ROOT, "custom/coldfire/usb-audio.s")

# ☠ TEST-ONLY. --omit-qh-clear rebuilds the payload WITHOUT clearing EP3's
# device queue head — the defect that crashed a real unit twice by letting the
# USB controller DMA through the garbage in its token and buffer pointers. It
# exists so tests/usb-audio-qh-test.sh can prove the emulator's UNINITIALIZED
# dQH detector actually fires: a detector that has never been seen to fire
# proves nothing. NEVER ship a payload built with this.
QH_CLEAR_SNIPPETS = (
    """    bsr     audio_qh_resolve        | %a0 = EP3 dQH, from the controller
    moveq   #15,%d1
1:  clrl    %a0@+
    subql   #1,%d1
    bpls    1b
""",
    """    clrl    %a0@(12)                | TOKEN — the field the firmware clears
""",
    """    | Same reason: clear EP3's queue head before anything can enumerate.
    bsr     audio_qh_resolve        | %a0 = EP3 dQH, from the controller
    moveq   #15,%d1
0:  clrl    %a0@+
    subql   #1,%d1
    bpls    0b
""",
)
ASM_TRAMP = os.path.join(ROOT, "custom/coldfire/usb-audio-tramp.s")
# ☠ The hook guard is IMAGE-resident by necessity: it exists to survive
# whatever erased the payload. It goes in the tail of the reporter's zone --
# 0x400d24d0 is NOT free despite being listed as the largest safe window: only
# its first 64 bytes are zero, the rest is usb-midi's overlay, which the
# expect() guard caught when this was first placed there.
ASM_GUARD = os.path.join(ROOT, "custom/coldfire/usb-audio-guard.s")
# ☠ The on-screen reporter needs its own zone: the trampoline's has tens of
# bytes left, not hundreds. 199 B free at 0x400c14d5 (measured).
ASM_REPORT = os.path.join(ROOT, "custom/coldfire/usb-audio-report.s")
ZONE_REPORT = 0x400C14D5
ZONE_REPORT_END = 0x400C159C
# ☠ The page allocator needs its own zone too; the trampoline's has single
# digits of slack. 150 B free at 0x400d3da2 (measured).
ASM_ALLOC = os.path.join(ROOT, "custom/coldfire/usb-audio-alloc.s")
ZONE_ALLOC = 0x400D3DA2
ZONE_ALLOC_END = 0x400D3E38
PAGE_SZ = 6144

# fs_card_detect_poll hook: 8 displaced bytes (two instructions), replaced by a
# 6-byte jmp (the trailing 2 bytes become dead — control jumps away).
FSCDP = 0x4003F174
FSCDP_BYTES = "2f022239460d1cbc"

SAMPLE_RATE = 44100
# ☠ THE SPEED DECIDES THE STREAM. High speed: sixteen channels (track 1 L/R on
# channels 1/2 ... track 8 L/R on 15/16), 23 frames x 32 B = 736 B packets
# polled every 500 us (bInterval 3). Full speed: the stereo sum, 45 x 4 =
# 180 B every 1 ms — sixteen channels need 1411 B/ms and a full-speed iso
# endpoint moves at most 1023. The two config descriptors differ only in
# these numbers, never in length (the clamp shims carry ONE length).
HS_CHANNELS, HS_MAXPKT, HS_BINTERVAL = 16, 23 * 32, 3
FS_CHANNELS, FS_MAXPKT, FS_BINTERVAL = 2, 45 * 4, 1
DMA_WINDOW = 0x4EC94A00                  # ..0x4EC95000: 2 dTDs + 2 x 736 B
UAC2_AC_IFACE = 3                        # the audio function's AudioControl
UAC2_AS_IFACE = 4                        # ... and its AudioStreaming
UAC2_CLOCK_ID, UAC2_IT_ID, UAC2_OT_ID = 0x10, 0x11, 0x12
DMA_WINDOW_LEN = 2 * 32 + 2 * HS_MAXPKT  # 1536: exactly the window


def config_descriptor(hs, other_speed=False):
    """The usb-midi MSC+AC+MS composite (byte-identical to usb-midi.py) grown
    with a second audio function: a UAC2 AudioControl (clock source, input
    terminal, USB-streaming output terminal) + AudioStreaming (alt 0
    zero-bandwidth, alt 1 with the iso IN EP), Type I PCM 44100/16, sixteen
    channels at high speed and the stereo sum at full speed."""
    bulk = 512 if hs else 64
    nch = HS_CHANNELS if hs else FS_CHANNELS
    maxpkt = HS_MAXPKT if hs else FS_MAXPKT

    def ep(addr, pkt):
        return bytes([7, 5, addr, 2]) + struct.pack("<H", pkt) + bytes([0])

    def ep_midi(addr, pkt):
        return bytes([9, 5, addr, 2]) + struct.pack("<H", pkt) + bytes(3)

    ms_class = (bytes([7, 0x24, 1, 0, 1, 37, 0]) +
                bytes([6, 0x24, 2, 1, 1, 0]) +
                bytes([6, 0x24, 2, 2, 2, 0]) +
                bytes([9, 0x24, 3, 1, 3, 1, 2, 1, 0]) +
                bytes([9, 0x24, 3, 2, 4, 1, 1, 1, 0]))
    # ---- the audio function: UAC2 --------------------------------------------
    # ☠ WHY UAC2 AND NOT UAC1. UAC1 is a 1 ms protocol even at high speed:
    # Apple's TN3190 says a high-speed UAC1 device MUST use bInterval 4 (1 ms)
    # and is "limited to data transfer sizes of 1024 bytes per millisecond",
    # and its driver does at most one transaction per microframe. Sixteen
    # channels of 16-bit 44.1 kHz are 1411 B/ms, so UAC1 tops out at 11
    # channels. UAC2 allows bInterval 3 (500 us), where a 23-frame packet is
    # 736 B. Entity ids: clock 0x10, input terminal 0x11, output terminal 0x12
    # (the MIDI function's jack ids live in a different function).
    #
    # The host learns the (only) sample rate through class requests to the
    # clock source (RANGE + CUR of CS_SAM_FREQ_CONTROL, CUR of CLOCK_VALID),
    # answered by the payload's EP0 shim (cf/usb-audio.s audio_ctrl_shim).
    # ☠ The clock frequency control is READ-ONLY (bmControls 0b01), and one
    # rate is all it offers. Read-only does not stop a host SETting it: some
    # UAC2 hosts SET_CUR the rate they have just read back (a control OUT
    # with a 4-byte data stage) and give the device up if that STALLs. The
    # stock EP0 stack has no OUT data stage, so the payload's shim provides
    # one: SET_CUR 44100 is acknowledged, any other rate STALLs.
    CLK, IT, OT = UAC2_CLOCK_ID, UAC2_IT_ID, UAC2_OT_ID
    clock = bytes([8, 0x24, 0x0A, CLK,
                   0x01,            # bmAttributes: internal fixed clock
                   0x05,            # bmControls: freq read-only, validity read-only
                   0, 0])
    in_term = (bytes([17, 0x24, 0x02, IT]) + struct.pack("<H", 0x0603) +
               bytes([0, CLK, nch]) + struct.pack("<I", 0) +
               bytes([0]) + struct.pack("<H", 0) + bytes([0]))
    out_term = (bytes([12, 0x24, 0x03, OT]) + struct.pack("<H", 0x0101) +
                bytes([0, IT, CLK]) + struct.pack("<H", 0) + bytes([0]))
    ac_audio_total = 9 + len(clock) + len(in_term) + len(out_term)
    ac_audio = (bytes([9, 0x24, 1]) + struct.pack("<H", 0x0200) +
                bytes([0x0A]) +                          # bCategory PRO-AUDIO
                struct.pack("<H", ac_audio_total) + bytes([0]) +
                clock + in_term + out_term)
    AC, AS = UAC2_AC_IFACE, UAC2_AS_IFACE
    as_iface = (
        bytes([9, 4, AS, 0, 0, 1, 2, 0x20, 0]) +            # IF4 alt0 AS (2.0)
        bytes([9, 4, AS, 1, 1, 1, 2, 0x20, 0]) +            # IF4 alt1 AS (2.0)
        bytes([16, 0x24, 1, OT, 0, 1]) +                    # AS_GENERAL: link OT,
        struct.pack("<I", 1) +                              #   Type I, PCM
        bytes([nch]) + struct.pack("<I", 0) + bytes([0]) +  #   channels, no map
        bytes([6, 0x24, 2, 1, 2, 16]) +                     # FORMAT_TYPE_I 2.0: 16-bit in 2 B
        bytes([7, 5, 0x83, 0x05]) +                         # iso IN EP, async, data
        struct.pack("<H", maxpkt) +
        # ☠ bInterval is SPEED-DEPENDENT for isochronous endpoints: 1 ms
        # frames at full speed, 2^(bInterval-1) microframes of 125 us at high
        # speed. 3 = every 500 us, the poll that lets 16 channels fit one
        # 1024 B transaction. The unit enumerates at 480 Mb/s, so the HIGH
        # SPEED config is the one a host actually uses; the packet bench
        # ignores bInterval entirely.
        bytes([HS_BINTERVAL if hs else FS_BINTERVAL]) +
        bytes([8, 0x25, 1, 0, 0, 0]) + struct.pack("<H", 0))  # CS iso EP 2.0
    # ☠ TWO SEPARATE AUDIO FUNCTIONS, mirroring a device macOS accepts.
    #
    # A working Elektron Digitone on the same Mac presents AudioControl +
    # AudioStreaming as one function and a SECOND, independent AudioControl +
    # MIDIStreaming as another. Putting AudioStreaming and MIDIStreaming under
    # ONE AudioControl collection is legal but macOS declines it silently:
    # usbaudiod logs "USBDevice create" and then publishes nothing. Measured,
    # not deduced — the Digitone is the reference.
    #
    #   iface 0  MSC
    #   IAD(1,2) iface 1 AudioControl [2] + iface 2 MIDIStreaming   <- UAC1, unchanged
    #   IAD(3,4) iface 3 AudioControl [4] + iface 4 AudioStreaming  <- UAC2 audio
    #
    # Keeping MSC/AC/MS numbering means the usb-midi half is untouched.
    # the MIDI function's AudioControl: header only, collecting interface 2
    ac_midi = bytes([9, 0x24, 1, 0, 1]) + struct.pack("<H", 9) + bytes([1, 2])
    iad_midi = bytes([8, 0x0B, 1, 2, 0x01, 0x00, 0x00, 0])
    iad_audio = bytes([8, 0x0B, AC, 2, 0x01, 0x00, 0x20, 0])  # AF_VERSION_02_00
    body = (bytes([9, 4, 0, 0, 2, 8, 6, 0x50, 0]) +
            ep(0x81, bulk) + ep(0x01, bulk) +
            iad_midi +
            bytes([9, 4, 1, 0, 0, 1, 1, 0, 0]) + ac_midi +
            bytes([9, 4, 2, 0, 2, 1, 3, 0, 0]) +
            ms_class +
            ep_midi(0x02, bulk) + bytes([5, 0x25, 1, 1, 1]) +
            ep_midi(0x82, bulk) + bytes([5, 0x25, 1, 1, 3]) +
            iad_audio +
            bytes([9, 4, AC, 0, 0, 1, 1, 0x20, 0]) + ac_audio +  # IP_VERSION_02_00
            as_iface)
    total = 9 + len(body)
    dt = 7 if other_speed else 2
    hdr = bytes([9, dt]) + struct.pack("<H", total) + bytes([5, 1, 0, 0xC0, 3])
    return hdr + body


def emit_cfg_include(path):
    """Write an assembly include placing the four configs, with symbols the
    payload links against.

    ☠ No page alignment is attempted, because none can be promised: the blob
    is linked at one address and relocated to whatever page the loader picks,
    which destroys any alignment beyond the relocation delta's. A config that
    straddles a 4 KB page used to transmit truncated, because the stock EP0
    sender fills only the dTD's buffer page 0; the payload's audio_ep0page_shim
    fills page 1 too, and THAT is the guarantee. An earlier 512-byte .balign
    here looked like one and was not."""
    fs = config_descriptor(False)
    hs = config_descriptor(True)
    os_fs = config_descriptor(False, other_speed=True)
    os_hs = config_descriptor(True, other_speed=True)
    assert len(fs) == len(hs) == len(os_fs) == len(os_hs), "configs must match"
    lines = ["| generated by custom/usb-audio.py — do not edit",
             "    .balign 4", "    .global usbaudio_configs", "usbaudio_configs:"]
    for name, blob in [("cfg_fs", fs), ("cfg_hs", hs),
                       ("cfg_os_fs", os_fs), ("cfg_os_hs", os_hs)]:
        lines.append("    .balign 4")
        lines.append("    .global %s" % name)
        lines.append("%s:" % name)
        lines.append("    .byte " + ",".join("0x%02x" % b for b in blob))
    open(path, "w").write("\n".join(lines) + "\n")
    return len(fs)


def link_at(obj, link_addr, out_blob, defsyms):
    """Link one already-assembled object at a given address."""
    elf = out_blob + ".elf"
    cmd_ld = ["m68k-elf-ld", "-Ttext=%#x" % link_addr, "-e", "0", "-o", elf, obj]
    for name, val in defsyms.items():
        cmd_ld += ["--defsym", "%s=%#x" % (name, val)]
    for cmd in (cmd_ld, ["m68k-elf-objcopy", "-O", "binary", elf, out_blob]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("failed: %s\n%s" % (" ".join(cmd), r.stderr))
    syms = {}
    nm = subprocess.run(["m68k-elf-nm", elf], capture_output=True, text=True)
    for ln in nm.stdout.splitlines():
        p = ln.split()
        if len(p) == 3:
            syms[p[2]] = int(p[0], 16)
    return open(out_blob, "rb").read(), syms


def find_relocs(obj, link_addr, out_blob, defsyms, delta=0x10000):
    """Every offset in the blob holding an absolute address of the blob ITSELF.

    Discovered rather than hand-listed: link the same object twice, at two
    addresses differing by `delta`, and diff. A longword that moved by exactly
    delta is a self-reference; anything else is position-independent and must
    not be touched. ☠ Hand-maintaining this list would rot the moment the
    payload gains a variable — the whole point is that it cannot.
    """
    a, _ = link_at(obj, link_addr, out_blob + ".a", defsyms)
    b, _ = link_at(obj, link_addr + delta, out_blob + ".b", defsyms)
    if len(a) != len(b):
        sys.exit("relocation probe: blob length changed with link address")
    # ☠ Absolute operands are NOT 4-byte aligned in the instruction stream
    # (`41f9 4900 3000` = lea 0x49003000,%a0 — the operand starts mid-word),
    # so scan every byte offset, not every longword.
    lo, hi = link_addr, link_addr + len(a) + 0x40000
    sites = []
    i = 0
    while i <= len(a) - 4:
        wa = struct.unpack_from(">I", a, i)[0]
        wb = struct.unpack_from(">I", b, i)[0]
        if wa != wb and wb - wa == delta and lo <= wa < hi:
            sites.append(i); i += 4; continue
        i += 1
    # Every byte that differs must be explained by one of those operands;
    # anything left over means the blob is not a pure relocation away from
    # being position-independent, and silently shipping it would be worse
    # than failing here.
    covered = set()
    for o in sites:
        covered.update(range(o, o + 4))
    stray = [i for i in range(min(len(a), len(b)))
             if a[i] != b[i] and i not in covered]
    if stray:
        sys.exit("relocation probe: %d differing bytes are not part of any "
                 "self-reference operand (first at 0x%x) — the payload is not "
                 "relocatable as-is" % (len(stray), stray[0]))
    return sites


def assemble(src, link_addr, out_blob, defsyms, incdir, asmsyms=None):
    """asmsyms are needed at ASSEMBLY time (e.g. by .if); the rest are link
    symbols. A linker --defsym is invisible to the assembler, so anything a
    .if tests has to go through m68k-elf-as --defsym instead."""
    obj = out_blob + ".o"
    elf = out_blob + ".elf"
    cmd_as = ["m68k-elf-as", "-mcpu=54454", "-I", incdir, "-o", obj, src]
    for name, val in (asmsyms or {}).items():
        cmd_as += ["--defsym", "%s=%d" % (name, val)]
    cmd_ld = ["m68k-elf-ld", "-Ttext=%#x" % link_addr, "-e", "0", "-o", elf, obj]
    for name, val in defsyms.items():
        cmd_ld += ["--defsym", "%s=%#x" % (name, val)]
    cmd_oc = ["m68k-elf-objcopy", "-O", "binary", elf, out_blob]
    for cmd in (cmd_as, cmd_ld, cmd_oc):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("failed: %s\n%s" % (" ".join(cmd), r.stderr))
    syms = {}
    nm = subprocess.run(["m68k-elf-nm", elf], capture_output=True, text=True)
    for ln in nm.stdout.splitlines():
        p = ln.split()
        if len(p) == 3:
            syms[p[2]] = int(p[0], 16)
    return open(out_blob, "rb").read(), syms


def read_sym(path):
    d = {}
    for ln in open(path):
        if "=" in ln:
            k, v = ln.strip().split("=")
            d[k] = int(v, 16)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp",
                    default=os.path.join(ROOT, "out/usb-midi.bin"))
    ap.add_argument("--out", default=os.path.join(ROOT, "out/usb-audio.bin"))
    ap.add_argument("--payload",
                    default=os.path.join(ROOT, "out/USBAUDIO.BIN"))
    ap.add_argument("--plain", action="store_true",
                   help="A/B ONLY: no rate servo. The endpoint is asynchronous, "
                        "so the servo (send what is produced, steering the ring "
                        "fill) is what keeps the ring from drifting into an "
                        "under/overrun as the device and host clocks diverge. "
                        "An earlier hardware click count that favoured --plain "
                        "(47 vs 89 discontinuities in 30 s) was taken on the "
                        "single-dTD stereo build, where prime latency made the "
                        "holes; re-measure on the two-dTD queue before trusting "
                        "it.")
    ap.add_argument("--no-guard", action="store_true",
                   help="TEST ONLY: omit the image-resident hook guard, so a "
                        "destroyed payload crashes as it did before the guard "
                        "existed. This is the negative control for "
                        "tests/usb-audio-guard-test.sh.")
    ap.add_argument("--heap-reserve", type=int, default=0,
                   help="take this many flex-heap pages out of the free "
                        "list and the memset at RUNTIME, so a payload loaded "
                        "at the heap base survives flex_heap_init. Requires "
                        "--load-base 0x40a955e0.")
    ap.add_argument("--dma-at", type=lambda x: int(x, 0), default=DMA_WINDOW,
                   help="the cache-inhibited window for the two dTDs and two "
                        "packet buffers (1536 B). Default: the verified "
                        "0x4ec94a00..0x4ec95000 in the firmware's USB arena.")
    ap.add_argument("--load-base", type=lambda x: int(x, 0), default=0,
                    help="TEST ONLY: load the payload at this fixed address "
                         "instead of asking the heap allocator. For probing "
                         "whether a candidate region survives a project load.")
    ap.add_argument("--source", choices=("tracks", "sine", "probe", "probe2", "probe3", "place", "telem"),
                   default="tracks",
                   help="audio source: the summed track bus (default) or a "
                        "synthetic 441 Hz triangle that ignores readback_buf, "
                        "to separate the tap point from the USB path")
    ap.add_argument("--patches", choices=("all", "usb", "frame", "none"),
                    default="all",
                    help="which image hooks stage2 installs. Subsets exist to "
                         "bisect a hardware fault by swapping ONE CARD FILE "
                         "instead of reflashing.")
    ap.add_argument("--omit-qh-clear", action="store_true",
                    help="TEST ONLY: build the payload WITHOUT clearing EP3's "
                         "device queue head, reproducing the defect that "
                         "crashed real hardware. For the detector's positive "
                         "control (tests/usb-audio-qh-test.sh). Never ship.")
    a = ap.parse_args()
    img = bytearray(open(a.inp, "rb").read())
    midi = read_sym(os.path.splitext(a.inp)[0] + ".sym")

    def off(va):
        return va - BASE

    def expect(va, want, what):
        got = bytes(img[off(va):off(va) + len(want)])
        if got != want:
            sys.exit("refusing: %s at %#x is %s, expected %s"
                     % (what, va, got.hex(), want.hex()))

    tmp = "/tmp"
    if a.dma_at & 31:
        sys.exit("--dma-at must be 32-byte aligned (dTDs)")
    if a.dma_at + DMA_WINDOW_LEN > 0x4EC95000 and a.dma_at < 0x4EC95000:
        sys.exit("--dma-at window %#x..%#x runs into the firmware's dTD pool "
                 "at 0x4ec95000" % (a.dma_at, a.dma_at + DMA_WINDOW_LEN))
    # ☠ The reporter is built FIRST: both the trampoline and stage2 call it.
    # stage2 must report its OWN outcome — it can abort on an expect-mismatch
    # and return normally, which the trampoline cannot distinguish from a
    # successful install. Reporting "installed" from the caller was a claim
    # the caller could not actually make.
    report, rsyms = assemble(ASM_REPORT, ZONE_REPORT,
                             os.path.join(tmp, "usbareport.bin"), {}, tmp)
    if ZONE_REPORT + len(report) > ZONE_REPORT_END:
        sys.exit("reporter is %d bytes, zone is %d"
                 % (len(report), ZONE_REPORT_END - ZONE_REPORT))
    cfg_len = emit_cfg_include(os.path.join(tmp, "usb-audio-cfg.s"))

    # 1. the payload. Chains into usb-midi's isr shim; the config-clamp shim
    # needs the grown length; the pea-repoint expect() bytes are the usb-midi
    # config addresses the responder currently holds.
    # The readback arena carries 32-bit samples; this is the right-shift that
    # lands them in int16. CALIBRATED against --recording by
    # tests/usb-audio-stream-test.sh, not assumed.
    AUDIO_SHIFT = 16
    # ☠ Pre-pass: the placement probe needs the address of the trampoline's
    # once-only latch so it can clear it and be re-run by re-inserting the
    # card. The trampoline is assembled below, AFTER the payload, because it
    # needs stage2's address -- but a --defsym never changes code size, so
    # assembling it here with placeholder values yields the right symbol
    # addresses and is thrown away.
    _pre, _tsyms = assemble(ASM_TRAMP, ZONE_TRAMP,
                            os.path.join(tmp, "usbatramp-pre.bin"),
                            {"PAYLOAD_BASE": 0x40000000, "PAYLOAD_MAGIC": 0,
                             "PAYLOAD_MAX": 0, "READ_CHUNK": 1,
                             "UA_ALLOC": 0x40000000, "UA_FIXED_BASE": 0,
                             "UA_REPORT": 0x40000000}, tmp,
                            asmsyms={"UA_FIXED_BASE": 0, "HAVE_GUARD": 0})
    # ☠ Same circular-dependency trick as the trampoline: the guard needs the
    # payload's shim address and the payload needs the guard's, and a --defsym
    # never changes code size, so a throwaway pass yields the right addresses.
    ZONE_GUARD = ZONE_REPORT + len(report)
    ZONE_GUARD_END = ZONE_REPORT_END
    _gpre, _gsyms = assemble(ASM_GUARD, ZONE_GUARD,
                             os.path.join(tmp, "usbaguard-pre.bin"),
                             {"PAYLOAD_BASE": 0x40000000},
                             tmp)
    guard_frame = _gsyms["usbaudio_guard_frame"] if (a.load_base and not a.no_guard) else 0
    guard_shim = _gsyms["usbaudio_guard_shim"] if guard_frame else 0
    defs = {"AUDIO_SHIFT": AUDIO_SHIFT,
            "UAC2_AC_IFACE": UAC2_AC_IFACE, "UAC2_AS_IFACE": UAC2_AS_IFACE,
            "UAC2_CLOCK_ID": UAC2_CLOCK_ID,
            "UA_TRAMP_DONE": _tsyms["usbaudio_tramp_done"],
            "UA_REPORT": rsyms["usbaudio_report"],
            "USBMIDI_ISR_SHIM": midi["usbmidi_isr_shim"], "CFG_LEN": cfg_len,
            "MIDI_CFG_FS": midi["cfg_fs"], "MIDI_CFG_HS": midi["cfg_hs"],
            "MIDI_CFG_OS_FS": midi["cfg_os_fs"], "MIDI_CFG_OS_HS": midi["cfg_os_hs"]}
    # The blob is linked 16 bytes above the load address: the self-describing
    # header occupies those bytes, so the code lands exactly where it is linked.
    asm = ASM
    if a.omit_qh_clear:
        src = open(ASM).read()
        for snip in QH_CLEAR_SNIPPETS:
            if snip not in src:
                sys.exit("--omit-qh-clear: the dQH-clearing code has moved; "
                         "update QH_CLEAR_SNIPPETS so the positive control "
                         "still reproduces the defect")
            src = src.replace(snip, "", 1)
        asm = os.path.join(tmp, "usb-audio-noqh.s")
        open(asm, "w").write(src)
        print("  ☠ TEST BUILD: EP3 queue head is NOT cleared")
    asmsyms = {"GUARD_FRAME": guard_frame,
               "AUD_SERVO": 0 if a.plain else 1,
               "GUARD_SHIM": guard_shim,
               "EXPECT_BASE": 1 if a.load_base else 0,
               "EXPECT_STAGE2": (a.load_base + PAYLOAD_HDR) if a.load_base else 0,
               "PATCH_USB": 0 if a.patches in ("none", "frame") else 1,
               "PATCH_FRAME": 0 if a.patches in ("none", "usb") else 1,
               "DMA_FIXED": a.dma_at,
               "HEAP_RESERVE": a.heap_reserve,
               "AUD_SOURCE": {"tracks": 0, "sine": 1, "probe": 2, "probe2": 3, "probe3": 4, "place": 5, "telem": 6}[a.source]}
    payload, psyms = assemble(asm, SCRATCH + PAYLOAD_HDR,
                              os.path.join(tmp, "usbaudio.bin"), defs, tmp,
                              asmsyms=asmsyms)
    relocs = find_relocs(os.path.join(tmp, "usbaudio.bin") + ".o",
                         SCRATCH + PAYLOAD_HDR,
                         os.path.join(tmp, "usbaudio-reloc"), defs)
    print("  relocation sites: %d (self-references the loader must fix up)"
          % len(relocs))

    # 2. trampoline -> image slack. Knows the payload's stage2 entry, load
    # address, sector count, and prologue magic (validates the blob landed).
    # Pad to a longword so the checksum covers the whole file and the card
    # copy's size is an exact, checkable number.
    payload += b"\x00" * (-len(payload) % 4)
    # ☠ The self-describing header. The trampoline checks magic, that the
    # declared length equals the file's ACTUAL size (the only thing that
    # catches a truncated copy — the read takes a sector count, so a short
    # file still has a valid first longword), and the checksum. It does NOT
    # compare against a constant baked into the image: that would force a
    # REFLASH for every payload experiment, which is the slowest possible way
    # to work on hardware. Self-consistency rejects truncation and corruption
    # just as hard while letting a flashed unit run any correct payload.
    link_base = SCRATCH + PAYLOAD_HDR
    reloc_blob = b"".join(struct.pack(">I", PAYLOAD_HDR + o) for o in relocs)
    body = payload + reloc_blob
    total = PAYLOAD_HDR + len(body)
    if total > PAYLOAD_MAX:
        sys.exit("payload is %d B, the limit is %d" % (total, PAYLOAD_MAX))
    entry_off = psyms["usbaudio_entry"] - link_base + PAYLOAD_HDR
    reloc_off = PAYLOAD_HDR + len(payload)
    fields = (PAYLOAD_MAGIC, total, 0, entry_off, reloc_off, len(relocs), link_base)
    head = struct.pack(">7I", *fields)
    acc = sum(struct.unpack(">%dI" % (total // 4), head + body)) & 0xFFFFFFFF
    head = struct.pack(">7I", PAYLOAD_MAGIC, total, acc, entry_off,
                       reloc_off, len(relocs), link_base)
    payload = head + body
    print("  header: entry +%#x, %d relocs at +%#x, link base %#x"
          % (entry_off, len(relocs), reloc_off, link_base))
    # ☠ Max sectors per fs_read call. ONE.
    #
    # The only transfer size ever PROVEN to land at the requested address is a
    # single sector: cf/alias-probe.s (132 B, 1 sector) validated and executed
    # from 0x48001000 on hardware. 8 sectors was briefly set here on the
    # strength of a 4 KB probe "booting fine" — but that probe was built to be
    # REJECTED on its length check, so booting proved only that the misplaced
    # write missed anything fatal, NOT that the data arrived. It did not: an
    # 8-sector-chunked load still overwrote the OS image and crashed the unit.
    #
    # ☠ Do not raise this without a probe that LANDS AND EXECUTES at that size.
    # "The machine booted" is not evidence that a read went where it was asked
    # to.
    READ_CHUNK = 1
    tdefs = {"PAYLOAD_BASE": SCRATCH, "PAYLOAD_MAGIC": PAYLOAD_MAGIC,
             "PAYLOAD_MAX": PAYLOAD_MAX, "READ_CHUNK": READ_CHUNK}

    # The allocator must ask for enough pages to hold the whole blob.
    npages = (len(payload) + PAGE_SZ - 1) // PAGE_SZ
    if a.heap_reserve and a.heap_reserve < npages:
        sys.exit("--heap-reserve %d pages holds %d B; the payload is %d B "
                 "(%d pages)" % (a.heap_reserve, a.heap_reserve * PAGE_SZ,
                                 len(payload), npages))
    alloc, asyms = assemble(ASM_ALLOC, ZONE_ALLOC,
                            os.path.join(tmp, "usbaalloc.bin"), {}, tmp,
                            asmsyms={"NPAGES": npages})
    if ZONE_ALLOC + len(alloc) > ZONE_ALLOC_END:
        sys.exit("allocator is %d bytes, zone is %d"
                 % (len(alloc), ZONE_ALLOC_END - ZONE_ALLOC))
    print("  allocator %d B at %#x (slack %d), asks for %d pages (%d B)"
          % (len(alloc), ZONE_ALLOC, ZONE_ALLOC_END - ZONE_ALLOC - len(alloc),
             npages, npages * PAGE_SZ))
    tdefs["UA_ALLOC"] = asyms["usbaudio_alloc"]
    tdefs["UA_FIXED_BASE"] = a.load_base
    if a.load_base:
        print("  ☠ TEST BUILD: fixed load base %#x (not the allocator)"
              % a.load_base)
    tdefs["UA_REPORT"] = rsyms["usbaudio_report"]
    if a.load_base and not a.no_guard:
        # runtime address of a payload symbol = link address + (load base - link base)
        shim_rt = psyms["audio_frame_shim"] - SCRATCH + a.load_base
        guard, gsyms = assemble(ASM_GUARD, ZONE_GUARD,
                                os.path.join(tmp, "usbaguard.bin"),
                                {"PAYLOAD_BASE": a.load_base}, tmp)
        if ZONE_GUARD + len(guard) > ZONE_GUARD_END:
            sys.exit("guard is %d bytes, zone is %d"
                     % (len(guard), ZONE_GUARD_END - ZONE_GUARD))
        print("  hook guard %d B at %#x (slack %d), payload base %#x "
              "(shim address published at run time)"
              % (len(guard), ZONE_GUARD, ZONE_GUARD_END - ZONE_GUARD - len(guard),
                 a.load_base))
        tdefs["UA_GUARD_POLL"] = gsyms["usbaudio_guard_poll"]
    else:
        guard, gsyms = b"", {}
        tdefs["UA_GUARD_POLL"] = 0
    tramp, _ = assemble(ASM_TRAMP, ZONE_TRAMP, os.path.join(tmp, "usbatramp.bin"),
                        tdefs, tmp,
                        asmsyms={"UA_FIXED_BASE": a.load_base,
                                 "HAVE_GUARD": 1 if guard else 0})
    if ZONE_TRAMP + len(tramp) > ZONE_END:
        sys.exit("trampoline is %d bytes, slack is %d"
                 % (len(tramp), ZONE_END - ZONE_TRAMP))

    # 3. plant the two in-image writes, expect()-guarded.
    # ☠ expect()-guard: the reporter's zone must be zero before we claim it.
    expect(ZONE_REPORT, b"\x00" * len(report), "usb-audio report zone")
    img[off(ZONE_REPORT):off(ZONE_REPORT) + len(report)] = report
    expect(ZONE_ALLOC, b"\x00" * len(alloc), "usb-audio allocator zone")
    img[off(ZONE_ALLOC):off(ZONE_ALLOC) + len(alloc)] = alloc
    if guard:
        # ☠ expect()-guard: the guard's zone must be zero before we claim it.
        expect(ZONE_GUARD, b"\x00" * len(guard), "usb-audio hook-guard zone")
        img[off(ZONE_GUARD):off(ZONE_GUARD) + len(guard)] = guard
    expect(FSCDP, bytes.fromhex(FSCDP_BYTES), "fs_card_detect_poll")
    img[off(FSCDP):off(FSCDP) + 6] = b"\x4e\xf9" + struct.pack(">I", ZONE_TRAMP)
    expect(ZONE_TRAMP, bytes(len(tramp)), "trampoline slack")
    img[off(ZONE_TRAMP):off(ZONE_TRAMP) + len(tramp)] = tramp

    open(a.out, "wb").write(img)
    open(a.payload, "wb").write(payload)
    sym_path = os.path.splitext(a.out)[0] + ".sym"
    with open(sym_path, "w") as f:
        for n, v in sorted(psyms.items()):
            f.write("%s=0x%08x\n" % (n, v))
    print("usb-audio: %s + %s -> %s" % (a.inp, a.payload, a.out))
    print("  payload %d B at %#x (card /USBAUDIO.BIN), config length %d"
          % (len(payload), SCRATCH, cfg_len))
    print("  trampoline %d B at %#x (slack %d), stage2 %#x"
          % (len(tramp), ZONE_TRAMP, ZONE_END - ZONE_TRAMP - len(tramp),
             psyms["usbaudio_stage2"]))


main()
