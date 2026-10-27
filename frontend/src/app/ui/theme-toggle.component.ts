import { Component, inject } from '@angular/core';
import { I18n } from '../core/i18n.service';
import { ThemeService } from '../core/theme.service';

/** Interruptor de tema claro/oscuro. */
@Component({
  selector: 'dj-theme-toggle',
  template: `
    <button type="button" class="toggle" (click)="theme.toggle()"
            [attr.aria-label]="i18n.t(theme.theme() === 'dark' ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro')"
            [attr.title]="i18n.t(theme.theme() === 'dark' ? 'Tema claro' : 'Tema oscuro')">
      @if (theme.theme() === 'dark') {
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="4.2" />
          <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7" />
        </svg>
      } @else {
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z" /></svg>
      }
    </button>
  `,
  styles: `
    :host { display: inline-block; }
    .toggle { width: 38px; height: 38px; padding: 0; display: grid; place-items: center; border-radius: 50%; }
    svg { width: 19px; height: 19px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round;
      stroke-linejoin: round; }
  `,
})
export class ThemeToggleComponent {
  readonly theme = inject(ThemeService);
  readonly i18n = inject(I18n);
}
