import { Injectable, inject } from '@angular/core';
import { I18n } from './i18n.service';
import type { MainstreamResult, TrackSummary } from './models';

/** Peticiones REST al backend local (mismo origen; en desarrollo, vía proxy). */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private i18n = inject(I18n);

  audioUrl(trackId: string, rate: number): string {
    return `/api/tracks/${encodeURIComponent(trackId)}/audio?rate=${rate.toFixed(3)}`;
  }

  async tracks(q = ''): Promise<TrackSummary[]> {
    const r = await fetch(`/api/tracks?q=${encodeURIComponent(q)}`);
    if (!r.ok) throw new Error(this.i18n.t('No se pudo cargar la biblioteca ({status})', { status: r.status }));
    return r.json();
  }

  async upload(file: File): Promise<TrackSummary> {
    const body = new FormData();
    body.append('file', file, file.name);
    const r = await fetch('/api/tracks', { method: 'POST', body });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail ?? this.i18n.t('No se pudo subir el archivo ({status})', { status: r.status }));
    }
    return r.json();
  }

  async searchMainstream(q: string): Promise<MainstreamResult[]> {
    const r = await fetch(`/api/library/search-mainstream?q=${encodeURIComponent(q)}&limit=8`);
    if (!r.ok) throw new Error(await this.errorOf(r, this.i18n.t('Error al buscar ({status})', { status: r.status })));
    return r.json();
  }

  /** Mete en la biblioteca la pista elegida (se descarga y analiza en segundo plano). */
  async addMainstream(url: string): Promise<TrackSummary> {
    const r = await fetch('/api/library/add-mainstream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) throw new Error(await this.errorOf(r, this.i18n.t('No se pudo añadir la pista ({status})', { status: r.status })));
    return (await r.json()).track as TrackSummary;
  }

  private async errorOf(r: Response, fallback: string): Promise<string> {
    const err = await r.json().catch(() => ({}));
    return typeof err.detail === 'string' ? err.detail : fallback;
  }

  async health(): Promise<{ llm: { enabled: boolean; provider: string }; tracks: number; importing: boolean }> {
    const r = await fetch('/api/health');
    return r.json();
  }
}
