import { Component } from '@angular/core';

/** Marca: tres barras de ecualizador. */
@Component({
  selector: 'dj-logo',
  template: `
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <rect x="4" y="14" width="6" height="14" rx="2" class="a" />
      <rect x="13" y="4" width="6" height="24" rx="2" class="b" />
      <rect x="22" y="10" width="6" height="18" rx="2" class="a" />
    </svg>
  `,
  styles: `
    :host { display: inline-block; line-height: 0; }
    svg { width: var(--logo-size, 30px); height: var(--logo-size, 30px); }
    .a { fill: var(--deck-a); }
    .b { fill: var(--deck-b); }
  `,
})
export class LogoComponent {}
