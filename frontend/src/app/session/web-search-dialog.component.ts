import { Component, ElementRef, afterNextRender, computed, inject, output, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../core/api.service';
import type { MainstreamResult } from '../core/models';
import { I18n, type Params } from '../core/i18n.service';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';

/**
 * Buscador de música en la web (SoundCloud). Va aparte de la biblioteca: lista las coincidencias
 * reproducibles y tú eliges cuáles añadir a la cola. Nada entra en la biblioteca hasta que lo pides.
 */
@Component({
  selector: 'dj-web-search-dialog',
  imports: [FormsModule, TPipe],
  template: `
    <div class="backdrop" (click)="closed.emit()"></div>

    <div
      class="dialog panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ws-title"
      (keydown.escape)="closed.emit()"
    >
      <header>
        <h2 id="ws-title">{{ 'Buscar en la web' | t }}</h2>
        <button class="ghost" (click)="closed.emit()" [attr.aria-label]="'Cerrar' | t">✕</button>
      </header>

      <form class="search" (submit)="$event.preventDefault(); search()">
        <input
          #box
          type="search"
          name="q"
          [(ngModel)]="query"
          [placeholder]="'Canción o artista (ej: As It Was - Harry Styles)' | t"
          [attr.aria-label]="'Buscar música en la web' | t"
        />
        <button type="submit" class="primary" [disabled]="searching() || query.trim().length < 2">
          {{ (searching() ? 'Buscando…' : 'Buscar') | t }}
        </button>
      </form>

      <p class="status" [class.err]="!!error()" role="status">{{ status() }}</p>

      @if (results(); as list) {
        <ul class="results" [attr.aria-label]="'Resultados de la búsqueda' | t">
          @for (r of list; track r.id) {
            <li>
              @if (r.thumbnail) {
                <img class="thumb" [src]="r.thumbnail" alt="" width="44" height="44" loading="lazy" />
              } @else {
                <span class="thumb ph" aria-hidden="true">♪</span>
              }
              <div class="info">
                <span class="title">{{ r.title }}</span>
                <span class="meta muted">
                  {{ r.artist }}
                  @if (r.duration) {
                    · {{ fmt(r.duration) }}
                  }
                  ·
                  <a [href]="r.url" target="_blank" rel="noopener">SoundCloud</a>
                </span>
              </div>
              <button
                type="button"
                (click)="add(r)"
                [disabled]="adding().has(r.id) || queued().has(r.id)"
              >
                {{ (queued().has(r.id) ? 'En cola' : adding().has(r.id) ? 'Añadiendo…' : 'Añadir a la cola') | t }}
              </button>
            </li>
          }
        </ul>
      }
    </div>
  `,
  styles: `
    .backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, .4);
      z-index: 40;
      -webkit-backdrop-filter: blur(3px);
      backdrop-filter: blur(3px);
    }

    .dialog {
      position: fixed;
      z-index: 41;
      top: 5vh;
      left: 50%;
      transform: translateX(-50%);
      width: min(720px, 94vw);
      max-height: 90vh;
      display: flex;
      flex-direction: column;
      padding: 18px;
      gap: 10px;
      background: var(--bg);
      border-color: var(--line-strong);
    }

    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    h2 {
      margin: 0;
      font-size: 20px;
    }

    .search {
      display: flex;
      gap: 10px;
    }

    .search input {
      flex: 1;
      min-width: 0;
    }

    .status {
      margin: 0;
      font-size: 13px;
      min-height: 1.4em;
      color: var(--muted);
    }

    .status.err {
      color: var(--err);
    }

    /* Filas compactas: las 8 coincidencias caben a la vez en una pantalla normal */
    .results {
      list-style: none;
      margin: 0;
      padding: 0;
      min-height: 0;
      overflow: auto;
      display: flex;
      flex-direction: column;
    }

    li {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 6px 4px;
      border-bottom: 1px solid var(--line);
    }

    .thumb {
      flex: none;
      width: 44px;
      height: 44px;
      border-radius: 6px;
      object-fit: cover;
    }

    .thumb.ph {
      display: grid;
      place-items: center;
      background: var(--field);
      color: var(--muted);
      border: 1px solid var(--line);
    }

    .info {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
    }

    .title,
    .meta {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .title {
      font-weight: 550;
    }

    .meta {
      font-size: 13px;
    }

    .meta a {
      color: inherit;
    }

    li button {
      flex: none;
      padding: 5px 12px;
      white-space: nowrap;
    }
  `,
})
export class WebSearchDialogComponent {
  private api = inject(ApiService);
  private i18n = inject(I18n);
  private store = inject(SessionStore);
  private box = viewChild<ElementRef<HTMLInputElement>>('box');

  readonly closed = output<void>();

  query = '';

  readonly searching = signal(false);
  /** null = todavía no se ha buscado. */
  readonly results = signal<MainstreamResult[] | null>(null);
  readonly error = signal<string | null>(null);
  readonly adding = signal<ReadonlySet<string>>(new Set());

  readonly queued = computed(() => new Set(this.store.queue().map((t) => t.id)));

  readonly status = computed(() => {
    const t = (s: string, p?: Params): string => this.i18n.t(s, p);
    if (this.searching()) return t('Buscando en SoundCloud… puede tardar unos segundos.');
    const err = this.error();
    if (err) return err;
    const list = this.results();
    if (list === null) {
      return t('Solo se muestran pistas que se pueden reproducir (se descartan las protegidas con DRM).');
    }
    return list.length
      ? t(list.length === 1 ? '{n} pista reproducible' : '{n} pistas reproducibles', { n: list.length })
      : t('No hay coincidencias que se puedan reproducir. Prueba con otro nombre.');
  });

  constructor() {
    afterNextRender(() => this.box()?.nativeElement.focus());
  }

  fmt(seconds: number): string {
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  /** Busca coincidencias. No añade nada a la biblioteca ni a la cola. */
  async search(): Promise<void> {
    const term = this.query.trim();
    if (term.length < 2 || this.searching()) return;

    this.searching.set(true);
    this.error.set(null);
    this.results.set(null);
    try {
      this.results.set(await this.api.searchMainstream(term));
    } catch (e) {
      this.error.set(String((e as Error).message ?? e));
    } finally {
      this.searching.set(false);
    }
  }

  /** Mete la coincidencia elegida en la biblioteca y la pone en la cola. */
  async add(r: MainstreamResult): Promise<void> {
    this.adding.update((s) => new Set(s).add(r.id));
    this.error.set(null);
    try {
      const track = await this.api.addMainstream(r.url);
      this.store.send({ type: 'queue_add', track_id: track.id });
    } catch (e) {
      this.error.set(String((e as Error).message ?? e));
    } finally {
      this.adding.update((s) => {
        const next = new Set(s);
        next.delete(r.id);
        return next;
      });
    }
  }
}
