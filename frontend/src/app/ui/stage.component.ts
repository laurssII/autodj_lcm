import { Component, DestroyRef, ElementRef, afterNextRender, inject, viewChild } from '@angular/core';
import { I18n } from '../core/i18n.service';
import { PlayerService } from '../core/player.service';
import { StageRenderer } from '../core/visualizer';

/** Los dos platos con la forma de onda de cada pista: la mezcla tal como se está ejecutando. */
@Component({
  selector: 'dj-stage',
  template: `<canvas #cv role="img" [attr.aria-label]="i18n.t('Dos platos con la forma de onda de la mezcla en directo')"></canvas>`,
  styles: `
    :host { display: block; }
    canvas { display: block; width: 100%; height: 100%; }
  `,
})
export class StageComponent {
  readonly i18n = inject(I18n);
  private player = inject(PlayerService);
  private destroyRef = inject(DestroyRef);
  private cv = viewChild.required<ElementRef<HTMLCanvasElement>>('cv');

  constructor() {
    afterNextRender(() => {
      const canvas = this.cv().nativeElement;
      const renderer = new StageRenderer(canvas, (s) => this.i18n.t(s));
      const ro = new ResizeObserver(() => renderer.resize());
      ro.observe(canvas);
      let raf = 0;
      const loop = (): void => {
        renderer.draw(this.player.frame);
        raf = requestAnimationFrame(loop);
      };
      loop();
      this.destroyRef.onDestroy(() => {
        cancelAnimationFrame(raf);
        ro.disconnect();
      });
    });
  }
}
