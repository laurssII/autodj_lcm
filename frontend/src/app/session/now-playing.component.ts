import { Component, computed, inject } from '@angular/core';
import { PlayerService } from '../core/player.service';
import { TPipe } from '../core/t.pipe';
import { SessionStore } from '../core/session-store.service';
import { DecidedBadgeComponent } from '../ui/decided-badge.component';
import { StageComponent } from '../ui/stage.component';

/** "Ahora sonando": los dos platos con la mezcla en vivo, la pista actual, la transición y la siguiente. */
@Component({
  selector: 'dj-now-playing',
  imports: [StageComponent, DecidedBadgeComponent, TPipe],
  template: `
    <section class="panel">
      <dj-stage class="stage" />
      <div class="text">
        @if (np(); as t) {
          <p class="now"><strong>{{ t.title }}</strong>@if (t.artist) { <span class="muted"> · {{ t.artist }}</span> }</p>
          <p class="meta mono">
            <span class="tag">{{ bpm() }} BPM</span>
            <span class="tag">{{ t.camelot ?? '—' }}</span>
            <span class="tag">{{ 'energía {n}' | t: { n: t.energy ?? '—' } }}</span>
            @if (t.genre) { <span class="tag">{{ t.genre }}</span> }
            @if (t.credit_url) {
              <a [href]="t.credit_url" target="_blank" rel="noopener">{{ 'original en {source}' | t: { source: t.source } }}</a>
            }
          </p>
        } @else if (waiting()) {
          <p class="now">{{ 'Analizando «{title}»…' | t: { title: waiting()?.title } }}</p>
          <p class="meta muted">{{ 'Empezará en cuanto termine el análisis.' | t }}</p>
        } @else {
          <p class="now">{{ 'Preparando la sesión…' | t }}</p>
        }
      </div>

      @if (player.transition() !== null) {
        <div class="transition">
          <div class="label">
            {{ 'Mezclando' | t }} {{ player.outgoing()?.title }} → <strong>{{ store.next()?.track?.title }}</strong>
            <span class="tag">{{ store.next()?.strategy_label ?? ('mezcla' | t) }}</span>
            <dj-decided [d]="store.next()?.decided" />
          </div>
          <div class="progress"><span [style.width.%]="(player.transition() ?? 0) * 100"></span></div>
        </div>
      } @else if (store.next(); as n) {
        <p class="next">
          <span class="muted">{{ 'Después:' | t }}</span> <strong>{{ n.track?.title }}</strong>
          <span class="muted"> · {{ n.strategy_label }}</span>
          <dj-decided [d]="n.decided" />
          @if (player.secondsToMix() !== null) { <span class="muted mono"> · {{ 'en {n} s' | t: { n: player.secondsToMix() } }}</span> }
          @if (n.reason) { <br /><span class="muted reason">«{{ n.reason }}»</span> }
        </p>
      }

      <div class="controls">
        <button class="primary" (click)="player.togglePause()">{{ (player.paused() ? 'Reanudar' : 'Pausa') | t }}</button>
        <button (click)="mixNow()" [disabled]="!np() || player.transition() !== null">{{ 'Mezclar ya' | t }}</button>
      </div>
    </section>
  `,
  styles: `
    :host { display: block; }
    section { display: flex; flex-direction: column; gap: 12px; padding: 12px 18px 16px; }
    .stage { height: clamp(250px, 34vh, 340px); }
    .text { min-width: 0; }
    .now { font-size: clamp(18px, 2.2vw, 24px); margin: 0 0 8px; overflow-wrap: anywhere; letter-spacing: -0.01em; }
    .now strong { font-weight: 650; }
    .meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0; font-size: 13px; }
    .meta a { color: var(--muted); font-family: var(--sans); margin-left: 4px; }
    .transition .label { margin-bottom: 6px; }
    .transition .tag { margin-left: 6px; vertical-align: 1px; }
    .progress { height: 6px; background: var(--line); border-radius: 6px; overflow: hidden; }
    .progress span { display: block; height: 100%; background: linear-gradient(90deg, var(--deck-a), var(--deck-b)); }
    .next { margin: 0; }
    dj-decided { margin-left: 6px; }
    .reason { font-size: 13px; }
    .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  `,
})
export class NowPlayingComponent {
  readonly store = inject(SessionStore);
  readonly player = inject(PlayerService);
  readonly np = this.store.nowPlaying;
  readonly waiting = computed(() => this.store.state()?.waiting_first ?? null);
  readonly bpm = computed(() => {
    const t = this.np();
    return t?.bpm ? (t.bpm * (t.rate ?? 1)).toFixed(1) : '—';
  });

  mixNow(): void {
    this.store.send({ type: 'mix_now' });
  }
}
