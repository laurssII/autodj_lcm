import { Component, inject, signal } from '@angular/core';
import { PlayerService } from '../core/player.service';
import { SessionStore } from '../core/session-store.service';
import { TPipe } from '../core/t.pipe';
import { AiStatusComponent } from '../ui/ai-status.component';
import { LangToggleComponent } from '../ui/lang-toggle.component';
import { LogoComponent } from '../ui/logo.component';
import { ThemeToggleComponent } from '../ui/theme-toggle.component';
import { ChatComponent } from './chat.component';
import { LibraryDialogComponent } from './library-dialog.component';
import { NowPlayingComponent } from './now-playing.component';
import { ObservabilityComponent } from './observability.component';
import { QueueComponent } from './queue.component';
import { WebSearchDialogComponent } from './web-search-dialog.component';

/** Pantalla de sesión: arriba los dos platos con la mezcla; debajo, cola, chat y observabilidad. */
@Component({
  selector: 'dj-session',
  imports: [QueueComponent, ObservabilityComponent, NowPlayingComponent, ChatComponent, LibraryDialogComponent,
    WebSearchDialogComponent, LogoComponent, ThemeToggleComponent, AiStatusComponent, LangToggleComponent, TPipe],
  template: `
    <div class="page">
      <header>
        <dj-logo />
        <span class="brand">AutoDJ</span>
        <span class="chips">
          @if (store.state()?.style; as style) { <span class="tag">{{ 'estilo' | t }} · {{ style }}</span> }
          <dj-ai-status />
        </span>
        <span class="spacer"></span>
        <dj-lang-toggle />
        <dj-theme-toggle />
        <button (click)="player.stop()">{{ 'Terminar sesión' | t }}</button>
      </header>

      <dj-now-playing class="now" />

      <div class="cols">
        <dj-queue class="queue" (openLibrary)="library.set(true)" (openWebSearch)="webSearch.set(true)" />
        <dj-chat class="chat" />
        <dj-observability class="obs" />
      </div>
    </div>
    @if (library()) {
      <dj-library-dialog (closed)="library.set(false)" />
    }
    @if (webSearch()) {
      <dj-web-search-dialog (closed)="webSearch.set(false)" />
    }
    @if (store.lastError(); as err) {
      <div class="toast" role="alert">
        {{ err }} <button class="ghost" (click)="store.lastError.set(null)" [attr.aria-label]="'Cerrar' | t">✕</button>
      </div>
    }
  `,
  styles: `
    :host { display: block; min-height: 100vh; padding: 18px 22px 28px; }
    .page { max-width: 1440px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; }
    header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .brand { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
    .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-left: 6px; }
    .tag.on { color: var(--ok); border-color: var(--ok); }
    .spacer { flex: 1; }
    .cols {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.2fr) minmax(280px, 1fr);
      gap: 18px;
      height: clamp(340px, 40vh, 480px);
    }
    .cols > * { min-height: 0; min-width: 0; }
    .toast {
      position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); z-index: 30;
      background: var(--err); color: #fff; padding: 8px 8px 8px 16px; border-radius: 999px;
      display: flex; align-items: center; gap: 8px; max-width: 90vw;
    }
    .toast button { color: #fff; }
    @media (max-width: 1060px) {
      .cols { grid-template-columns: 1fr 1fr; height: auto; }
      .cols > * { height: 380px; }
      .obs { grid-column: 1 / -1; }
    }
    @media (max-width: 680px) {
      :host { padding: 12px; }
      .cols { grid-template-columns: 1fr; }
      .obs { grid-column: auto; }
    }
  `,
})
export class SessionComponent {
  readonly store = inject(SessionStore);
  readonly player = inject(PlayerService);
  readonly library = signal(false);
  readonly webSearch = signal(false);
}
