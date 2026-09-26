/*
 * The window IS the MKII front panel.
 *
 * out/panel/panel.bin (assets/panel/rasterize.py) holds a base raster of the
 * SVG with the fader handle and phones pointer excised, and per lightable
 * element a lit UNDERLAY plus an INK-COVERAGE alpha sprite (alpha solved from
 * a black-ink and a green-ink render: cov = green.G - black.G). That lets the
 * ink be tinted to ANY wire colour by exact linear compositing: draw the
 * underlay, then the coverage with SDL_SetTextureColorMod(palette x
 * brightness). Plus a PRESSED variant per key, the handle and pointer as alpha
 * sprites, and hit geometry.
 *
 * The SVG-unit -> pixel transform is ours (no SDL logical size), so mouse
 * mapping survives resize and HighDPI and the 128x64 screen blit can snap to a
 * whole-pixel multiple.
 *
 * SPDX-License-Identifier: MIT
 */
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <SDL.h>

#include "emu.h"

typedef struct {
    char name[28];
    uint8_t kind;                       /* 0 pixel-replacement, 1 alpha */
    int32_t x, y;
    uint32_t w, h;
    uint8_t *rgba;
    SDL_Texture *tex;
} Sprite;

typedef struct {
    char name[28];
    uint32_t kind;                      /* 0 button, 1 knob, 2 fader, 3 screen */
    float v[6];
    int action;                         /* key id / encoder index */
} PanelHit;

static struct {
    bool ok;
    uint32_t scale, vbw, vbh, bw, bh;
    uint32_t px, py, pw, ph;            /* the face plate, in viewBox units */
    uint8_t *base;
    SDL_Texture *base_tex, *screen_tex;
    Sprite *spr;
    uint32_t nspr;
    PanelHit *hit;
    uint32_t nhit;
    Sprite *handle, *pointer;
    PanelHit *fader, *screen;
} g_skin;

static SDL_Window *g_win;
static SDL_Renderer *g_ren;
/* mpx: renderer pixels per mouse-event unit. SDL2 reports the mouse in points
 * (mpx == dpr); sdl2-compat on SDL3 reports it in pixels on a retina display
 * (mpx == 1). mouse_px records which, learned by skin_view_calibrate. */
static struct { float scale, offx, offy, dpr, mpx; bool mouse_px; } g_view;

/* The fb_gen the screen texture currently holds; see skin_render. */
static uint32_t g_screen_gen;
static bool g_screen_valid;

/* Lamp map, measured. L<n> = bit ids 2n/2n+1; chan is which channels this
 * element shows (3 = both, additive — the meter lamps split their two channels
 * across two physical LEDs). */
static const struct {
    const char *svg;
    int lamp, chan;
} g_lampmap[] = {
    {"trig-1",0,3},{"trig-2",1,3},{"trig-3",2,3},{"trig-4",3,3},
    {"trig-5",4,3},{"trig-6",5,3},{"trig-7",6,3},{"trig-8",7,3},
    {"trig-9",8,3},{"trig-10",9,3},{"trig-11",10,3},{"trig-12",11,3},
    {"trig-13",12,3},{"trig-14",13,3},{"trig-15",14,3},{"trig-16",15,3},
    {"btn-a",16,3},{"btn-b",17,3},
    {"led-card-status",18,3},          /* green, flickers with CF activity */
    {"btn-tempo",19,3},                /* pulses with the tempo */
    {"btn-t1",20,3},{"btn-t2",21,3},{"btn-t3",22,3},{"btn-t4",23,3},
    {"btn-t5",24,3},{"btn-t6",25,3},{"btn-t7",26,3},{"btn-t8",27,3},
    {"led-rec-status",32,3},           /* FUNC+ARR lights it red */
    {"btn-src",33,3},{"btn-amp",34,3},{"btn-midi",35,3},
    /* page LEDs: dim red = the page exists, bright = the active edit page in
     * grid rec, amber beat-flash = the page the sequencer is playing */
    {"led-page-1",36,3},{"led-page-2",37,3},{"led-page-3",38,3},
    {"led-page-4",39,3},
    {"btn-fx1",40,3},{"btn-fx2",41,3},{"btn-rec",42,3},{"btn-lfo",43,3},
    {"btn-stop",44,3},{"btn-play",45,3},
    {"btn-mix",51,3},
    {"btn-proj",60,3},{"btn-part",61,3},{"btn-aed",62,3},{"btn-arr",63,3},
    {"led-in-a",64,1},{"led-in-b",64,2},
    {"led-in-c",65,1},{"led-in-d",65,2},
    {"led-int-l",66,1},{"led-int-r",66,2},
};

/* The runtime half of the map above, filled by skin_load: the two sprites
 * each element draws with, and its key id (-1 when the element is not a
 * key). Parallel rather than three more members of g_lampmap, because that
 * table is measured data written three entries to a line — carrying fields
 * it does not initialise would mean spelling out all six on every row. */
static struct { Sprite *u, *i; int key; }
    g_lampst[sizeof g_lampmap / sizeof *g_lampmap];

static Sprite *g_pressed[64];          /* key id -> pressed sprite */

static const struct { const char *svg; const char *btn; } g_hitbtn[] = {
    {"btn-midi","MIDI"},{"btn-rec1","REC1"},{"btn-rec2","REC2"},
    {"btn-rec3","REC3"},{"btn-proj","PROJ"},{"btn-part","PART"},
    {"btn-aed","AED"},{"btn-mix","MIX"},{"btn-arr","ARR"},{"btn-func","FUNC"},
    {"btn-cue","CUE"},{"btn-ptn","PTN"},{"btn-bank","BANK"},{"btn-yes","YES"},
    {"btn-no","NO"},{"btn-up","UP"},{"btn-down","DOWN"},{"btn-left","LEFT"},
    {"btn-right","RIGHT"},{"btn-rec","REC"},{"btn-play","PLAY"},
    {"btn-stop","STOP"},{"btn-src","SRC"},{"btn-amp","AMP"},{"btn-lfo","LFO"},
    {"btn-fx1","FX1"},{"btn-fx2","FX2"},{"btn-tempo","TEMPO"},{"btn-a","A"},
    {"btn-b","B"},{"btn-page","PAGE"},
};

bool skin_ok(void) { return g_skin.ok; }

static int hit_action(const PanelHit *h)
{
    if (h->kind == 0) {
        if (!strncmp(h->name, "trig-", 5)) {
            return atoi(h->name + 5) - 1;
        }
        if (h->name[0] == 'b' && h->name[4] == 't' &&
            isdigit((unsigned char)h->name[5])) {
            return 16 + h->name[5] - '1';           /* btn-tN -> TRACK N */
        }
        for (size_t i = 0; i < sizeof g_hitbtn / sizeof *g_hitbtn; i++) {
            if (!strcmp(h->name, g_hitbtn[i].svg)) {
                return panel_button_id(g_hitbtn[i].btn);
            }
        }
        return -1;
    }
    if (h->kind == 1) {                             /* encoder index */
        if (!strcmp(h->name, "knob-level")) return 6;
        if (!strcmp(h->name, "knob-phones")) return 7;   /* analog pot, local */
        if (h->name[5] >= 'a' && h->name[5] <= 'f' && !h->name[6]) {
            return h->name[5] - 'a';
        }
    }
    return -1;
}

static Sprite *sprite(const char *name)
{
    for (uint32_t i = 0; i < g_skin.nspr; i++) {
        if (!strcmp(g_skin.spr[i].name, name)) {
            return &g_skin.spr[i];
        }
    }
    return NULL;
}

static PanelHit *hit_by_name(const char *n)
{
    for (uint32_t i = 0; i < g_skin.nhit; i++) {
        if (!strcmp(g_skin.hit[i].name, n)) {
            return &g_skin.hit[i];
        }
    }
    return NULL;
}

bool skin_load(void)
{
    FILE *f = fopen("out/panel/panel.bin", "rb");
    uint32_t hdr[12];

    if (!f) {
        fprintf(stderr, "octemu: no out/panel/panel.bin "
                "(run 'make panel')\n");
        return false;
    }
    /* ☠ Version 2 is not optional — it is the version that carries the plate
     * rect, and a v1 bin left over from an older tree would load with a zero
     * rect and record a zero-sized GIF. Rebuild it: 'make panel'. */
    if (fread(hdr, 4, 12, f) != 12 || hdr[0] != 0x4E50544F || hdr[1] != 2) {
        emu_die("panel.bin is not a version 2 panel (run 'make panel')");
    }
    g_skin.scale = hdr[2];
    g_skin.vbw = hdr[3];
    g_skin.vbh = hdr[4];
    g_skin.bw = hdr[6];
    g_skin.bh = hdr[7];
    g_skin.px = hdr[8];
    g_skin.py = hdr[9];
    g_skin.pw = hdr[10];
    g_skin.ph = hdr[11];
    g_skin.base = malloc((size_t)g_skin.bw * g_skin.bh * 4);
    if (!g_skin.base ||
        fread(g_skin.base, 4, (size_t)g_skin.bw * g_skin.bh, f) !=
            (size_t)g_skin.bw * g_skin.bh) {
        emu_die("short panel.bin");
    }
    if (fread(&g_skin.nspr, 4, 1, f) != 1) {
        emu_die("short panel.bin");
    }
    g_skin.spr = calloc(g_skin.nspr, sizeof *g_skin.spr);
    for (uint32_t i = 0; i < g_skin.nspr; i++) {
        Sprite *s = &g_skin.spr[i];
        uint8_t meta[20];

        if (fread(s->name, 1, 28, f) != 28 || fread(meta, 1, 20, f) != 20) {
            emu_die("short panel.bin");
        }
        memcpy(&s->kind, meta, 1);
        memcpy(&s->x, meta + 4, 4);
        memcpy(&s->y, meta + 8, 4);
        memcpy(&s->w, meta + 12, 4);
        memcpy(&s->h, meta + 16, 4);
        s->rgba = malloc((size_t)s->w * s->h * 4);
        if (!s->rgba ||
            fread(s->rgba, 4, (size_t)s->w * s->h, f) != (size_t)s->w * s->h) {
            emu_die("short panel.bin");
        }
    }
    if (fread(&g_skin.nhit, 4, 1, f) != 1) {
        emu_die("short panel.bin");
    }
    g_skin.hit = calloc(g_skin.nhit, sizeof *g_skin.hit);
    for (uint32_t i = 0; i < g_skin.nhit; i++) {
        PanelHit *h = &g_skin.hit[i];

        if (fread(h->name, 1, 28, f) != 28 || fread(&h->kind, 4, 1, f) != 1 ||
            fread(h->v, 4, 6, f) != 6) {
            emu_die("short panel.bin");
        }
        h->action = hit_action(h);
        if (h->kind == 2) g_skin.fader = h;
        if (h->kind == 3) g_skin.screen = h;
    }
    fclose(f);
    g_skin.handle = sprite("fader-handle");
    g_skin.pointer = sprite("phones-pointer");
    if (!g_skin.handle || !g_skin.fader || !g_skin.screen) {
        emu_die("panel.bin lacks fader/screen");
    }
    for (size_t i = 0; i < sizeof g_lampmap / sizeof *g_lampmap; i++) {
        char n[36];
        PanelHit *h;

        snprintf(n, sizeof n, "%s|u", g_lampmap[i].svg);
        g_lampst[i].u = sprite(n);
        snprintf(n, sizeof n, "%s|i", g_lampmap[i].svg);
        g_lampst[i].i = sprite(n);
        g_lampst[i].key = -1;
        h = hit_by_name(g_lampmap[i].svg);
        if (h && h->kind == 0) {
            g_lampst[i].key = h->action;
        }
    }
    for (uint32_t i = 0; i < g_skin.nhit; i++) {
        PanelHit *h = &g_skin.hit[i];

        if (h->kind == 0 && h->action >= 0 && h->action < 64) {
            char n[36];

            snprintf(n, sizeof n, "%s|p", h->name);
            g_pressed[h->action] = sprite(n);
        }
    }
    g_skin.ok = true;
    return true;
}

/* ---- view transform ------------------------------------------------------- */
void skin_view_update(void)
{
    int dw, dh, ww, wh;
    float sx, sy;

    SDL_GetRendererOutputSize(g_ren, &dw, &dh);
    SDL_GetWindowSize(g_win, &ww, &wh);
    g_view.dpr = ww ? (float)dw / ww : 1;
    g_view.mpx = g_view.mouse_px ? 1 : g_view.dpr;
    sx = (float)dw / g_skin.vbw;
    sy = (float)dh / g_skin.vbh;
    g_view.scale = sx < sy ? sx : sy;
    g_view.offx = (dw - g_skin.vbw * g_view.scale) / 2;
    g_view.offy = (dh - g_skin.vbh * g_view.scale) / 2;
}

static SDL_FRect view_rect(float x, float y, float w, float h)
{
    return (SDL_FRect){ g_view.offx + x * g_view.scale,
                        g_view.offy + y * g_view.scale,
                        w * g_view.scale, h * g_view.scale };
}

void skin_view_mouse(int wx, int wy, float *sx, float *sy)
{
    *sx = (wx * g_view.mpx - g_view.offx) / g_view.scale;
    *sy = (wy * g_view.mpx - g_view.offy) / g_view.scale;
}

void skin_view_window(float sx, float sy, int *wx, int *wy)
{
    *wx = (int)((g_view.offx + sx * g_view.scale) / g_view.mpx);
    *wy = (int)((g_view.offy + sy * g_view.scale) / g_view.mpx);
}

/* Learn the unit of a REAL mouse event (ex, ey) by setting it against the OS
 * cursor, which SDL always reports in points. Only a retina display can tell
 * the two apart, and only far enough from the window's corner that the 2x is
 * unambiguous. Never feed it a scripted event: those carry no cursor. */
void skin_view_calibrate(int ex, int ey)
{
    int gx, gy, px, py;

    if (g_view.dpr < 1.5f) {
        return;
    }
    SDL_GetGlobalMouseState(&gx, &gy);
    SDL_GetWindowPosition(g_win, &px, &py);
    gx -= px;
    gy -= py;
    if (gx + gy < 40) {
        return;
    }
    g_view.mouse_px = (ex + ey) > (gx + gy) * (1 + g_view.dpr) / 2;
    g_view.mpx = g_view.mouse_px ? 1 : g_view.dpr;
}

void skin_window(SDL_Window *win, SDL_Renderer *ren)
{
    g_win = win;
    g_ren = ren;
    g_skin.base_tex = SDL_CreateTexture(ren, SDL_PIXELFORMAT_RGBA32,
                                        SDL_TEXTUREACCESS_STATIC,
                                        g_skin.bw, g_skin.bh);
    SDL_UpdateTexture(g_skin.base_tex, NULL, g_skin.base, g_skin.bw * 4);
    free(g_skin.base);
    g_skin.base = NULL;
    for (uint32_t i = 0; i < g_skin.nspr; i++) {
        Sprite *s = &g_skin.spr[i];

        s->tex = SDL_CreateTexture(ren, SDL_PIXELFORMAT_RGBA32,
                                   SDL_TEXTUREACCESS_STATIC, s->w, s->h);
        SDL_UpdateTexture(s->tex, NULL, s->rgba, s->w * 4);
        SDL_SetTextureBlendMode(s->tex, SDL_BLENDMODE_BLEND);
        free(s->rgba);
        s->rgba = NULL;
    }
    g_skin.screen_tex = SDL_CreateTexture(ren, SDL_PIXELFORMAT_RGBA32,
                                          SDL_TEXTUREACCESS_STREAMING, W, H);
    SDL_SetTextureScaleMode(g_skin.screen_tex, SDL_ScaleModeNearest);
    skin_view_update();
}

void skin_resize(int width)
{
    if (g_win) {
        SDL_SetWindowSize(g_win, width,
                          (int)(width * (long)g_skin.vbh / g_skin.vbw));
    }
}

/* ---- render --------------------------------------------------------------- */
static SDL_FRect sprite_dst(const Sprite *s, float dx, float dy)
{
    const float sc = g_skin.scale;

    return view_rect(s->x / sc + dx, s->y / sc + dy, s->w / sc, s->h / sc);
}

/* the inset the pressed art is drawn with; lit ink follows it */
#define PRESS_DX 1.2f
#define PRESS_DY 1.8f


/* A press on a key holds it until release; a drag on a knob turns it (8 px per
 * detent, up = clockwise, ctrl holds the encoder switch for a push-turn); a
 * plain click on a knob taps its switch; the fader tracks the pointer. */
#define DETENT_PX 8.0f
static struct {
    int btn, enc;
    bool turned, push, fader;
    float acc, last_y;
    int tap_id;                        /* deferred switch release */
    int64_t tap_up_ms;
    int64_t click_up_ms;               /* deferred scripted-click release */
    int click_x, click_y;
} g_ui = { .btn = -1, .enc = -1, .tap_id = -1 };

/* ---- turn and press hints -------------------------------------------------
 * Chrome the hardware does not have: a yellow outline round a key while it is
 * held, and a pair of arrows round a knob while it is being turned. It is for
 * the watcher, not the machine — nothing here is sent to the guest, and
 * --no-hints turns it all off.
 *
 * ☠ It is drawn INTO the panel, so it is in --recording too. That is the
 * point (a film of a walk should show what is being touched), but it means a
 * recorded panel is no longer a faithful picture of the hardware.
 *
 * The arrows are two numbers per knob: `base`, where the pair of dots sits,
 * and `wind`, the accumulated turn in degrees since the last change of
 * direction.
 *
 *     sweep = min(|wind|, MAX)     both arcs, growing from their dots
 *     phase = |wind| - MAX         once saturated, the pair rotates rigidly
 *     dir   = sign(wind)
 *     dot s = base + s*180 + dir*phase        (s is 0 or 1)
 *
 * Turning the other way does not unwind: the arrows collapse back to dots
 * AT THE POSITION THEIR HEADS HAD REACHED, and start winding again from
 * there. That falls out of the arithmetic — a head sits at
 * base + s*180 + dir*(phase + sweep) and |wind| is exactly phase + sweep, so
 * re-basing on the heads is base += wind, and then wind starts over.
 *
 * Up is clockwise on a mouse drag, so a positive detent sweeps the
 * right-hand dot DOWN and a negative one sweeps it up over the top.
 */
#define HINT_R_PAD     8.0f        /* ring this far outside the knob edge */
#define HINT_ARC_W     3.0f
#define HINT_BOX_W     2.5f
#define HINT_BOX_PAD   2.0f        /* outline this far outside the key edge */
#define HINT_BOX_R     7.0f        /* the key art's own corner radius */
#define HINT_DOT_R     3.0f
#define HINT_HEAD_L    9.0f
#define HINT_HEAD_W    7.5f
/*
 * ☠ Not 180 less a gap: the arrowhead sticks out PAST the end of its arc by
 * its own length, which at this radius is about 17 degrees, so a 168 degree
 * sweep put the head on top of the opposite dot. This is the sweep that
 * leaves a gap once the head is counted.
 */
#define HINT_MAX_SWEEP 150.0f
#define HINT_DEG_TICK  14.0f       /* per detent: 12 of them to saturate */
#define HINT_HOLD_MS   500         /* how long a still knob keeps its arrows */
#define HINT_FADE_MS   200
#define HINT_LATCH_MS  250         /* ☠ a scripted tap is 150 ms: 4 frames at
                                    * 33 fps. Latch it or it is a flicker. */
/*
 * ☠ The fade is what a hinted film costs: assets/demo.gif went from 1.2 MB to
 * 2.3 MB when the outlines stopped cutting out and started fading, because
 * every frame of every fade is a frame the palette has not seen. Quantising
 * the fade into six steps was tried, on the theory that identical frames
 * would collapse — it saved nothing at all. The cost is the number of frames
 * carrying a changed rectangle, not the number of distinct alphas in them.
 */
#define HINT_OUT_FADE  450         /* and then let go of it slowly */

static uint8_t fade_alpha(int64_t left)
{
    return left >= 0 ? 255
                     : (uint8_t)(255 * (HINT_OUT_FADE + left) / HINT_OUT_FADE);
}
#define HINT_MAXPT     72

static bool g_hints = true;

static struct {
    float wind;                    /* degrees, signed, since the last turn */
    float base;                    /* where this pair of dots sits */
    int32_t seen;                  /* enc_ticks already folded in */
    bool primed;
    int64_t last_ms;
} g_knob[8];
static int64_t g_key_until[64];
static struct { int64_t until_ms; int last; bool primed; } g_fader_hint;

void skin_hints(bool on)
{
    g_hints = on;
}

/* A detent the panel wire never sees (the headphones pot is ours alone). */
static void hint_tick(int knob, int delta)
{
    const float d = delta * HINT_DEG_TICK;

    if (knob < 0 || knob >= 8) {
        return;
    }
    if (g_knob[knob].wind != 0 && (g_knob[knob].wind < 0) != (d < 0)) {
        /* Turned back: the heads become the new dots, and winding restarts. */
        g_knob[knob].base = fmodf(g_knob[knob].base + g_knob[knob].wind, 360);
        g_knob[knob].wind = 0;
    }
    g_knob[knob].wind += d;
    g_knob[knob].last_ms = now_ms();
}

static SDL_FPoint vpt(float x, float y)
{
    return (SDL_FPoint){ g_view.offx + x * g_view.scale,
                         g_view.offy + y * g_view.scale };
}

/*
 * Stroke a polyline of viewBox points as a triangle strip. Each point gets a
 * pair of vertices offset along the average normal of the segments meeting
 * there, which is all the joint handling a 3 px line at these angles needs.
 */
static void stroke(const SDL_FPoint *pt, int n, float w, SDL_Color c)
{
    SDL_Vertex v[HINT_MAXPT * 2];
    int idx[(HINT_MAXPT - 1) * 6];
    const float hw = w * g_view.scale / 2;
    int ni = 0;

    if (n < 2 || n > HINT_MAXPT) {
        return;
    }
    for (int i = 0; i < n; i++) {
        const SDL_FPoint a = vpt(pt[i > 0 ? i - 1 : 0].x, pt[i > 0 ? i - 1 : 0].y);
        const SDL_FPoint b = vpt(pt[i < n - 1 ? i + 1 : i].x,
                                 pt[i < n - 1 ? i + 1 : i].y);
        const SDL_FPoint here = vpt(pt[i].x, pt[i].y);
        float dx = b.x - a.x, dy = b.y - a.y;
        float len = sqrtf(dx * dx + dy * dy);

        if (len < 1e-6f) {
            dx = 1;
            dy = 0;
            len = 1;
        }
        dx /= len;
        dy /= len;
        v[i * 2] = (SDL_Vertex){ { here.x - dy * hw, here.y + dx * hw }, c, { 0, 0 } };
        v[i * 2 + 1] = (SDL_Vertex){ { here.x + dy * hw, here.y - dx * hw }, c, { 0, 0 } };
    }
    for (int i = 0; i < n - 1; i++) {
        idx[ni++] = i * 2;
        idx[ni++] = i * 2 + 1;
        idx[ni++] = i * 2 + 2;
        idx[ni++] = i * 2 + 1;
        idx[ni++] = i * 2 + 3;
        idx[ni++] = i * 2 + 2;
    }
    SDL_RenderGeometry(g_ren, NULL, v, n * 2, idx, ni);
}

static void fill_tri(SDL_FPoint a, SDL_FPoint b, SDL_FPoint cpt, SDL_Color c)
{
    const SDL_Vertex v[3] = {
        { vpt(a.x, a.y), c, { 0, 0 } },
        { vpt(b.x, b.y), c, { 0, 0 } },
        { vpt(cpt.x, cpt.y), c, { 0, 0 } },
    };

    SDL_RenderGeometry(g_ren, NULL, v, 3, NULL, 0);
}

static void fill_disc(float cx, float cy, float r, SDL_Color c)
{
    SDL_FPoint prev = { cx + r, cy };

    for (int i = 1; i <= 12; i++) {
        const float a = (float)i * 2 * (float)M_PI / 12;
        const SDL_FPoint p = { cx + r * cosf(a), cy + r * sinf(a) };

        fill_tri((SDL_FPoint){ cx, cy }, prev, p, c);
        prev = p;
    }
}

/* One arc from a0 through a1 (degrees, screen space: y down), head at a1. */
static void arc_arrow(float cx, float cy, float r, float a0, float a1,
                      SDL_Color c)
{
    SDL_FPoint pt[HINT_MAXPT];
    const float span = a1 - a0;
    int n = (int)(fabsf(span) / 6) + 2;
    float head, tx, ty;

    if (n > HINT_MAXPT) {
        n = HINT_MAXPT;
    }
    for (int i = 0; i < n; i++) {
        const float a = (a0 + span * i / (n - 1)) * (float)M_PI / 180;

        pt[i] = (SDL_FPoint){ cx + r * cosf(a), cy + r * sinf(a) };
    }
    if (fabsf(span) < 1.0f) {          /* not turning yet: just the dot */
        fill_disc(pt[0].x, pt[0].y, HINT_DOT_R, c);
        return;
    }
    fill_disc(pt[0].x, pt[0].y, HINT_DOT_R * 0.8f, c);
    stroke(pt, n, HINT_ARC_W, c);
    /* The head, on the tangent at a1, pointing the way the arc grew. */
    head = (a1 + (span > 0 ? -90 : 90)) * (float)M_PI / 180;
    tx = cosf(head);
    ty = sinf(head);
    fill_tri((SDL_FPoint){ pt[n - 1].x - tx * HINT_HEAD_L,
                           pt[n - 1].y - ty * HINT_HEAD_L },
             (SDL_FPoint){ pt[n - 1].x + ty * HINT_HEAD_W / 2,
                           pt[n - 1].y - tx * HINT_HEAD_W / 2 },
             (SDL_FPoint){ pt[n - 1].x - ty * HINT_HEAD_W / 2,
                           pt[n - 1].y + tx * HINT_HEAD_W / 2 }, c);
}

static void outline_box(float x, float y, float w, float h, float rad,
                        SDL_Color c)
{
    SDL_FPoint pt[HINT_MAXPT];
    int n = 0;

    x -= HINT_BOX_PAD;
    y -= HINT_BOX_PAD;
    w += 2 * HINT_BOX_PAD;
    h += 2 * HINT_BOX_PAD;
    rad += HINT_BOX_PAD;
    if (rad > w / 2) rad = w / 2;
    if (rad > h / 2) rad = h / 2;
    for (int corner = 0; corner < 4; corner++) {
        const float ccx = corner == 0 || corner == 3 ? x + rad : x + w - rad;
        const float ccy = corner < 2 ? y + rad : y + h - rad;
        const float a0 = 180 + corner * 90;

        for (int i = 0; i <= 4; i++) {
            const float a = (a0 + i * 22.5f) * (float)M_PI / 180;

            pt[n++] = (SDL_FPoint){ ccx + rad * cosf(a), ccy + rad * sinf(a) };
        }
    }
    pt[n++] = pt[0];
    stroke(pt, n, HINT_BOX_W, c);
}

static void hints_draw(const PanelState *p)
{
    const int64_t now = now_ms();
    SDL_Color c = { 0xff, 0xd2, 0x3e, 0xff };

    if (!g_hints) {
        return;
    }
    SDL_SetRenderDrawBlendMode(g_ren, SDL_BLENDMODE_BLEND);
    for (uint32_t i = 0; i < g_skin.nhit; i++) {
        const PanelHit *h = &g_skin.hit[i];
        const float *v = h->v;

        if (h->kind == 0 && h->action >= 0 && h->action < 64) {
            const int64_t left = g_key_until[h->action] - now;

            if (p->keys[h->action >> 3] >> (h->action & 7) & 1) {
                g_key_until[h->action] = now + HINT_LATCH_MS;
            }
            if (left > -HINT_OUT_FADE) {
                c.a = fade_alpha(left);
                outline_box(v[0], v[1], v[2], v[3], HINT_BOX_R, c);
                c.a = 255;
            }
        } else if (h->kind == 1 && h->action >= 0 && h->action < 8) {
            const int k = h->action;
            const float r = v[2] + HINT_R_PAD;
            float sweep, phase, dir, age;

            if (k < 7) {               /* the wire's own detents */
                if (!g_knob[k].primed) {
                    g_knob[k].primed = true;
                    g_knob[k].seen = p->enc_ticks[k];
                }
                if (p->enc_ticks[k] != g_knob[k].seen) {
                    hint_tick(k, p->enc_ticks[k] - g_knob[k].seen);
                    g_knob[k].seen = p->enc_ticks[k];
                }
            }
            age = (float)(now - g_knob[k].last_ms);
            if (g_ui.enc == k) {
                age = 0;               /* held under the mouse: stay up */
            }
            if (age > HINT_HOLD_MS + HINT_FADE_MS) {
                g_knob[k].wind = 0;
                g_knob[k].base = 0;    /* next touch starts at 3 and 9 again */
                continue;
            }
            if (g_knob[k].wind == 0 && g_ui.enc != k) {
                continue;              /* never touched: draw nothing */
            }
            c.a = age > HINT_HOLD_MS
                      ? (uint8_t)(255 * (1 - (age - HINT_HOLD_MS) / HINT_FADE_MS))
                      : 255;
            dir = g_knob[k].wind < 0 ? -1.0f : 1.0f;
            sweep = fabsf(g_knob[k].wind);
            phase = sweep > HINT_MAX_SWEEP ? sweep - HINT_MAX_SWEEP : 0;
            if (sweep > HINT_MAX_SWEEP) {
                sweep = HINT_MAX_SWEEP;
            }
            for (int side = 0; side < 2; side++) {
                const float a0 = g_knob[k].base + side * 180 + dir * phase;

                arc_arrow(v[0], v[1], r, a0, a0 + dir * sweep, c);
            }
            c.a = 255;
        } else if (h->kind == 2 && g_skin.handle) {
            /* The handle, not the slot: it is the piece that moves, and an
             * outlined track just boxes in scenery. */
            const Sprite *s = g_skin.handle;
            const float sc = g_skin.scale;
            const float off = v[4] * (255 - p->xfader) / 255.0f;
            const int64_t left = g_fader_hint.until_ms - now;

            if (!g_fader_hint.primed) {
                g_fader_hint.primed = true;
                g_fader_hint.last = p->xfader;
            }
            if (p->xfader != g_fader_hint.last || g_ui.fader) {
                g_fader_hint.last = p->xfader;
                g_fader_hint.until_ms = now + HINT_LATCH_MS;
            }
            if (left > -HINT_OUT_FADE) {
                c.a = fade_alpha(left);
                outline_box(s->x / sc + off, s->y / sc, s->w / sc, s->h / sc,
                            HINT_BOX_R, c);
                c.a = 255;
            }
        }
    }
}

void skin_render(void)
{
    enum { NLAMP = sizeof g_lampmap / sizeof *g_lampmap };
    SDL_FRect full = view_rect(0, 0, g_skin.vbw, g_skin.vbh);
    int tint[NLAMP][3];
    bool pressed[NLAMP];
    PanelState p;
    void *px;
    int pitch;
    SDL_FRect sd, hd;
    float m;
    int n;

    panel_snapshot(&p);
    SDL_RenderCopyF(g_ren, g_skin.base_tex, NULL, &full);

    /* tint = sum over lit channels of palette colour x brightness, with
     * 0x44 x 15 as full scale (the firmware's maximum) */
    for (size_t k = 0; k < NLAMP; k++) {
        int acc[3] = {0, 0, 0};

        for (int bit = 0; bit < 2; bit++) {
            const int id = 2 * g_lampmap[k].lamp + bit;
            const int br = p.bright[id];
            const uint8_t *c = p.rgb[4 * g_lampmap[k].lamp + 1 + bit];

            if (!(g_lampmap[k].chan >> bit & 1) || !br ||
                !(p.lit[id >> 3] >> (id & 7) & 1)) {
                continue;
            }
            for (int ch = 0; ch < 3; ch++) {
                acc[ch] += c[ch] * br;
            }
        }
        for (int ch = 0; ch < 3; ch++) {
            const int v = acc[ch] * 255 / (0x44 * 15);

            tint[k][ch] = v > 255 ? 255 : v;
        }
        pressed[k] = g_lampst[k].key >= 0 &&
                     (p.keys[g_lampst[k].key >> 3] >>
                      (g_lampst[k].key & 7) & 1);
        if ((tint[k][0] | tint[k][1] | tint[k][2]) && g_lampst[k].u &&
            !pressed[k]) {
            SDL_FRect d = sprite_dst(g_lampst[k].u, 0, 0);

            SDL_RenderCopyF(g_ren, g_lampst[k].u->tex, NULL, &d);
        }
    }
    for (int id = 0; id < 64; id++) {          /* held keys draw inset */
        if ((p.keys[id >> 3] >> (id & 7) & 1) && g_pressed[id]) {
            SDL_FRect d = sprite_dst(g_pressed[id], 0, 0);

            SDL_RenderCopyF(g_ren, g_pressed[id]->tex, NULL, &d);
        }
    }
    for (size_t k = 0; k < NLAMP; k++) {
        SDL_FRect d;

        if (!(tint[k][0] | tint[k][1] | tint[k][2]) || !g_lampst[k].i) {
            continue;
        }
        SDL_SetTextureColorMod(g_lampst[k].i->tex, tint[k][0], tint[k][1],
                               tint[k][2]);
        d = sprite_dst(g_lampst[k].i, pressed[k] ? PRESS_DX : 0,
                       pressed[k] ? PRESS_DY : 0);
        SDL_RenderCopyF(g_ren, g_lampst[k].i->tex, NULL, &d);
    }

    /* The screen changes far more slowly than the window redraws (vsync), and
     * the texture keeps its contents between frames — so re-expand the 8192
     * pixels only when the panel link has actually written to the display.
     * fb_gen is bumped per display block by panel.c. */
    if (!g_screen_valid || p.fb_gen != g_screen_gen) {
        g_screen_valid = true;
        g_screen_gen = p.fb_gen;
        SDL_LockTexture(g_skin.screen_tex, NULL, &px, &pitch);
        for (int y = 0; y < H; y++) {
            uint8_t *row = (uint8_t *)px + y * pitch;

            for (int x = 0; x < W; x++) {
                const bool on = p.fb[y][x];

                row[x * 4 + 0] = on ? 0xD8 : 0x18;
                row[x * 4 + 1] = on ? 0xF0 : 0x1C;
                row[x * 4 + 2] = on ? 0xFF : 0x1E;
                row[x * 4 + 3] = 0xFF;
            }
        }
        SDL_UnlockTexture(g_skin.screen_tex);
    }
    /* Snap the blit to an integer pixel multiple when one lies near the bezel
     * rect (the sliver of margin hides in the dark cutout); the default window
     * size makes it exact. */
    sd = view_rect(g_skin.screen->v[0], g_skin.screen->v[1],
                   g_skin.screen->v[2], g_skin.screen->v[3]);
    m = sd.w / W;
    n = (int)(m + 0.5f);
    SDL_SetRenderDrawColor(g_ren, 0x18, 0x1C, 0x1E, 255);
    SDL_RenderFillRectF(g_ren, &sd);
    if (n >= 1 && fabsf(m - n) <= 0.15f) {
        sd.x = floorf(sd.x + (sd.w - W * n) / 2);
        sd.y = floorf(sd.y + (sd.h - H * n) / 2);
        sd.w = W * n;
        sd.h = H * n;
    }
    SDL_RenderCopyF(g_ren, g_skin.screen_tex, NULL, &sd);

    if (g_skin.pointer) {              /* min at bottom-left, 270 deg cw sweep */
        SDL_FRect pd = sprite_dst(g_skin.pointer, 0, 0);

        SDL_RenderCopyExF(g_ren, g_skin.pointer->tex, NULL, &pd,
                          270.0 * audio_phones(), NULL, SDL_FLIP_NONE);
    }
    /* wire 255 = far LEFT (measured against the screen's 01..09 widget), so
     * the handle draws mirrored */
    hd = sprite_dst(g_skin.handle,
                    g_skin.fader->v[4] * (255 - p.xfader) / 255.0f, 0);
    SDL_RenderCopyF(g_ren, g_skin.handle->tex, NULL, &hd);
    hints_draw(&p);
}

/*
 * The rendered panel, as RGB24 at an arbitrary smaller size — what `--video
 * panel` records. The readback is the renderer's own output (retina: twice the
 * logical size, ~13 MB a frame), so it is box-downscaled on the way out rather
 * than pushed at full size: 30 fps of 2500x1364 is ~400 MB/s into the encoder,
 * and nothing about the recording is better for it.
 *
 * Called from the render thread only, and BEFORE the present, not after:
 * SDL_RenderReadPixels reads the CURRENT target, which after a present is the
 * next, undrawn back buffer.
 */
static bool grab_rect(uint8_t *dst, int dw, int dh, const SDL_Rect *src)
{
    static uint8_t *scratch;
    static int cap_w, cap_h;
    int w, h;

    if (!g_ren || dw <= 0 || dh <= 0) {
        return false;
    }
    SDL_GetRendererOutputSize(g_ren, &w, &h);
    if (w <= 0 || h <= 0) {
        return false;
    }
    if (src) {
        w = src->w;
        h = src->h;
    }
    /* Nothing to average: read straight into the caller's frame. */
    if (dw == w && dh == h) {
        return SDL_RenderReadPixels(g_ren, src, SDL_PIXELFORMAT_RGB24,
                                    dst, w * 3) == 0;
    }
    if (w != cap_w || h != cap_h) {
        free(scratch);
        scratch = malloc((size_t)w * h * 3);
        cap_w = w;
        cap_h = h;
    }
    if (!scratch || SDL_RenderReadPixels(g_ren, src, SDL_PIXELFORMAT_RGB24,
                                         scratch, w * 3)) {
        return false;
    }
    /* Box filter: every destination pixel averages the source rectangle it
     * covers, so a downscale of a panel full of thin printed labels stays
     * readable where nearest-neighbour would drop half of them. */
    for (int y = 0; y < dh; y++) {
        const int sy0 = y * h / dh, sy1 = (y + 1) * h / dh > sy0 ? (y + 1) * h / dh : sy0 + 1;

        for (int x = 0; x < dw; x++) {
            const int sx0 = x * w / dw, sx1 = (x + 1) * w / dw > sx0 ? (x + 1) * w / dw : sx0 + 1;
            unsigned r = 0, g = 0, b = 0, n = 0;

            for (int sy = sy0; sy < sy1; sy++) {
                const uint8_t *row = scratch + (size_t)sy * w * 3;

                for (int sx = sx0; sx < sx1; sx++) {
                    r += row[sx * 3];
                    g += row[sx * 3 + 1];
                    b += row[sx * 3 + 2];
                    n++;
                }
            }
            dst[(y * dw + x) * 3]     = (uint8_t)(r / n);
            dst[(y * dw + x) * 3 + 1] = (uint8_t)(g / n);
            dst[(y * dw + x) * 3 + 2] = (uint8_t)(b / n);
        }
    }
    return true;
}

bool skin_grab(uint8_t *dst, int dw, int dh)
{
    return grab_rect(dst, dw, dh, NULL);
}

/*
 * The face plate at the renderer's own resolution, with the chassis surround
 * left behind — what a GIF wants, since the surround is margin and a README
 * has none to spare. Nothing is resampled here on purpose: one Lanczos in
 * ffmpeg beats this box filter followed by one. MEASURED against a single
 * ideal resample of the pristine 2500 px raster: box to 1250 then Lanczos to
 * 880 is 0.82% RMSE (0.49% of it the two-stage resample, the rest the h264
 * 4:2:0 intermediate); handing ffmpeg these native pixels is 0.08%.
 */
bool skin_plate_size(int *w, int *h)
{
    int ow, oh;

    if (!g_skin.ok || !skin_output_size(&ow, &oh) || !g_skin.vbw) {
        return false;
    }
    *w = (int)((int64_t)g_skin.pw * ow / g_skin.vbw);
    *h = (int)((int64_t)g_skin.ph * oh / g_skin.vbh);
    return *w > 0 && *h > 0;
}

/* The plate as the artwork draws it, 1:1, whatever this display's scale is. */
bool skin_plate_native(int *w, int *h)
{
    if (!g_skin.ok || !g_skin.pw) {
        return false;
    }
    *w = (int)g_skin.pw;
    *h = (int)g_skin.ph;
    return true;
}

/*
 * ☠ The caller's buffer was sized once, when the recording opened, and this
 * window is resizable: a drag changes the renderer's output and with it the
 * plate's size. Refuse rather than write the new size into the old buffer.
 * The shutter simply repeats the last frame, which it already counts.
 */
bool skin_grab_plate(uint8_t *dst, int w, int h)
{
    SDL_Rect r;
    int ow, oh, pw, ph;

    if (!g_ren || !skin_output_size(&ow, &oh) || !skin_plate_size(&pw, &ph)) {
        return false;
    }
    /* ☠ The caller's buffer is sized once and this window is resizable, so a
     * drag that changes the plate's readback size must not be written into
     * it. The reduction below is allowed only where it divides exactly. */
    if (w > pw || h > ph || pw % w || ph % h || pw / w != ph / h) {
        return false;
    }
    r.x = (int)((int64_t)g_skin.px * ow / g_skin.vbw);
    r.y = (int)((int64_t)g_skin.py * oh / g_skin.vbh);
    r.w = pw;
    r.h = ph;
    return grab_rect(dst, w, h, &r);
}

/* The renderer's output size, so a recording can be sized before it starts. */
bool skin_output_size(int *w, int *h)
{
    if (!g_ren) {
        return false;
    }
    SDL_GetRendererOutputSize(g_ren, w, h);
    return *w > 0 && *h > 0;
}

void skin_shot(const char *path)
{
    int w, h;
    uint8_t *buf;
    FILE *f;

    SDL_GetRendererOutputSize(g_ren, &w, &h);
    buf = malloc((size_t)w * h * 3);
    if (!buf || SDL_RenderReadPixels(g_ren, NULL, SDL_PIXELFORMAT_RGB24,
                                     buf, w * 3)) {
        free(buf);
        return;
    }
    f = fopen(path, "wb");
    if (f) {
        fprintf(f, "P6\n%d %d\n255\n", w, h);
        fwrite(buf, 3, (size_t)w * h, f);
        fclose(f);
    }
    free(buf);
}

/* ---- hit test and gestures ------------------------------------------------ */
static PanelHit *skin_hit(float x, float y)
{
    for (uint32_t i = 0; i < g_skin.nhit; i++) {
        PanelHit *h = &g_skin.hit[i];
        const float *v = h->v;

        if (h->kind == 0 && h->action >= 0 &&
            x >= v[0] && x <= v[0] + v[2] && y >= v[1] && y <= v[1] + v[3]) {
            return h;
        }
        if (h->kind == 1 && h->action >= 0) {
            const float dx = x - v[0], dy = y - v[1], r = v[2] + 5;

            if (dx * dx + dy * dy <= r * r) {
                return h;
            }
        }
        if (h->kind == 2 && x >= v[0] - 6 && x <= v[0] + v[2] + 6 &&
            y >= v[1] - 14 && y <= v[1] + v[3] + 14) {
            return h;
        }
    }
    return NULL;
}

bool skin_over_control(float x, float y) { return skin_hit(x, y) != NULL; }

int skin_knob_at(float x, float y)
{
    PanelHit *h = skin_hit(x, y);

    return h && h->kind == 1 ? h->action : -1;
}


static void fader_to(float x)
{
    const float c0 = (g_skin.handle->x + g_skin.handle->w / 2.0f) / g_skin.scale;

    panel_xfader(255 - (int)((x - c0) * 255.0f / g_skin.fader->v[4] + 0.5f));
}

void skin_mouse_down(float x, float y, bool ctrl)
{
    PanelHit *h = skin_hit(x, y);

    if (!h) {
        return;
    }
    if (h->kind == 0) {
        g_ui.btn = h->action;
        panel_key(h->action, true);
    } else if (h->kind == 1) {
        g_ui.enc = h->action;
        g_ui.turned = false;
        g_ui.push = ctrl && h->action < 7;
        g_ui.acc = 0;
        g_ui.last_y = y;
        if (g_ui.push) {
            panel_key(56 + h->action, true);
        }
    } else if (h->kind == 2) {
        g_ui.fader = true;
        fader_to(x);
    }
}

void skin_mouse_motion(float x, float y)
{
    if (g_ui.enc == 7) {               /* headphones pot: local volume only */
        audio_set_phones(audio_phones() + (g_ui.last_y - y) / 150.0f);
        /* It turns like the others on screen even though the wire never
         * hears about it, so feed the hint its detents by hand. */
        g_ui.acc += g_ui.last_y - y;
        while (g_ui.acc >= DETENT_PX) {
            hint_tick(7, 1);
            g_ui.acc -= DETENT_PX;
        }
        while (g_ui.acc <= -DETENT_PX) {
            hint_tick(7, -1);
            g_ui.acc += DETENT_PX;
        }
        g_ui.last_y = y;
    } else if (g_ui.enc >= 0) {
        g_ui.acc += g_ui.last_y - y;
        g_ui.last_y = y;
        while (g_ui.acc >= DETENT_PX) {
            panel_encoder(g_ui.enc, 1);
            g_ui.acc -= DETENT_PX;
            g_ui.turned = true;
        }
        while (g_ui.acc <= -DETENT_PX) {
            panel_encoder(g_ui.enc, -1);
            g_ui.acc += DETENT_PX;
            g_ui.turned = true;
        }
    } else if (g_ui.fader) {
        fader_to(x);
    }
}

void skin_mouse_up(void)
{
    if (g_ui.btn >= 0) {
        panel_key(g_ui.btn, false);
        g_ui.btn = -1;
    }
    if (g_ui.enc >= 0) {
        if (g_ui.push) {
            panel_key(56 + g_ui.enc, false);
        } else if (!g_ui.turned && g_ui.enc < 7) {
            panel_key(56 + g_ui.enc, true);
            g_ui.tap_id = 56 + g_ui.enc;
            g_ui.tap_up_ms = now_ms() + 90;
        }
        g_ui.enc = -1;
    }
    g_ui.fader = false;
}

void skin_tick(void)
{
    if (g_ui.tap_id >= 0 && now_ms() >= g_ui.tap_up_ms) {
        panel_key(g_ui.tap_id, false);
        g_ui.tap_id = -1;
    }
    if (g_ui.click_up_ms && now_ms() >= g_ui.click_up_ms) {
        SDL_Event e = {0};

        e.type = SDL_MOUSEBUTTONUP;
        e.button.button = SDL_BUTTON_LEFT;
        e.button.x = g_ui.click_x;
        e.button.y = g_ui.click_y;
        SDL_PushEvent(&e);
        g_ui.click_up_ms = 0;
    }
}

static bool control_centre(const char *name, float *cx, float *cy)
{
    PanelHit *h = hit_by_name(name);

    if (!h || h->action < 0) {
        return false;
    }
    *cx = h->kind == 1 ? h->v[0] : h->v[0] + h->v[2] / 2;
    *cy = h->kind == 1 ? h->v[1] : h->v[1] + h->v[3] / 2;
    return true;
}

bool skin_script_click(const char *name)
{
    SDL_Event e = {0};
    float cx, cy;

    if (!control_centre(name, &cx, &cy)) {
        return false;                      /* no such control: a script error */
    }
    if (g_ui.click_up_ms) {
        return true;                       /* previous click still down */
    }
    /* Real SDL events at inverse-mapped window coords, so this exercises the
     * view transform as well as the hit geometry. */
    skin_view_window(cx, cy, &g_ui.click_x, &g_ui.click_y);
    e.type = SDL_MOUSEBUTTONDOWN;
    e.button.button = SDL_BUTTON_LEFT;
    e.button.x = g_ui.click_x;
    e.button.y = g_ui.click_y;
    SDL_PushEvent(&e);
    g_ui.click_up_ms = now_ms() + 150;
    return true;
}

bool skin_script_drag(const char *name, long detents, bool push)
{
    float cx, cy, yy;

    if (!control_centre(name, &cx, &cy)) {
        return false;
    }
    skin_mouse_down(cx, cy, push);
    yy = cy;
    for (long k = 0; k < labs(detents); k++) {
        yy -= detents > 0 ? DETENT_PX : -DETENT_PX;
        skin_mouse_motion(cx, yy);
    }
    skin_mouse_up();
    return true;
}
