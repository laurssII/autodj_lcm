import { Component, OnInit, computed, inject, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../core/api.service';
import type { TrackSummary } from '../core/models';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';

/** Biblioteca virtual: catálogo libre, pistas adjuntas y lo que ya has traído de la web. */
@Component({
  selector: 'dj-library-dialog',
  imports: [FormsModule, TPipe],
  template: `
    <div class="backdrop" (click)="closed.emit()"></div>

    <div
      class="dialog panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="lib-title"
      (keydown.escape)="closed.emit()"
    >
      <header>
        <h2 id="lib-title">{{ 'Biblioteca virtual' | t }}</h2>
        <button class="ghost" (click)="closed.emit()" [attr.aria-label]="'Cerrar' | t">
          ✕
        </button>
      </header>

      <div class="divider"></div>

      <input
        type="search"
        [(ngModel)]="q"
        (ngModelChange)="query.set($event)"
        [placeholder]="'Busca por título, artista o género' | t"
        [attr.aria-label]="'Buscar en la biblioteca' | t"
        autofocus
      />

      <p class="count muted">
        {{ '{shown} de {total} pistas' | t: { shown: filtered().length, total: all().length } }}
        @if (analyzing()) {
          · {{ '{n} analizándose' | t: { n: analyzing() } }}
        }
      </p>

      <ul class="library">
        @for (t of filtered(); track t.id) {
          <li>
            <div class="info">
              <span class="title">{{ t.title }}</span>
              <span class="meta muted">
                {{ t.artist || ('Artista desconocido' | t) }}
                @if (t.genre) {
                  · {{ t.genre }}
                }
                @if (t.credit_url) {
                  ·
                  <a [href]="t.credit_url" target="_blank" rel="noopener">
                    {{ t.source }}
                  </a>
                } @else {
                  · {{ t.source === 'local' ? ('tu pista' | t) : t.source }}
                }
              </span>
            </div>

            <span class="mono tags">
              @switch (t.status) {
                @case ('ready') {
                  <span class="tag">{{ t.bpm }}</span>
                  <span class="tag">{{ t.camelot }}</span>
                }
                @case ('needs_review') {
                  <span class="tag">{{ t.bpm }}</span>
                  <span class="tag">{{ t.camelot }}?</span>
                }
                @case ('error') {
                  <span class="tag err">{{ 'error' | t }}</span>
                }
                @default {
                  <span class="tag">{{ 'analizando' | t }}</span>
                }
              }
            </span>

            <button
              (click)="add(t)"
              [disabled]="t.status === 'error' || queued().has(t.id)"
            >
              {{ (queued().has(t.id) ? 'En cola' : 'Añadir') | t }}
            </button>
          </li>
        } @empty {
          <li class="muted">{{ 'No hay pistas que coincidan.' | t }}</li>
        }
      </ul>
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
      top: 6vh;
      left: 50%;
      transform: translateX(-50%);
      width: min(760px, 94vw);
      max-height: 88vh;
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

    .count {
      margin: 0;
      font-size: 13px;
    }

    .divider {
      height: 1px;
      background: var(--line);
      margin: 6px 0;
    }

    ul {
      list-style: none;
      margin: 0;
      padding: 0;
      overflow: auto;
      display: flex;
      flex-direction: column;
    }

    li {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 4px;
      border-bottom: 1px solid var(--line);
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

    .tags {
      display: flex;
      gap: 4px;
      flex: none;
    }

    .tag.err {
      color: var(--err);
      border-color: var(--err);
    }

    li button {
      flex: none;
      padding: 5px 12px;
    }
  `,
})
export class LibraryDialogComponent implements OnInit {
  private api = inject(ApiService);
  private store = inject(SessionStore);

  readonly closed = output<void>();

  q = '';

  readonly query = signal('');

  private loaded = signal<TrackSummary[]>([]);

  readonly all = computed(() => {
    const live = this.store.library();
    const base = new Map(
      this.loaded().map((t) => [t.id, t])
    );

    for (const [id, t] of live) {
      base.set(id, t);
    }

    return [...base.values()];
  });

  readonly filtered = computed(() => {
    const q = this.query().toLowerCase().trim();

    const list = q
      ? this.all().filter((t) =>
          `${t.title} ${t.artist} ${t.genre ?? ''}`
            .toLowerCase()
            .includes(q)
        )
      : this.all();

    return list.slice(0, 300);
  });

  readonly analyzing = computed(() =>
    this.all().filter(
      (t) => t.status === 'pending' || t.status === 'analyzing'
    ).length
  );

  readonly queued = computed(
    () => new Set(this.store.queue().map((t) => t.id))
  );

  async ngOnInit(): Promise<void> {
    try {
      this.loaded.set(await this.api.tracks());
    } catch (e) {
      this.store.lastError.set(
        String((e as Error).message ?? e)
      );
    }
  }

  add(t: TrackSummary): void {
    this.store.send({
      type: 'queue_add',
      track_id: t.id,
    });
  }
}
