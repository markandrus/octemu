/*
 * octemu — shared declarations.
 *
 * The frontend is the panel MCU and the audio consumer. It is NOT the
 * Octatrack's clock: the DSP shim inside QEMU owns the pace, and this
 * process consumes blocks whenever it runs. A preempted frontend costs
 * nothing.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef OT_EMU_H
#define OT_EMU_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#include "wav.h"

#define W 128                  /* screen, pixels */
#define H 64
#define FRAMES 16              /* audio frames per block */
#define SLOTS 8                /* ESAI TX slots */
#define INS 4                  /* ESAI RX slots, IN A-D */
#define RATE 44100

/*
 * Everything the panel wire carries, as one snapshot.
 *
 * The lamp model, all measured on the wire:
 *   [0x20|n][byte]  ABSOLUTE snapshot of LED-bit group n (bits 8n..8n+7).
 *   [0xA0|n][byte]  the same for groups 16+n; bit ids 128..133 are the six
 *                   meter LEDs, the only lamps past id 127.
 *   [0x30|v][id]    4-bit brightness per BIT id (not per lamp).
 *   [0xB5][hi][lo][r][g][b]  palette. Lamp l (bit ids 2l/2l+1) has colour
 *                   registers 4l+1 (channel A) and 4l+2 (B); default A=440000
 *                   red, B=004400 green, 0x44 = full scale. The firmware
 *                   REWRITES these — white highlights are A=444444, and both
 *                   channels lit is additive (amber).
 * Visible lamp = bit set AND brightness > 0; colour = palette x bright/15.
 *
 * ☠ Reading 0x20/0xA0 as "LED on/off" masks is WRONG, and it is an easy trap
 * because a decoder that parses without drawing never notices: the chase walks
 * by REWRITING group 0 (03 -> 0c -> 30 -> c0 -> 00), so an OR/AND-mask decode
 * accumulates lamps that never clear.
 */
typedef struct {
    uint8_t fb[H][W];
    uint32_t fb_gen;                   /* bumped per display write */
    uint8_t lit[17];                   /* 134 lamp bits, group snapshots */
    uint8_t bright[256];               /* per bit id, 0-15 */
    uint8_t rgb[512][3];               /* palette */
    uint8_t keys[8];                   /* key-group snapshots we last sent */
    int xfader;                        /* wire value; 255 = far LEFT */
    /* Every detent ever sent, per encoder, signed. The renderer's turn hints
     * diff it against what they drew last frame; going through panel_encoder
     * means a scripted walk moves them exactly like a mouse does. */
    int32_t enc_ticks[7];
} PanelState;

/* ---- panel.c: the wire ---------------------------------------------------- */
bool panel_start(const char *sock_path);
void panel_stop(void);
void panel_key(int id, bool down);
void panel_encoder(int enc, int delta);
void panel_xfader(int pos);
void panel_snapshot(PanelState *out);
bool panel_lamp_lit(int bit_id);
uint32_t panel_fb_gen(void);
bool panel_peer_gone(void);
int panel_button_id(const char *name);

/* ---- ocr.c: reading the screen -------------------------------------------- */
bool ocr_load_fonts(const char *os_image);
/* Every text run on the framebuffer, newline-joined, so a needle can be found
 * with strstr. Both video polarities are read: titles are inverse video. */
void ocr_read(const PanelState *p, char *out, size_t cap);
/* The panel font draws O and 0 (and S and 5) as the same pixels, so the exact
 * matcher cannot tell them apart; canonicalise both sides before comparing. */
void ocr_canon(char *s);

/* ---- audio.c: the shared ring, recording, live monitor -------------------- */
/* InGen and the IN_* kinds live in wav.h, shared with octdsp. */
/* What --recording x.mov records: the rendered front panel (the default), or
 * the 128x64 display alone, which is all a headless run can offer. */
typedef enum { VIDEO_PANEL, VIDEO_SCREEN } VideoMode;
void audio_video_mode(VideoMode mode, int panel_width);
/* Render thread, right after a present: answers a pending panel shutter. */
void audio_video_tick(void);
void audio_record_gate(bool on);
/* Frames the shutter had to repeat because the render thread was behind. */
uint64_t audio_video_repeats(void);

bool audio_start(const char *sock_path, const InGen in[INS],
                 const char *recording, bool live_monitor);
void audio_stop(void);
void audio_recording_flush(void);
uint64_t audio_blocks(void);
float audio_phones(void);
void audio_set_phones(float v);

/* The host-specific calls — app activation, the USB DISK MODE host mount and
 * the --midi bridge — are in src/platform/platform.h, one file per platform. */

/* ---- skin.c: the panel window --------------------------------------------- */
struct SDL_Renderer;
struct SDL_Window;
bool skin_load(void);
bool skin_ok(void);
void skin_hints(bool on);
void skin_window(struct SDL_Window *win, struct SDL_Renderer *ren);
void skin_render(void);
void skin_view_update(void);
void skin_mouse_down(float x, float y, bool ctrl);
void skin_mouse_motion(float x, float y);
void skin_mouse_up(void);
bool skin_over_control(float x, float y);
int skin_knob_at(float x, float y);          /* encoder index, or -1 */
void skin_view_mouse(int wx, int wy, float *sx, float *sy);
void skin_view_window(float sx, float sy, int *wx, int *wy);
void skin_view_calibrate(int ex, int ey);    /* real mouse events only */
void skin_shot(const char *path);
/* The rendered panel as RGB24 at dw x dh (box-downscaled), and the renderer's
 * own output size. Render thread only — see skin.c. */
bool skin_grab(uint8_t *dst, int dw, int dh);
bool skin_plate_size(int *w, int *h);
bool skin_plate_native(int *w, int *h);
bool skin_grab_plate(uint8_t *dst, int w, int h);
bool skin_output_size(int *w, int *h);
void skin_resize(int width);
void skin_tick(void);                        /* deferred switch/click release */
/* Scripted gestures go through the SAME hit-test and gesture path as the
 * mouse, so a scripted click proves the panel geometry rather than just the
 * key id — and the click round-trips through synthetic SDL events at
 * inverse-mapped window coords, which exercises the view transform too. */
bool skin_script_click(const char *name);
bool skin_script_drag(const char *name, long detents, bool push);

/* ---- script.c: the JSONL walk --------------------------------------------- */
bool script_open(const char *path);
/* Called from the main loop. Returns false when the walk is over. */
bool script_step(void);
bool script_done(void);
bool script_failed(void);

/* ---- main.c --------------------------------------------------------------- */
extern bool g_quit;
void emu_die(const char *msg);
int64_t now_ms(void);

#endif
