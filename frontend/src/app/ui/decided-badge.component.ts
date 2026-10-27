import { Component, computed, inject, input } from '@angular/core';
import { I18n } from '../core/i18n.service';
import type { Decided } from '../core/models';

/** Etiqueta que dice quién decidió una transición: la IA (y cuál), el modo reserva, etc. */
@Component({
  selector: 'dj-decided',
  template: `
    @if (d(); as v) {
      <span class="badge" [class]="v.by" [attr.title]="title()">{{ text() }}</span>
    }
  `,
  styles: `
    :host { display: inline; }
    .badge {
      display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; line-height: 1; white-space: nowrap;
      padding: 3px 7px; border-radius: 999px; border: 1px solid var(--line-strong); color: var(--muted);
      vertical-align: 1px; cursor: default;
    }
    .badge.ia { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
    .badge.reserva { color: var(--warn); border-color: var(--warn); }
  `,
})
export class DecidedBadgeComponent {
  private i18n = inject(I18n);
  readonly d = input<Decided | null | undefined>(null);

  readonly text = computed(() => {
    const v = this.d();
    if (!v) return '';
    switch (v.by) {
      case 'ia': return `✦ IA · ${v.provider || '?'}`;
      case 'reserva': return this.i18n.t('modo reserva');
      case 'única': return this.i18n.t('única opción');
      default: return this.i18n.t('pedida por ti');
    }
  });

  readonly title = computed(() => {
    const v = this.d();
    if (!v) return '';
    if (v.by === 'ia') {
      return this.i18n.t('Decidido por {who}', { who: v.provider + (v.model ? ' (' + v.model + ')' : '') }) +
        (v.latency_ms ? ' ' + this.i18n.t('en {n} s', { n: (v.latency_ms / 1000).toFixed(1) }) : '') +
        (v.option ? ' · ' + this.i18n.t('opción {id}', { id: v.option }) : '');
    }
    if (v.by === 'reserva') {
      return this.i18n.t('La IA no pudo decidir: {why}. Se eligió por sorteo ponderado.',
        { why: v.why || this.i18n.t('sin respuesta') });
    }
    if (v.by === 'única') return this.i18n.t('Solo había una forma viable de hacer esta mezcla: no hacía falta preguntar a la IA.');
    return this.i18n.t('La pediste tú.');
  });
}
