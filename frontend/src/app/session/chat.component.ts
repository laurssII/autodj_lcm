import { Component, ElementRef, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../core/api.service';
import { I18n } from '../core/i18n.service';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';

/** Conversación con la IA. Acepta texto y pistas adjuntas ("pínchame esta"). */
@Component({
  selector: 'dj-chat',
  imports: [FormsModule, TPipe],
  template: `
    <section class="panel">
      <h2>{{ 'Chat' | t }}</h2>
      <div class="log" #log aria-live="polite">
        @for (m of store.chat(); track m.id) {
          <p class="msg" [class.user]="m.role === 'user'" [class.limit]="m.via === 'límite'">{{ m.text }}@if (m.role === 'dj' && m.via) {<span class="via">{{ (m.via === 'reserva' ? 'sin IA' : m.via === 'límite' ? 'límite de mensajes' : '✦ ' + m.via) | t }}</span>}</p>
        } @empty {
          <p class="hint muted">{{ 'Pídeme lo que quieras: «ponme algo para saltar», «algo más oscuro», «¿quién canta esto?»… o adjunta una pista con el botón +.' | t }}</p>
        }
        @if (store.awaitingReply()) {
          <p class="msg typing" [attr.aria-label]="'La IA está escribiendo' | t"><span></span><span></span><span></span></p>
        }
      </div>
      <form (ngSubmit)="send()">
        <label class="sr-only" for="chat-input">{{ 'Mensaje para AutoDJ' | t }}</label>
        <input id="chat-input" type="text" name="msg" [(ngModel)]="text" autocomplete="off"
               [placeholder]="uploading() ?? ('Escribe aquí tus sugerencias…' | t)" [disabled]="!!uploading()" />
        <button type="button" class="attach" (click)="file.click()" [disabled]="!!uploading()"
                [attr.aria-label]="'Adjuntar una pista' | t" [attr.title]="'Adjuntar una pista' | t">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        </button>
        <input #file type="file" accept="audio/*" class="sr-only" (change)="attach(file)" />
      </form>
    </section>
  `,
  styles: `
    :host { display: block; }
    section {
      height: 100%; display: flex; flex-direction: column; padding: 16px 18px;
    }
    h2 { margin: 0 0 8px; font-size: 18px; font-weight: 650; }
    .log { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 8px; padding: 4px 2px 10px; }
    .msg {
      margin: 0; max-width: 80%; padding: 8px 14px; border-radius: 16px; align-self: flex-start;
      background: var(--accent-soft); border: 1px solid var(--accent); white-space: pre-wrap; border-bottom-left-radius: 4px;
    }
    .msg.user { align-self: flex-end; background: var(--field); border-color: var(--line-strong); border-radius: 16px 16px 4px 16px; }
    .hint { margin: auto 0; text-align: center; font-size: 14px; }
    .via { display: block; margin-top: 4px; font-size: 11px; color: var(--muted); }
    .msg.limit { border-color: var(--warn); background: transparent; }
    .typing { display: flex; gap: 4px; }
    .typing span { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); animation: blink 1.2s infinite; }
    .typing span:nth-child(2) { animation-delay: 0.2s; }
    .typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes blink { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }
    form { display: flex; gap: 10px; }
    .attach { width: 46px; flex: none; display: grid; place-items: center; padding: 0; }
    .attach svg { width: 22px; height: 22px; stroke: currentColor; stroke-width: 2; fill: none; stroke-linecap: round; }
  `,
})
export class ChatComponent {
  readonly store = inject(SessionStore);
  private api = inject(ApiService);
  private i18n = inject(I18n);
  private logEl = viewChild<ElementRef<HTMLElement>>('log');

  text = '';
  readonly uploading = signal<string | null>(null);

  constructor() {
    effect(() => {
      this.store.chat();
      this.store.awaitingReply();
      const el = this.logEl()?.nativeElement;
      if (el) queueMicrotask(() => (el.scrollTop = el.scrollHeight));
    });
  }

  send(): void {
    const text = this.text.trim();
    if (!text) return;
    this.store.send({ type: 'chat', text });
    this.text = '';
  }

  async attach(input: HTMLInputElement): Promise<void> {
    const f = input.files?.[0];
    input.value = '';
    if (!f) return;
    this.uploading.set(this.i18n.t('Subiendo «{name}»…', { name: f.name }));
    try {
      const t = await this.api.upload(f);
      this.store.send({ type: 'chat', text: this.text.trim(), track_id: t.id });
      this.text = '';
    } catch (e) {
      this.store.lastError.set(String((e as Error).message ?? e));
    } finally {
      this.uploading.set(null);
    }
  }
}
