import { Pipe, PipeTransform, inject } from '@angular/core';
import { I18n, type Params } from './i18n.service';

/** `{{ 'Texto en español' | t }}` o `{{ 'Hay {n} pistas' | t: { n: total } }}`. */
@Pipe({ name: 't', pure: false })
export class TPipe implements PipeTransform {
  private i18n = inject(I18n);

  transform(text: string, params?: Params): string {
    return this.i18n.t(text, params);
  }
}
