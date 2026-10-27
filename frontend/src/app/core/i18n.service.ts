import { Injectable, effect, signal } from '@angular/core';
import { EN } from './i18n.en';

export type Lang = 'es' | 'en';
export type Params = Record<string, string | number | null | undefined>;

const KEY = 'autodj-lang';

function initial(): Lang {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'es' || v === 'en') return v;
  } catch {
    /* almacenamiento bloqueado: se usa el idioma del navegador */
  }
  return typeof navigator !== 'undefined' && navigator.language?.toLowerCase().startsWith('en') ? 'en' : 'es';
}

/**
 * Idioma de la interfaz. El texto original está en español y hace de clave: en español se muestra tal cual,
 * en inglés se busca en `EN` y, si no está, se deja el original. Los `{marcadores}` se rellenan con `params`.
 */
@Injectable({ providedIn: 'root' })
export class I18n {
  readonly lang = signal<Lang>(initial());

  constructor() {
    effect(() => {
      document.documentElement.lang = this.lang();
    });
  }

  set(lang: Lang): void {
    this.lang.set(lang);
    try {
      localStorage.setItem(KEY, lang);
    } catch {
      /* sin almacenamiento: vale solo para esta visita */
    }
  }

  toggle(): void {
    this.set(this.lang() === 'en' ? 'es' : 'en');
  }

  t(text: string, params?: Params): string {
    const out = this.lang() === 'en' ? (EN[text] ?? text) : text;
    return params ? out.replace(/\{(\w+)\}/g, (m, k: string) => (k in params ? String(params[k] ?? '') : m)) : out;
  }
}
