/**
 * Motor de mezcla en Web Audio: dos decks con EQ de 3 bandas que ejecutan los planes de
 * transición que envía el backend. No depende de Angular.
 */
import type { Lane, MixPlan, TrackSummary } from './models';
import type { StageDeck, StageFrame, StageMix, WavePeaks } from './visualizer';
import { computePeaksAsync } from './waveform';

const TARGET_LUFS = -11;
const COMMIT_AHEAD = 1.5; // s antes de la mezcla en que se programa en firme
const TICK_MS = 100;
/** Traspaso entre la pista nativa y el clip renderizado (y al revés): lo bastante corto para no oírse. */
const HANDOFF = 0.04;

export interface EngineEvents {
  trackStarted(track: TrackSummary, rate: number, offset: number): void;
  transitionStarted(planId: string): void;
  planFailed(planId: string, error: string): void;
  planLate(planId: string): void;
  position(trackId: string, position: number): void;
  ended(trackId: string): void;
}

/** Lo que hace falta para dibujar una pista, ya en el tiempo en que suena (con su estiramiento). */
interface DeckInfo {
  downbeats: number[];
  introEnd: number | null;
  outroStart: number | null;
  phraseOffset: number;
  peaks: WavePeaks | null;
}

function deckInfo(track: TrackSummary, rate: number, peaks: WavePeaks | null): DeckInfo {
  const r = rate > 0 ? rate : 1;
  const scale = (t: number | null | undefined): number | null => (t == null ? null : t / r);
  return {
    downbeats: (track.downbeats ?? []).map((d) => d / r),
    introEnd: scale(track.intro_end),
    outroStart: scale(track.outro_start),
    phraseOffset: track.phrase_offset ?? 0,
    peaks,
  };
}

/**
 * Posición en la pista original (s) para un instante del clip, según el mapa `[instante del clip, instante
 * original]`. Dentro del mapa interpola; fuera sigue con la pendiente del primer o el último tramo.
 */
export function mapAt(map: [number, number][], t: number): number {
  const n = map.length;
  if (n === 0) return 0;
  if (n === 1) return map[0][1] + (t - map[0][0]);
  let i = 1;
  while (i < n - 1 && t > map[i][0]) i++;
  const [t0, s0] = map[i - 1];
  const [t1, s1] = map[i];
  return t1 > t0 ? s0 + ((s1 - s0) * (t - t0)) / (t1 - t0) : s1;
}

/** Valor de una curva `[t, valor]` en el instante `t` (interpolación lineal, fija fuera de los extremos). */
export function laneValue(points: [number, number][], t: number): number {
  if (!points.length) return NaN;
  if (t <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i++) {
    const [t1, v1] = points[i];
    if (t <= t1) {
      const [t0, v0] = points[i - 1];
      return t1 > t0 ? v0 + ((v1 - v0) * (t - t0)) / (t1 - t0) : v1;
    }
  }
  return points[points.length - 1][1];
}

class Deck {
  readonly meter: AnalyserNode;
  private meterData: Float32Array<ArrayBuffer>;
  info: DeckInfo | null = null;
  readonly trim: GainNode;
  readonly low: BiquadFilterNode;
  readonly mid: BiquadFilterNode;
  readonly high: BiquadFilterNode;
  readonly gain: GainNode;
  source: AudioBufferSourceNode | null = null;
  track: TrackSummary | null = null;
  rate = 1;
  /** Instante (reloj de audio) en que la posición del buffer era `offset`. */
  startAt = 0;
  offset = 0;
  duration = 0;

  constructor(private ctx: AudioContext, out: AudioNode) {
    this.trim = ctx.createGain();
    this.low = ctx.createBiquadFilter();
    this.low.type = 'lowshelf';
    this.low.frequency.value = 200;
    this.mid = ctx.createBiquadFilter();
    this.mid.type = 'peaking';
    this.mid.frequency.value = 1000;
    this.mid.Q.value = 0.8;
    this.high = ctx.createBiquadFilter();
    this.high.type = 'highshelf';
    this.high.frequency.value = 4000;
    this.gain = ctx.createGain();
    this.trim.connect(this.low).connect(this.mid).connect(this.high).connect(this.gain).connect(out);
    // Medidor propio: lo que de verdad sale de este plato, ya con su volumen y su EQ.
    this.meter = ctx.createAnalyser();
    this.meter.fftSize = 512;
    this.meter.smoothingTimeConstant = 0;
    this.meterData = new Float32Array(this.meter.fftSize);
    this.gain.connect(this.meter);
  }

  /** Nivel 0..1 de la señal que sale del plato (RMS). */
  level(): number {
    this.meter.getFloatTimeDomainData(this.meterData);
    let s = 0;
    for (const x of this.meterData) s += x * x;
    return Math.min(1, Math.sqrt(s / this.meterData.length) * 3.2);
  }

  /** Parámetro de audio de la curva; los stems no existen en el navegador (solo los usa el worker). */
  param(name: Lane['param']): AudioParam | null {
    switch (name) {
      case 'gain': return this.gain.gain;
      case 'low': return this.low.gain;
      case 'mid': return this.mid.gain;
      case 'high': return this.high.gain;
      default: return null;
    }
  }

  neutral(at: number): void {
    for (const p of ['gain', 'low', 'mid', 'high'] as const) {
      const ap = this.param(p) as AudioParam;
      ap.cancelScheduledValues(at);
      ap.setValueAtTime(p === 'gain' ? 1 : 0, at);
    }
  }

  position(now: number): number {
    return this.offset + Math.max(0, now - this.startAt);
  }

  play(buffer: AudioBuffer, track: TrackSummary, rate: number, when: number, offset: number, info: DeckInfo): void {
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.trim);
    src.start(when, Math.max(0, offset));
    this.source = src;
    this.track = track;
    this.rate = rate;
    this.startAt = when;
    this.offset = offset;
    this.duration = buffer.duration;
    this.info = info;
    this.trim.gain.setValueAtTime(loudnessGain(track.loudness_lufs), when);
  }

  stop(at: number): void {
    try {
      this.source?.stop(at);
    } catch {
      /* ya parado */
    }
  }
}

/** Reproduce el clip renderizado de una mezcla. Va directo al master: el clip ya lleva EQ y volúmenes dentro. */
class ClipPlayer {
  readonly gain: GainNode;
  readonly meter: AnalyserNode;
  private data: Float32Array<ArrayBuffer>;
  source: AudioBufferSourceNode | null = null;

  constructor(private ctx: AudioContext, out: AudioNode) {
    this.gain = ctx.createGain();
    this.gain.gain.value = 0;
    this.meter = ctx.createAnalyser();
    this.meter.fftSize = 512;
    this.meter.smoothingTimeConstant = 0;
    this.data = new Float32Array(this.meter.fftSize);
    this.gain.connect(out);
    this.gain.connect(this.meter);
  }

  level(): number {
    this.meter.getFloatTimeDomainData(this.data);
    let s = 0;
    for (const x of this.data) s += x * x;
    return Math.min(1, Math.sqrt(s / this.data.length) * 3.2);
  }

  /** Programa el clip en `when` con traspasos de entrada y salida; la duración es la del propio audio. */
  play(buffer: AudioBuffer, when: number, h: number = HANDOFF): void {
    this.cancel();
    const g = this.gain.gain;
    const d = buffer.duration;
    g.cancelScheduledValues(0);
    g.setValueAtTime(0, Math.max(0, when - 0.001));
    g.setValueAtTime(0, when);
    g.linearRampToValueAtTime(1, when + h);
    g.setValueAtTime(1, when + d - h);
    g.linearRampToValueAtTime(0, when + d);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.gain);
    src.start(when);
    src.stop(when + d + 0.05);
    this.source = src;
  }

  cancel(): void {
    try {
      this.source?.stop();
    } catch {
      /* ya parado */
    }
    this.source = null;
    this.gain.gain.cancelScheduledValues(0);
    this.gain.gain.setValueAtTime(0, this.ctx.currentTime);
  }
}

export function loudnessGain(lufs: number | null | undefined): number {
  if (lufs == null || !isFinite(lufs)) return 1;
  return Math.min(2.5, Math.max(0.25, Math.pow(10, (TARGET_LUFS - lufs) / 20)));
}

/** Aplica una curva `[t, valor]` a un AudioParam, con t relativo a `when`. */
export function applyLane(param: AudioParam | null, points: [number, number][], when: number): void {
  if (!param || !points.length) return;
  param.cancelScheduledValues(when);
  const [t0, v0] = points[0];
  param.setValueAtTime(v0, when + t0);
  for (const [t, v] of points.slice(1)) param.linearRampToValueAtTime(v, when + t);
}

interface Pending {
  plan: MixPlan;
  /** Audio de la entrante: a la velocidad del plan en las mezclas clásicas; nativa en las renderizadas. */
  buffer: AudioBuffer | null;
  /** Clip renderizado por el worker (solo en mezclas adaptativas). */
  clip: AudioBuffer | null;
  error: string | null;
  committed: boolean;
  when: number;
  info?: DeckInfo;
}

export class MixEngine {
  readonly ctx: AudioContext;
  readonly analyser: AnalyserNode;
  private master: GainNode;
  private decks: [Deck, Deck];
  private cur = 0;
  private pending: Pending | null = null;
  private transitionEnd: { at: number; plan: MixPlan; when: number } | null = null;
  private cache = new Map<string, Promise<AudioBuffer>>();
  private clips = new Map<string, Promise<AudioBuffer>>();
  private clip: ClipPlayer;
  private peaks = new WeakMap<AudioBuffer, WavePeaks>();
  private spectrumData: Uint8Array<ArrayBuffer>;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastReport = 0;
  private endedSent = false;

  constructor(private events: EngineEvents, private audioUrl: (trackId: string, rate: number) => string) {
    this.ctx = new AudioContext({ latencyHint: 'playback' });
    this.master = this.ctx.createGain();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.7;
    this.spectrumData = new Uint8Array(this.analyser.frequencyBinCount);
    this.master.connect(this.analyser).connect(this.ctx.destination);
    this.decks = [new Deck(this.ctx, this.master), new Deck(this.ctx, this.master)];
    this.clip = new ClipPlayer(this.ctx, this.master);
  }

  get current(): Deck {
    return this.decks[this.cur];
  }

  get paused(): boolean {
    return this.ctx.state === 'suspended';
  }

  async resume(): Promise<void> {
    await this.ctx.resume();
  }

  async pause(): Promise<void> {
    await this.ctx.suspend();
  }

  setVolume(v: number): void {
    this.master.gain.setTargetAtTime(Math.max(0, Math.min(1.5, v)), this.ctx.currentTime, 0.05);
  }

  /** Nivel 0..1 en tres bandas, para el indicador animado. */
  levels(): [number, number, number] {
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    this.analyser.getByteFrequencyData(data);
    const band = (a: number, b: number) => {
      let s = 0;
      for (let i = a; i < b; i++) s += data[i];
      return s / ((b - a) * 255);
    };
    const n = data.length;
    return [band(4, 24), band(24, n / 4), band(n / 4, n)];
  }

  load(trackId: string, rate: number): Promise<AudioBuffer> {
    const url = this.audioUrl(trackId, rate);
    let p = this.cache.get(url);
    if (!p) {
      p = fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.arrayBuffer();
        })
        .then((buf) => this.ctx.decodeAudioData(buf))
        .then(async (audio) => {
          // La onda se resume una sola vez por pista, nada más decodificarla, y por tandas para no frenar la interfaz.
          this.peaks.set(audio, await computePeaksAsync(audio.getChannelData(0), audio.sampleRate));
          return audio;
        });
      p.catch(() => this.cache.delete(url));
      this.cache.set(url, p);
      if (this.cache.size > 6) this.cache.delete(this.cache.keys().next().value as string);
    }
    return p;
  }

  /** Descarga y decodifica el clip de una mezcla renderizada (no se dibuja su onda). */
  loadClip(url: string): Promise<AudioBuffer> {
    let p = this.clips.get(url);
    if (!p) {
      p = fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.arrayBuffer();
        })
        .then((buf) => this.ctx.decodeAudioData(buf));
      p.catch(() => this.clips.delete(url));
      this.clips.set(url, p);
      if (this.clips.size > 3) this.clips.delete(this.clips.keys().next().value as string);
    }
    return p;
  }

  /** Arranca una pista desde cero (primera de la sesión o tras un fallo). */
  async playNow(track: TrackSummary, rate: number, offset: number): Promise<void> {
    const buffer = await this.load(track.id, rate);
    await this.ctx.resume();
    const when = this.ctx.currentTime + 0.1;
    for (const d of this.decks) {
      d.stop(when);
      d.neutral(when);
    }
    this.pending = null;
    this.transitionEnd = null;
    this.clip.cancel();
    this.endedSent = false;
    this.cur = 0;
    this.current.play(buffer, track, rate, when, offset, deckInfo(track, rate, this.peaks.get(buffer) ?? null));
    this.ensureTimer();
    this.events.trackStarted(track, rate, offset);
  }

  /** Recibe un plan nuevo. Sustituye al anterior si todavía no se ha programado en firme. */
  setPlan(plan: MixPlan): void {
    if (this.pending?.committed || this.transitionEnd) return;
    const deck = this.current;
    if (!deck.track || deck.track.id !== plan.from_id) return;
    const when = deck.startAt + (plan.out_start - deck.offset);
    const p: Pending = { plan, buffer: null, clip: null, error: null, committed: false, when };
    this.pending = p;
    if (plan.render?.url) {
      // Mezcla renderizada: la entrante se carga a su velocidad original y el clip aparte.
      this.load(plan.track.id, 1).then(
        (b) => (p.buffer = b),
        (e: unknown) => (p.error = String(e)),
      );
      this.loadClip(plan.render.url).then(
        (c) => (p.clip = c),
        // El prefijo "clip:" avisa a la sesión de que el fallo es del clip, no de la pista: pasa a mezcla clásica.
        (e: unknown) => (p.error = `clip: ${String(e)}`),
      );
    } else {
      this.load(plan.track.id, plan.rate_b).then(
        (b) => (p.buffer = b),
        (e: unknown) => (p.error = String(e)),
      );
    }
    this.ensureTimer();
  }

  stop(): void {
    this.clip.cancel();
    for (const d of this.decks) d.stop(this.ctx.currentTime);
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    void this.ctx.close();
  }

  private ensureTimer(): void {
    if (!this.timer) this.timer = setInterval(() => this.tick(), TICK_MS);
  }

  private tick(): void {
    if (this.paused) return;
    const now = this.ctx.currentTime;
    const deck = this.current;
    const p = this.pending;

    if (p && !p.committed) {
      if (p.error) {
        this.pending = null;
        this.events.planFailed(p.plan.plan_id, p.error);
      } else if (now >= p.when - COMMIT_AHEAD) {
        const ready = !!p.buffer && (!p.plan.render?.url || !!p.clip);
        if (ready && p.when > now + 0.05) this.commit(p);
        else if (!ready && p.when > now + 0.2) {
          /* todavía cargando: se espera al último momento */
        } else {
          this.pending = null;
          this.events.planLate(p.plan.plan_id);
        }
      }
    }

    const te = this.transitionEnd;
    if (te && now >= te.at) {
      this.transitionEnd = null;
      this.decks[1 - this.cur].stop(now);
      this.decks[1 - this.cur].neutral(now + 0.05);
      const d = this.current;
      this.endedSent = false;
      this.events.trackStarted(te.plan.track, te.plan.render?.url ? 1 : te.plan.rate_b, d.position(now));
    }

    if (deck.track && now - this.lastReport >= 1) {
      this.lastReport = now;
      this.events.position(deck.track.id, deck.position(now));
    }

    if (deck.track && !this.transitionEnd && !this.endedSent && deck.position(now) >= deck.duration - 0.05) {
      this.endedSent = true;
      this.events.ended(deck.track.id);
    }
  }

  /**
   * Mezcla renderizada: la saliente suena nativa hasta `when`, entonces entra el clip (traspaso de 40 ms) y,
   * 40 ms antes de que acabe, la entrante sigue por su cuenta desde donde lo deja el clip. Todo el DSP
   * (tempo, tono, EQ, stems) ya está dentro del clip: aquí solo hay que programar tres arranques.
   */
  private commitRendered(p: Pending): void {
    p.committed = true;
    const { plan } = p;
    const clip = p.clip as AudioBuffer;
    const when = p.when;
    const d = clip.duration;
    const out = this.current;
    const inc = this.decks[1 - this.cur];
    const now = this.ctx.currentTime;

    // Los cortes con eco empalman en pocos ms (la saliente se corta en un compás); el resto, en 40 ms.
    const h = plan.render?.handoff ?? HANDOFF;
    this.clip.play(clip, when, h);

    out.neutral(when);
    out.gain.gain.setValueAtTime(1, when);
    out.gain.gain.linearRampToValueAtTime(0, when + h);
    out.stop(when + h + 0.02);

    const tB = when + d - h;
    inc.stop(tB);
    inc.neutral(now);
    inc.gain.gain.cancelScheduledValues(now);
    inc.gain.gain.setValueAtTime(0, now);
    inc.gain.gain.setValueAtTime(0, tB);
    inc.gain.gain.linearRampToValueAtTime(1, tB + h);
    inc.play(p.buffer as AudioBuffer, plan.track, 1, tB, Math.max(0, plan.in_start - h), this.infoOf(p));

    this.cur = 1 - this.cur;
    this.pending = null;
    this.transitionEnd = { at: when + d + 0.06, plan, when };
    this.events.transitionStarted(plan.plan_id);
  }

  private commit(p: Pending): void {
    if (p.plan.render?.url) {
      this.commitRendered(p);
      return;
    }
    p.committed = true;
    const { plan } = p;
    const when = p.when;
    const out = this.current;
    const inc = this.decks[1 - this.cur];
    inc.stop(when);
    inc.neutral(when);
    out.neutral(when);
    for (const lane of plan.automation) {
      const deck = lane.deck === 'A' ? out : inc;
      applyLane(deck.param(lane.param), lane.points, when);
    }
    if (!plan.automation.some((l) => l.deck === 'B' && l.param === 'gain')) {
      inc.gain.gain.setValueAtTime(1, when);
    }
    inc.play(p.buffer as AudioBuffer, plan.track, plan.rate_b, when, plan.in_start, this.infoOf(p));
    out.stop(when + plan.duration + 0.05);
    this.cur = 1 - this.cur;
    this.pending = null;
    this.transitionEnd = { at: when + plan.duration + 0.06, plan, when };
    this.events.transitionStarted(plan.plan_id);
  }

  /** Progreso 0..1 de la transición en curso (null si no hay). */
  transitionProgress(): number | null {
    const te = this.transitionEnd;
    if (!te) return null;
    const total = Math.max(0.01, te.at - te.when);
    return Math.min(1, Math.max(0, (this.ctx.currentTime - te.when) / total));
  }

  /** Segundos que faltan para la próxima mezcla planificada. */
  secondsToMix(): number | null {
    if (!this.pending) return null;
    return Math.max(0, this.pending.when - this.ctx.currentTime);
  }

  private infoOf(p: Pending): DeckInfo {
    const rate = p.plan.render?.url ? 1 : p.plan.rate_b;
    if (!p.info) p.info = deckInfo(p.plan.track, rate, p.buffer ? (this.peaks.get(p.buffer) ?? null) : null);
    else if (!p.info.peaks && p.buffer) p.info.peaks = this.peaks.get(p.buffer) ?? null;
    return p.info;
  }

  /** Espectro 0..255 del audio de salida; null si no está sonando nada. */
  spectrum(): Uint8Array | null {
    if (this.paused || !this.current.track) return null;
    this.analyser.getByteFrequencyData(this.spectrumData);
    return this.spectrumData;
  }

  /**
   * Estado de los dos platos para dibujarlo. Todo se deduce del reloj de audio y del plan, no de
   * ningún temporizador aparte: lo que se ve es lo que se está ejecutando.
   */
  stageFrame(): StageFrame {
    const now = this.ctx.currentTime;
    const cur = this.cur;
    const te = this.transitionEnd;
    const p = this.pending;
    const decks: [StageDeck | null, StageDeck | null] = [null, null];
    let mix: StageMix | null = null;

    const planMix = (plan: MixPlan, outSlot: 0 | 1, progress: number | null): StageMix => {
      const r = plan.render;
      if (r?.url) {
        // Mezcla renderizada: el tramo "de mezcla" es el solapamiento, y las posiciones salen de los mapas del clip.
        const a = (t: number) => mapAt(r.maps.a, t);
        const b = (t: number) => mapAt(r.maps.b, t);
        return {
          label: plan.label ?? plan.strategy,
          outSlot,
          inSlot: (1 - outSlot) as 0 | 1,
          outFrom: a(r.overlap_at),
          outTo: a(r.overlap_end),
          inFrom: b(r.overlap_at),
          inTo: b(r.overlap_end),
          swapOut: plan.swap_at != null ? a(r.overlap_at + plan.swap_at) : null,
          swapIn: plan.swap_at != null ? b(r.overlap_at + plan.swap_at) : null,
          aligned: !!plan.aligned,
          progress: progress == null ? null : Math.min(1, Math.max(0, (progress * plan.duration - r.overlap_at) / Math.max(0.01, r.overlap_end - r.overlap_at))),
        };
      }
      return {
        label: plan.label ?? plan.strategy,
        outSlot,
        inSlot: (1 - outSlot) as 0 | 1,
        outFrom: plan.out_start,
        outTo: plan.out_start + plan.duration,
        inFrom: plan.in_start,
        inTo: plan.in_start + plan.duration,
        swapOut: plan.swap_at != null ? plan.out_start + plan.swap_at : null,
        swapIn: plan.swap_at != null ? plan.in_start + plan.swap_at : null,
        aligned: !!plan.aligned,
        progress,
      };
    };

    const make = (slot: 0 | 1, role: StageDeck['role'], d: Deck, plan: MixPlan | null, t: number): StageDeck => {
      const info = d.info as DeckInfo;
      const track = d.track as TrackSummary;
      const rendered = plan?.render?.url ? plan.render : null;
      // En las curvas de una mezcla renderizada el 0 es el inicio del solapamiento, no el del clip.
      const tl = rendered ? t - rendered.overlap_at : t;
      const lane = (name: Lane['param'], who: 'A' | 'B', fallback: number): number => {
        const l = plan?.automation.find((x) => x.deck === who && x.param === name);
        const v = l ? laneValue(l.points, tl) : NaN;
        return Number.isFinite(v) ? v : fallback;
      };
      const who = role === 'in' || role === 'next' ? 'B' : 'A';
      // Antes de arrancar, la entrante se enseña ya alineada con el punto donde entrará (posición negativa)
      let position = role === 'next' ? d.offset + (now - d.startAt) : d.position(now);
      let level = this.paused || role === 'next' ? 0 : d.level();
      if (rendered) {
        // Durante el clip no suena ningún plato: la posición sale del mapa del clip y el nivel, del propio clip.
        const clipOn = t >= 0 && t <= (rendered.maps.a[rendered.maps.a.length - 1]?.[0] ?? 0);
        if (who === 'A' || t < (rendered.maps.b[rendered.maps.b.length - 1]?.[0] ?? 0)) {
          position = mapAt(who === 'A' ? rendered.maps.a : rendered.maps.b, t);
        }
        if (clipOn && !this.paused && role !== 'next') level = this.clip.level();
      }
      return {
        slot,
        role,
        title: track.title,
        position: Math.min(position, d.duration),
        duration: d.duration,
        level,
        gain: plan ? lane('gain', who, 1) : 1,
        lowDb: plan ? lane('low', who, 0) : 0,
        peaks: info.peaks,
        downbeats: info.downbeats,
        phraseOffset: info.phraseOffset,
        introEnd: info.introEnd,
        outroStart: info.outroStart,
      };
    };

    if (te) {
      // Mezcla en curso: sale el plato que estaba y entra el nuevo
      const out = this.decks[1 - cur];
      const inc = this.decks[cur];
      const t = now - te.when;
      // La mezcla se programa en firme unos instantes antes de empezar: hasta entonces sigue siendo "siguiente"
      const started = t >= 0;
      if (out.track && out.info) decks[1 - cur] = make((1 - cur) as 0 | 1, started ? 'out' : 'air', out, te.plan, t);
      if (inc.track && inc.info) decks[cur] = make(cur as 0 | 1, started ? 'in' : 'next', inc, te.plan, t);
      mix = planMix(te.plan, (1 - cur) as 0 | 1, started ? this.transitionProgress() : null);
    } else if (this.current.track && this.current.info) {
      decks[cur] = make(cur as 0 | 1, 'air', this.current, null, 0);
      if (p && !p.committed && !p.error && p.buffer && p.when - now < 25) {
        // La siguiente ya está cargada: se enseña alineada con el punto donde entrará
        const info = this.infoOf(p);
        const t0 = p.when - now;
        const planA = p.plan;
        const gb = planA.automation.find((l) => l.deck === 'B' && l.param === 'gain');
        const lb = planA.automation.find((l) => l.deck === 'B' && l.param === 'low');
        // (en una mezcla renderizada las curvas empiezan en el solapamiento; antes de él, el primer valor)
        decks[1 - cur] = {
          slot: (1 - cur) as 0 | 1,
          role: 'next',
          title: planA.track.title,
          position: planA.render?.url ? mapAt(planA.render.maps.b, -t0) : planA.in_start - t0,
          duration: p.buffer.duration,
          level: 0,
          gain: gb ? laneValue(gb.points, 0) : 1,
          lowDb: lb ? laneValue(lb.points, 0) : 0,
          peaks: info.peaks,
          downbeats: info.downbeats,
          phraseOffset: info.phraseOffset,
          introEnd: info.introEnd,
          outroStart: info.outroStart,
        };
      }
      if (p && !p.error) mix = planMix(p.plan, cur as 0 | 1, null);
    }

    const g0 = decks[0] && decks[0].role !== 'next' ? decks[0].gain : 0;
    const g1 = decks[1] && decks[1].role !== 'next' ? decks[1].gain : 0;
    const total = g0 + g1;
    return {
      paused: this.paused,
      decks,
      mix,
      balance: total > 0.001 ? g1 / total : null,
    };
  }

  outgoing(): TrackSummary | null {
    return this.transitionEnd ? this.decks[1 - this.cur].track : null;
  }
}
