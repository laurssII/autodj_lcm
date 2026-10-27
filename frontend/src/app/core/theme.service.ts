import { Injectable, effect, signal } from '@angular/core';

export type Theme = 'light' | 'dark';

const KEY = 'autodj-theme';

function saved(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : null;
  } catch {
    return null; // almacenamiento bloqueado: se sigue al sistema
  }
}

const system = (): Theme =>
  typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

/**
 * Tema claro/oscuro. Sin elección guardada sigue al sistema; la elección del usuario manda.
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  private chosen = saved();
  readonly theme = signal<Theme>(this.chosen ?? system());

  constructor() {
    effect(() => {
      const t = this.theme();
      document.documentElement.dataset['theme'] = t;
      document.querySelector('meta[name="theme-color"]')?.setAttribute('content', t === 'dark' ? '#0d0d12' : '#f2efe9');
    });
    if (typeof matchMedia === 'function') {
      matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (!this.chosen) this.theme.set(system());
      });
    }
  }

  toggle(): void {
    const next: Theme = this.theme() === 'dark' ? 'light' : 'dark';
    this.chosen = next;
    this.theme.set(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* sin almacenamiento: vale solo para esta visita */
    }
  }
}
