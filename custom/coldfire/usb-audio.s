| SPDX-License-Identifier: MIT
| usb-audio.s — the USB-audio payload, loaded from the CF card into flex-heap
| pages the firmware no longer believes exist (custom/usb-audio.py).
|
| Copied there at runtime by custom/coldfire/usb-audio-tramp.s, this holds
| everything too large for the image's free space: the four grown UAC2 config
| descriptors (generated into usb-audio-cfg.s by custom/usb-audio.py), the
| SET/GET_INTERFACE + EP0 clock-request + usb_isr + descriptor-clamp shims,
| and the EP3 iso packet builder that streams the eight tracks to the host as
| SIXTEEN channels at 44.1 kHz 16-bit (track 1 L/R on channels 1/2 ... track 8
| L/R on 15/16) at high speed, or their stereo sum at full speed.
|
| stage2 installs every image hook AT RUNTIME (expect-guarded, D-cache pushed
| and I-cache invalidated), so an image whose card lacks /USBAUDIO.BIN is the
| stock usb-midi composite and nothing here ever runs.
|
| defsyms from usb-audio.py: USBMIDI_ISR_SHIM (chain target), CFG_LEN (grown
| config length), MIDI_CFG_{FS,HS,OS_FS,OS_HS} (the usb-midi config addresses
| the responder's pea sites currently hold — the expect() values),
| UAC2_AC_IFACE / UAC2_AS_IFACE / UAC2_CLOCK_ID (the audio function's
| interface numbers and clock entity, shared with the descriptor generator).

| ---- image sites (all from disassembly of out/usb-midi.bin) ----
.if AUD_SOURCE == 6
.set SRC_IS_TRACKS, 1
.elseif AUD_SOURCE == 0
.set SRC_IS_TRACKS, 1
.else
.set SRC_IS_TRACKS, 0
.endif
.ifndef AUD_SERVO
.set AUD_SERVO, 1
.endif
.ifndef GUARD_SHIM
.set GUARD_SHIM, 0
.endif
.ifndef EXPECT_BASE
.set EXPECT_BASE, 0
.endif
.ifndef EXPECT_STAGE2
.set EXPECT_STAGE2, 0
.endif
.ifndef GUARD_FRAME
.set GUARD_FRAME, 0
.endif
.ifndef HEAP_RESERVE
.set HEAP_RESERVE, 0
.endif
.ifndef AUD_SOURCE
.set AUD_SOURCE, 0                  | 0 = the eight tracks, 1 = synthetic tone
.endif
.set SETUP_ALT,      0x46c8ce0a     | SETUP wValue low = alt setting
.set SETUP_IFACE,    0x46c8ce0c     | SETUP wIndex low = interface number
.set EP0_STATUS_IN,  0x4001d524     | zero-length EP0 IN status (ACK)
.set SETIFACE_DONE,  0x4001de74     | control-request-done
.set SETIFACE_STOCK, 0x4001dd0a     | rejoin after the displaced movel
.set GETIFACE_REJOIN,0x4001d81e     | GET_INTERFACE send tail (expects pea'd ptr)
.set GETIFACE_STOCKP,0x400e20a1     | the stock 1-byte "00" the send reads
.set CLAMP1_REJOIN,  0x4001d864     | after the CONFIG length clamp
.set CLAMP2_REJOIN,  0x4001d8a2     | after the OTHER_SPEED length clamp
.set SETUP_BMREQ,    0x46c8ce08     | SETUP bmRequestType
.set SETUP_BREQ,     0x46c8ce09     | SETUP bRequest
.set SETUP_WVALH,    0x46c8ce0b     | SETUP wValue high = control selector
.set SETUP_WIDXH,    0x46c8ce0d     | SETUP wIndex high = entity id
.set EP0_SEND_TAIL,  0x4001de5c     | jsr usb_ep0_send(len, buf); addq; done
.set CTRL_STOCK,     0x4001de6a     | the stock STALL, after the displaced movel

| ---- USB controller registers ----
.set EPLISTADDR, 0xfc0b0158         | the controller's OWN dQH list base
.set ENDPTSTAT,  0xfc0b01b8
.set DCCPARAMS,  0xfc0b0124         | DEN[4:0] = endpoint pairs this core has
.set USBSTS,     0xfc0b0144
.set USBINTR,    0xfc0b0148
.set EPPRIME,    0xfc0b01b0
.set EPFLUSH,    0xfc0b01b4
.set EPCOMPLETE, 0xfc0b01bc
.set SETUPSTAT,  0xfc0b01ac         | ENDPTSETUPSTAT
.set EP0CTRL,    0xfc0b01c0         | ENDPTCTRL0: bit 16 = EP0 IN (TX) stall
.set EP0OUT_DTD, 0x4ec95000         | the stock EP0 OUT dTD (usb_ep0_send's status OUT)
.set EP0_BUF,    0x4ec96000         | ... and its 64-byte buffer
.set QH0_OUT_PTR,0x46c8ce18         | -> EP0 OUT dQH
.set ENDPTCTRL3, 0xfc0b01cc
.set USBCMD,     0xfc0b0140
.set ATDTW,      0x00004000         | USBCMD bit 14: the add-dTD tripwire
.set QH_EP3IN,   0x4ec949c0         | EP3 IN dQH as OBSERVED IN THE EMULATOR.
                                    | ☠ Only ever a fallback now. The real base
                                    | is read from ENDPTLISTADDR at run time:
                                    | hardcoding it assumed the firmware puts
                                    | its endpoint list where QEMU showed it,
                                    | and if silicon differs the controller
                                    | never sees our queue head at all -- which
                                    | presents as an endpoint that enumerates
                                    | and then transmits nothing, forever.
.set QH_EP3IN_OFF, 7*64             | EP3 IN is list entry (3*2)+1
.set EP3IN_BIT,  0x00080000         | ENDPTPRIME/STAT/COMPLETE bit for EP3 IN

| ---- the audio source: the post-FX readback arena, which EXISTS ON HARDWARE -
| ☠ This used to read a ring that only `-M octatrack,audio-tap=on` ever wrote
| (QEMU mirroring the DSP's ESAI TX, which no CPU on a real board can see). A
| unit running that build had no producer at all. The source here is
| readback_buf: the eDMA deposits every track's post-FX, pre-fader block into
| SRAM each frame, on silicon as in the emulator, and it is the same memory
| the RECEIVE machine sums — confirmed working on real hardware.
|
| Layout: RB_BASE + prev*1024 + track*128 + frame*8, two 32-bit words (L,R)
| per frame, 16 frames per block, 8 tracks. Reads use the PREVIOUS bank
| (the pipeline is one block deep).
|
| The arena holds the eight tracks SEPARATELY and the stream carries them that
| way: one stereo pair per track. Measured, not assumed — with the fixture's
| SINE440 trigged on track 1, `OCTA_RB_LOG=1` shows slot 0 peaking at 2.7e8
| while tracks 2-8 hold a constant idle 768, in both ping-pong banks.
|
| ☠ Every pair is PRE-FADER: track level, the crossfader and MAIN volume are
| downstream of it, and the master track's processing happens inside the DSP
| where no CPU can read it. These are the tracks' post-FX signals, not a
| recording of what the MAIN jacks emit.
|
| ☠ A track whose machine is RECEIVE carries whatever that machine is summing,
| not a signal of its own — that is the routing the user asked for, and it is
| what the arena holds.
.set FLEX_FREE_BASE, 0x8000691c     | live free-stack base cursor (index)
.set RB_BASE,      0x80003190
.set RB_PREV,      0x800000e4      | pingpong_prev: reads use prev
.set RB_TRACKS,    8
.set RB_SHIFT,     AUDIO_SHIFT     | 32-bit readback -> s16 (calibrated)

| ---- frame geometry --------------------------------------------------------
| The producer fills TWO rings per frame: a 32-byte slot (16 channels of s16
| LE, track 1 L/R first, track 8 L/R last) for the HIGH SPEED stream, and a
| 4-byte stereo sum of the eight tracks for FULL SPEED. Both are indexed by
| the same frame count, so the wrap arithmetic has one form.
|
| ☠ Why the speed decides the channel count: 16 ch at 44.1 kHz / 16 bit is
| 1411 B per millisecond, and a full-speed iso endpoint may move at most 1023 B
| per 1 ms frame, with no faster poll to escape into. Sixteen channels cannot
| be described at full speed at all. High speed polls every 500 us
| (bInterval 3), where 22.05 frames * 32 B = 706 B fits a single transaction
| under the 1024 B cap with room for the servo. ☠ UAC1 cannot poll faster
| than 1 ms even at high speed (Apple TN3190: bInterval must be 4), which
| caps a UAC1 stream at 11 channels; this is why the descriptors are UAC2.
.set SLOT_BYTES,   32           | ring slot: 8 tracks * (L,R) * s16
.set SLOT_SHIFT,   5            | log2(SLOT_BYTES), for the index math
.set PKT_MAX_HS,   23*SLOT_BYTES | 736: the largest 500 us packet
.set PKT_MAX_FS,   45*4         | 180: the largest 1 ms stereo packet
.set STEP_HS,      2205         | 22.05 frames per 500 us packet, x100
.set STEP_FS,      4410         | 44.1 frames per 1 ms packet, x100

| The payload's own audio rings. ☠ They live INSIDE the blob, as reserved
| space, so they move with the code when the loader relocates: there is no
| second address to verify, and they cannot collide with the sample heap
| because the heap itself handed us the memory. 1024 frames — 23 ms of
| buffer, ample between a ~2756/s producer and a 2 kHz consumer. ☠ A slot is
| eight times the size it was when the stream was stereo, so the frame COUNT
| came down to keep the blob inside the trampoline's 64 KB limit: 1024 * 32 B
| is 32 KB of ring (+4 KB for the stereo sum), and the whole payload lands
| around 50 KB. ☠ Only the CPU ever reads these rings — the USB controller
| reads the packet buffers in the cache-inhibited DMA window — so the rings
| need no cache maintenance at all.
.set AUD_FRAMES,   1024
.set AUD_TARGET,   512          | ring fill the stream starts at and the servo
                                    | steers towards: ~12 ms, enough that a
                                    | momentary producer stall is absorbed
                                    | rather than becoming a dropout. This
                                    | stream is for recording; latency here
                                    | costs nothing.
.set AUD_BAND,     128          | servo deadband, so it does not hunt

.set CACR_ICINVA,  0xa40ce100       | steady-state CACR + ICINVA (FREE-SPACE.md)
.set PORTSC1,    0xfc0b0184         | bits 27:26 = port speed, 2 = high
| The firmware's own device attach/detach — usb_attach(1) attaches,
| usb_attach(0) detaches; USB DISK MODE uses it on entry and exit.
.set USB_ATTACH,   0x4001eb44

    .text
| ---- self-relocating entry (POSITION INDEPENDENT) ------------------------
| ☠ The loader no longer picks a fixed address: it asks the firmware's page
| allocator for memory, so the payload cannot know at build time where it will
| run. This prologue is the only part that must work at any address, so it
| touches memory ONLY through a base register derived from its own PC. It then
| fixes up the self-references listed in the header and calls the real stage2,
| which may go back to plain absolute addressing.
|
| ☠ The heap is CACHEABLE COPYBACK (ACR0 covers 0x40000000-0x47ffffff), unlike
| the old cache-inhibited scratch. Relocation writes CODE, so the dirty data
| lines must be pushed and the instruction cache invalidated before any of it
| is executed, or the CPU fetches the pre-relocation bytes.
|
| Header (at base): +0 magic, +4 length, +8 checksum, +12 entry offset,
| +16 reloc table offset, +20 reloc count, +24 link base.
    .global usbaudio_entry
usbaudio_entry:
    lea     %sp@(-32),%sp
    moveml  %d0-%d4/%a0-%a2,%sp@
    lea     %pc@(usbaudio_entry),%a0
    lea     %a0@(-28),%a0           | a0 = load base (entry sits right after the header)
    movel   %a0@(16),%d0
    lea     %a0@(0,%d0:l),%a1       | a1 = relocation table
    movel   %a0@(20),%d1            | d1 = entry count
    movel   %a0,%d2
    addil   #28,%d2                 | runtime address of the code
    subl    %a0@(24),%d2            | minus the link base = fixup delta
    tstl    %d1
    beqs    2f
1:  movel   %a1@+,%d3               | offset of one self-reference
    lea     %a0@(0,%d3:l),%a2
    movel   %a2@,%d4
    addl    %d2,%d4
    movel   %d4,%a2@
    subql   #1,%d1
    bnes    1b
2:  movel   %a0@(4),%d1             | push the whole blob out of the D-cache
    lsrl    #4,%d1                  | one cpushl per 16-byte line
    addql   #1,%d1
    moveal  %a0,%a2
3:  cpushl  %bc,%a2@
    lea     %a2@(16),%a2
    subql   #1,%d1
    bnes    3b
    movel   #0xa40ce100,%d0         | steady-state CACR + ICINVA
    movec   %d0,%cacr
    nop
    moveml  %sp@,%d0-%d4/%a0-%a2
    lea     %sp@(32),%sp
    bra     usbaudio_stage2         | PC-relative: valid before and after

    .global usbaudio_stage2

| ---- stage2: install the image hooks, once, from scratch --------------------
| Table-driven: verify EVERY site matches its expected stock bytes before
| touching any, then apply all, D-cache-push each patched line, and invalidate
| the I-cache. A single mismatch aborts the whole install (the image stays the
| stock usb-midi composite). Runs in supervisor context (the fs poll task).
usbaudio_stage2:
    lea     %sp@(-48),%sp
    moveml  %d0-%d7/%a0-%a3,%sp@
    | ☠ Scratch holds whatever the SDRAM powered up with — it is NOT zero on
    | hardware, and QEMU's zeroed RAM hides that (--hw-faithful poisons it).
    | The producer's cursor must start from a known value, not from whatever
    | was in those four bytes.
    clrl    aud_produced
    | Same reason: clear EP3's queue head before anything can enumerate.
    bsr     audio_qh_resolve        | %a0 = EP3 dQH, from the controller
    moveq   #15,%d1
0:  clrl    %a0@+
    subql   #1,%d1
    bpls    0b
    | ☠ And the two dTDs in the DMA window: it is cache-inhibited memory the
    | firmware never touches, so on hardware it holds power-on garbage, and a
    | garbage ACTIVE bit would make the queue look busy forever (or worse,
    | look like a transfer to the controller).
    bsr     audio_dtds_clear
    | ---- verify pass ----
    lea     usbaudio_patches,%a0
1:  movel   %a0@,%d0                | target va
    beqs    3f
    moveal  %d0,%a1
    moveal  %a0@(4),%a2             | expect ptr
    movel   %a0@(12),%d1           | len
2:  mvzb    %a1@+,%d2               | zero-extend: moveb would leave stale
    mvzb    %a2@+,%d3               | upper bytes and cmpl would mis-compare
    cmpl    %d2,%d3
    bne     .Labort                 | mismatch -> abort, install nothing
    subql   #1,%d1
    bnes    2b
    lea     %a0@(16),%a0
    bras    1b
    | ---- apply pass ----
3:  lea     usbaudio_patches,%a0
4:  movel   %a0@,%d0
    beqs    6f
    moveal  %d0,%a1                 | dest
    moveal  %a0@(8),%a2             | patch ptr
    movel   %a0@(12),%d1           | len
5:  moveb   %a2@+,%a1@+
    subql   #1,%d1
    bnes    5b
    | push the patched line(s) out of the copyback D-cache so the I-fetch sees
    | them (the image region is data-cacheable; QEMU no-ops this).
    moveal  %a0@,%a3
    cpushl  %bc,%a3@
    movel   %a0@(12),%d1
    addal   %d1,%a3
    subql   #1,%a3
    cpushl  %bc,%a3@
    lea     %a0@(16),%a0
    bra     4b                      | not bras: the diagnostic prefill pushed this out of range
6:  movel   #CACR_ICINVA,%d0        | invalidate the whole I + branch cache
    movec   %d0,%cacr
    nop
    | ☠ DO NOT RE-ATTACH USB FROM HERE. An earlier version called
    | usb_attach(0)/usb_attach(1) so a host that had already cached the stock
    | descriptors would re-read them. It CORRUPTED A REAL CF CARD.
    |
    | usb_attach is USB DISK MODE's routine, and DISK MODE's contract is that
    | the guest UNMOUNTS ITS FILESYSTEM FIRST — usb_diskmode_enter unmounts the
    | card and only then attaches. Calling it from here attaches the mass
    | storage device to the host while the machine still has the card mounted
    | and live, so two writers share one medium: on hardware whole directories
    | (PRESETS, the Sets) came back as zero-byte entries. It also wedges the
    | machine before the UI in the emulator, which is how the gate caught it.
    |
    | A host holding stale descriptors is a nuisance; replugging the cable
    | fixes it. Corrupting the user's card is not a trade worth making. If
    | this is ever revisited it must go through the firmware's own unmount
    | path, not around it.
    .if AUD_SOURCE == 1
    | ---- diagnostic prefill -------------------------------------------------
    | Fill the ring ONCE, here in ordinary task context, with a 1 kHz square at
    | +-2000 -- a signal the per-block producer never generates. Three
    | distinguishable outcomes from a single boot:
    |   441 Hz triangle at 8000 -> producer runs and delivery works
    |   1 kHz square at 2000    -> delivery works, the frame_isr hook is DEAD
    |   digital silence         -> EP3 delivery itself is broken
    | 44 frames per cycle at 44100 = 1002 Hz: 22 frames high, 22 low.
    lea     aud_ring,%a1
    lea     aud_sum,%a2
    movel   #AUD_FRAMES,%d1         | frames to fill
    moveq   #22,%d2                 | frames left in this half
    movew   #2000,%d3               | current level
.Lpre_frame:
    movel   %d3,%d0
    lsrl    #8,%d0                  | ColdFire: no word shifts
    moveq   #15,%d4                 | 16 channels of the slot
.Lpre_ch:
    moveb   %d3,%a1@+               | low byte (little-endian, as the shim packs)
    moveb   %d0,%a1@+               | high byte
    subql   #1,%d4
    bpls    .Lpre_ch
    moveb   %d3,%a2@+               | and the stereo sum ring, L then R
    moveb   %d0,%a2@+
    moveb   %d3,%a2@+
    moveb   %d0,%a2@+
    subql   #1,%d2
    bnes    .Lpre_next
    moveq   #22,%d2                 | half elapsed: flip the level
    negl    %d3                     | ColdFire has no negw; the value is small
.Lpre_next:
    subql   #1,%d1
    bnes    .Lpre_frame
    .endif
    .if AUD_SOURCE == 4
    | ---- does the controller ACCEPT a prime on EP3? -------------------------
    | EP3 exists per the spec (DCCPARAMS DEN = 4) and the queue head address is
    | a firmware literal, so the remaining question is whether ENDPTPRIME on
    | EP3 IN is honoured at all. Set the endpoint up exactly as the stream
    | does, prime one descriptor, let the controller chew on it, then read the
    | status back. Writes nothing outside the USB controller and its own dQH.
    |
    | Shows SS PP CCCC:
    |   SS   = ENDPTSTAT  TX byte -- 08 means EP3 IN is primed and armed
    |   PP   = ENDPTPRIME TX byte -- 00 means the controller consumed the prime
    |   CCCC = ENDPTCTRL3 high half -- 0084 means TXE + iso type stuck
    | 08 00 0084 is a healthy endpoint; 00 00 0084 means the prime evaporated.
    bsr     audio_qh_resolve        | %a0 = EP3 dQH
    moveq   #15,%d1
9:  clrl    %a0@+
    subql   #1,%d1
    bpls    9b
    moveal  qh_ep3,%a0
    movel   #0x60b40000,%d0         | Mult 1, maxpkt 180
    movel   %d0,%a0@
    moveq   #1,%d0
    movel   %d0,%a0@(8)             | next dTD = terminate for now
    movel   #0x00840000,%d0
    movel   %d0,ENDPTCTRL3          | TXE + isochronous
    lea     usbaudio_dtd0,%a1
    moveq   #1,%d0
    movel   %d0,%a1@                | dTD next = terminate
    movel   #0x00040080,%d0         | 4 bytes, ACTIVE
    movel   %d0,%a1@(4)
    lea     usbaudio_buf0,%a2
    movel   %a2,%a1@(8)
    movel   %a1,%a0@(8)             | dQH next dTD -> ours
    movel   #EP3IN_BIT,%d0
    movel   %d0,EPPRIME
    movel   #200000,%d1             | let the controller act on it
.Lpr_wait:
    subql   #1,%d1
    bnes    .Lpr_wait
    movel   ENDPTSTAT,%d0
    lsrl    #8,%d0
    lsrl    #8,%d0
    andil   #0xff,%d0
    lsll    #8,%d0
    movel   EPPRIME,%d2
    lsrl    #8,%d2
    lsrl    #8,%d2
    andil   #0xff,%d2
    orl     %d2,%d0
    lsll    #8,%d0
    lsll    #8,%d0                  | SS PP 0000
    movel   ENDPTCTRL3,%d2
    swap    %d2
    andil   #0xffff,%d2
    orl     %d2,%d0
    bsr     audio_show_hex
    bra     .Lstage2_done
    .endif
    .if GUARD_SHIM
    | Tell the image-resident guard where the producer actually is. Must happen
    | BEFORE the hook is installed, or the first block interrupt could arrive
    | with the guard still holding zero.
    lea     %pc@(audio_frame_shim),%a0
    movel   %a0,GUARD_SHIM
    .endif
    .if EXPECT_BASE
    | ---- am I actually loaded where the build assumed? ---------------------
    | ☠ A payload built for a fixed base but loaded by an ALLOCATOR trampoline
    | runs fine, reserves the wrong pages, and is then wiped by the next
    | project load -- after which the hooks jump into its remains. That is the
    | VEC:04 illegal instruction at IPL 5 seen four times, and the crash PC was
    | always the frame shim's offset from the ALLOCATOR's base, not from the
    | base this build was compiled for. Refuse to install instead.
    | usbaudio_entry sits exactly at the header boundary (entry +0x1c ==
    | PAYLOAD_HDR), so its runtime address is the load base + 0x1c with no
    | dependency on any other payload symbol. stage2 is NOT at the body start
    | (it links at +0x84), and comparing against that is what made this check
    | abort every install while appearing to work.
    lea     %pc@(usbaudio_entry),%a0
    movel   #EXPECT_STAGE2,%d0
    cmpl    %a0,%d0
    beqs    .Lbase_ok
    moveq   #10,%d0                 | wrong load address for this build
    bra     .Lreport
.Lbase_ok:
    .endif
    .if HEAP_RESERVE
    | ---- take the reserved pages out of the LIVE free stack ----------------
    | ☠ Patching flex_heap_init's constants only changes FUTURE re-seeds. The
    | stack was already seeded at boot with pages 1..14602 before this payload
    | loaded, and FLEX sample loads consume it from the BASE -- pages 1, 2, 3
    | ... which is exactly where the payload sits. On hardware, changing
    | projects loaded samples straight over our code and the frame ISR then
    | executed sample data: VEC:04 illegal instruction at IPL 5, PC inside the
    | heap. Advancing the base cursor removes those pages from the live stack
    | immediately, so nothing can be handed them before the next re-seed.
    |
    | ☠ GUARDED: if the cursor has already moved, something has ALREADY been
    | allocated from the bottom of the heap and the payload may be sitting on
    | live sample data. Abort the install rather than corrupt a sample -- no
    | hooks are installed at this point, so aborting leaves a stock machine.
    movel   FLEX_FREE_BASE,%d0
    bnes    .Lheap_busy
    moveq   #HEAP_RESERVE,%d0
    movel   %d0,FLEX_FREE_BASE
    .endif
    .if AUD_SOURCE == 5
    | ---- placement monitor: what survives REAL USE on a unit ---------------
    | Runs again on every card poll, because stage2 clears the trampoline's
    | once-only latch on the way out. That is only safe with a FIXED load base
    | (--load-base): asking the heap allocator on every poll would leak pages
    | until the heap was gone. It installs NO hooks (--patches none), so it is
    | inert between runs and nothing points into freed memory.
    |
    | State lives in the first 8 bytes of the EP3-OUT queue head, the one
    | region with a structural durability argument (the controller requires an
    | 8-entry list, the firmware uses EP0-2, we enable EP3 IN only):
    |   +0 magic  -> set once stamping has happened
    |   +4 last reported bitmap, so the popup fires ONLY on a change
    | If that state is itself destroyed the probe simply re-stamps and shows
    | 11111111 again, which is the honest report that region 0 did not survive.
    movel   PLACE_STATE,%d0
    cmpil   #PLACE_SENTINEL,%d0
    beq     .Lplace_verify

    lea     place_tbl,%a2           | STAMP
    moveq   #PLACE_N-1,%d7
.Lplace_stamp_one:
    movel   %a2@,%d0
    moveal  %d0,%a0
    movel   %a2@(4),%d1
    bsr     place_stamp
    lea     %a2@(8),%a2
    subql   #1,%d7
    bpls    .Lplace_stamp_one
    movel   #PLACE_SENTINEL,%d0
    movel   %d0,PLACE_STATE
    clrl    PLACE_STATE+4           | no bitmap reported yet
    clrb    UA_TRAMP_DONE           | run again on the next card poll
    movel   #0x11111111,%d0
    bsr     audio_show_hex
    bra     .Lstage2_done

.Lplace_verify:
    lea     place_tbl,%a2
    moveq   #0,%d6                  | bitmap of survivors
    moveq   #0,%d5                  | region index
.Lplace_check_one:
    movel   %a2@,%d0
    moveal  %d0,%a0
    movel   %a2@(4),%d1
    bsr     place_check
    tstl    %d0
    beqs    .Lplace_next
    moveq   #1,%d4
    lsll    %d5,%d4                 | shift by register: immediate is 1-8 only
    orl     %d4,%d6
.Lplace_next:
    lea     %a2@(8),%a2
    addql   #1,%d5
    cmpil   #PLACE_N,%d5
    bnes    .Lplace_check_one
    clrb    UA_TRAMP_DONE
    movel   PLACE_STATE+4,%d0
    cmpl    %d6,%d0
    beq     .Lstage2_done           | unchanged: stay quiet
    movel   %d6,PLACE_STATE+4
    movel   %d6,%d0
    bsr     audio_show_hex
    bra     .Lstage2_done
    .endif
    .if AUD_SOURCE >= 2
    | ---- EP3 existence probe ------------------------------------------------
    | Everything upstream of the data path is proven on this unit: the host is
    | served our descriptors and selects alt 1, yet EP3 transmits nothing and
    | even a ring filled in task context never leaves the device. The remaining
    | question is whether EP3 EXISTS on this silicon. The stock firmware only
    | ever uses EP1 (mass storage) and EP2 (MIDI), so EP3 has never once been
    | exercised on hardware -- and a controller with fewer endpoint pairs than
    | we assume would behave exactly like this: the write to ENDPTCTRL3 does
    | nothing, the host still believes the descriptor, and the endpoint is
    | silent forever. QEMU's model cannot show this; only the chip can.
    |
    | Displays DCCPARAMS low half : ENDPTCTRL3 high half.
    |   xxxx004 / 0104 etc -> DEN, the endpoint-pair count (>=4 means EP3 is real)
    |   low half 0084      -> TXE + iso type stuck, EP3 is configurable
    |   low half 0000      -> the write did not stick: EP3 does not exist
    .if AUD_SOURCE == 3
    | probe2: show ENDPTLISTADDR raw. Expect 0x4ec94800 if the firmware puts
    | its endpoint list where the emulator does; anything else means the
    | hardcoded queue-head address was pointing at memory the controller never
    | reads, and the resolver in the normal build is the fix.
    movel   EPLISTADDR,%d0
    bsr     audio_show_hex
    .else
    movel   #0x00840000,%d0
    movel   %d0,ENDPTCTRL3          | try to enable EP3 IN as isochronous
    movel   ENDPTCTRL3,%d1
    swap    %d1                     | high half -> low half
    andil   #0xffff,%d1
    movel   DCCPARAMS,%d0
    andil   #0xffff,%d0
    swap    %d0                     | DCCPARAMS low half -> high half
    orl     %d1,%d0
    bsr     audio_show_hex
    .endif
    | ☠ Do NOT fall through to the E0 report. popup() shows one notification at
    | a time, so reporting E0 straight after replaces the probe's value before
    | it can be read -- which is exactly what happened the first time this ran
    | on hardware. A probe build reports the register and nothing else.
    bra     .Lstage2_done
    .endif
    .if HEAP_RESERVE
    bra     .Lheap_ok
.Lheap_busy:
    moveq   #9,%d0                  | "USBAUD E9" — heap bottom already in use
    bra     .Lreport
.Lheap_ok:
    .endif
    moveq   #0,%d0                  | "USBAUD E0" — hooks really installed
    bra     .Lreport
.Labort:
    moveq   #7,%d0                  | "USBAUD E7" — a patch site did not match
.Lreport:
    movel   #UA_REPORT,%a0
    jsr     %a0@
.Lstage2_done:
8:  moveml  %sp@,%d0-%d7/%a0-%a3
    lea     %sp@(48),%sp
    rts

| ---- SET_INTERFACE shim (installed at 0x4001dd04) ---------------------------
| Displaced: movel 0xfc0b01c4,%d0. Interface 4 (AudioStreaming) records the
| requested alt setting and completes the status stage; any other interface
| falls through to the stock handler unchanged.
|
| ☠ The shim RECORDS the request. Everything that builds EP3 state — the
| queue head, the dTDs, ENDPTCTRL3, the ring cursors — is done by the frame
| ISR (audio_ep3_up / audio_ep3_down), the one context that also queues
| packets. This runs from the USB interrupt; if it re-initialized the
| endpoint itself, a SET_INTERFACE landing in the middle of a kick would
| rewrite a queue the kick was still building, and the kick would then prime
| the remains. One owner, one byte of shared state.
|
| The one exception is alt 0's FLUSH, done here as well: the host may send
| its next IN within microseconds of the status stage, before the frame ISR
| has had its block, and a packet already queued would answer it — audio
| after teardown. A flush only cancels what is in flight, is idempotent, and
| builds nothing, so it is safe from this context; the kick checks the
| request byte before it queues, so nothing new follows it; and the frame
| ISR flushes again on its way down. (The audio-alt gate asserts silence
| after alt 0, and caught the deferred-only version.)
|
| ☠ Interrupt levels, from the firmware's own INTC writes: the USB interrupt
| is INTC1 source 47 at level 4 (moveq #4 / moveb ->0xfc04c06f at
| 0x4001e024); the frame IRQ is EPORT IRQ1 at level 5 (0x4001fc30). So the
| frame ISR can preempt this shim — harmless, it only ever sees the request
| byte — but this shim can NEVER interrupt a kick. The single-owner rule is
| belt and braces over that fact, not a substitute for it.
    .global audio_setiface_shim
audio_setiface_shim:
    mvzb    SETUP_IFACE,%d0
    moveq   #UAC2_AS_IFACE,%d1
    cmpl    %d0,%d1
    bne     5f                      | not the AudioStreaming interface -> stock
    mvzb    SETUP_ALT,%d0
    tstl    %d0
    beqs    2f
    moveq   #1,%d0                  | any non-zero alt is the streaming one
    moveb   %d0,usbaudio_alt        | the request; the frame ISR brings EP3 up
    bras    3f
2:  clrb    usbaudio_alt            | the request FIRST: no kick queues past it
    bsr     audio_ep3_flush         | then cancel what is already queued
3:  jsr     EP0_STATUS_IN
    jmp     SETIFACE_DONE
5:  movel   0xfc0b01c4,%d0          | displaced
    jmp     SETIFACE_STOCK

| ---- GET_INTERFACE shim (installed at 0x4001d824) ---------------------------
| Displaced: pea 0x400e20a1 (the stock 1-byte "00"). Interface 4 reports the
| alt setting the host asked for (whether or not the frame ISR has acted on it
| yet, which it does within one block); every other interface keeps the stock
| answer.
    .global audio_getiface_shim
audio_getiface_shim:
    mvzb    SETUP_IFACE,%d0
    moveq   #UAC2_AS_IFACE,%d1
    cmpl    %d0,%d1
    bnes    1f
    pea     usbaudio_alt
    jmp     GETIFACE_REJOIN
1:  pea     GETIFACE_STOCKP
    jmp     GETIFACE_REJOIN

| ---- EP0 buffer-page fix (installed at 0x4001d4b2 inside usb_ep0_send) -------
| ☠ usb_ep0_send sets only the dTD's buffer PAGE 0 (0x4ec95028), never PAGE 1.
| A descriptor whose buffer crosses a 4 KB page then transmits only the bytes
| before the boundary and zeros after. The grown UAC2 config straddles a page
| once the payload relocates into a heap page (the shipping load path), so the
| FULL-SPEED config truncated to the stock 32-byte MSC part. This shim also
| fills PAGE 1 = (buffer & ~0xfff) + 0x1000, so any EP0 descriptor send that
| spans a page boundary completes. It is load-address independent — the fix a
| page-safe placement could only approximate.
|
| Displaced: movel %a0@,0x4ec95028 (a0 = &buffer-arg, set by the preceding
| lea %sp@(12),%a0); rejoin at the following lea (0x4001d4b8). %d0 is dead
| across the rejoin (reloaded at 0x4001d4da), so clobbering it is safe.
    .global audio_ep0page_shim
audio_ep0page_shim:
    movel   %a0@,%d0               | the descriptor buffer pointer (arg)
    movel   %d0,0x4ec95028         | dTD buffer page 0 (displaced instruction)
    andil   #0xfffff000,%d0
    addil   #0x1000,%d0
    movel   %d0,0x4ec9502c         | dTD buffer page 1 — the fix
    jmp     0x4001d4b8

| ---- usb_isr shim (installed at 0x4001e606, was jmp usbmidi_isr_shim) -------
| Services the block-clock SOF (kick the builder) and EP3 IN completions (free
| the dTD, kick again), then chains to the usb-midi ISR shim which handles EP2
| and runs the original displaced instruction.
    .global audio_isr_shim
audio_isr_shim:
    lea     %sp@(-16),%sp
    moveml  %d0-%d1/%a0-%a1,%sp@    | all this shim touches
    | A clock SET_CUR whose data stage outlived audio_ctrl_shim's wait: a new
    | SETUP means the host gave it up; otherwise its OUT completion is ours,
    | taken before the stock completion loop can see it.
    tstb    uac2_set_pending
    beqs    4f
    movel   SETUPSTAT,%d0
    btst    #0,%d0
    beqs    3f
    clrb    uac2_set_pending        | superseded by a new SETUP
    bras    4f
3:  movel   EPCOMPLETE,%d0
    btst    #0,%d0                  | EP0 OUT
    beqs    4f
    bsr     uac2_set_done
4:
    | ☠ Exactly ONE place queues packets on EP3: the per-block producer in
    | frame_isr. This shim only retires the completion bit so it cannot go
    | stale; the queue's own state is the ACTIVE bit of each dTD, which the
    | controller clears itself, so there is no bookkeeping to race on. The
    | dTDs are queued WITHOUT IOC — 2000 completion interrupts a second buy
    | nothing when nobody needs to be told.
    movel   EPCOMPLETE,%d0
    movel   #EP3IN_BIT,%d1
    andl    %d1,%d0
    beqs    2f
    movel   #EP3IN_BIT,%d1
    movel   %d1,EPCOMPLETE          | W1C EP3 IN
2:  moveml  %sp@,%d0-%d1/%a0-%a1
    lea     %sp@(16),%sp
    jmp     USBMIDI_ISR_SHIM

| ---- the iso packet builder -------------------------------------------------
| Each packet holds the next n frames from the ring: n = 22/23 at high speed
| (2205/100 per packet) or 44/45 at full speed (4410/100), so the emitted
| stream is an exact, gap-free substring of what the producer wrote. Underrun
| queues nothing (the host's IN gets an empty packet); a producer that laps
| the ring (host stopped draining) resyncs and counts an overrun.
| Runs from the frame shim only; may clobber every register but %sp.
|
| ☠ TWO dTDs, so the host always finds a packet waiting. An isochronous IN
| cannot NAK: an IN token that arrives with nothing primed is answered with a
| zero-length packet, and a 22-frame hole in the stream is a click. With one
| dTD the packet for each 500 us poll had to be primed by the frame ISR after
| the previous poll completed — usually fine (blocks are 363 us apart), but
| any block-interrupt latency became a hole, and the ring's fill is the wrong
| cushion for that: it protects the producer, not the prime. With two, the
| ISR keeps one packet queued BEHIND the one in flight and the controller
| chains into it on its own. (An earlier two-deep attempt wedged against the
| bench, which walked linked iso dTDs as one bulk transfer; the bench now
| serves one dTD per poll, as silicon does.)
|
| The two slots alternate: aud_tail is the next to fill. A slot whose dTD is
| still ACTIVE is in flight or queued and is left alone.
usbaudio_kick:
    tstb    usbaudio_alt            | alt 0 requested since this block began:
    beqs    9f                      | queue nothing more (teardown follows)
    bsr     audio_pkt_build         | fill the tail slot, if free
    tstl    %d0
    beqs    .Lkick_heal
    bsr     audio_pkt_build         | and the other, if that one is free too
.Lkick_heal:
    | ☠ Self-heal. The add-dTD tripwire (audio_pkt_build) is the documented
    | way to append to a running queue, and its hazard window is reported by
    | the hardware clearing ATDTW. Should anything ever leave an ACTIVE dTD
    | behind with the endpoint idle — a missed hazard, a flush that raced a
    | prime — the stream would otherwise stall until the next alt 0/1. So
    | every block ends with: idle endpoint + queued dTD = prime it, and count
    | it, so a silent recovery is still measurable.
    movel   ENDPTSTAT,%d0
    movel   EPPRIME,%d1
    orl     %d1,%d0
    andil   #EP3IN_BIT,%d0
    bnes    9f                      | primed or priming: running
    mvzb    aud_tail,%d0            | the oldest queued dTD is the tail slot
    lsll    #5,%d0                  | (filled first) if it is ACTIVE, else
    lea     usbaudio_dtd0,%a4       | the other one
    addal   %d0,%a4
    movel   %a4@(4),%d1
    btst    #7,%d1
    bnes    .Lheal_prime
    mvzb    aud_tail,%d0
    eoril   #1,%d0
    lsll    #5,%d0
    lea     usbaudio_dtd0,%a4
    addal   %d0,%a4
    movel   %a4@(4),%d1
    btst    #7,%d1
    beqs    9f                      | nothing queued: idle is correct
.Lheal_prime:
    bsr     audio_prime             | %a4 = head dTD
    addql   #1,usbaudio_reprimes
9:  rts

| Point the queue head at %a4 and prime EP3 IN — the "list empty" case of the
| Chipidea add-dTD procedure. Clobbers d0/a1.
audio_prime:
    moveal  qh_ep3,%a1
    movel   %a4,%a1@(8)             | dQH next dTD
    clrl    %a1@(12)                | dQH token: ACTIVE/HALT clear before a prime
    movel   #EP3IN_BIT,%d0
    movel   %d0,EPPRIME
    rts

| Build one packet into the tail slot and queue it. Returns d0 = 1 if a packet
| was queued, 0 if the slot was busy or the ring could not fill one.
audio_pkt_build:
    mvzb    aud_tail,%d0
    moveal  %d0,%a6                 | a6 = slot index (0/1), kept across the copy
    lsll    #5,%d0
    lea     usbaudio_dtd0,%a0
    addal   %d0,%a0                 | a0 = this slot's dTD
    movel   %a0@(4),%d1
    btst    #7,%d1                  | ACTIVE: in flight or queued
    bne     .Lpb_none
    tstb    usbaudio_alt            | (re-checked per packet: alt 0 can land
    beq     .Lpb_none               |  between two builds in one block)
    movel   aud_produced,%d0
    movel   usbaudio_consumed,%d1
    movel   %d0,%d2
    subl    %d1,%d2                 | d2 = frames available
    cmpil   #AUD_FRAMES,%d2
    blss    3f                      | within the ring: fine
    addql   #1,usbaudio_overruns
    movel   %d0,%d1
    subil   #AUD_FRAMES,%d1
    movel   %d1,usbaudio_consumed   | resync to the ring's trailing edge
    movel   #AUD_FRAMES,%d2
3:  movel   usbaudio_acc,%d3
    | ☠ RATE SERVO. The endpoint is ASYNCHRONOUS: the device sends at its own
    | clock and the host adapts. The host's polls run on ITS clock, so a fixed
    | 22.05 frames per poll would drain the ring faster or slower than the
    | producer fills it, by the two clocks' drift, and the ring would under-
    | or overrun within minutes. The servo nudges the drain by +-0.1 frame per
    | packet against a target fill, which is exactly "send what is produced".
    | --plain disables it, for A/B measurement only.
    movel   aud_step,%d5            | nominal frames per packet, x100
    .if AUD_SERVO == 0
    bras    .Lsrv_done
    .endif
    cmpil   #(AUD_TARGET+AUD_BAND),%d2
    bcss    .Lsrv_low
    addil   #10,%d5
    bras    .Lsrv_done
.Lsrv_low:
    cmpil   #(AUD_TARGET-AUD_BAND),%d2
    bccs    .Lsrv_done
    subil   #10,%d5
.Lsrv_done:
    addl    %d5,%d3
    moveq   #100,%d7
    movel   %d3,%d4
    divu.l  %d7,%d4                 | d4 = n = (acc+step)/100
    movel   %d4,%d5
    mulu.l  %d7,%d5
    movel   %d3,%d6
    subl    %d5,%d6                 | d6 = new acc
    cmpl    %d4,%d2
    bccs    .Lhave_frames
    addql   #1,usbaudio_underruns
    bra     .Lpb_none               | available < n: underrun, send nothing
.Lhave_frames:
    movel   %d6,usbaudio_acc
    movel   %d4,usbaudio_lastn
    movel   %d2,usbaudio_lastfill
    | ---- copy n frames into this slot's buffer -----------------------------
    | a3 = the buffer, a1 = write cursor. The ring is copied in at most two
    | straight runs (up to its end, then from its start), with no per-frame
    | index masking; at high speed each 32-byte frame moves as one moveml pair.
    lea     usbaudio_buf0,%a3
    movel   %a6,%d0
    beqs    4f
    lea     %a3@(PKT_MAX_HS),%a3    | slot 1's buffer
4:  moveal  %a3,%a1
    movel   %d4,%d3                 | d3 = n
    movel   usbaudio_consumed,%d1
    andil   #AUD_FRAMES-1,%d1       | ring index of the first frame
    movel   #AUD_FRAMES,%d0
    subl    %d1,%d0                 | d0 = frames before the ring wraps (>= 1)
    tstb    aud_hs
    beqs    .Lcopy_fs
    lea     aud_ring,%a2
    lsll    #SLOT_SHIFT,%d1
    addal   %d1,%a2                 | a2 = the first frame's slot
    cmpl    %d0,%d3
    bhis    .Lhs_wrap               | n > frames to the end: two runs
    movel   %d3,%d0
    bsr     audio_copy32
    bras    .Lcopied
.Lhs_wrap:
    subl    %d0,%d3
    moveal  %d3,%a5                 | a5 = frames in the second run (the copy
    bsr     audio_copy32            |      clobbers every data register)
    lea     aud_ring,%a2
    movel   %a5,%d0
    bsr     audio_copy32
    bras    .Lcopied
.Lcopy_fs:
    lea     aud_sum,%a2
    lsll    #2,%d1
    addal   %d1,%a2
    cmpl    %d0,%d3
    bhis    .Lfs_wrap
    movel   %d3,%d0
    bsr     audio_copy4
    bras    .Lcopied
.Lfs_wrap:
    subl    %d0,%d3
    moveal  %d3,%a5
    bsr     audio_copy4
    lea     aud_sum,%a2
    movel   %a5,%d0
    bsr     audio_copy4
.Lcopied:
    movel   usbaudio_lastn,%d4      | n again (the copy clobbered it)
    addl    %d4,usbaudio_consumed
    movel   %d4,%d6
    tstb    aud_hs
    beqs    5f
    lsll    #SLOT_SHIFT,%d6         | nbytes = n * 32
    bras    6f
5:  lsll    #2,%d6                  | nbytes = n * 4
6:  | ---- the dTD: buffer pointers first, the ACTIVE token LAST -------------
    | The DMA window is cache-inhibited precise, so these stores reach memory
    | in program order: the controller cannot see an ACTIVE token over a
    | half-built descriptor or a half-copied buffer.
    moveq   #1,%d1
    movel   %d1,%a0@                | next = terminate
    movel   %a3,%a0@(8)             | buffer page 0
    movel   %a3,%d1
    andil   #0xfffff000,%d1
    addil   #0x1000,%d1
    movel   %d1,%a0@(12)            | page 1 (a straddle guard; the packet fits one page)
    movel   %d6,%d1
    swap    %d1                     | nbytes << 16
    oril    #0x80,%d1               | ACTIVE (no IOC: 2000 completions/s buy nothing)
    movel   %d1,%a0@(4)
    | ---- queue it: the Chipidea "add dTD" procedure -------------------------
    | Case 1, list empty (the other slot's dTD is not ACTIVE): point the queue
    | head at this dTD and prime. Case 2, list running: link this dTD after
    | the other, then the tripwire — set ATDTW, sample ENDPTSTAT, trust the
    | sample only if ATDTW is still set (hardware clears it when the sample
    | fell in its hazard window). Still primed: the controller follows the
    | link by itself. Not primed: it retired the other dTD before it saw the
    | link, so the list is empty after all and the HEAD to prime is the other
    | dTD if it is somehow still ACTIVE (never skip a queued packet), else
    | this one.
    moveal  %a0,%a4                 | a4 = the head to prime, if it comes to that
    movel   %a6,%d0
    eoril   #1,%d0
    lsll    #5,%d0
    lea     usbaudio_dtd0,%a2
    addal   %d0,%a2                 | a2 = the other slot's dTD
    movel   %a2@(4),%d0
    btst    #7,%d0
    beqs    .Lenq_prime             | case 1
    movel   %a0,%a2@                | case 2: other.next = this
    moveal  %a2,%a4
    movel   EPPRIME,%d0
    andil   #EP3IN_BIT,%d0
    bnes    .Lenq_done              | a prime is pending: it will read the list
    moveq   #16,%d2                 | tripwire attempts (bounded: this is an ISR)
.Lenq_trip:
    movel   USBCMD,%d0
    oril    #ATDTW,%d0
    movel   %d0,USBCMD
    movel   ENDPTSTAT,%d1
    andil   #EP3IN_BIT,%d1          | the sample
    movel   USBCMD,%d0
    andil   #ATDTW,%d0
    bnes    .Lenq_sampled
    subql   #1,%d2
    bnes    .Lenq_trip              | hazard: the sample is void, take another
    bras    .Lenq_done              | never settled: assume running; the
.Lenq_sampled:                      | self-heal re-primes next block if not
    movel   USBCMD,%d0
    andil   #0xffffbfff,%d0         | ~ATDTW
    movel   %d0,USBCMD
    tstl    %d1
    bnes    .Lenq_done              | still running: it will follow the link
    movel   %a2@(4),%d0
    btst    #7,%d0
    bnes    .Lenq_prime             | the other is still queued: head is it
    moveal  %a0,%a4                 | it retired meanwhile: head is this one
.Lenq_prime:
    bsr     audio_prime
.Lenq_done:
    movel   %a6,%d0
    eoril   #1,%d0
    moveb   %d0,aud_tail            | the other slot is next
    moveq   #1,%d0
    rts
.Lpb_none:
    moveq   #0,%d0
    rts

| Copy d0 (>= 1) 32-byte frames from %a2 to %a1, both advanced: one moveml
| pair per frame where the indexed version took 22 instructions. Clobbers
| d1-d7/a4.
audio_copy32:
1:  moveml  %a2@,%d1-%d7/%a4
    moveml  %d1-%d7/%a4,%a1@
    lea     %a2@(32),%a2
    lea     %a1@(32),%a1
    subql   #1,%d0
    bnes    1b
    rts

| Copy d0 (>= 1) 4-byte stereo-sum frames from %a2 to %a1, both advanced.
audio_copy4:
1:  movel   %a2@+,%a1@+
    subql   #1,%d0
    bnes    1b
    rts

| Mark the queue idle: zero both dTDs (ACTIVE clear, next = terminate) and
| start filling at slot 0. The DMA window is cache-inhibited, so no cpushl.
| Clobbers d1/a1.
audio_dtds_clear:
    lea     usbaudio_dtd0,%a1
    moveq   #15,%d1
1:  clrl    %a1@+
    subql   #1,%d1
    bpls    1b
    moveq   #1,%d1
    movel   %d1,usbaudio_dtd0       | next = terminate
    movel   %d1,usbaudio_dtd1
    clrb    aud_tail
    rts

| Resolve EP3's queue head from ENDPTLISTADDR. Returns it in %a0 and caches it
| in qh_ep3. ☠ VALIDATE BEFORE TRUSTING: stage2 runs during the card poll,
| early enough that the controller need not be set up, so this register can
| hold power-on garbage. The firmware programs 0x4EC94800 here; anything
| outside SDRAM is not an endpoint list and the known-good constant is used.
audio_qh_resolve:
    movel   EPLISTADDR,%d0
    andil   #0xfffff800,%d0
    cmpil   #0x40000000,%d0
    blts    .Lqh_fallback
    cmpil   #0x50000000,%d0
    bges    .Lqh_fallback
    bras    .Lqh_have
.Lqh_fallback:
    movel   #(QH_EP3IN - QH_EP3IN_OFF),%d0
.Lqh_have:
    addil   #QH_EP3IN_OFF,%d0
    movel   %d0,qh_ep3
    moveal  %d0,%a0
    rts

| ---- EP3 bring-up / teardown: frame-ISR context ONLY ------------------------
| The SET_INTERFACE shim records the host's request in usbaudio_alt; the frame
| shim compares it with aud_running once per block and calls one of these.
| Everything EP3 — queue head, dTDs, ENDPTCTRL3, ring cursors — is owned by
| that one context, so nothing here can interleave with a kick.
audio_ep3_up:
    | ☠ FLUSH FIRST. A dTD left primed by an earlier session must not be in
    | flight while the queue head is rewritten underneath it.
    bsr     audio_ep3_flush
    | ☠ ZERO THE WHOLE 64-BYTE dQH. EP3's queue head sits past anything the
    | stock firmware ever initializes, so on hardware its TOKEN (+0x0C) and
    | buffer pointers (+0x10..+0x1C) hold power-on garbage. The device
    | controller is a real bus master on silicon: it acts on that token's
    | ACTIVE bit and those pointers, then writes transfer status back through
    | them — an arbitrary memory write, which lands wherever the garbage
    | points (image code included). The firmware's own EP0 setup at
    | 0x4001d656 clears the token for exactly this reason; clearing the whole
    | structure is the safe superset. QEMU's packet bench never showed this
    | because it does not fetch the dQH out of guest memory at all.
    bsr     audio_qh_resolve        | %a0 = EP3 dQH, from the controller
    moveq   #15,%d1
1:  clrl    %a0@+
    subql   #1,%d1
    bpls    1b
    moveal  qh_ep3,%a0              | resolved just above
    | ☠ The SPEED decides the stream: 16 channels in 736 B packets every
    | 500 us at high speed, the stereo sum in 180 B packets every 1 ms at
    | full speed. PORTSC1 bits 27:26 are the negotiated speed (2 = high); the
    | responder picks the config descriptor by the same bits, so the format
    | the host was told and the packets it gets cannot disagree.
    movel   PORTSC1,%d0
    lsrl    #8,%d0
    lsrl    #8,%d0
    lsrl    #8,%d0
    lsrl    #2,%d0
    andil   #3,%d0
    cmpil   #2,%d0
    bnes    .Lspeed_fs
    moveq   #1,%d0
    moveb   %d0,aud_hs
    movel   #STEP_HS,%d0
    movel   %d0,aud_step
    movel   #(0x60000000+(PKT_MAX_HS<<16)),%d0  | dQH cap: Mult 1, ZLT off, maxpkt 736
    bras    .Lspeed_set
.Lspeed_fs:
    clrb    aud_hs
    movel   #STEP_FS,%d0
    movel   %d0,aud_step
    movel   #(0x60000000+(PKT_MAX_FS<<16)),%d0  | Mult 1, ZLT off, maxpkt 180
.Lspeed_set:
    movel   %d0,%a0@
    clrl    %a0@(4)                 | current dTD
    moveq   #1,%d0
    movel   %d0,%a0@(8)             | no dTD primed yet (terminate)
    clrl    %a0@(12)                | TOKEN — the field the firmware clears
    bsr     audio_dtds_clear        | both queue slots idle
    movel   #0x00840000,%d0         | ENDPTCTRL3: TXE + isochronous (the bench
    movel   %d0,ENDPTCTRL3          | reads the type to serve one dTD per poll)
    movel   aud_produced,%d0
    subil   #AUD_TARGET,%d0         | start a full cushion BEHIND the producer:
    bccs    .Lcons_ok               | the ring is already full, so there is no
    moveq   #0,%d0                  | priming gap and no startup underruns
.Lcons_ok:
    movel   %d0,usbaudio_consumed
    clrl    usbaudio_acc
    moveq   #1,%d0
    moveb   %d0,aud_running
    rts

audio_ep3_down:
    | ☠ FLUSH the endpoint. Clearing ENDPTCTRL3 disables it but does NOT
    | cancel a dTD that is already primed, so a packet queued microseconds
    | before alt 0 would still go out and the host would see audio after
    | teardown — intermittent by nature, which is why it first showed up as a
    | flaky gate rather than a clean failure.
    bsr     audio_ep3_flush
    clrl    ENDPTCTRL3
    bsr     audio_dtds_clear        | a flushed dTD still reads ACTIVE
    clrb    aud_running
    rts

| Flush EP3 IN and wait for it. ☠ The documented Chipidea sequence, not a
| single write: a prime that lands while a flush is in progress survives it,
| so after ENDPTFLUSH clears, ENDPTSTAT is checked and the flush repeated
| while the endpoint still shows primed. Bounded, because this runs in the
| frame ISR and a controller that never answers must not wedge the machine.
| Clobbers d0/d1.
audio_ep3_flush:
    moveq   #16,%d1                 | attempts
1:  movel   #EP3IN_BIT,%d0
    movel   %d0,EPFLUSH
2:  movel   EPFLUSH,%d0             | complete when the bit clears
    andil   #EP3IN_BIT,%d0
    bnes    2b
    movel   ENDPTSTAT,%d0
    andil   #EP3IN_BIT,%d0
    beqs    3f                      | idle: done
    subql   #1,%d1
    bnes    1b
3:  rts

    .if AUD_SOURCE == 6
| d1 = frame index 0..15 -> d2 = the telemetry field for that frame (diagnostic
| --source telem only). ☠ Must not touch d3 (the live right-channel sum).
audio_telem_value:
    moveq   #0,%d2
    tstl    %d1
    bnes    .Ltv1
    movel   #0x5A5A,%d2
    rts
.Ltv1:
    cmpil   #1,%d1
    bnes    .Ltv2
    movel   usbaudio_overruns,%d2
    lsrl    #8,%d2
    lsrl    #8,%d2
    bra     .Ltvdone
.Ltv2:
    cmpil   #2,%d1
    bnes    .Ltv3
    movel   usbaudio_overruns,%d2
    bra     .Ltvdone
.Ltv3:
    cmpil   #3,%d1
    bnes    .Ltv5
    movel   usbaudio_underruns,%d2
    bra     .Ltvdone
.Ltv5:
    cmpil   #5,%d1
    bnes    .Ltv6
    movel   usbaudio_lastfill,%d2
    bra     .Ltvdone
.Ltv6:
    cmpil   #6,%d1
    bnes    .Ltvdone
    movel   usbaudio_lastn,%d2
.Ltvdone:
    andil   #0x7fff,%d2
    rts
    .endif

    .if AUD_SOURCE >= 2
| Show %d0 as "USBAUD xxxxxxxx" via the firmware's dismissible popup. In the
| PAYLOAD (not the image reporter zone), so adding a probe never costs a
| reflash -- only the card file changes.
.set POPUP_FN, 0x4005a2b8
audio_show_hex:
    lea     %sp@(-16),%sp
    moveml  %d1-%d3/%a0,%sp@
    movel   %d0,%d1
    lea     aud_hexmsg+15,%a0
    moveq   #7,%d3
.Lhex_digit:
    movel   %d1,%d2
    andil   #15,%d2
    addil   #0x30,%d2
    cmpil   #0x39,%d2
    bles    .Lhex_store
    addil   #7,%d2
.Lhex_store:
    moveb   %d2,%a0@-
    lsrl    #4,%d1
    subql   #1,%d3
    bpls    .Lhex_digit
    pea     0x30
    pea     aud_hexmsg
    movel   #POPUP_FN,%a0
    jsr     %a0@
    lea     %sp@(8),%sp
    moveml  %sp@,%d1-%d3/%a0
    lea     %sp@(16),%sp
    rts
aud_hexmsg:
    .asciz  "USBAUD 00000000"
    .balign 2
    .endif

| ---- the per-block producer (installed at 0x4000d9a0, inside frame_isr) ----
| frame_isr runs once per 16-frame block on real hardware. This is the whole
| reason the feature can work on a unit: the block clock, the audio and the
| trigger to send are all firmware events, with nothing supplied by the
| emulator.
|
| ☠ The hook site is the LAST instruction before frame_isr's
| `moveml %sp@,%d0-%fp` epilogue, so every register is about to be reloaded
| from the stack — this shim may clobber d0-a6 freely. It must not touch %sp.
|
| Displaced: clrl 0x46104d4e (6 bytes), rejoin 0x4000d9a6.

| d -> saturated int16 in the low word of d. Inline, because the producer
| does it 256 times per block (eight tracks, two channels, sixteen frames):
| as a subroutine that was 512 bsr/rts per block. The bounds live in %a5
| (32767) and %a6 (-32768), loaded once per block.
.macro SAT16 d
    cmpl    %a5,\d
    jble    .Lsat_lo\@              | jbcc: gas picks the shortest branch
    movel   %a5,\d
    jbra    .Lsat_ok\@
.Lsat_lo\@:
    cmpl    %a6,\d
    jbge    .Lsat_ok\@
    movel   %a6,\d
.Lsat_ok\@:
.endm

    .global audio_frame_shim
audio_frame_shim:
    | ☠ The producer used to idle until the host opened the stream, so the ring
    | was empty at alt 1 and the first ~127 packets could not be filled --
    | measured on hardware as exactly 127 underruns, all at startup, and heard
    | as a burst of clicks in the first 1.5 s. Keeping it running costs one
    | block of summing per interrupt whether or not anyone is listening, and
    | buys a stream that starts instantly with a full buffer behind it.
    | aud_running gates the SENDING, just not the producing.
audio_frame_shim_body:
    .if AUD_SOURCE != 6
    | ☠ The stereo sum is only ever SENT at full speed. At high speed (PORTSC1
    | bits 27:26 = 2) it is skipped — sixteen adds and a clamp-and-store per
    | frame, ~15% of the producer, for a ring nobody reads. Decided per block
    | from the PORT, not from aud_hs, so a re-enumeration at full speed refills
    | the sum ring long before the host can select alt 1 (enumeration alone
    | takes >100 ms; the ring holds 23 ms). A telemetry build keeps it: the
    | telemetry rides on the sum's left channel.
    moveq   #0,%d1
    movel   PORTSC1,%d0
    andil   #0x0c000000,%d0
    cmpil   #0x08000000,%d0
    seq     %d1                     | d1 = 0xff at high speed
    moveal  %d1,%a1                 | a1 != 0: skip the sum this block
    .else
    suba    %a1,%a1                 | never skip
    .endif
    movel   RB_PREV,%d0
    | ☠ Telemetry: does the ping-pong bank actually alternate? Hardware shows
    | zero overruns and zero steady-state underruns, so mid-stream clicks
    | cannot be dropped or duplicated PACKETS -- which leaves the producer
    | reading the same bank twice (a duplicated 16-frame block) or skipping
    | one. Counted here so it is measurable instead of inferred.
    movel   %d0,%d2
    cmpl    usbaudio_lastbank,%d2
    bnes    .Lbank_ok
    addql   #1,usbaudio_bankdup
.Lbank_ok:
    movel   %d2,usbaudio_lastbank
    lsll    #8,%d0
    lsll    #2,%d0                  | prev * 1024 (imm shift is 1-8)
    addil   #RB_BASE,%d0
    moveal  %d0,%a2                 | a2 = this bank's track 0, frame 0
    movel   aud_produced,%d4
    movel   %d4,%d5
    andil   #AUD_FRAMES-1,%d5
    movel   %d5,%d0
    lsll    #SLOT_SHIFT,%d0
    lea     aud_ring,%a3
    addal   %d0,%a3                 | a3 = 16-channel slot cursor (a block never
                                    | wraps: 16 divides the ring size)
    lsll    #2,%d5
    lea     aud_sum,%a4
    addal   %d5,%a4                 | a4 = stereo-sum cursor
    moveq   #RB_SHIFT,%d1           | asr.l immediate is 1-8 only: shift via d1
    moveal  #32767,%a5              | SAT16 bounds
    moveal  #-32768,%a6
    moveq   #15,%d6                 | 16 frames
1:
    .if SRC_IS_TRACKS
    | ---- the eight tracks, one stereo pair each, plus their sum -----------
    | Per frame: track t's L,R (32-bit, post-FX, pre-fader) >> RB_SHIFT,
    | saturated to s16, stored little-endian at slot + t*4; the unsaturated
    | shifted values accumulate into the stereo sum (d5 = L, d3 = R), which
    | is what the full-speed stream carries.
    | The pair is packed as one longword: R16 in the high half, L16 in the
    | low half, then BYTEREV (ISA_C, and QEMU's cfv4e has it) turns big-endian
    | R:L into little-endian L,R in memory — one store where the byte-by-byte
    | version took four stores and two shifts.
    moveal  %a2,%a0                 | track 0, this frame
    moveq   #0,%d5                  | L sum
    moveq   #0,%d3                  | R sum
    moveq   #RB_TRACKS-1,%d7
2:  movel   %a0@,%d2                | track L
    asrl    %d1,%d2
    addl    %d2,%d5
    SAT16   %d2
    movel   %a0@(4),%d0             | track R
    asrl    %d1,%d0
    addl    %d0,%d3
    SAT16   %d0
    swap    %d0                     | R16 to the high half
    movew   %d2,%d0                 | L16 to the low half
    byterev %d0                     | -> L lo, L hi, R lo, R hi
    movel   %d0,%a3@+               | the channel pair
    lea     %a0@(128),%a0           | next track, same frame
    subql   #1,%d7
    bpl     2b
    movel   %d5,%d2                 | the stereo sum, L
    .else
    | ---- diagnostic source: synthetic 441 Hz triangle, +-8000 -------------
    | Ignores readback_buf entirely, on ALL sixteen channels and the sum.
    | Separates "can the device produce samples over USB at all" from "is the
    | tap point carrying audio". 100 frames per cycle at 44100 = 441 Hz, which
    | is also the emulator gate's tone.
    | ☠ No mulsl: 320 = 256 + 64 via shifts, because ColdFire immediate
    | multiply forms are not dependable here.
    movel   aud_phase,%d2
    cmpil   #50,%d2
    bges    .Lsyn_down
    movel   %d2,%d7
    lsll    #8,%d7                  | phase * 256
    lsll    #6,%d2                  | phase * 64
    addl    %d7,%d2                 | phase * 320
    subil   #8000,%d2               | rising: -8000 -> +8000
    bras    .Lsyn_have
.Lsyn_down:
    subil   #50,%d2
    movel   %d2,%d7
    lsll    #8,%d7
    lsll    #6,%d2
    addl    %d7,%d2                 | (phase-50) * 320
    movel   #8000,%d0
    subl    %d2,%d0                 | falling: +8000 -> -8000
    movel   %d0,%d2
.Lsyn_have:
    movel   aud_phase,%d0
    addql   #1,%d0
    cmpil   #100,%d0
    blts    .Lsyn_wrap
    moveq   #0,%d0
.Lsyn_wrap:
    movel   %d0,aud_phase
    movel   %d2,%d3                 | R sum = the tone too
    movel   %d2,%d0
    lsrl    #8,%d0
    moveq   #15,%d7                 | all 16 channels of the slot
.Lsyn_fill:
    moveb   %d2,%a3@+
    moveb   %d0,%a3@+
    subql   #1,%d7
    bpls    .Lsyn_fill
    .endif
    movel   %a1,%d0
    bne     .Lsum_skip              | high speed: the sum ring is not sent
    | ---- the stereo sum: saturate and store little-endian -----------------
    SAT16   %d2
    | ☠ producer-side discontinuity detector (telemetry only; the compare is a
    | few cycles and the counter is never read on a normal build)
    movel   usbaudio_lastsamp,%d0
    subl    %d2,%d0
    bpls    .Lsj_abs
    negl    %d0
.Lsj_abs:
    cmpil   #800,%d0
    bcss    .Lsj_done
    addql   #1,usbaudio_srcjump
.Lsj_done:
    movel   %d2,usbaudio_lastsamp
    .if AUD_SOURCE == 6
    | ☠ Telemetry rides on the LEFT channel of the sum AND of track 1 (slot
    | channel 0), substituted AFTER the detector above has compared real
    | samples. Every other channel stays real audio, so the same capture is
    | both measurable and listenable.
    movel   %d6,%d0
    movel   #15,%d1
    subl    %d0,%d1                 | d1 = frame index within the block
    bsr     audio_telem_value       | d1 -> d2 = the field for this frame
    moveq   #RB_SHIFT,%d1           | restore the shift
    moveb   %d2,%a3@(-32)
    movel   %d2,%d0
    lsrl    #8,%d0
    moveb   %d0,%a3@(-31)
    .endif
    movel   %d3,%d0                 | sum R
    SAT16   %d0
    swap    %d0
    movew   %d2,%d0                 | R16:L16
    byterev %d0                     | -> L LE, R LE
    movel   %d0,%a4@+
.Lsum_skip:
    lea     %a2@(8),%a2             | next frame
    subql   #1,%d6
    bpl     1b                      | not bpls: the loop body is long
    addql   #8,%d4
    addql   #8,%d4                  | 16 frames produced
    movel   %d4,aud_produced
    | ---- EP3, owned by this context alone ---------------------------------
    | usbaudio_alt is what the host asked for (SET_INTERFACE); aud_running is
    | what EP3 currently is. Bring it up or down when they differ, and when
    | it is up the block clock IS the send clock: top the queue up now.
    mvzb    usbaudio_alt,%d0
    mvzb    aud_running,%d1
    cmpl    %d0,%d1
    beqs    .Lep3_same
    tstl    %d0
    beqs    .Lep3_down
    bsr     audio_ep3_up
    bras    .Lep3_kick
.Lep3_down:
    bsr     audio_ep3_down
    bras    9f
.Lep3_same:
    tstl    %d1
    beqs    9f                      | nobody listening: produce, but do not send
.Lep3_kick:
    bsr     usbaudio_kick
9:  clrl    0x46104d4e              | displaced
    jmp     0x4000d9a6

| ---- UAC2 class-request shim (installed at 0x4001de64) ----------------------
| Displaced: movel 0xfc0b01c0,%d0 — the first instruction of the stock
| "unknown request: STALL EP0" tail, which every request the dispatcher does
| not recognise falls into. A UAC2 host asks the CLOCK SOURCE for its sample
| rate before it will publish a device (RANGE + CUR of CS_SAM_FREQ_CONTROL,
| CUR of CS_CLOCK_VALID_CONTROL, all class GET to the AudioControl interface
| with the entity id in wIndex's high byte), and a STALL there means no
| audio device. A SET CUR of that rate is taken too (.Lctrl_set). Everything
| else falls through to the stock STALL, which is the legal answer for a
| control we do not implement.
|
| Reply the way the stock string-descriptor path does: push the buffer and
| min(wLength, len) and jump to the shared usb_ep0_send tail. d2 still holds
| wLength here (nothing between the dispatcher and the stall touches it). The
| reply buffers are CONSTANTS in this blob: the EP0 send DMAs them straight
| out of memory, and the loader's cache push after relocation is the last
| time they may be written.
    .global audio_ctrl_shim
audio_ctrl_shim:
    mvzb    SETUP_BMREQ,%d0
    cmpil   #0x21,%d0               | class SET, interface recipient
    beq     .Lctrl_set
    cmpil   #0xa1,%d0               | class GET, interface recipient
    bne     .Lctrl_stock
    mvzb    SETUP_IFACE,%d0
    cmpil   #UAC2_AC_IFACE,%d0      | the audio function's AudioControl
    bne     .Lctrl_stock
    mvzb    SETUP_WIDXH,%d0
    cmpil   #UAC2_CLOCK_ID,%d0      | the clock source entity
    bne     .Lctrl_stock
    mvzb    SETUP_WVALH,%d0         | control selector
    mvzb    SETUP_BREQ,%d1          | 1 = CUR, 2 = RANGE
    cmpil   #1,%d0                  | CS_SAM_FREQ_CONTROL
    beqs    .Lctrl_freq
    cmpil   #2,%d0                  | CS_CLOCK_VALID_CONTROL
    bne     .Lctrl_stock
    cmpil   #1,%d1
    bne     .Lctrl_stock            | only CUR exists for validity
    pea     uac2_clock_valid
    moveq   #1,%d0
    bras    .Lctrl_send
.Lctrl_freq:
    cmpil   #1,%d1
    bnes    .Lctrl_freq_range
    pea     uac2_freq_cur
    moveq   #4,%d0
    bras    .Lctrl_send
.Lctrl_freq_range:
    cmpil   #2,%d1
    bne     .Lctrl_stock
    pea     uac2_freq_range
    moveq   #14,%d0
.Lctrl_send:
    cmpl    %d2,%d0                 | min(wLength, len)
    blss    1f
    movel   %d2,%d0
1:  movel   %d0,%sp@-
    jmp     EP0_SEND_TAIL
| ---- SET CUR of the clock's sample frequency ---------------------------------
| The clock is fixed and its frequency control is declared read-only, but a
| UAC2 host may still SET it to the rate it has just read back (CUR, 4
| bytes), and some hosts give the audio function up when that STALLs. So a
| SET CUR of CS_SAM_FREQ_CONTROL on this clock is taken: 44100, the one rate
| RANGE offers, is acknowledged and changes nothing; any other rate STALLs
| the status stage. Every other class SET still STALLs at once (stock).
| The stock EP0 stack has no control OUT data stage, so this primes EP0's OUT
| dTD for it exactly as usb_ep0_send primes its status OUT, and waits here
| for the data: returning first races the stock completion loop that runs
| right after this request in the same interrupt, which would take a data
| stage that lands by then for the previous transfer's status OUT. A host
| sends it in the next microframe; 100,000 polls (~10 ms) is the bound, and
| audio_isr_shim finishes it for a host slower than that.
.Lctrl_set:
    mvzb    SETUP_IFACE,%d0
    cmpil   #UAC2_AC_IFACE,%d0      | the audio function's AudioControl
    bne     .Lctrl_stock
    mvzb    SETUP_WIDXH,%d0
    cmpil   #UAC2_CLOCK_ID,%d0      | the clock source entity
    bne     .Lctrl_stock
    mvzb    SETUP_WVALH,%d0
    cmpil   #1,%d0                  | CS_SAM_FREQ_CONTROL
    bne     .Lctrl_stock
    mvzb    SETUP_BREQ,%d0
    cmpil   #1,%d0                  | CUR
    bne     .Lctrl_stock
    moveq   #4,%d0
    cmpl    %d0,%d2                 | wLength
    bne     .Lctrl_stock
    movel   #0x00408080,%d0         | 64 bytes, IOC, ACTIVE (usb_ep0_send's status OUT)
    movel   %d0,EP0OUT_DTD+4
    movel   #EP0_BUF,%d0
    movel   %d0,EP0OUT_DTD+8
    movel   #0xdead0001,%d0         | next: terminate
    movel   %d0,EP0OUT_DTD
    moveal  QH0_OUT_PTR,%a0
    movel   #EP0OUT_DTD,%d0
    movel   %d0,%a0@(8)             | dQH next dTD
    movel   EPPRIME,%d0
    moveq   #1,%d1                  | EP0 OUT
    orl     %d1,%d0
    movel   %d0,EPPRIME
    movel   #100000,%d1
5:  movel   EP0OUT_DTD+4,%d0
    btst    #7,%d0                  | ACTIVE
    beqs    6f
    subql   #1,%d1
    bnes    5b
    moveq   #1,%d0
    moveb   %d0,uac2_set_pending
    jmp     SETIFACE_DONE
6:  bsr     uac2_set_done
    jmp     SETIFACE_DONE
.Lctrl_stock:
    movel   0xfc0b01c0,%d0          | displaced
    jmp     CTRL_STOCK

| The data stage of a clock SET CUR has landed in EP0_BUF: take the OUT
| completion (before the stock loop sees it), then ACK 44100 (status IN) or
| STALL the status stage. Clobbers d0-d1/a0-a1.
uac2_set_done:
    moveq   #1,%d0
    movel   %d0,EPCOMPLETE          | W1C EP0 OUT
    clrb    uac2_set_pending
    movel   EP0_BUF,%d0             | wire order: 44 ac 00 00 = 44100
    cmpil   #0x44ac0000,%d0
    bnes    1f
    jsr     EP0_STATUS_IN
    rts
1:  movel   EP0CTRL,%d0
    bset    #16,%d0                 | STALL EP0 IN: the status stage
    movel   %d0,EP0CTRL
    rts

| ---- descriptor length-clamp shims -----------------------------------------
| The stock responder clamps GET_DESCRIPTOR(CONFIG / OTHER_SPEED) replies to a
| hardcoded length via a moveq that cannot hold the grown config; each shim
| computes min(wLength, CFG_LEN) and rejoins. d2 = wLength (host order).
    .global audio_clamp1_shim, audio_clamp2_shim
audio_clamp1_shim:
    movel   %d2,%d1
    cmpil   #CFG_LEN,%d1
    blss    1f
    movel   #CFG_LEN,%d1
1:  jmp     CLAMP1_REJOIN
audio_clamp2_shim:
    movel   %d2,%d1
    cmpil   #CFG_LEN,%d1
    blss    1f
    movel   #CFG_LEN,%d1
1:  jmp     CLAMP2_REJOIN

| ---- runtime patch table ---------------------------------------------------
| Each record: target va, expected-bytes ptr, patch-bytes ptr, length.
    .data
    .balign 4
| ☠ PATCH_USB / PATCH_FRAME are build-time defsyms (both 1 normally). They
| exist so a payload can install a SUBSET of the hooks (--patches usb /
| --patches frame), which is how a hardware fault gets bisected by copying
| one file to the card instead of reflashing — the whole point of the
| self-describing payload header.
usbaudio_patches:
.if PATCH_USB
    .long 0x4001e606, exp_isr,      pat_isr,      6
    .long 0x4001dd04, exp_setiface, pat_setiface, 6
    .long 0x4001d824, exp_getiface, pat_getiface, 6
    .long 0x4001d882, exp_cfg_fs,   pat_cfg_fs,   4
    .long 0x4001d88a, exp_cfg_hs,   pat_cfg_hs,   4
    .long 0x4001d8c0, exp_cfg_os_hs,pat_cfg_os_hs,4
    .long 0x4001d8c8, exp_cfg_os_fs,pat_cfg_os_fs,4
    .long 0x4001d858, exp_clamp,    pat_clamp1,   6
    .long 0x4001d896, exp_clamp,    pat_clamp2,   6
    .long 0x4001de64, exp_ctrl,     pat_ctrl,     6
    .long 0x4001d4b2, exp_ep0page,  pat_ep0page,  6
.endif
.if PATCH_FRAME
    .long 0x4000d9a0, exp_frame,    pat_frame,    6
.endif
.if PATCH_USB
    .long 0x400e2004, exp_devcls,   pat_devcls,   3
    .if HEAP_RESERVE
    | ---- shrink the flex heap so the bottom pages are permanently ours ------
    | flex_heap_init memsets the WHOLE heap and re-seeds the free-page stack on
    | every project load, which is what destroys a heap-allocated payload. Four
    | immediates make it skip the lowest HEAP_RESERVE+1 pages (page 0 is
    | already reserved), so those pages are outside BOTH the memset and the
    | free list -- memory the firmware itself no longer believes exists. The
    | payload is loaded straight into them, so nothing ever has to relocate.
    |
    | ☠ Bottom, not top: the recorder reserve pops from the TOP of the free
    | stack, so the top pages are contended. The bottom sits at a fixed known
    | address (sample_heap_base) that nothing claims once it is out of the list.
    |
    | ☠ These are RUNTIME writes by the card payload, never flashed edits. With
    | no /USBAUDIO.BIN, or with NO held at boot, flex_heap_init keeps its stock
    | constants and the machine is untouched -- which is the whole recovery
    | contract.
    .long 0x40096f82, exp_heap_cnt, pat_heap_cnt, 4   | free_top seed
    .long 0x40096fa5, exp_heap_1st, pat_heap_1st, 1   | first seeded index
    .long 0x40097008, exp_heap_len, pat_heap_len, 4   | memset length
    .long 0x4009700e, exp_heap_base,pat_heap_base,4   | memset base
    .endif
.endif
    .long 0

exp_isr:       .byte 0x4e,0xf9
               .long USBMIDI_ISR_SHIM
    .if HEAP_RESERVE
.set HEAP_PAGES,  HEAP_RESERVE + 1      | page 0 is already out of the free list
.set HEAP_BYTES,  HEAP_PAGES * 6144
exp_heap_cnt:  .long 14602
pat_heap_cnt:  .long 14602 - HEAP_RESERVE
exp_heap_1st:  .byte 1
pat_heap_1st:  .byte 1 + HEAP_RESERVE
    .balign 2
exp_heap_len:  .long 89720832
pat_heap_len:  .long 89720832 - HEAP_BYTES
exp_heap_base: .long 0x40a955e0
pat_heap_base: .long 0x40a955e0 + HEAP_BYTES
    .endif

pat_isr:       .byte 0x4e,0xf9
               .long audio_isr_shim
exp_setiface:  .byte 0x20,0x39,0xfc,0x0b,0x01,0xc4
pat_setiface:  .byte 0x4e,0xf9
               .long audio_setiface_shim
exp_getiface:  .byte 0x48,0x79,0x40,0x0e,0x20,0xa1
pat_getiface:  .byte 0x4e,0xf9
               .long audio_getiface_shim
exp_clamp:     .byte 0x70,0x7c,0xb0,0x82,0x65,0x04
pat_clamp1:    .byte 0x4e,0xf9
               .long audio_clamp1_shim
pat_clamp2:    .byte 0x4e,0xf9
               .long audio_clamp2_shim
exp_ctrl:      .byte 0x20,0x39,0xfc,0x0b,0x01,0xc0   | movel 0xfc0b01c0,%d0
pat_ctrl:      .byte 0x4e,0xf9
               .long audio_ctrl_shim
exp_ep0page:   .byte 0x23,0xd0,0x4e,0xc9,0x50,0x28   | movel %a0@,0x4ec95028
pat_ep0page:   .byte 0x4e,0xf9
               .long audio_ep0page_shim
| UAC2 clock-source replies, little-endian on the wire. CONSTANT: see the shim.
uac2_freq_cur:   .byte 0x44,0xac,0x00,0x00          | 44100
uac2_freq_range: .byte 0x01,0x00                    | wNumSubRanges = 1
                 .byte 0x44,0xac,0x00,0x00          | dMIN 44100
                 .byte 0x44,0xac,0x00,0x00          | dMAX 44100
                 .byte 0x00,0x00,0x00,0x00          | dRES 0
uac2_clock_valid: .byte 0x01
    .balign 4
exp_cfg_fs:    .long MIDI_CFG_FS
pat_cfg_fs:    .long cfg_fs
exp_cfg_hs:    .long MIDI_CFG_HS
pat_cfg_hs:    .long cfg_hs
exp_cfg_os_hs: .long MIDI_CFG_OS_HS
pat_cfg_os_hs: .long cfg_os_hs
exp_cfg_os_fs: .long MIDI_CFG_OS_FS
pat_cfg_os_fs: .long cfg_os_fs
| ☠ The device-descriptor class triple. An Interface Association Descriptor
| is only honoured when the DEVICE declares Miscellaneous / Common Class /
| IAD; with 00/00/00 a host may ignore the association entirely and leave the
| audio function ungrouped. Interface-level classes are unaffected, so mass
| storage still binds exactly as before.
exp_devcls:    .byte 0x00,0x00,0x00
pat_devcls:    .byte 0xef,0x02,0x01
exp_frame:     .byte 0x42,0xb9,0x46,0x10,0x4d,0x4e   | clrl 0x46104d4e
pat_frame:     .byte 0x4e,0xf9
    .if GUARD_FRAME
               .long GUARD_FRAME    | via the image-resident guard, so a missing
                                    | payload is silence rather than an illegal
                                    | instruction at IPL 5
    .else
               .long audio_frame_shim
    .endif

| ---- payload state ----------------------------------------------------------
    .balign 4
    .global usbaudio_consumed, usbaudio_acc, usbaudio_overruns
    .global usbaudio_underruns, usbaudio_lastn, usbaudio_lastfill
    .global usbaudio_lastbank, usbaudio_bankdup, usbaudio_srcjump
    .global usbaudio_reprimes
usbaudio_consumed: .long 0          | frames pulled from the ring
usbaudio_acc:      .long 0          | frames-per-packet accumulator (x100)
usbaudio_overruns: .long 0
usbaudio_underruns: .long 0    | packets we could not fill
usbaudio_lastn:    .long 0    | frames in the most recent packet
usbaudio_lastfill: .long 0    | ring fill at the most recent packet
usbaudio_lastbank: .long -1   | previous ping-pong bank
usbaudio_bankdup:  .long 0    | blocks where the bank did NOT alternate
usbaudio_lastsamp: .long 0    | previous summed L sample
usbaudio_srcjump:  .long 0    | discontinuities present at production
usbaudio_reprimes: .long 0    | idle endpoint found holding a queued dTD
aud_produced:      .long 0          | producer frame count (relocates with us)
aud_phase:         .long 0          | synthetic-source phase, 0..99
qh_ep3:            .long 0          | EP3 IN dQH, read from ENDPTLISTADDR
aud_step:          .long STEP_HS    | frames per packet x100, set by the speed
aud_ring:          .space AUD_FRAMES*SLOT_BYTES  | 1024 x 16 ch s16 LE
aud_sum:           .space AUD_FRAMES*4           | 1024 x stereo sum s16 LE
usbaudio_alt:      .byte 0          | alt setting the host asked for
uac2_set_pending:  .byte 0          | a clock SET CUR's data stage is primed
aud_running:       .byte 0          | EP3 is up (frame-ISR owned)
aud_hs:            .byte 0          | 1 = high speed (16 ch), 0 = full (sum)
aud_tail:          .byte 0          | next dTD slot to fill (0/1)
    .balign 4
.ifndef DMA_FIXED
.set DMA_FIXED, 0
.endif
.if DMA_FIXED == 0
.error "DMA_FIXED is required: the dTDs and packet buffers must live in cache-inhibited memory (--dma-at)"
.endif
| ☠ Everything the USB controller reads by DMA lives OUTSIDE the payload blob,
| in cache-inhibited memory. The blob runs from flex heap pages, which ACR0
| maps cacheable copyback, and the controller is a bus master that does not
| snoop the CPU's data cache -- so a dTD written by the CPU can still be dirty
| in cache when the controller fetches it, and the controller then reads
| whatever RAM held before. That presented exactly as measured on hardware:
| ENDPTPRIME clears (the prime was consumed) while ENDPTSTAT never arms (the
| descriptor it found was not ACTIVE). Per-line cpushl did NOT fix it, in
| either form; moving the structures out did, first try.
|
| The window is 0x4ec94a00..0x4ec95000 (--dma-at): past the controller's own
| 8-entry endpoint list (0x4ec94800 + 8*64) and below the firmware's dTD pool
| at 0x4ec95000, referenced by nothing in the image (scan 2026-09-22), and
| EXACTLY 1536 B: two 32-byte dTDs and two 736-byte packet buffers, one pair
| per queue slot.
.set usbaudio_dtd0, DMA_FIXED                   | EP3 IN dTD, slot 0
.set usbaudio_dtd1, DMA_FIXED + 32              | slot 1
.set usbaudio_buf0, DMA_FIXED + 64              | slot 0's packet (<= 736 B)
.set usbaudio_buf1, DMA_FIXED + 64 + PKT_MAX_HS | slot 1's

    .balign 4
    .include "usb-audio-cfg.s"
