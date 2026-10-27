import { Component, inject } from '@angular/core';
import { I18n } from '../core/i18n.service';

/** Selector de idioma de la interfaz: ES / EN. */
@Component({
  selector: 'dj-lang-toggle',
  template: `
    <div class="lang" role="group" [attr.aria-label]="i18n.t('Idioma')">
      <button type="button" [class.on]="i18n.lang() === 'es'" [attr.aria-pressed]="i18n.lang() === 'es'"
              (click)="i18n.set('es')" lang="es" title="Español">ES</button>
      <button type="button" [class.on]="i18n.lang() === 'en'" [attr.aria-pressed]="i18n.lang() === 'en'"
              (click)="i18n.set('en')" lang="en" title="English">EN</button>
    </div>
  `,
  styles: `
    :host { display: inline-block; }
    .lang { display: inline-flex; padding: 2px; border: 1px solid var(--line); border-radius: 999px; background: var(--field); }
    button { border: 0; background: transparent; color: var(--muted); padding: 6px 10px; border-radius: 999px;
      font-size: 12.5px; font-weight: 650; letter-spacing: 0.04em; cursor: pointer; }
    button.on { background: var(--panel, var(--bg)); color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,.18); }
  `,
})
export class LangToggleComponent {
  readonly i18n = inject(I18n);
}
