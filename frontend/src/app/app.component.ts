import { Component, inject } from '@angular/core';
import { PlayerService } from './core/player.service';
import { TPipe } from './core/t.pipe';
import { SessionStore } from './core/session-store.service';
import { SessionComponent } from './session/session.component';
import { StartComponent } from './start/start.component';
import { BackgroundComponent } from './ui/background.component';

@Component({
  selector: 'dj-root',
  imports: [StartComponent, SessionComponent, BackgroundComponent, TPipe],
  template: `
    <dj-background />
    @if (!store.connected()) {
      <div class="offline" role="status">{{ 'Conectando con el servidor de AutoDJ…' | t }}</div>
    }
    @if (player.started()) {
      <dj-session />
    } @else {
      <dj-start />
    }
  `,
  styles: `
    :host { display: block; min-height: 100vh; }
    dj-session, dj-start { position: relative; z-index: 1; }
    .offline {
      position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 20;
      background: var(--ink); color: var(--bg); padding: 6px 14px; border-radius: 999px; font-size: 13px;
    }
  `,
})
export class AppComponent {
  readonly store = inject(SessionStore);
  readonly player = inject(PlayerService);
}
