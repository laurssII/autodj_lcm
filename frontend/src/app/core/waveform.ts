/**
 * Resumen de la forma de onda de una pista: un pico por cada 1/`pps` de segundo, en dos bandas.
 * Sirve para dibujar la onda que se desplaza en el visualizador sin volver a recorrer el audio.
 *
 * No depende de Angular ni del navegador: recibe los datos de un canal tal cual.
 */

export interface WavePeaks {
  /** Picos por segundo de audio. */
  pps: number;
  /** Pico de la señal completa por bin, normalizado a 0..255 respecto al máximo de la pista. */
  max: Uint8Array;
  /** Pico de la banda grave (< ~180 Hz) por bin, en la misma escala: ahí se ve el bombo y el bajo. */
  low: Uint8Array;
}

const LOW_CUTOFF_HZ = 180;

/** Cede el hilo principal para no bloquear la interfaz (y la animación) mientras se resume una pista larga. */
function yieldToUi(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/**
 * Igual que `computePeaks`, pero troceado para no bloquear la interfaz.
 */
export async function computePeaksAsync(samples: Float32Array, sampleRate: number, pps = 50,
                                        binsPerSlice = 400): Promise<WavePeaks> {
  const bin = Math.max(1, Math.floor(sampleRate / pps));
  const bins = Math.max(1, Math.ceil(samples.length / bin));
  const state = newState(bins);
  for (let b = 0; b < bins; b += binsPerSlice) {
    peaksRange(samples, sampleRate, bin, b, Math.min(bins, b + binsPerSlice), state);
    if (b + binsPerSlice < bins) await yieldToUi();
  }
  return finishPeaks(state, pps);
}

interface PeaksState {
  hi: Float32Array;
  lo: Float32Array;
  lp: number;
  peak: number;
}

function newState(bins: number): PeaksState {
  return { hi: new Float32Array(bins), lo: new Float32Array(bins), lp: 0, peak: 1e-6 };
}

function peaksRange(samples: Float32Array, sampleRate: number, bin: number, from: number, to: number,
                    st: PeaksState): void {
  // Paso bajo de un polo: suficiente para separar el bombo del resto sin gastar CPU.
  const alpha = 1 - Math.exp((-2 * Math.PI * LOW_CUTOFF_HZ) / sampleRate);
  let lp = st.lp;
  let peak = st.peak;
  for (let b = from; b < to; b++) {
    const end = Math.min(samples.length, (b + 1) * bin);
    let mh = 0;
    let ml = 0;
    for (let i = b * bin; i < end; i++) {
      const x = samples[i];
      lp += alpha * (x - lp);
      const ax = x < 0 ? -x : x;
      const al = lp < 0 ? -lp : lp;
      if (ax > mh) mh = ax;
      if (al > ml) ml = al;
    }
    st.hi[b] = mh;
    st.lo[b] = ml;
    if (mh > peak) peak = mh;
  }
  st.lp = lp;
  st.peak = peak;
}

function finishPeaks(st: PeaksState, pps: number): WavePeaks {
  const bins = st.hi.length;
  const max = new Uint8Array(bins);
  const low = new Uint8Array(bins);
  for (let b = 0; b < bins; b++) {
    max[b] = Math.round((st.hi[b] / st.peak) * 255);
    // La banda grave se escala con la onda completa para que su altura relativa sea la real.
    low[b] = Math.round((Math.min(st.lo[b], st.hi[b]) / st.peak) * 255);
  }
  return { pps, max, low };
}

export function computePeaks(samples: Float32Array, sampleRate: number, pps = 50): WavePeaks {
  const bin = Math.max(1, Math.floor(sampleRate / pps));
  const bins = Math.max(1, Math.ceil(samples.length / bin));
  const hi = new Float32Array(bins);
  const lo = new Float32Array(bins);
  // Paso bajo de un polo: suficiente para separar el bombo del resto sin gastar CPU.
  const alpha = 1 - Math.exp((-2 * Math.PI * LOW_CUTOFF_HZ) / sampleRate);
  let lp = 0;
  let peak = 1e-6;
  for (let b = 0; b < bins; b++) {
    const end = Math.min(samples.length, (b + 1) * bin);
    let mh = 0;
    let ml = 0;
    for (let i = b * bin; i < end; i++) {
      const x = samples[i];
      lp += alpha * (x - lp);
      const ax = x < 0 ? -x : x;
      const al = lp < 0 ? -lp : lp;
      if (ax > mh) mh = ax;
      if (al > ml) ml = al;
    }
    hi[b] = mh;
    lo[b] = ml;
    if (mh > peak) peak = mh;
  }
  const max = new Uint8Array(bins);
  const low = new Uint8Array(bins);
  for (let b = 0; b < bins; b++) {
    max[b] = Math.round((hi[b] / peak) * 255);
    // La banda grave se escala con la onda completa para que su altura relativa sea la real.
    low[b] = Math.round((Math.min(lo[b], hi[b]) / peak) * 255);
  }
  return { pps, max, low };
}
