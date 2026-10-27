import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { I18n } from './i18n.service';
import type { ChatMessage, LogEntry, MixPlan, SessionState, TrackSummary } from './models';

type Listener = (msg: any) => void;

/** Conexión WebSocket con la sesión y estado reactivo (signals) para la interfaz. */
@Injectable({ providedIn: 'root' })
export class SessionStore {
  readonly connected = signal(false);
  readonly state = signal<SessionState | null>(null);
  readonly logs = signal<LogEntry[]>([]);
  readonly chat = signal<ChatMessage[]>([]);
  readonly awaitingReply = signal(false);
  readonly lastError = signal<string | null>(null);
  readonly library = signal<Map<string, TrackSummary>>(new Map());

  readonly queue = computed(() => this.state()?.queue ?? []);
  readonly history = computed(() => this.state()?.history ?? []);
  readonly nowPlaying = computed(() => this.state()?.now_playing ?? null);
  readonly next = computed(() => this.state()?.next ?? null);

  private ws: WebSocket | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private outbox: unknown[] = [];
  private retry = 0;

  private i18n = inject(I18n);

  constructor() {
    this.connect();
    // El servidor redacta sus mensajes en el idioma elegido aquí (también al reconectar: ver `onopen`).
    effect(() => {
      const lang = this.i18n.lang();
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: 'lang', lang }));
    });
  }

  on(type: 'load' | 'plan', fn: (msg: any) => void): () => void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(fn);
    return () => this.listeners.get(type)!.delete(fn);
  }

  send(msg: Record<string, unknown>): void {
    if (msg['type'] === 'chat') this.awaitingReply.set(true);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
    else if (msg['type'] !== 'position') this.outbox.push(msg);
  }

  private connect(): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.connected.set(true);
      this.retry = 0;
      ws.send(JSON.stringify({ type: 'lang', lang: this.i18n.lang() }));
      for (const m of this.outbox.splice(0)) ws.send(JSON.stringify(m));
    };
    ws.onclose = () => {
      this.connected.set(false);
      const delay = Math.min(10000, 500 * 2 ** this.retry++);
      setTimeout(() => this.connect(), delay);
    };
    ws.onmessage = (ev) => this.dispatch(JSON.parse(ev.data));
  }

  private dispatch(msg: any): void {
    switch (msg.type) {
      case 'hello':
        this.logs.set(msg.logs ?? []);
        this.chat.set(msg.chat ?? []);
        break;
      case 'state':
        this.state.set(msg as SessionState);
        break;
      case 'reset':
        // Sesión nueva o terminada: se vacía todo salvo el estado de los proveedores de IA.
        this.logs.set([]);
        this.chat.set([]);
        this.awaitingReply.set(false);
        this.lastError.set(null);
        this.state.update((s) => s && { ...s, active: false, now_playing: null, next: null, queue: [],
          history: [], waiting_first: null });
        break;
      case 'log':
        this.logs.update((l) => [...l, msg.entry as LogEntry].slice(-300));
        break;
      case 'chat':
        this.chat.update((c) => [...c, msg.message as ChatMessage].slice(-200));
        if ((msg.message as ChatMessage).role === 'dj') this.awaitingReply.set(false);
        break;
      case 'library':
        this.library.update((m) => new Map(m).set(msg.track.id, msg.track));
        break;
      case 'error':
        this.lastError.set(msg.message);
        this.awaitingReply.set(false);
        break;
    }
    this.listeners.get(msg.type)?.forEach((fn) => fn(msg));
  }

  static planOf(msg: any): MixPlan {
    return msg.plan as MixPlan;
  }
}
