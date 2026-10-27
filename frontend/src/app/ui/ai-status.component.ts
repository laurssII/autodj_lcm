import { Component, computed, inject } from '@angular/core';
import { I18n } from '../core/i18n.service';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';

/** Estado de la cadena de IA en la cabecera: qué proveedores están listos, enfriando o sin clave. */
@Component({
  selector: 'dj-ai-status',
  imports: [TPipe],
  template: `
    <span class="wrap" [class.on]="enabled()" [attr.title]="title()">
      <span class="lead">{{ (enabled() ? 'IA' : 'IA desactivada') | t }}</span>
      @for (p of providers(); track p.key) {
        <span class="p" [class]="stateClass(p.state)">
          <span class="dot"></span>{{ p.label }}@if (p.state === 'enfriando') { <span class="cd">{{ p.cooldown_s }} s</span> }
        </span>
      }
    </span>
  `,
  styles: `
    :host { display: inline-flex; }
    .wrap {
      display: inline-flex; align-items: center; gap: 8px; font-size: 12px; padding: 4px 10px; border-radius: 999px;
      border: 1px solid var(--line-strong); color: var(--muted); cursor: default; flex-wrap: wrap;
    }
    .wrap.on .lead { color: var(--ok); font-weight: 600; }
    .p { display: inline-flex; align-items: center; gap: 4px; }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--line-strong); }
    .p.ready .dot { background: var(--ok); }
    .p.cool .dot { background: var(--warn); }
    .p.nokey { opacity: 0.55; }
    .cd { font-family: var(--mono); font-size: 11px; }
  `,
})
export class AiStatusComponent {
  private store = inject(SessionStore);
  private i18n = inject(I18n);
  readonly enabled = computed(() => this.store.state()?.ai?.enabled ?? this.store.state()?.llm ?? false);
  readonly providers = computed(() => this.store.state()?.ai?.providers ?? []);
  readonly title = computed(() => {
    const ps = this.providers();
    if (!ps.length) return this.i18n.t('Sin proveedores de IA: el DJ pincha en modo reserva.');
    return ps.map((p) => `${p.label} (${p.model}): ${this.i18n.t(p.state)}` + (p.last_error ? ` — ${p.last_error}` : '') +
      (p.ok || p.failed ? ` · ${this.i18n.t('{ok} bien / {failed} fallos', { ok: p.ok, failed: p.failed })}` : '')).join('\n');
  });

  stateClass(s: string): string {
    return s === 'listo' ? 'ready' : s === 'enfriando' ? 'cool' : 'nokey';
  }
}
