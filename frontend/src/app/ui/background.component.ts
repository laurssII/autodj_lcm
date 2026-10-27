import { Component, DestroyRef, ElementRef, afterNextRender, inject, viewChild } from '@angular/core';
import { PlayerService } from '../core/player.service';
import { BackdropRenderer } from '../core/visualizer';

/** Espectro del audio de salida como fondo de toda la pantalla. En reposo respira suavemente. */
@Component({
  selector: 'dj-background',
  template: `<canvas #cv aria-hidden="true"></canvas>`,
  styles: `
    :host { position: fixed; inset: 0; z-index: 0; pointer-events: none; }
    canvas { display: block; width: 100%; height: 100%; }
  `,
})
export class BackgroundComponent {
  private player = inject(PlayerService);
  private destroyRef = inject(DestroyRef);
  private cv = viewChild.required<ElementRef<HTMLCanvasElement>>('cv');

  constructor() {
    afterNextRender(() => {
      const canvas = this.cv().nativeElement;
      const renderer = new BackdropRenderer(canvas);
      const ro = new ResizeObserver(() => renderer.resize());
      ro.observe(canvas);
      let raf = 0;
      const loop = (t: number): void => {
        renderer.draw(t / 1000, this.player.spectrum, this.player.frame?.balance ?? null);
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
      this.destroyRef.onDestroy(() => {
        cancelAnimationFrame(raf);
        ro.disconnect();
      });
    });
  }
}
