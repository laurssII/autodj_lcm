# AutoDJ — Especificaciones y evolución

*[English version](SPECIFICATIONS.md)*

Este documento explica cómo funciona AutoDJ por dentro, qué especifica y cómo ha llegado hasta aquí. Parte de la especificación de requisitos inicial (20 de octubre de 2026) y refleja el estado del código a **26 de octubre de 2026**. Para probarlo basta con el [README](../README.md).

## Índice

1. [Visión y alcance](#1-visión-y-alcance)
2. [Arquitectura](#2-arquitectura)
3. [Datos y análisis](#3-datos-y-análisis)
4. [El DJ con IA](#4-el-dj-con-ia)
5. [Candidatas: armonía, tempo y puntuación](#5-candidatas-armonía-tempo-y-puntuación)
6. [Estrategias de mezcla](#6-estrategias-de-mezcla)
7. [Worker y renderizado](#7-worker-y-renderizado)
8. [Sesión e interfaz](#8-sesión-e-interfaz)
9. [Evolución del proyecto](#9-evolución-del-proyecto)
10. [Limitaciones y pendientes](#10-limitaciones-y-pendientes)
11. [Desarrollo](#11-desarrollo)
12. [Licencias de terceros](#12-licencias-de-terceros)

## 1. Visión y alcance

AutoDJ es una aplicación web que actúa como DJ automático: un agente de IA elige en cada momento qué canción suena y cómo mezclarla con la anterior, y atiende peticiones del usuario en lenguaje natural. Está pensada para pop y urbano (reggaetón incluido), con una sesión abierta: no hay un set cerrado y la secuencia cambia de una sesión a otra.

La biblioteca tiene cuatro orígenes:

| Origen | Qué aporta | Condiciones |
| --- | --- | --- |
| [Audius](https://audius.co) | Catálogo libre, importado al arrancar (unas 12 pistas por defecto) | No requiere clave; hay que acreditar al artista |
| [Jamendo](https://devportal.jamendo.com) | Música Creative Commons (opcional, con `JAMENDO_CLIENT_ID`) | Uso no comercial |
| Pistas del usuario | Cualquier archivo de audio (subida desde la interfaz) | Solo se guardan en la instancia local |
| SoundCloud | Una pista concreta pedida por chat o desde «Buscar en la web» | Solo uso personal en local (ver [§10](#10-limitaciones-y-pendientes)) |

Apple Music, Spotify y YouTube se descartaron desde el principio: su audio está protegido con DRM y no permite mezclarlo.

## 2. Arquitectura

```
Navegador (Angular + Web Audio)       Backend (Python, FastAPI)
┌───────────────────────────┐ WebSocket ┌──────────────────────────────────────┐
│ Inicio · Cola · Chat      │◀─────────▶│ Sesión ── Agente IA ── cadena de LLM │
│ Observabilidad            │           │   │                                  │
│ Motor de mezcla (2 decks) │◀── plan ──│ Candidatas · Planificador de mezclas │
│ reproduce: saliente →     │◀── audio ─│ Biblioteca · cola de trabajos        │
│ clip mezclado → entrante  │◀── clip ──│            SQLite                    │
└───────────────────────────┘           └──────────────┬───────────────────────┘
                                                       │ trabajos (SQLite compartido)
                                        ┌──────────────▼───────────────────────┐
                                        │ Worker (otro proceso, misma máquina) │
                                        │ análisis · letras · mezclas · stems  │
                                        └──────────────────────────────────────┘
```

El navegador es quien **reproduce** (el reloj de Web Audio es el único lo bastante preciso para sincronizar), pero ya no quien **mezcla**: el trabajo pesado lo hace un worker aparte y el navegador solo encadena tres piezas de audio. El backend decide, el worker prepara y la interfaz se mantiene fluida.

| Capa | Elección | Por qué |
| --- | --- | --- |
| Backend | Python, FastAPI, WebSocket | Las librerías de análisis musical más completas están en Python |
| Análisis | Essentia (BPM, tonalidad) si está; análisis propio en numpy para el resto y como respaldo | madmom está sin mantener y no se instala en Python actual |
| Letras | faster-whisper, con tiempo por palabra | Transcripción local |
| Mezcla | Rubber Band (vía ffmpeg) para tempo y tono; Demucs `htdemucs` para stems | Calidad de estirado y separación en CPU |
| Agente | Cadena de LLM gratuitos con tool calling | Sin coste y sin depender de un solo proveedor |
| Datos | SQLite (un archivo) | Sin configuración; también hace de cola de trabajos |
| Frontend | Angular, Web Audio API, Canvas 2D | El audio en el navegador exige TypeScript |
| Ejecución | Docker Compose: `backend`, `worker`, `frontend` (nginx) | Essentia, Whisper y Demucs son difíciles de instalar a mano |

## 3. Datos y análisis

**Base de datos.** SQLite con `tracks` (metadatos y análisis), `sessions` (semilla y estilo), `transitions` (de → a, estrategia, puntuación, si la pidió el usuario) y las tablas de la cola de trabajos (`jobs`, `workers`). `transitions` es la memoria entre sesiones: sirve para que los pares A→B ya usados pierdan puntos y para variar la primera canción. Una pista pasa por los estados `pending → analyzing → ready` (o `needs_review` / `error`).

**Análisis de cada pista**, al entrar en la biblioteca:

- BPM, beats y downbeats; tonalidad en notación Camelot; energía, ánimo y sonoridad (LUFS).
- Estructura (intro, drop, outro…) y **rejilla de frases**: en qué compás empiezan los bloques de 8, 16 y 32, y dónde acaban la intro y empieza el outro.
- Letra con el tiempo de cada palabra (faster-whisper), necesaria para el wordplay.
- Cada resultado lleva su confianza (`bpm_conf`, `key_conf`) y la versión del análisis (`1.1` actual), de modo que las pistas analizadas con una versión anterior se reanalizan.

## 4. El DJ con IA

### El bucle de decisión

Las decisiones las toma un agente (un LLM con herramientas), pero **el código decide qué es posible y la IA decide qué conviene**:

1. El sistema puntúa las candidatas (§5) y se queda con las **5 mejores**.
2. Para cada una prepara hasta **3 variantes de mezcla** viables y distintas entre sí (estrategia, longitud, punto de la canción actual donde empieza la mezcla, cómo entra la nueva), todas ya comprobadas en tempo, tonalidad y compases, y descritas en palabras.
3. La IA elige una opción por su id con la herramienta `choose`, y da un motivo. **Esa opción se ejecuta tal cual**; solo se reajusta si ya no da tiempo, si el usuario pide «mezclar ya» o si llegan stems para una mezcla por compases/EQ.
4. Si hay algo en la cola del usuario, la canción es esa y la IA solo decide **cómo mezclarla**.

### Una consulta por canción

La decisión se cachea y se protege con `asyncio.shield`: los cambios automáticos (stems que llegan, análisis que terminan, un clip que falla) la reutilizan en vez de repetirla o cancelarla. Solo se vuelve a preguntar si cambia algo que importa: canción nueva, cola, ambiente, un veto, la estrategia preferida o un fallo de carga. El contexto va en texto compacto, no JSON, por la cuota de tokens de Groq (~8 K por minuto).

### Proveedores y fiabilidad

La IA es una **cadena** (`LLM_CHAIN`, por defecto Groq → Gemini → GitHub Models; también admite Mistral, OpenRouter, Cerebras, Ollama y Anthropic). Cada proveedor tiene `LLM_TIMEOUT` (8 s); si responde 429 o 5xx, o se agota el tiempo, se pasa al siguiente y el que falló queda «enfriando» (60 s por límite de ritmo, 45 s por saturación, 30 s por tiempo agotado, 1 h si agotó la cuota diaria). Se pide el mínimo de razonamiento para que respondan en 1–3 s. Si **ningún** proveedor responde, el DJ sigue en **modo reserva**: sorteo ponderado entre las candidatas, con una temperatura de `0,04 + 0,3 × sorpresa`.

### Transparencia

Cada transición indica quién la decidió (`ia`, `reserva`, `única` o `usuario`), con proveedor, modelo, latencia y motivo. La interfaz lo muestra como «✦ IA · Groq» o «modo reserva», y la cabecera da el estado de cada proveedor. La observabilidad registra las opciones que vio la IA, la elegida y cada herramienta del chat.

### El chat

Todo mensaje recibe respuesta. El agente interpreta peticiones libres y actúa **solo mediante herramientas** (hasta 5 pasos por mensaje):

| Herramienta | Efecto |
| --- | --- |
| `search_library`, `library_overview` | Buscar en la biblioteca; resumen de géneros, BPM y pistas |
| `search_soundcloud`, `add_soundcloud` | Si la canción no está, buscarla y añadirla a la cola (opcionalmente la siguiente) |
| `queue_add`, `queue_remove` | Gestionar la cola del usuario |
| `mix_now` | Mezclar en el siguiente inicio de frase |
| `set_vibe` | Cambiar rumbo durante N canciones: energía, tempo, ánimo, géneros, sorpresa |
| `ban` | Vetar una canción o un artista |
| `set_strategy` | Preferir una técnica de mezcla |

Si entiende la petición, confirma qué hará; si no, pregunta; si no puede, explica por qué y propone una alternativa. Sin IA, el chat solo encola las pistas adjuntas. Para proteger la cuota en un despliegue público hay un límite de mensajes por visitante (`CHAT_PER_MINUTE`=6, `CHAT_PER_DAY`=60, por IP real) y un tope diario opcional (`LLM_DAILY_BUDGET`).

## 5. Candidatas: armonía, tempo y puntuación

**Reglas fijas** (no dependen de la IA): no se repite una pista en la sesión ni un artista entre los 3 últimos, y se respetan vetos, géneros a evitar y pistas que fallaron.

**Puntuación** de cada candidata respecto a la que suena (0–1):

| Criterio | Peso | Cómo se mide |
| --- | --- | --- |
| Armonía | 0,35 | Rueda de Camelot (tabla de abajo) |
| Tempo | 0,25 | 1,0 hasta un 2 % de diferencia, bajando linealmente hasta 0 en un 12 %; con mezcla adaptativa puntúa cuánto hay que estirar |
| Energía | 0,20 | Cercanía al objetivo de energía vigente |
| Ambiente | 0,10 | Género y ánimo pedidos por el usuario |
| Novedad | 0,10 | `1 / (1 + n)`, con *n* las veces que ese par A→B ya sonó en sesiones anteriores |

Si una candidata no admite más que un fundido, su puntuación se multiplica por 0,6. La energía objetivo sigue el **estilo** de la sesión, elegido al azar: *subida progresiva* (de 4,5 a 8 en 45 min), *olas* (6 ± 1,6 con periodo de 20 min) o *mantener arriba* (7,2); `set_vibe` lo modifica.

| Movimiento (desde 8A) | Ejemplo | Puntuación |
| --- | --- | --- |
| Misma clave | 8A | 1,0 |
| ±1 en el mismo anillo | 7A, 9A | 0,9 |
| Relativa (mismo número, otra letra) | 8B | 0,85 |
| Diagonal | 9B | 0,7 |
| +2 (*energy boost*) | 10A | 0,6 |
| +7 (un semitono) | 3A | 0,5 |
| Resto | — | 0,1 |

Con tonalidades poco fiables la puntuación tiende a 0,5. Con mezcla adaptativa, una tonalidad que choca se puede corregir desplazando la entrante 1–2 semitonos (penalización de 0,05 por semitono).

## 6. Estrategias de mezcla

Cada estrategia es un módulo con la misma interfaz: dice si es viable para una pareja y genera los puntos de entrada y salida y las curvas de volumen y EQ. Añadir otra es añadir un módulo.

| Estrategia | Cómo suena | Necesita |
| --- | --- | --- |
| **Por compases** | Fundido de 8 o 16 compases desde un inicio de frase, con intercambio de graves a mitad | Compases detectados; tonalidades compatibles |
| **EQ mixing** | De 8 a 32 compases: la entrante entra sin graves; los graves se intercambian en un downbeat y se vacía la saliente | Compases detectados |
| **Wordplay** | La saliente llega a una palabra y la entrante dice esa misma palabra justo ahí, dentro de una mezcla por EQ de 8 compases | Letra con tiempo por palabra y una palabra en común |
| **Stems** | El bajo cambia sin tocar el bombo; la voz de la saliente se retira antes de que entre la entrante | Stems listos |
| **Relevo de voz** | La voz de la saliente sigue cantando sobre la base de la entrante hasta acabar su frase; entonces entra la otra voz | Stems; armonía ≥ 0,85; la saliente canta durante la mezcla y la entrante no |
| **Mezcla corta** | 4 compases de solape, para parejas cuyas tonalidades chocan | Worker |
| **Corte con eco** | La saliente se corta en un compás con su eco y la entrante arranca a su tempo | Worker (último recurso con tempos a >~30 %) |

**Escalera de respaldo.** La estrategia preferida, luego las que encajan (con variedad: no se repite la anterior, y con stems listos van primero *Stems* y *Relevo*), luego *Mezcla corta*, luego *Corte con eco*. El **fundido suave** (unos 6 s, sin cortes en seco) solo queda para cuando no hay worker o no hay compases; su nota en el registro dice el motivo («tempos muy distintos…», «no hay worker…»).

### Alineación por frases

Las mezclas con graves empiezan siempre en una frontera de frase de la saliente: prefieren un bloque de 32 compases, luego 16, 8 y 4, y solo en último caso un compás cualquiera. El intercambio de graves cae a mitad de la mezcla y se hace coincidir con dos puntos musicales: el **inicio del outro de A** (su bajo se va justo cuando ya no hace falta) y el **final de la intro de B** (su bajo llega cuando empieza su cuerpo). Las curvas de volumen son de potencia constante, así que la suma no se hunde a mitad de mezcla.

### Mezcla adaptativa (tempo y tonalidad)

Antes, dos pistas con más de un 6 % de diferencia de tempo acababan en un fundido. Ahora se **adaptan** como con *master tempo*:

- **Tempo.** Se elige un tempo común (la saliente no se toca si la diferencia es menor del 6 %; si es mayor, se encuentran a mitad de camino), con hasta un 15 % de desviación por pista y probando también el doble y la mitad (70 ↔ 140). La saliente acelera o frena con una rampa de 4 a 12 compases, suenan juntas y la entrante **recupera su tempo original** con otra rampa, sin cambiar el tono.
- **Tonalidad.** Si chocan y ambas son fiables, la entrante se desplaza solo durante la mezcla y vuelve a su tono con un deslizamiento.
- **Rejilla.** Los compases se ajustan por mínimos cuadrados para que los bombos de las dos pistas coincidan (error de ~1 ms en pruebas sintéticas); una pista con rejilla irregular no se alinea.

### Canciones recortadas

Con `MIX_SHORTEN=true` (por defecto) las canciones no se dejan enteras: se deja sonar **hasta el final del primer estribillo** y la mezcla empieza ahí, de modo que la entrante coge el verso siguiente. Sin estribillo detectado, se corta en un inicio de frase hacia el 55 %. Con `false`, se mezcla en el outro.

### Stems y relevo de voz

Con Demucs en el worker se separan (batería, bajo, armonía, voz) tres tramos de cada canción: el **principio**, el **final** (80 s) y el que rodea al **punto de corte**. Se hace en segundo plano para la que suena, la siguiente y las 3 primeras de la cola, y se guarda la **actividad de la voz** de cada tramo. Si los stems llegan tarde se rehace la mezcla pendiente; si no llegan, se usa EQ.

## 7. Worker y renderizado

El análisis, las letras, los stems y el renderizado corren en un **worker**: un proceso aparte, en la misma máquina, que toma trabajos de una cola en SQLite. Tiene cinco líneas: `render` (la prioridad máxima: una mezcla nunca espera detrás de un análisis), `analysis` (lo que pides tú se analiza enseguida), `lyrics`, `stems` (para lo que va a sonar pronto) y `background` (el catálogo, con prioridad baja y pausas para no saturar la CPU).

Para cada transición el worker produce **un clip WAV con las dos pistas ya mezcladas**: tempo, tono, EQ, stems y eco incluidos. La sesión lo pide con unos 18 s de antelación (10 s si es «mezclar ya»). Si el clip no llega a tiempo o falla, esa pareja usa la **mezcla clásica** (el backend sirve las pistas ya estiradas y el navegador aplica los volúmenes y el EQ) y no se reintenta. El navegador reproduce la saliente tal cual, el clip (con un traspaso de 40 ms) y la entrante desde donde acaba el clip.

Sin Docker, `AUTODJ_WORKER=inline` (valor por defecto) lo ejecuta en hilos de la propia API; con `external`, en otro proceso (`python -m autodj.worker`). `/api/health` indica qué workers están vivos y qué pueden hacer.

## 8. Sesión e interfaz

**Sesión.** El backend mantiene **una única sesión** compartida por todos los clientes. Se inicia desde la pantalla de inicio (reproducción autónoma, una pista adjunta o una sugerencia escrita) y **«Terminar sesión» la borra por completo**: cola, historial, chat, registros, decisiones, vetos y los clips renderizados, y cancela cualquier consulta o descarga en curso. Se conservan la biblioteca y la memoria de transiciones entre sesiones. Los mensajes rezagados del navegador tras terminar se ignoran.

**Interfaz.** Dos pantallas: inicio y sesión. La sesión muestra dos platos que giran con la reproducción real, con la forma de onda de cada pista, su rejilla y la zona de mezcla programada. Debajo están la **cola** (solo lo que pide el usuario; reordenable hasta que una pista entra en la mezcla) con su **historial** de hasta 100 canciones, la **biblioteca virtual** y la búsqueda en la web, el **chat** y la **observabilidad**. Hay tema claro y oscuro, y un selector de **idioma (ES/EN)** en la pantalla de inicio y en la cabecera.

**Idioma.** El texto original está en español y hace de clave: la interfaz lo traduce con un diccionario (`frontend/src/app/core/i18n.en.ts`) y el backend con otro (`backend/autodj/catalog_en.py`); lo que no está en el diccionario se muestra en español. La elección se guarda en el navegador y se envía al servidor con un mensaje `lang`, de modo que sus avisos, las razones de cada mezcla y las respuestas del chat salen en ese idioma (a la IA se le pide expresamente que conteste en inglés). El idioma del servidor es **uno solo**, como la sesión. El worker, que es otro proceso, recibe el idioma en cada trabajo. Un test (`tests/test_i18n.py`) comprueba que todo texto envuelto en `tr()` tiene su traducción.

## 9. Evolución del proyecto

La especificación inicial acotaba una v1 sencilla. Casi todo lo que dejaba fuera de alcance —stems, más estrategias, voz— acabó entrando en una semana.

| Aspecto | En la especificación | Hoy |
| --- | --- | --- |
| Estrategias | 3 (compases, EQ, wordplay) + fundido | 7, con una escalera de respaldo; el fundido es el último recurso |
| Tempo | Exige ΔBPM ≤ 6 % | Adaptativo: hasta ~15 % por pista, doble/mitad, rampas y cambio de tono |
| Dónde se mezcla | En el outro | Tras el primer estribillo (canciones recortadas) |
| Voz y stems | Fuera de alcance | Demucs en el worker: *Stems* y *Relevo de voz* |
| Quién mezcla | El navegador (volúmenes y EQ), con las pistas estiradas por el backend | El worker renderiza un clip; el navegador solo lo reproduce (la mezcla clásica queda de respaldo) |
| Procesos | `backend` y `frontend` | Más un `worker` |
| IA | Un LLM | Cadena de proveedores; elige entre opciones y se ejecuta lo elegido |
| Fuentes | Audius, Jamendo y archivos propios | Más SoundCloud por chat, para uso personal |
| Historial, terminar sesión | — | Pestaña de historial; la sesión se borra de verdad |
| Idioma | Español | Español e inglés, con selector en la interfaz |

**Cronología**

- **20 oct.** Especificación inicial.
- **21 oct.** Funciona en local con Docker Compose. Se elimina el *drop mix* (demasiado brusco); el respaldo es un fundido suave de unos 6 s.
- **22 oct.** Mezcla adaptativa y worker aparte; stems con Demucs y estrategia *Stems*.
- **23 oct.** Menos fundidos (mezcla corta y corte con eco), canciones recortadas tras el estribillo, wordplay con EQ, relevo de voz, historial y SoundCloud por chat.
- **24 oct.** **La IA decide de verdad.** Antes casi nunca llegaba a hacerlo: el modelo gratuito disponible tenía unas 20 peticiones diarias y respondía 503 con frecuencia, y cada replanificación cancelaba la consulta en curso. Se sustituyó por la cadena de proveedores, las opciones concretas con una consulta por canción, la transparencia por transición y el límite de chat por visitante.
- **26 oct.** «Terminar sesión» borra de verdad el estado, en servidor y navegador. Interfaz bilingüe (ES/EN) y documentación en los dos idiomas.

**Decisiones y descartes**

- *Drop mix*: eliminado por brusco.
- *madmom* (compases): descartado por estar sin mantener y no instalarse en Python actual. Se usa Essentia más un análisis propio.
- *Java* para el backend: descartado por falta de librerías maduras de análisis musical.
- *Spotify, Apple Music, YouTube*: descartados por DRM.
- *Contexto de la IA en JSON*: sustituido por texto compacto, por la cuota de tokens del proveedor gratuito.
- *Puntuar las candidatas por «mezclabilidad» real* (coste de adaptación): considerado y no elegido por ahora.

## 10. Limitaciones y pendientes

- **Una sola sesión compartida.** Pensado para uso local. En un despliegue público todos los visitantes verían y controlarían la misma sesión; haría falta una sesión por visitante o una «radio» colectiva.
- **SoundCloud.** Las herramientas `search_soundcloud`/`add_soundcloud` y el botón «Buscar en la web» descargan audio con yt-dlp. SoundCloud no permite redistribuirlo: es solo para uso personal en local, y no es apto para una versión pública (que debería quedarse con Audius/Jamendo).
- **Worker y despliegue.** API y worker comparten volumen y cola SQLite: tienen que estar en la misma máquina. En un hosting de un solo contenedor hay que usar `AUTODJ_WORKER=inline`, más lento; el worker con Demucs necesita unos 4 GB de RAM.
- **Sin ajustar a oído.** Parámetros pendientes de afinar con música real: `MIN_PLAY`/`MIN_TAIL` del recorte, la tolerancia del wordplay (90 ms), las curvas de EQ del wordplay y el umbral de voz del relevo.
- **Sin validar a fondo:** fiabilidad de BPM y tonalidad en reggaetón, y transcripción de letras en español sin stems.
- **Idioma.** Lo ya escrito (chat, registros, historial) no se retraduce al cambiar de idioma; solo lo nuevo. Los motivos que da la IA dependen de que el modelo obedezca la instrucción de idioma, y los errores internos poco habituales pueden salir en español.
- **Cuotas.** Los modelos gratuitos cambian sus límites con frecuencia; el modo reserva evita que el DJ se pare, pero sin IA no hay chat.

## 11. Desarrollo

```bash
# Backend (requiere ffmpeg con el filtro rubberband; en Debian/Ubuntu ya lo trae)
cd backend
pip install -r requirements-dev.txt
pip install -r requirements-analysis.txt   # opcional: Essentia y Whisper
pip install -r requirements-stems.txt      # opcional: Demucs (instala antes torch para CPU)
AUTODJ_DATA_DIR=./data uvicorn autodj.api:app --reload --port 8000    # worker en línea

# Frontend
cd frontend && npm install && npm start    # http://localhost:4200, con proxy a :8000
```

**Tests:** `cd backend && python -m pytest` (o `python -m unittest discover -s tests -t .`). Son más de 200 (más un test que verifica las traducciones): Camelot y puntuación, estrategias, análisis con canciones sintéticas, biblioteca con ffmpeg real, adaptación de tempo y tono, renderizado (alineación de bombos incluida), stems, cola de trabajos, sesión (cola, historial, fallos, terminar sesión, chat → SoundCloud → cola) y la IA (conmutación de proveedores, modo reserva, límite por visitante).

Toda la configuración está comentada en [`.env.example`](../.env.example).

## 12. Licencias de terceros

El código de AutoDJ es MIT ([`LICENSE`](../LICENSE)). Las dependencias conservan la suya, y algunas son *copyleft*:

| Dependencia | Licencia | Cómo se usa |
| --- | --- | --- |
| [Essentia](https://essentia.upf.edu/documentation.html) | AGPL v3 (hay licencia propietaria bajo petición) | Opcional, en el worker |
| [Rubber Band](https://breakfastquay.com/rubberband/license.html) | GPL v2 o posterior (hay licencia comercial) | Vía el ffmpeg del sistema |
| Demucs, faster-whisper, FastAPI, Angular | Permisivas (MIT) | — |

Publicar este código no redistribuye esas librerías, pero **distribuir imágenes Docker ya construidas** (que sí las incluyen) puede imponer obligaciones: conviene revisarlas antes. La música tiene la licencia de cada artista, y AutoDJ muestra siempre el autor y el enlace al original.
