import { DatePipe, JsonPipe } from '@angular/common';
import { Component, ElementRef, effect, inject, viewChild } from '@angular/core';
import { I18n } from '../core/i18n.service';
import type { LogEntry } from '../core/models';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';

const KIND_LABEL: Record<LogEntry['kind'], string> = {
  decision: 'decisión', plan: 'mezcla', tool: 'herramienta', info: 'info', error: 'error', chat: 'chat',
};

interface Cand { id: string; title: string; artist: string; bpm: number; camelot: string; score: number; strategies: string[] }

/** Registro cronológico de las decisiones de la IA y de sus pasos. */
@Component({
  selector: 'dj-observability',
  imports: [DatePipe, JsonPipe, TPipe],
  template: `
    <section class="panel">
      <h2>{{ 'Observabilidad' | t }}</h2>
      <div class="list" #list>
        @for (e of store.logs(); track e.id) {
          <details [class]="e.kind">
            <summary>
              <span class="time mono">{{ e.ts * 1000 | date: 'HH:mm:ss' }}</span>
              <span class="kind">{{ label(e.kind) }}</span>
              <span class="title">{{ e.title }}</span>
            </summary>
            @if (e.detail['reason']) { <p class="reason">«{{ e.detail['reason'] }}»</p> }
            @if (e.kind === 'decision' && e.detail['by']) {
              <p class="who">
                @switch (e.detail['by']) {
                  @case ('ia') { {{ '✦ Decidido por' | t }} <strong>{{ providerOf(e) }}</strong>@if (e.detail['latency_ms']) { {{ 'en {n} s' | t: { n: secs(e) } }} } }
                  @case ('reserva') { {{ 'Modo reserva:' | t }} {{ e.detail['why_fallback'] || ('la IA no respondió' | t) }} }
                  @case ('única') { {{ 'Solo había una opción viable' | t }} }
                }
              </p>
            }
            @if (optionsOf(e); as opts) {
              <ul class="opts mono">
                @for (o of opts; track $index) {
                  <li [class.chosen]="isChosen(e, o)" [class.track]="!o.startsWith('   ')">{{ o.trim() }}</li>
                }
              </ul>
            }
            @if (cands(e); as cs) {
              <table class="mono">
                <thead><tr><th></th><th>{{ 'candidata' | t }}</th><th>BPM</th><th>{{ 'clave' | t }}</th><th>{{ 'punt.' | t }}</th></tr></thead>
                <tbody>
                  @for (c of cs; track c.id) {
                    <tr [class.chosen]="c.id === chosenId(e)">
                      <td>{{ c.id === chosenId(e) ? '✓' : '' }}</td>
                      <td class="name">{{ c.title }}</td><td>{{ c.bpm }}</td><td>{{ c.camelot }}</td>
                      <td>{{ c.score }}</td>
                    </tr>
                  }
                </tbody>
              </table>
            } @else {
              <pre class="mono">{{ e.detail | json }}</pre>
            }
          </details>
        } @empty {
          <p class="muted">{{ 'Aquí aparecerán las decisiones de la IA y cada paso que dé.' | t }}</p>
        }
      </div>
    </section>
  `,
  styles: `
    :host { display: block; }
    section { height: 100%; display: flex; flex-direction: column; padding: 16px; }
    h2 { margin: 0 0 8px; font-size: 18px; font-weight: 650; }
    .list { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
    details { border-left: 3px solid var(--line); padding: 3px 0 3px 8px; }
    details.decision { border-color: var(--accent); }
    details.plan { border-color: var(--ok); }
    details.tool { border-color: var(--warn); }
    details.error { border-color: var(--err); }
    summary { cursor: pointer; display: flex; gap: 8px; align-items: baseline; list-style: none; }
    summary::-webkit-details-marker { display: none; }
    .time { color: var(--muted); font-size: 11.5px; flex: none; }
    .kind { color: var(--muted); flex: none; min-width: 74px; }
    .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    details[open] .title { white-space: normal; }
    .reason { margin: 4px 0; font-style: italic; }
    pre { margin: 4px 0; white-space: pre-wrap; word-break: break-word; font-size: 11.5px; color: var(--muted); max-height: 220px; overflow: auto; }
    table { border-collapse: collapse; font-size: 11.5px; margin: 4px 0; width: 100%; }
    th { text-align: left; color: var(--muted); font-weight: 500; }
    td, th { padding: 1px 6px 1px 0; }
    td.name { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--sans); }
    tr.chosen { color: var(--accent); font-weight: 600; }
    .who { margin: 4px 0; }
    .opts { list-style: none; margin: 4px 0; padding: 0; font-size: 11px; color: var(--muted); max-height: 220px; overflow: auto; }
    .opts li { padding: 1px 0 1px 12px; }
    .opts li.track { padding-left: 0; color: var(--ink); font-family: var(--sans); }
    .opts li.chosen { color: var(--accent); font-weight: 600; }
  `,
})
export class ObservabilityComponent {
  readonly store = inject(SessionStore);
  private i18n = inject(I18n);
  private listEl = viewChild<ElementRef<HTMLElement>>('list');

  constructor() {
    effect(() => {
      this.store.logs();
      const el = this.listEl()?.nativeElement;
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      if (nearBottom) queueMicrotask(() => (el.scrollTop = el.scrollHeight));
    });
  }

  label(k: LogEntry['kind']): string {
    return this.i18n.t(KIND_LABEL[k] ?? k);
  }

  providerOf(e: LogEntry): string {
    const p = String(e.detail['provider'] ?? '');
    return p ? p.replace(':', ' · ') : this.i18n.t('la IA');
  }

  secs(e: LogEntry): string {
    return (Number(e.detail['latency_ms'] ?? 0) / 1000).toFixed(1);
  }

  optionsOf(e: LogEntry): string[] | null {
    const o = e.detail['options'];
    return Array.isArray(o) && o.length ? (o as string[]) : null;
  }

  isChosen(e: LogEntry, line: string): boolean {
    const id = String(e.detail['chosen_option'] ?? '');
    return !!id && line.trim().startsWith(id + ':');
  }

  chosenId(e: LogEntry): string | null {
    return (e.detail['chosen'] as Cand | undefined)?.id ?? null;
  }

  cands(e: LogEntry): Cand[] | null {
    const d = e.detail;
    if (d['candidates']) return d['candidates'] as Cand[];
    if (d['chosen']) return [d['chosen'] as Cand, ...((d['discarded'] as Cand[]) ?? [])];
    return null;
  }
}
