import { Injectable, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import { I18n } from './i18n.service';
import { MixEngine } from './mix-engine';
import type { MixPlan, TrackSummary } from './models';
import type { StageFrame } from './visualizer';
import { SessionStore } from './session-store.service';

/** Conecta los mensajes de la sesión (load/plan) con el motor de audio del navegador. */
@Injectable({ providedIn: 'root' })
export class PlayerService {
  private store = inject(SessionStore);
  private api = inject(ApiService);
  private i18n = inject(I18n);
  private engine: MixEngine | null = null;
  private raf = 0;

  readonly started = signal(false);
  readonly paused = signal(false);
  readonly transition = signal<number | null>(null);
  readonly secondsToMix = signal<number | null>(null);
  readonly outgoing = signal<TrackSummary | null>(null);
  readonly loadingFirst = signal(false);

  /**
   * Estado de los platos y espectro del último fotograma. No son signals a propósito: los
   * lienzos los leen en su propio requestAnimationFrame y no hay que repintar plantillas.
   */
  frame: StageFrame | null = null;
  spectrum: Uint8Array | null = null;

  constructor() {
    this.store.on('load', (m) => void this.onLoad(m.track as TrackSummary, m.rate, m.offset));
    this.store.on('plan', (m) => this.engine?.setPlan(m.plan as MixPlan));
  }

  /** Debe llamarse desde un clic: los navegadores solo permiten audio tras un gesto del usuario. */
  unlock(): void {
    if (this.engine) return;
    this.engine = new MixEngine(
      {
        trackStarted: (t, rate, offset) => this.store.send({ type: 'track_started', track_id: t.id, rate, offset }),
        transitionStarted: (planId) => this.store.send({ type: 'transition_started', plan_id: planId }),
        planFailed: (planId, error) => this.store.send({ type: 'plan_failed', plan_id: planId, error }),
        planLate: () => this.store.send({ type: 'mix_now' }),
        position: (trackId, position) => this.store.send({ type: 'position', track_id: trackId, position }),
        ended: () => this.store.send({ type: 'ended' }),
      },
      (id, rate) => this.api.audioUrl(id, rate),
    );
    void this.engine.resume();
    this.loop();
  }

  /** Pasa a la pantalla de sesión. */
  begin(): void {
    this.unlock();
    this.started.set(true);
  }

  async togglePause(): Promise<void> {
    if (!this.engine) return;
    if (this.engine.paused) await this.engine.resume();
    else await this.engine.pause();
    this.paused.set(this.engine.paused);
  }

  stop(): void {
    this.store.send({ type: 'stop' });
    this.engine?.stop();
    this.engine = null;
    cancelAnimationFrame(this.raf);
    this.frame = null;
    this.spectrum = null;
    this.started.set(false);
    this.paused.set(false);
    this.transition.set(null);
    this.secondsToMix.set(null);
    this.outgoing.set(null);
    this.loadingFirst.set(false);
  }

  private async onLoad(track: TrackSummary, rate: number, offset: number): Promise<void> {
    if (!this.engine) return;
    this.loadingFirst.set(true);
    try {
      await this.engine.playNow(track, rate ?? 1, offset ?? 0);
    } catch (e) {
      console.warn('No se pudo cargar la pista', track.id, e);
      this.store.lastError.set(this.i18n.t('No se pudo reproducir «{title}». Paso a otra.', { title: track.title }));
      this.store.send({ type: 'ended' });
    } finally {
      this.loadingFirst.set(false);
    }
  }

  private loop = (): void => {
    const e = this.engine;
    if (!e) return;
    this.frame = e.stageFrame();
    this.spectrum = e.spectrum();
    this.transition.set(e.transitionProgress());
    const s = e.secondsToMix();
    this.secondsToMix.set(s == null ? null : Math.round(s));
    this.outgoing.set(e.outgoing());
    this.raf = requestAnimationFrame(this.loop);
  };
}
