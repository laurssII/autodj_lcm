import { CdkDrag, CdkDragDrop, CdkDragHandle, CdkDropList, moveItemInArray } from '@angular/cdk/drag-drop';
import { Component, DestroyRef, inject, output, signal } from '@angular/core';
import { I18n } from '../core/i18n.service';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';
import { DecidedBadgeComponent } from '../ui/decided-badge.component';

type View = 'queue' | 'history';
const VIEW_KEY = 'autodj.queueView';

/**
 * Cola del usuario: solo las canciones que él ha pedido. Se reordena arrastrando; cuando una
 * entra en la mezcla sale de la cola y ya no se puede mover. Con el selector de arriba se alterna
 * con el historial: lo que ya ha sonado en la sesión, lo más reciente primero.
 */
@Component({
  selector: 'dj-queue',
  imports: [CdkDropList, CdkDrag, CdkDragHandle, DecidedBadgeComponent, TPipe],
  template: `
    <section class="panel">
      <div class="tabs" role="tablist" [attr.aria-label]="'Cola o historial' | t">
        <button role="tab" class="tab" [class.on]="view() === 'queue'" [attr.aria-selected]="view() === 'queue'"
                (click)="show('queue')">
          {{ 'Cola' | t }} @if (store.queue().length) { <span class="count">{{ store.queue().length }}</span> }
        </button>
        <button role="tab" class="tab" [class.on]="view() === 'history'" [attr.aria-selected]="view() === 'history'"
                (click)="show('history')">
          {{ 'Historial' | t }} @if (store.history().length) { <span class="count">{{ store.history().length }}</span> }
        </button>
      </div>
      @if (view() === 'history') {
        @if (store.history().length === 0) {
          <p class="empty muted">{{ 'Aquí aparecerán las canciones que vayan sonando.' | t }}</p>
        } @else {
          <ol class="history" [attr.aria-label]="'Canciones que han sonado' | t">
            @for (h of store.history(); track h.played_at + h.id) {
              <li>
                <span class="info">
                  <span class="title">{{ h.title }}</span>
                  <span class="meta muted">
                    {{ h.artist || ('Artista desconocido' | t) }} · {{ ago(h.played_at) }}
                    @if (h.via) { · {{ h.via }} }
                    <dj-decided [d]="h.decided" />
                  </span>
                </span>
                <button class="ghost" (click)="requeue(h.id)" [attr.aria-label]="'Volver a poner «{title}»' | t: { title: h.title }"
                        [attr.title]="'Volver a ponerla en la cola' | t">＋</button>
              </li>
            }
          </ol>
        }
      } @else if (store.queue().length === 0) {
        <p class="empty muted">{{ 'Añade aquí canciones para que suenen a continuación…' | t }}</p>
      } @else {
        <ol cdkDropList (cdkDropListDropped)="drop($event)" [attr.aria-label]="'Canciones en cola' | t">
          @for (t of store.queue(); track t.id; let i = $index) {
            <li cdkDrag cdkDragLockAxis="y" [class.next]="i === 0 && store.next()?.from_queue">
              <span class="handle" cdkDragHandle [attr.aria-label]="'Arrastrar para reordenar' | t" [attr.title]="'Arrastrar' | t">⠿</span>
              <span class="info">
                <span class="title">{{ t.title }}</span>
                <span class="meta muted">
                  {{ t.artist || ('Artista desconocido' | t) }}
                  @if (t.status === 'pending' || t.status === 'analyzing') { · {{ 'analizando…' | t }} }
                  @if (t.status === 'error') { · {{ 'no se pudo analizar' | t }} }
                </span>
              </span>
              <button class="ghost" (click)="remove(t.id)" [attr.aria-label]="'Quitar «{title}»' | t: { title: t.title }">✕</button>
            </li>
          }
        </ol>
      }
      <div class="actions">
        <button (click)="openLibrary.emit()">{{ 'Biblioteca virtual' | t }}</button>
        <button (click)="openWebSearch.emit()">{{ 'Buscar en la web' | t }}</button>
      </div>
    </section>
  `,
  styles: `
    :host { display: block; }
    section { height: 100%; display: flex; flex-direction: column; padding: 16px 18px; }
    .tabs { display: flex; gap: 4px; margin: 0 0 12px; padding: 3px; border: 1px solid var(--line);
      border-radius: 12px; background: var(--field); }
    .tab { flex: 1; padding: 7px 8px; border: 0; border-radius: 9px; background: transparent; color: var(--muted);
      font-size: 15px; font-weight: 600; cursor: pointer; }
    .tab.on { background: var(--panel, var(--bg)); color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,.18); }
    .count { margin-left: 4px; font-size: 12px; font-weight: 600; opacity: .7; }
    .empty { margin: 8px 4px; font-size: 14px; }
    ol { list-style: none; margin: 0; padding: 0; overflow: auto; flex: 1; display: flex; flex-direction: column; gap: 8px; }
    li {
      display: flex; align-items: center; gap: 10px; padding: 8px 6px 8px 10px;
      border: 1px solid var(--line); border-radius: 12px; background: var(--field);
    }
    li.next { border-color: var(--accent); }
    .handle { cursor: grab; color: var(--muted); font-size: 18px; user-select: none; }
    .info { flex: 1; min-width: 0; display: flex; flex-direction: column; }
    .title, .meta { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .title { font-weight: 550; }
    .meta { font-size: 13px; }
    .cdk-drag-preview { box-shadow: 0 10px 30px rgba(0,0,0,.28); border-radius: 12px; background: var(--field); }
    .cdk-drag-placeholder { opacity: 0.3; }
    .cdk-drag-animating, ol.cdk-drop-list-dragging li:not(.cdk-drag-placeholder) { transition: transform 200ms ease; }
    .actions { margin-top: auto; display: flex; flex-wrap: wrap; gap: 8px; }
    .actions button { flex: 1 1 130px; padding: 10px 8px; font-weight: 550; white-space: nowrap; }
    ol + .actions { margin-top: 14px; }
  `,
})
export class QueueComponent {
  readonly store = inject(SessionStore);
  private i18n = inject(I18n);
  readonly openLibrary = output<void>();
  readonly openWebSearch = output<void>();
  readonly view = signal<View>(this.savedView());
  /** Reloj para «hace X min»: se refresca cada medio minuto. */
  private readonly now = signal(Date.now());

  constructor() {
    const id = setInterval(() => this.now.set(Date.now()), 30_000);
    inject(DestroyRef).onDestroy(() => clearInterval(id));
  }

  show(v: View): void {
    this.view.set(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      /* sin almacenamiento: la elección solo dura mientras la página esté abierta */
    }
  }

  private savedView(): View {
    try {
      return localStorage.getItem(VIEW_KEY) === 'history' ? 'history' : 'queue';
    } catch {
      return 'queue';
    }
  }

  /** «ahora», «hace 5 min», «hace 2 h»… */
  ago(playedAt: number): string {
    const min = Math.max(0, Math.floor((this.now() - playedAt * 1000) / 60_000));
    if (min < 1) return this.i18n.t('ahora mismo');
    if (min < 60) return this.i18n.t('hace {n} min', { n: min });
    const h = Math.floor(min / 60);
    return h < 24 ? this.i18n.t('hace {n} h', { n: h }) : this.i18n.t('hace {n} d', { n: Math.floor(h / 24) });
  }

  requeue(id: string): void {
    this.store.send({ type: 'queue_add', track_id: id });
  }

  drop(ev: CdkDragDrop<unknown>): void {
    const queue = [...this.store.queue()];
    moveItemInArray(queue, ev.previousIndex, ev.currentIndex);
    // Actualización optimista: el servidor confirmará con el nuevo estado.
    this.store.state.update((s) => (s ? { ...s, queue } : s));
    this.store.send({ type: 'queue_reorder', ids: queue.map((t) => t.id) });
  }

  remove(id: string): void {
    this.store.send({ type: 'queue_remove', track_id: id });
  }
}
