export interface TrackSummary {
  id: string;
  source: 'audius' | 'jamendo' | 'soundcloud' | 'local';
  title: string;
  artist: string;
  genre: string | null;
  duration: number;
  status: 'pending' | 'analyzing' | 'ready' | 'needs_review' | 'error';
  status_detail: string | null;
  bpm: number | null;
  camelot: string | null;
  energy: number | null;
  mood: number | null;
  credit_url: string | null;
  license: string | null;
  lyrics: string;
  loudness_lufs?: number | null;
  rate?: number;
  /** Instante de cada compás en el audio original, en segundos. */
  downbeats?: number[];
  /** Compás (0..31) donde empieza un bloque de 32; módulo 8 = fase de las frases. */
  phrase_offset?: number;
  intro_end?: number | null;
  outro_start?: number | null;
}

/** Coincidencia del buscador de música en la nube (todavía no está en la biblioteca). */
export interface MainstreamResult {
  id: string;
  title: string;
  artist: string;
  duration: number;
  url: string;
  thumbnail: string | null;
  in_library: boolean;
}

export interface Lane {
  deck: 'A' | 'B';
  /** gain/low/mid/high los aplica el navegador en las mezclas clásicas; los stems solo los usa el worker al renderizar. */
  param: 'gain' | 'low' | 'mid' | 'high' | 'drums' | 'bass' | 'other' | 'vocals';
  points: [number, number][];
}

/**
 * Mezcla ya renderizada por el worker: un único clip de audio con las dos pistas adaptadas (tempo, tono, EQ,
 * stems). El navegador reproduce la saliente tal cual hasta `out_start`, el clip y la entrante desde `in_start`.
 */
export interface RenderInfo {
  file: string;
  /** Ruta del clip WAV en la API. */
  url: string;
  tempo: number;
  mult: number;
  bars: number;
  a_ramp: number;
  b_ramp: number;
  pitch_b: number;
  /** Segundos del clip donde empieza y acaba el solapamiento (las curvas de `automation` son relativas al inicio). */
  overlap_at: number;
  overlap_end: number;
  /** Correspondencia [segundo del clip, segundo del audio original] de cada pista, para dibujar las posiciones. */
  maps: { a: [number, number][]; b: [number, number][] };
  stems: boolean;
  /** "echo_cut" para el corte con eco (sin solapamiento de ritmos); ausente en las mezclas a tempo común. */
  kind?: string;
  /** Segundos de empalme entre el audio nativo y el clip, si no son los 40 ms de siempre. */
  handoff?: number;
}

/** Quién decidió una transición (ver Decision.badge en el backend). */
export interface Decided {
  /** ia = la eligió el modelo; reserva = la IA no respondió y se sorteó; única = solo había una opción; usuario = la pidió el usuario. */
  by: 'ia' | 'reserva' | 'única' | 'usuario';
  provider: string;
  model: string;
  latency_ms: number;
  /** Por qué se usó el modo reserva (si es el caso). */
  why: string;
  /** Opción elegida (p. ej. "2b"). */
  option: string;
}

/** Estado de un proveedor de la cadena de IA. */
export interface AiProvider {
  key: string;
  label: string;
  model: string;
  state: 'listo' | 'enfriando' | 'sin clave';
  cooldown_s: number;
  last_error: string;
  ok: number;
  failed: number;
}

export interface MixPlan {
  plan_id: string;
  from_id: string;
  strategy: string;
  out_start: number;
  in_start: number;
  duration: number;
  rate_b: number;
  bars: number;
  label?: string;
  /** Segundos desde el inicio de la mezcla hasta el cambio de graves (null si no hay). */
  swap_at?: number | null;
  /** El cambio de graves cae donde acaba la intro de B y empieza el outro de A. */
  aligned?: boolean;
  automation: Lane[];
  notes: string;
  reason: string;
  track: TrackSummary;
  decided?: Decided | null;
  /** Presente si la mezcla es adaptativa y está renderizada (ver `RenderInfo`). */
  render?: RenderInfo | null;
}

export interface LogEntry {
  id: number;
  ts: number;
  kind: 'decision' | 'plan' | 'tool' | 'info' | 'error' | 'chat';
  title: string;
  detail: Record<string, unknown>;
}

export interface ChatMessage {
  id: number;
  ts: number;
  role: 'user' | 'dj';
  text: string;
  /** Qué IA contestó ("Groq"…), "reserva" si no contestó ninguna, o "límite" si se pasó el límite de mensajes. */
  via?: string;
}

export interface NextInfo {
  track: TrackSummary | null;
  strategy: string;
  strategy_label: string;
  reason: string;
  from_queue: boolean;
  in_transition: boolean;
  decided?: Decided | null;
}

/** Una canción que ya ha sonado en la sesión. */
export interface HistoryItem extends TrackSummary {
  /** Instante (s desde 1970) en que empezó a sonar. */
  played_at: number;
  /** Nombre de la transición con la que entró; null si fue la primera de la sesión. */
  via: string | null;
  from_queue: boolean;
  decided?: Decided | null;
}

export interface SessionState {
  type: 'state';
  active: boolean;
  session_id: string;
  style: string;
  llm: boolean;
  ai?: { enabled: boolean; providers: AiProvider[]; last: Decided | null };
  now_playing: TrackSummary | null;
  next: NextInfo | null;
  queue: TrackSummary[];
  /** Lo que ya ha sonado, lo más reciente primero (sin la que suena ahora). */
  history?: HistoryItem[];
  targets: Record<string, unknown>;
  waiting_first: TrackSummary | null;
}
