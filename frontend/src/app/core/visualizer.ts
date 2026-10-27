/**
 * Visualizador de la mezcla en directo, en Canvas 2D.
 *
 *  - `StageRenderer`: los dos platos (giran al compás de la reproducción real), la forma de onda de
 *    cada pista desplazándose con su rejilla de compases y frases, la zona de mezcla programada, el
 *    instante del cambio de graves y un indicador del cruce entre las dos pistas.
 *  - `BackdropRenderer`: el espectro del audio de salida como fondo de toda la pantalla.
 *
 * No depende de Angular ni del motor de audio: recibe un `StageFrame` por fotograma y dibuja.
 */
import type { WavePeaks } from './waveform';

export type { WavePeaks };

export type DeckRole = 'air' | 'in' | 'out' | 'next';

export interface StageDeck {
  /** Plato físico (0 = izquierda, 1 = derecha). Las pistas se alternan entre los dos. */
  slot: 0 | 1;
  role: DeckRole;
  title: string;
  /** Segundos del audio tal como suena (ya a su velocidad de reproducción). */
  position: number;
  duration: number;
  /** Nivel real de la señal de este plato, 0..1. */
  level: number;
  /** Volumen del plato en la mezcla, 0..1. */
  gain: number;
  /** Ganancia del EQ de graves en dB (-40 = graves fuera). */
  lowDb: number;
  peaks: WavePeaks | null;
  /** Instante de cada compás, en segundos del audio tal como suena. */
  downbeats: number[];
  /** Compás (0..31) donde empieza un bloque de 32; módulo 8 = fase de las frases. */
  phraseOffset: number;
  introEnd: number | null;
  outroStart: number | null;
}

export interface StageMix {
  label: string;
  outSlot: 0 | 1;
  inSlot: 0 | 1;
  /** Tramo de la mezcla en el audio de la saliente y de la entrante (segundos tal como suenan). */
  outFrom: number;
  outTo: number;
  inFrom: number;
  inTo: number;
  /** Instante del cambio de graves en cada una; null si la estrategia no lo tiene. */
  swapOut: number | null;
  swapIn: number | null;
  /** El cambio de graves coincide con el cuerpo de la entrante. */
  aligned: boolean;
  /** 0..1 si la mezcla ya está en curso; null si solo está programada. */
  progress: number | null;
}

export interface StageFrame {
  paused: boolean;
  decks: [StageDeck | null, StageDeck | null];
  mix: StageMix | null;
  /** Reparto del volumen entre los platos: 0 = todo el plato 0, 1 = todo el plato 1; null si no suena nada. */
  balance: number | null;
}

type Rgb = [number, number, number];

export interface StageTheme {
  dark: boolean;
  deckA: Rgb;
  deckB: Rgb;
  ink: Rgb;
  muted: Rgb;
  vinyl: Rgb;
  mono: string;
  sans: string;
}


export const clamp01 = (x: number): number => (x < 0 ? 0 : x > 1 ? 1 : x);
const mod = (n: number, m: number): number => ((n % m) + m) % m;
const rgba = (c: Rgb, a: number): string => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const lerpRgb = (a: Rgb, b: Rgb, t: number): Rgb => [
  Math.round(a[0] + (b[0] - a[0]) * t),
  Math.round(a[1] + (b[1] - a[1]) * t),
  Math.round(a[2] + (b[2] - a[2]) * t),
];

/** Primer índice con `values[i] >= x` (búsqueda binaria; `values` está ordenado). */
export function lowerBound(values: number[], x: number): number {
  let lo = 0;
  let hi = values.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (values[m] < x) lo = m + 1;
    else hi = m;
  }
  return lo;
}

let probe: CanvasRenderingContext2D | null | undefined;

/** Convierte cualquier color CSS a [r, g, b] dejando que el propio navegador lo interprete. */
export function toRgb(css: string, fallback: Rgb = [128, 128, 128]): Rgb {
  if (probe === undefined) probe = document.createElement('canvas').getContext('2d');
  if (!probe) return fallback;
  probe.fillStyle = '#808080';
  probe.fillStyle = css;
  const s = String(probe.fillStyle);
  const hex = /^#([0-9a-f]{6})$/i.exec(s);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const m = /^rgba?\(([^)]+)\)/.exec(s);
  if (m) {
    const p = m[1].split(',').map((x) => parseFloat(x));
    return [p[0], p[1], p[2]];
  }
  return fallback;
}

export const themeKey = (): string => document.documentElement.dataset['theme'] ?? '';

/** Lee los colores del tema actual de las variables CSS (--deck-a, --deck-b, --ink…). */
export function readTheme(root: HTMLElement = document.documentElement): StageTheme {
  const cs = getComputedStyle(root);
  const v = (name: string, fallback: string): string => cs.getPropertyValue(name).trim() || fallback;
  return {
    dark: root.dataset['theme'] === 'dark',
    deckA: toRgb(v('--deck-a', '#ff7a50')),
    deckB: toRgb(v('--deck-b', '#35d6cf')),
    ink: toRgb(v('--ink', '#eeeeee')),
    muted: toRgb(v('--muted', '#999999')),
    vinyl: toRgb(v('--vinyl', '#14141a')),
    mono: v('--mono', 'ui-monospace, monospace'),
    sans: v('--sans', 'system-ui, sans-serif'),
  };
}

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function fitText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const m = Math.ceil((lo + hi) / 2);
    if (ctx.measureText(text.slice(0, m) + '…').width <= maxWidth) lo = m;
    else hi = m - 1;
  }
  return lo > 0 ? text.slice(0, lo) + '…' : '';
}

function setupCanvas(canvas: HTMLCanvasElement, ctx: CanvasRenderingContext2D): { w: number; h: number } {
  const r = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(1, Math.round(r.width));
  const h = Math.max(1, Math.round(r.height));
  const pw = Math.round(w * dpr);
  const ph = Math.round(h * dpr);
  if (canvas.width !== pw || canvas.height !== ph) {
    canvas.width = pw;
    canvas.height = ph;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w, h };
}

const ROLE_LABEL: Record<DeckRole, string> = { air: 'EN EL AIRE', in: 'ENTRA', out: 'SALE', next: 'SIGUIENTE' };


const WINDOW_SEC = 16; // segundos de onda visibles
const PLAYHEAD = 0.3; // posición del cabezal, como fracción del ancho del carril
const COL_PITCH = 3;
const COL_WIDTH = 2;
const STRIP_H = 22; // altura de la franja del cruce, entre los dos carriles
const SPIN = (2 * Math.PI) / 1.8; // un disco a 33⅓ rpm da una vuelta cada 1,8 s

export class StageRenderer {
  private ctx: CanvasRenderingContext2D;
  private w = 1;
  private h = 1;
  private theme: StageTheme;
  private key: string;

  constructor(private canvas: HTMLCanvasElement, private t: (text: string) => string = (s) => s) {
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D no disponible');
    this.ctx = ctx;
    this.theme = readTheme();
    this.key = themeKey();
    this.resize();
  }

  resize(): void {
    ({ w: this.w, h: this.h } = setupCanvas(this.canvas, this.ctx));
  }

  draw(frame: StageFrame | null): void {
    const k = themeKey();
    if (k !== this.key) {
      this.key = k;
      this.theme = readTheme();
    }
    const { ctx, w, h, theme } = this;
    ctx.setTransform(Math.min(window.devicePixelRatio || 1, 2), 0, 0, Math.min(window.devicePixelRatio || 1, 2), 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (w < 40 || h < 40) return;

    const decks = frame?.decks ?? [null, null];
    const pad = 14;
    const showPlatters = w >= 640;
    const R = showPlatters ? Math.min((h - 2 * pad) / 2, w * 0.11) : 0;
    let x0 = pad;
    let x1 = w - pad;
    if (showPlatters) {
      this.platter(pad + R, h / 2, R, decks[0], theme.deckA, 0);
      this.platter(w - pad - R, h / 2, R, decks[1], theme.deckB, 1);
      x0 = pad + 2 * R + 22;
      x1 = w - pad - 2 * R - 22;
    }
    const laneW = x1 - x0;
    const laneH = (h - 2 * pad - STRIP_H) / 2;
    const yA = pad;
    const yB = pad + laneH + STRIP_H;
    this.lane(decks[0], x0, yA, laneW, laneH, theme.deckA, frame);
    this.lane(decks[1], x0, yB, laneW, laneH, theme.deckB, frame);
    this.strip(x0, yA + laneH, laneW, frame);
    this.playhead(x0 + laneW * PLAYHEAD, yA, yB + laneH);
    if (!decks[0] && !decks[1]) {
      ctx.fillStyle = rgba(theme.muted, 0.9);
      ctx.font = `500 13px ${theme.sans}`;
      ctx.textAlign = 'center';
      ctx.fillText(this.t('Esperando la primera pista…'), x0 + laneW / 2, h / 2 + 4);
      ctx.textAlign = 'left';
    }
  }


  private lane(deck: StageDeck | null, x: number, y: number, w: number, h: number, col: Rgb, frame: StageFrame | null): void {
    const { ctx, theme } = this;
    ctx.fillStyle = rgba(theme.ink, theme.dark ? 0.05 : 0.045);
    roundedRect(ctx, x, y, w, h, 10);
    ctx.fill();
    if (!deck) {
      ctx.fillStyle = rgba(theme.muted, 0.7);
      ctx.font = `500 11px ${theme.mono}`;
      ctx.fillText(this.t('libre'), x + 12, y + h / 2 + 4);
      return;
    }
    const pxs = w / WINDOW_SEC;
    const ph = x + w * PLAYHEAD;
    const tL = deck.position - (ph - x) / pxs;
    const tR = tL + w / pxs;
    const atX = (t: number): number => x + (t - tL) * pxs;

    ctx.save();
    roundedRect(ctx, x, y, w, h, 10);
    ctx.clip();

    // Zona de la mezcla y cambio de graves
    const mix = frame?.mix ?? null;
    let swapX: number | null = null;
    if (mix) {
      const isOut = deck.slot === mix.outSlot;
      const isIn = deck.slot === mix.inSlot;
      if (isOut || isIn) {
        const a = atX(isOut ? mix.outFrom : mix.inFrom);
        const b = atX(isOut ? mix.outTo : mix.inTo);
        ctx.fillStyle = rgba(col, theme.dark ? 0.13 : 0.12);
        ctx.fillRect(a, y, b - a, h);
        ctx.fillStyle = rgba(col, 0.9);
        ctx.fillRect(a, y, b - a, 3);
        const swap = isOut ? mix.swapOut : mix.swapIn;
        if (swap != null) swapX = atX(swap);
        if (isOut && b - a > 60) {
          ctx.fillStyle = rgba(theme.ink, 0.75);
          ctx.font = `600 10px ${theme.sans}`;
          ctx.fillText(mix.label.toUpperCase(), Math.max(x + 8, a + 8), y + h - 8);
        }
      }
    }

    // Rejilla de compases y frases
    const db = deck.downbeats;
    for (let i = lowerBound(db, tL - 0.01); i < db.length && db[i] <= tR + 0.01; i++) {
      const gx = atX(db[i]);
      const rel = i - deck.phraseOffset;
      const block32 = mod(rel, 32) === 0;
      const phrase = mod(rel, 8) === 0;
      if (block32) {
        ctx.fillStyle = rgba(theme.ink, 0.42);
        ctx.fillRect(gx - 0.5, y, 1.5, h);
      } else if (phrase) {
        ctx.fillStyle = rgba(theme.ink, 0.22);
        ctx.fillRect(gx - 0.5, y, 1, h);
      } else {
        ctx.fillStyle = rgba(theme.ink, 0.12);
        ctx.fillRect(gx - 0.5, y, 1, 6);
        ctx.fillRect(gx - 0.5, y + h - 6, 1, 6);
      }
    }

    // Forma de onda: la banda completa y, encima, los graves
    if (deck.peaks) this.wave(deck, x, y, w, h, tL, pxs, ph, col);

    // Intro y outro
    this.flag(deck.introEnd, 'cuerpo', atX, y, h, col, x, w);
    this.flag(deck.outroStart, 'outro', atX, y, h, col, x, w);

    // Cambio de graves
    if (swapX != null && swapX >= x - 2 && swapX <= x + w + 2) {
      ctx.save();
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = rgba(theme.ink, 0.9);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(swapX, y + 3);
      ctx.lineTo(swapX, y + h);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = rgba(theme.ink, 0.95);
      ctx.font = `700 10px ${theme.sans}`;
      const label = this.t(w < 460 ? 'GRAVES' : mix?.aligned ? 'CAMBIO DE GRAVES · ENTRA SU CUERPO' : 'CAMBIO DE GRAVES');
      const tw = ctx.measureText(label).width;
      const lx = swapX + 6 + tw > x + w ? swapX - 6 - tw : swapX + 6;
      ctx.fillText(label, lx, y + 38);
    }
    ctx.restore();

    // Título y papel del plato
    ctx.font = `600 12px ${theme.sans}`;
    ctx.fillStyle = rgba(theme.ink, 0.92);
    const roleText = ROLE_LABEL[deck.role];
    ctx.font = `700 10px ${theme.sans}`;
    const rw = ctx.measureText(roleText).width + 14;
    ctx.font = `600 12px ${theme.sans}`;
    const title = fitText(ctx, `${deck.slot + 1} · ${deck.title}`, Math.max(40, w * 0.55 - rw));
    // Fondo sutil para que el título se lea sobre la onda
    const tw = ctx.measureText(title).width;
    ctx.fillStyle = rgba(theme.dark ? [10, 10, 16] : [255, 255, 255], 0.55);
    roundedRect(ctx, x + 6, y + 5, tw + 12, 18, 6);
    ctx.fill();
    ctx.fillStyle = rgba(theme.ink, 0.95);
    ctx.fillText(title, x + 12, y + 18);
    this.pill(x + w - rw - 6, y + 5, roleText, rw, col, deck.role === 'air' || deck.role === 'in');
  }

  private wave(deck: StageDeck, x: number, y: number, w: number, h: number, tL: number, pxs: number, ph: number, col: Rgb): void {
    const { ctx, theme } = this;
    const pk = deck.peaks as WavePeaks;
    const mid = y + h / 2;
    const half = h / 2 - 5;
    const bpc = Math.max(1, Math.round((pk.pps * COL_PITCH) / pxs));
    const alpha = (0.3 + 0.7 * clamp01(deck.gain)) * (deck.role === 'next' ? 0.75 : 1);
    const lowK = clamp01((deck.lowDb + 40) / 40);
    const bright = theme.dark ? lerpRgb(col, [255, 255, 255], 0.38) : lerpRgb(col, [0, 0, 0], 0.28);

    const past = new Path2D();
    const future = new Path2D();
    const pastLow = new Path2D();
    const futureLow = new Path2D();
    for (let cx = x; cx < x + w; cx += COL_PITCH) {
      const t = tL + (cx - x) / pxs;
      if (t < 0 || t > deck.duration) continue;
      const i0 = Math.floor(t * pk.pps);
      let vm = 0;
      let vl = 0;
      for (let i = i0; i < i0 + bpc && i < pk.max.length; i++) {
        if (pk.max[i] > vm) vm = pk.max[i];
        if (pk.low[i] > vl) vl = pk.low[i];
      }
      const hh = Math.max(1, Math.pow(vm / 255, 0.8) * half);
      const hl = Math.max(0, Math.pow(vl / 255, 0.8) * half);
      const isPast = cx < ph;
      (isPast ? past : future).rect(cx, mid - hh, COL_WIDTH, hh * 2);
      if (hl > 0.5) (isPast ? pastLow : futureLow).rect(cx, mid - hl, COL_WIDTH, hl * 2);
    }
    ctx.fillStyle = rgba(col, 0.42 * alpha);
    ctx.fill(past);
    ctx.fillStyle = rgba(col, 0.8 * alpha);
    ctx.fill(future);
    ctx.fillStyle = rgba(bright, 0.65 * alpha * lowK);
    ctx.fill(pastLow);
    ctx.fillStyle = rgba(bright, 1 * alpha * lowK);
    ctx.fill(futureLow);
  }

  private flag(t: number | null, text: string, atX: (t: number) => number, y: number, h: number, col: Rgb, x: number, w: number): void {
    if (t == null) return;
    const fx = atX(t);
    if (fx < x || fx > x + w) return;
    const { ctx, theme } = this;
    ctx.save();
    ctx.setLineDash([2, 3]);
    ctx.strokeStyle = rgba(col, 0.9);
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(fx, y + 20);
    ctx.lineTo(fx, y + h);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = rgba(theme.ink, 0.7);
    ctx.font = `600 9px ${theme.sans}`;
    ctx.fillText(text, fx + 4, y + h - 6);
  }

  private pill(x: number, y: number, text: string, w: number, col: Rgb, strong: boolean): void {
    const { ctx, theme } = this;
    ctx.fillStyle = strong ? rgba(col, 0.95) : rgba(theme.ink, 0.12);
    roundedRect(ctx, x, y, w, 18, 9);
    ctx.fill();
    ctx.font = `700 10px ${theme.sans}`;
    ctx.fillStyle = strong ? (theme.dark ? 'rgba(10,10,16,0.95)' : 'rgba(255,255,255,0.98)') : rgba(theme.ink, 0.8);
    ctx.fillText(text, x + 7, y + 12.5);
  }

  private playhead(x: number, y0: number, y1: number): void {
    const { ctx, theme } = this;
    ctx.fillStyle = rgba(theme.ink, 0.92);
    ctx.fillRect(x - 1, y0 - 2, 2, y1 - y0 + 4);
    ctx.beginPath();
    ctx.moveTo(x - 5, y0 - 4);
    ctx.lineTo(x + 5, y0 - 4);
    ctx.lineTo(x, y0 + 3);
    ctx.closePath();
    ctx.fill();
  }


  private strip(x: number, y: number, w: number, frame: StageFrame | null): void {
    const { ctx, theme } = this;
    const cy = y + STRIP_H / 2;
    const bal = frame?.balance ?? null;
    ctx.fillStyle = rgba(theme.ink, 0.1);
    roundedRect(ctx, x, cy - 3, w, 6, 3);
    ctx.fill();
    if (bal === null) return;
    const knob = x + w * (1 - bal);
    // El plato 0 reparte desde la izquierda y el 1 desde la derecha: donde se juntan está el cruce
    ctx.fillStyle = rgba(theme.deckA, 0.95);
    roundedRect(ctx, x, cy - 3, Math.max(0, knob - x), 6, 3);
    ctx.fill();
    ctx.fillStyle = rgba(theme.deckB, 0.95);
    roundedRect(ctx, knob, cy - 3, Math.max(0, x + w - knob), 6, 3);
    ctx.fill();
    ctx.fillStyle = rgba(theme.dark ? [245, 245, 250] : [255, 255, 255], 1);
    ctx.strokeStyle = rgba(theme.ink, 0.5);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(knob, cy, 6.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }


  private platter(cx: number, cy: number, R: number, deck: StageDeck | null, col: Rgb, slot: number): void {
    const { ctx, theme } = this;
    const active = !!deck && deck.role !== 'next';
    const level = deck ? clamp01(deck.level) : 0;
    const gain = deck ? clamp01(deck.gain) : 0;
    const angle = deck && active ? deck.position * SPIN : 0;

    // Anillo de nivel: crece y brilla con la señal real del plato
    ctx.lineWidth = 3;
    ctx.strokeStyle = rgba(col, 0.16);
    ctx.beginPath();
    ctx.arc(cx, cy, R + 7, 0, Math.PI * 2);
    ctx.stroke();
    if (deck && active) {
      ctx.strokeStyle = rgba(col, 0.35 + 0.65 * level);
      ctx.lineWidth = 3 + level * 4;
      ctx.beginPath();
      ctx.arc(cx, cy, R + 7, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.max(0.04, gain), false);
      ctx.stroke();
    }

    // Disco
    const disc = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.35, R * 0.1, cx, cy, R);
    disc.addColorStop(0, rgba(lerpRgb(theme.vinyl, [255, 255, 255], 0.14), 1));
    disc.addColorStop(1, rgba(theme.vinyl, 1));
    ctx.fillStyle = disc;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, Math.PI * 2);
    ctx.fill();

    // Surcos
    ctx.lineWidth = 1;
    for (let r = R * 0.42; r < R * 0.97; r += R * 0.085) {
      ctx.strokeStyle = `rgba(255,255,255,${0.035 + 0.02 * ((Math.round(r) * 7) % 3)})`;
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Reflejo que gira con el disco
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(angle);
    for (const a0 of [0.25, Math.PI + 0.25]) {
      ctx.fillStyle = 'rgba(255,255,255,0.05)';
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, R * 0.96, a0, a0 + 0.5);
      ctx.closePath();
      ctx.fill();
    }
    // Etiqueta
    const lr = R * 0.34;
    ctx.fillStyle = rgba(col, active ? 1 : 0.4);
    ctx.beginPath();
    ctx.arc(0, 0, lr, 0, Math.PI * 2);
    ctx.fill();
    // Marca que permite ver el giro
    ctx.fillStyle = theme.dark ? 'rgba(10,10,16,0.55)' : 'rgba(255,255,255,0.7)';
    ctx.fillRect(-1.5, -lr + 3, 3, lr * 0.55);
    ctx.restore();

    // Número del plato y eje
    ctx.fillStyle = theme.dark ? 'rgba(10,10,16,0.9)' : 'rgba(255,255,255,0.95)';
    ctx.font = `800 ${Math.round(R * 0.26)}px ${theme.sans}`;
    ctx.textAlign = 'center';
    ctx.fillText(String(slot + 1), cx, cy + R * 0.09);
    ctx.textAlign = 'left';
    ctx.fillStyle = rgba(theme.vinyl, 1);
    ctx.beginPath();
    ctx.arc(cx, cy - R * 0.22, 2.2, 0, Math.PI * 2);
    ctx.fill();
  }
}


const BARS = 72;

export class BackdropRenderer {
  private ctx: CanvasRenderingContext2D;
  private w = 1;
  private h = 1;
  private theme: StageTheme;
  private key: string;
  private levels = new Float32Array(BARS);
  private reduced: boolean;

  constructor(private canvas: HTMLCanvasElement) {
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D no disponible');
    this.ctx = ctx;
    this.theme = readTheme();
    this.key = themeKey();
    this.reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.resize();
  }

  resize(): void {
    ({ w: this.w, h: this.h } = setupCanvas(this.canvas, this.ctx));
  }

  /**
   * @param time segundos (para la animación en reposo)
   * @param spectrum espectro de magnitudes 0..255 del audio de salida (null si no suena nada)
   * @param balance reparto entre platos (0..1) para mezclar los dos colores
   */
  draw(time: number, spectrum: Uint8Array | null, balance: number | null): void {
    const k = themeKey();
    if (k !== this.key) {
      this.key = k;
      this.theme = readTheme();
    }
    const { ctx, w, h, theme, levels } = this;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const usable = spectrum ? Math.min(spectrum.length, 300) : 0;
    for (let i = 0; i < BARS; i++) {
      let target: number;
      if (spectrum && usable > 8) {
        // Escala logarítmica: los graves ocupan más sitio, como los oye el oído
        const a = 1 + Math.floor(Math.pow(i / BARS, 2.1) * (usable - 1));
        const b = 1 + Math.floor(Math.pow((i + 1) / BARS, 2.1) * (usable - 1));
        let m = 0;
        for (let j = a; j <= Math.max(a, b); j++) m = Math.max(m, spectrum[j] ?? 0);
        target = Math.pow(m / 255, 1.6);
      } else {
        target = this.reduced ? 0.05 : 0.055 + 0.04 * Math.sin(time * 0.9 + i * 0.32) + 0.02 * Math.sin(time * 1.7 + i * 0.11);
      }
      levels[i] = target > levels[i] ? levels[i] + (target - levels[i]) * 0.55 : levels[i] * 0.9 + target * 0.1;
    }

    const bw = w / BARS;
    const maxH = h * 0.5;
    const bal = balance ?? 0.5;
    ctx.globalCompositeOperation = theme.dark ? 'lighter' : 'source-over';
    for (let i = 0; i < BARS; i++) {
      const c = lerpRgb(theme.deckA, theme.deckB, clamp01((i / (BARS - 1)) * 0.7 + (bal - 0.5) * 0.6 + 0.15));
      const bh = Math.max(3, levels[i] * maxH);
      const alpha = theme.dark ? 0.16 + levels[i] * 0.34 : 0.1 + levels[i] * 0.2;
      const g = ctx.createLinearGradient(0, h - bh, 0, h);
      g.addColorStop(0, rgba(c, alpha));
      g.addColorStop(1, rgba(c, alpha * 0.15));
      ctx.fillStyle = g;
      roundedRect(ctx, i * bw + bw * 0.12, h - bh, bw * 0.76, bh + 6, bw * 0.3);
      ctx.fill();
    }
    ctx.globalCompositeOperation = 'source-over';
  }
}
