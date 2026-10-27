import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../core/api.service';
import { I18n } from '../core/i18n.service';
import { PlayerService } from '../core/player.service';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';
import { LangToggleComponent } from '../ui/lang-toggle.component';
import { LogoComponent } from '../ui/logo.component';
import { ThemeToggleComponent } from '../ui/theme-toggle.component';

/** Pantalla de inicio: reproducción autónoma, adjuntar canción o escribir una sugerencia. */
@Component({
  selector: 'dj-start',
  imports: [FormsModule, LogoComponent, ThemeToggleComponent, LangToggleComponent, TPipe],
  template: `
    <div class="tools"><dj-lang-toggle /><dj-theme-toggle /></div>
    <main>
      <header>
        <dj-logo class="logo" />
        <h1>{{ 'Bienvenido a AutoDJ' | t }}</h1>
        <p class="muted">{{ 'Selecciona una opción para comenzar' | t }}</p>
      </header>

      <div class="options">
        <button class="option" (click)="autonomous()" [disabled]="busy()">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l12 8-12 8z" /></svg>
          <span>{{ 'Reproducción autónoma' | t }}</span>
        </button>
        <button class="option" (click)="file.click()" [disabled]="busy()">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20V5M6 11l6-6 6 6" /></svg>
          <span>{{ 'Adjuntar canción' | t }}</span>
        </button>
        <input #file type="file" accept="audio/*" class="sr-only" (change)="attach(file)" />
      </div>

      @if (status()) {
        <p class="status" role="status">{{ status() }}</p>
      }

      <form class="suggest" (ngSubmit)="suggest()">
        <label class="sr-only" for="suggestion">{{ 'Sugerencia para empezar' | t }}</label>
        <input id="suggestion" type="text" name="suggestion" [(ngModel)]="text"
               [placeholder]="i18n.t('Escribe alguna sugerencia…')" autocomplete="off" [disabled]="busy()" />
      </form>
    </main>
  `,
  styles: `
    :host { display: grid; place-items: center; min-height: 100vh; padding: 24px; }
    .tools { position: absolute; top: 18px; right: 22px; display: flex; align-items: center; gap: 10px; }
    main { width: min(720px, 100%); display: flex; flex-direction: column; align-items: center; gap: 40px; }
    header { text-align: center; }
    .logo { margin-bottom: 14px; --logo-size: 52px; }
    h1 { font-size: clamp(30px, 5vw, 46px); margin: 0 0 6px; letter-spacing: -0.03em; font-weight: 700; }
    header p { margin: 0; font-size: 18px; }
    .options { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; width: 100%; }
    .option {
      display: flex; align-items: center; gap: 18px; padding: 26px 24px;
      font-size: 19px; font-weight: 550; text-align: left; border-radius: 20px;
      background: var(--panel); box-shadow: var(--shadow);
      -webkit-backdrop-filter: blur(16px) saturate(1.3); backdrop-filter: blur(16px) saturate(1.3);
      border-color: var(--line);
    }
    .option:hover:not(:disabled) { transform: translateY(-2px); }
    .option svg { width: 34px; height: 34px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.8;
      stroke-linejoin: round; stroke-linecap: round; }
    .option:first-child svg { stroke: var(--deck-a); }
    .option:nth-child(2) svg { stroke: var(--deck-b); }
    .status { margin: -16px 0 0; color: var(--muted); }
    .suggest { width: min(560px, 100%); margin-top: 48px; }
    .suggest input { padding: 14px 22px; box-shadow: var(--shadow); }
    @media (max-width: 560px) { .options { grid-template-columns: 1fr; } }
  `,
})
export class StartComponent {
  private store = inject(SessionStore);
  private player = inject(PlayerService);
  private api = inject(ApiService);
  readonly i18n = inject(I18n);

  text = '';
  readonly busy = signal(false);
  readonly status = signal<string | null>(null);

  autonomous(): void {
    this.player.begin();
    this.store.send({ type: 'start', mode: 'auto' });
  }

  suggest(): void {
    const message = this.text.trim();
    if (!message) return;
    this.player.begin();
    this.store.send({ type: 'start', mode: 'auto', message });
  }

  async attach(input: HTMLInputElement): Promise<void> {
    const f = input.files?.[0];
    input.value = '';
    if (!f) return;
    this.player.unlock(); // el gesto del usuario permite crear el audio ahora
    this.busy.set(true);
    this.status.set(this.i18n.t('Subiendo «{name}»…', { name: f.name }));
    try {
      const t = await this.api.upload(f);
      this.store.send({ type: 'start', mode: 'upload', track_id: t.id });
      this.player.begin();
    } catch (e) {
      this.status.set(String((e as Error).message ?? e));
    } finally {
      this.busy.set(false);
    }
  }
}
