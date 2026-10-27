# AutoDJ — Specifications and evolution

*[Versión en español](ESPECIFICACIONES.md)*

This document explains how AutoDJ works inside, what it specifies and how it got here. It starts from the initial requirements specification (20 October 2026) and reflects the state of the code as of **26 October 2026**. To just try it out, the [README](../README.md) is enough.

## Contents

1. [Vision and scope](#1-vision-and-scope)
2. [Architecture](#2-architecture)
3. [Data and analysis](#3-data-and-analysis)
4. [The AI DJ](#4-the-ai-dj)
5. [Candidates: harmony, tempo and scoring](#5-candidates-harmony-tempo-and-scoring)
6. [Mixing strategies](#6-mixing-strategies)
7. [Worker and rendering](#7-worker-and-rendering)
8. [Session and interface](#8-session-and-interface)
9. [Project evolution](#9-project-evolution)
10. [Limitations and open items](#10-limitations-and-open-items)
11. [Development](#11-development)
12. [Third-party licenses](#12-third-party-licenses)

## 1. Vision and scope

AutoDJ is a web application that acts as an automatic DJ: an AI agent chooses at every moment which song plays and how to mix it with the previous one, and handles the user's natural-language requests. It is aimed at pop and urban music (reggaeton included), with an open-ended session: there is no fixed set and the sequence differs from one session to the next.

The library has four sources:

| Source | What it provides | Conditions |
| --- | --- | --- |
| [Audius](https://audius.co) | Free catalog, imported at startup (about 12 tracks by default) | No key needed; the artist must be credited |
| [Jamendo](https://devportal.jamendo.com) | Creative Commons music (optional, with `JAMENDO_CLIENT_ID`) | Non-commercial use |
| User tracks | Any audio file (uploaded from the interface) | Stored only in the local instance |
| SoundCloud | One specific track requested by chat or from “Search the web” | Personal, local use only (see [§10](#10-limitations-and-open-items)) |

Apple Music, Spotify and YouTube were ruled out from the start: their audio is DRM-protected and cannot be mixed.

## 2. Architecture

```
Browser (Angular + Web Audio)         Backend (Python, FastAPI)
┌───────────────────────────┐ WebSocket ┌──────────────────────────────────────┐
│ Start · Queue · Chat      │◀─────────▶│ Session ── AI agent ── LLM chain     │
│ Observability             │           │   │                                  │
│ Mix engine (2 decks)      │◀── plan ──│ Candidates · Mix planner             │
│ plays: outgoing →         │◀── audio ─│ Library · job queue                  │
│ mixed clip → incoming     │◀── clip ──│            SQLite                    │
└───────────────────────────┘           └──────────────┬───────────────────────┘
                                                       │ jobs (shared SQLite)
                                        ┌──────────────▼───────────────────────┐
                                        │ Worker (separate process, same host) │
                                        │ analysis · lyrics · mixes · stems    │
                                        └──────────────────────────────────────┘
```

The browser is what **plays** (the Web Audio clock is the only one precise enough to keep things in sync), but it is no longer what **mixes**: the heavy work is done by a separate worker and the browser only chains three pieces of audio. The backend decides, the worker prepares, and the interface stays smooth.

| Layer | Choice | Why |
| --- | --- | --- |
| Backend | Python, FastAPI, WebSocket | The most complete music-analysis libraries are in Python |
| Analysis | Essentia (BPM, key) if available; own numpy analysis for the rest and as a fallback | madmom is unmaintained and does not install on current Python |
| Lyrics | faster-whisper, with per-word timing | Local transcription |
| Mixing | Rubber Band (via ffmpeg) for tempo and pitch; Demucs `htdemucs` for stems | Stretching and separation quality on CPU |
| Agent | Chain of free LLMs with tool calling | No cost and no dependence on a single provider |
| Data | SQLite (one file) | Zero configuration; also works as the job queue |
| Frontend | Angular, Web Audio API, Canvas 2D | Browser audio requires TypeScript |
| Runtime | Docker Compose: `backend`, `worker`, `frontend` (nginx) | Essentia, Whisper and Demucs are hard to install by hand |

## 3. Data and analysis

**Database.** SQLite with `tracks` (metadata and analysis), `sessions` (seed and style), `transitions` (from → to, strategy, score, whether the user asked for it) and the job-queue tables (`jobs`, `workers`). `transitions` is the memory across sessions: it makes already-used A→B pairs lose points and varies the first song. A track goes through the states `pending → analyzing → ready` (or `needs_review` / `error`).

**Analysis of each track**, when it enters the library:

- BPM, beats and downbeats; key in Camelot notation; energy, mood and loudness (LUFS).
- Structure (intro, drop, outro…) and **phrase grid**: at which bar the 8-, 16- and 32-bar blocks start, and where the intro ends and the outro begins.
- Lyrics with the timing of every word (faster-whisper), needed for wordplay.
- Every result carries its confidence (`bpm_conf`, `key_conf`) and the analysis version (currently `1.1`), so tracks analyzed with an older version are re-analyzed.

## 4. The AI DJ

### The decision loop

Decisions are made by an agent (an LLM with tools), but **the code decides what is possible and the AI decides what suits**:

1. The system scores the candidates (§5) and keeps the **top 5**.
2. For each one it prepares up to **3 viable, distinct mix variants** (strategy, length, the point of the current song where the mix starts, how the new one comes in), all already checked for tempo, key and bars, and described in words.
3. The AI picks one option by its id with the `choose` tool and gives a reason. **That option is executed as is**; it is only readjusted if there is no longer time, if the user asks to “mix now”, or if stems arrive for a bar-matched/EQ mix.
4. If there is something in the user's queue, the song is that one and the AI only decides **how to mix it**.

### One query per song

The decision is cached and protected with `asyncio.shield`: automatic changes (stems arriving, analyses finishing, a failed clip) reuse it instead of repeating or cancelling it. The AI is only asked again if something that matters changes: a new song, the queue, the mood, a ban, the preferred strategy or a load failure. The context is sent as compact text, not JSON, because of Groq's token quota (~8 K per minute).

### Providers and reliability

The AI is a **chain** (`LLM_CHAIN`, by default Groq → Gemini → GitHub Models; it also supports Mistral, OpenRouter, Cerebras, Ollama and Anthropic). Each provider has `LLM_TIMEOUT` (8 s); if it answers 429 or 5xx, or times out, the next one is used and the failed one is left “cooling down” (60 s for rate limits, 45 s for overload, 30 s for timeouts, 1 h if the daily quota is exhausted). The minimum reasoning effort is requested so they answer in 1–3 s. If **no** provider answers, the DJ carries on in **fallback mode**: a weighted draw among the candidates, with a temperature of `0.04 + 0.3 × surprise`.

### Transparency

Every transition states who decided it (`ia`, `reserva`, `única` or `usuario`), with provider, model, latency and reason. The interface shows it as “✦ AI · Groq” or “fallback mode”, and the header gives the state of each provider. Observability records the options the AI saw, the chosen one and every chat tool call.

### The chat

Every message gets an answer. The agent interprets free-form requests and acts **only through tools** (up to 5 steps per message):

| Tool | Effect |
| --- | --- |
| `search_library`, `library_overview` | Search the library; summary of genres, BPM and tracks |
| `search_soundcloud`, `add_soundcloud` | If the song is not there, find it and add it to the queue (optionally as the next one) |
| `queue_add`, `queue_remove` | Manage the user's queue |
| `mix_now` | Mix at the next phrase start |
| `set_vibe` | Change course for N songs: energy, tempo, mood, genres, surprise |
| `ban` | Ban a song or an artist |
| `set_strategy` | Prefer a mixing technique |

If it understands the request, it confirms what it will do; if not, it asks; if it cannot, it explains why and proposes an alternative. Without AI, the chat only queues attached tracks. To protect the quota in a public deployment there is a per-visitor message limit (`CHAT_PER_MINUTE`=6, `CHAT_PER_DAY`=60, by real IP) and an optional daily cap (`LLM_DAILY_BUDGET`).

## 5. Candidates: harmony, tempo and scoring

**Fixed rules** (independent of the AI): a track is never repeated within the session nor an artist among the last 3, and bans, genres to avoid and tracks that failed are respected.

**Scoring** of each candidate relative to the one playing (0–1):

| Criterion | Weight | How it is measured |
| --- | --- | --- |
| Harmony | 0.35 | Camelot wheel (table below) |
| Tempo | 0.25 | 1.0 up to a 2 % difference, falling linearly to 0 at 12 %; with adaptive mixing it scores how much stretching is needed |
| Energy | 0.20 | Closeness to the current energy target |
| Mood | 0.10 | Genre and mood requested by the user |
| Novelty | 0.10 | `1 / (1 + n)`, with *n* the number of times that A→B pair has already played in earlier sessions |

If a candidate only allows a fade, its score is multiplied by 0.6. The energy target follows the session's **style**, chosen at random: *gradual build-up* (from 4.5 to 8 over 45 min), *waves* (6 ± 1.6 with a 20-minute period) or *keep it high* (7.2); `set_vibe` modifies it.

| Move (from 8A) | Example | Score |
| --- | --- | --- |
| Same key | 8A | 1.0 |
| ±1 on the same ring | 7A, 9A | 0.9 |
| Relative (same number, other letter) | 8B | 0.85 |
| Diagonal | 9B | 0.7 |
| +2 (*energy boost*) | 10A | 0.6 |
| +7 (one semitone) | 3A | 0.5 |
| Anything else | — | 0.1 |

With unreliable keys the score tends to 0.5. With adaptive mixing, a clashing key can be corrected by shifting the incoming track 1–2 semitones (0.05 penalty per semitone).

## 6. Mixing strategies

Each strategy is a module with the same interface: it says whether it is viable for a pair and generates the entry and exit points and the volume and EQ curves. Adding another means adding a module.

| Strategy | How it sounds | Needs |
| --- | --- | --- |
| **Bar-matched** | 8- or 16-bar fade from a phrase start, with a bass swap halfway | Detected bars; compatible keys |
| **EQ mixing** | 8 to 32 bars: the incoming track enters without bass; the bass swaps on a downbeat and the outgoing track drains away | Detected bars |
| **Wordplay** | The outgoing track reaches a word and the incoming one says that same word right there, inside an 8-bar EQ mix | Lyrics with per-word timing and a shared word |
| **Stems** | The bass swaps without touching the kick; the outgoing vocal is pulled before the incoming track arrives | Stems ready |
| **Vocal handoff** | The outgoing vocal keeps singing over the incoming backing until its phrase ends; then the other vocal comes in | Stems; harmony ≥ 0.85; the outgoing track sings during the mix and the incoming one does not |
| **Short mix** | 4 bars of overlap, for pairs whose keys clash | Worker |
| **Echo cut** | The outgoing track is cut on a bar with its echo and the incoming one starts at its own tempo | Worker (last resort with tempos >~30 % apart) |

**Fallback ladder.** The preferred strategy, then those that fit (with variety: the previous one is not repeated, and with stems ready *Stems* and *Vocal handoff* go first), then *Short mix*, then *Echo cut*. The **soft fade** (about 6 s, no hard cuts) is left only for when there is no worker or no detected bars; its note in the log states the reason (“tempos too far apart…”, “no worker…”).

### Phrase alignment

Mixes with bass always start at a phrase boundary of the outgoing track: they prefer a 32-bar block, then 16, 8 and 4, and only as a last resort any bar. The bass swap lands halfway through the mix and is made to coincide with two musical points: the **start of A's outro** (its bass leaves right when it is no longer needed) and the **end of B's intro** (its bass arrives as its body begins). The volume curves are constant-power, so the sum does not dip mid-mix.

### Adaptive mixing (tempo and key)

Before, two tracks more than 6 % apart in tempo ended in a fade. Now they are **adapted**, as with *master tempo*:

- **Tempo.** A common tempo is chosen (the outgoing track is left alone if the difference is under 6 %; if larger, they meet halfway), with up to 15 % deviation per track and also trying double and half (70 ↔ 140). The outgoing track speeds up or slows down over a ramp of 4 to 12 bars, they play together, and the incoming track **recovers its original tempo** with another ramp, without changing pitch.
- **Key.** If they clash and both are reliable, the incoming track is shifted only during the mix and glides back to its own pitch.
- **Grid.** Bars are fitted by least squares so the kicks of both tracks coincide (~1 ms error in synthetic tests); a track with an irregular grid is not aligned.

### Shortened songs

With `MIX_SHORTEN=true` (default) songs are not left whole: they play **until the end of the first chorus** and the mix starts there, so the incoming track takes the following verse. With no chorus detected, it cuts at a phrase start around 55 %. With `false`, the mix happens at the outro.

### Stems and vocal handoff

With Demucs in the worker, three sections of each song are separated (drums, bass, harmony, vocals): the **beginning**, the **end** (80 s) and the one around the **cut point**. It is done in the background for the song playing, the next one and the first 3 in the queue, and the **vocal activity** of each section is stored. If the stems arrive late the pending mix is redone; if they never arrive, EQ is used.

## 7. Worker and rendering

Analysis, lyrics, stems and rendering run in a **worker**: a separate process on the same machine that takes jobs from a SQLite queue. It has five lanes: `render` (top priority: a mix never waits behind an analysis), `analysis` (what you ask for is analyzed right away), `lyrics`, `stems` (for what will play soon) and `background` (the catalog, with low priority and pauses to avoid saturating the CPU).

For each transition the worker produces **a WAV clip with both tracks already mixed**: tempo, pitch, EQ, stems and echo included. The session requests it about 18 s ahead (10 s for “mix now”). If the clip does not arrive in time or fails, that pair falls back to the **classic mix** (the backend serves the already-stretched tracks and the browser applies volumes and EQ) and is not retried. The browser plays the outgoing track as is, the clip (with a 40 ms crossover) and the incoming track from where the clip ends.

Without Docker, `AUTODJ_WORKER=inline` (the default) runs it in threads of the API itself; with `external`, in another process (`python -m autodj.worker`). `/api/health` reports which workers are alive and what they can do.

## 8. Session and interface

**Session.** The backend keeps **a single session** shared by all clients. It starts from the start screen (autonomous playback, an attached track or a typed suggestion) and **“End session” wipes it completely**: queue, history, chat, logs, decisions, bans and the rendered clips, and it cancels any query or download in progress. The library and the transition memory across sessions are kept. Stray browser messages arriving after ending are ignored.

**Interface.** Two screens: start and session. The session shows two decks that spin with the real playback, with each track's waveform, its grid and the scheduled mix zone. Below are the **queue** (only what the user requests; reorderable until a track enters the mix) with its **history** of up to 100 songs, the **virtual library** and web search, the **chat** and **observability**. There are light and dark themes, and a **language (ES/EN)** selector on the start screen and in the header.

**Language.** The original text is in Spanish and acts as the key: the interface translates it with one dictionary (`frontend/src/app/core/i18n.en.ts`) and the backend with another (`backend/autodj/catalog_en.py`); anything missing from the dictionary is shown in Spanish. The choice is stored in the browser and sent to the server in a `lang` message, so its notices, the reasons for each mix and the chat replies come out in that language (the AI is explicitly told to answer in English). The server's language is **a single one**, like the session. The worker, being another process, receives the language with each job. A test (`tests/test_i18n.py`) checks that every text wrapped in `tr()` has its translation.

## 9. Project evolution

The initial specification scoped a simple v1. Almost everything it left out of scope —stems, more strategies, vocals— ended up in within a week.

| Aspect | In the specification | Today |
| --- | --- | --- |
| Strategies | 3 (bars, EQ, wordplay) + fade | 7, with a fallback ladder; the fade is the last resort |
| Tempo | Requires ΔBPM ≤ 6 % | Adaptive: up to ~15 % per track, double/half, ramps and pitch shifting |
| Where it mixes | At the outro | After the first chorus (shortened songs) |
| Vocals and stems | Out of scope | Demucs in the worker: *Stems* and *Vocal handoff* |
| Who mixes | The browser (volumes and EQ), with tracks stretched by the backend | The worker renders a clip; the browser just plays it (the classic mix remains as a fallback) |
| Processes | `backend` and `frontend` | Plus a `worker` |
| AI | One LLM | Provider chain; it chooses among options and the choice is executed |
| Sources | Audius, Jamendo and own files | Plus SoundCloud by chat, for personal use |
| History, end session | — | History tab; the session is truly wiped |
| Language | Spanish | Spanish and English, with a selector in the interface |

**Timeline**

- **20 Oct.** Initial specification.
- **21 Oct.** Works locally with Docker Compose. The *drop mix* is removed (too abrupt); the fallback is a soft fade of about 6 s.
- **22 Oct.** Adaptive mixing and a separate worker; stems with Demucs and the *Stems* strategy.
- **23 Oct.** Fewer fades (short mix and echo cut), songs shortened after the chorus, wordplay with EQ, vocal handoff, history and SoundCloud by chat.
- **24 Oct.** **The AI really decides.** Before, it almost never got to: the free model available had about 20 requests a day and often answered 503, and every re-plan cancelled the query in progress. It was replaced by the provider chain, concrete options with one query per song, per-transition transparency and the per-visitor chat limit.
- **26 Oct.** “End session” truly wipes the state, on server and browser. Bilingual interface (ES/EN) and documentation in both languages.

**Decisions and discards**

- *Drop mix*: removed for being abrupt.
- *madmom* (bars): discarded for being unmaintained and not installing on current Python. Essentia plus an own analysis is used.
- *Java* for the backend: discarded for lack of mature music-analysis libraries.
- *Spotify, Apple Music, YouTube*: discarded because of DRM.
- *AI context as JSON*: replaced by compact text, because of the free provider's token quota.
- *Scoring candidates by real “mixability”* (adaptation cost): considered and not chosen for now.

## 10. Limitations and open items

- **A single shared session.** Meant for local use. In a public deployment all visitors would see and control the same session; a session per visitor or a collective “radio” would be needed.
- **SoundCloud.** The `search_soundcloud`/`add_soundcloud` tools and the “Search the web” button download audio with yt-dlp. SoundCloud does not allow redistributing it: it is for personal, local use only, and is not suitable for a public version (which should stay with Audius/Jamendo).
- **Worker and deployment.** API and worker share a volume and a SQLite queue: they must be on the same machine. On a single-container host you must use `AUTODJ_WORKER=inline`, which is slower; the worker with Demucs needs about 4 GB of RAM.
- **Not tuned by ear.** Parameters still to be tuned with real music: the clipping `MIN_PLAY`/`MIN_TAIL`, the wordplay tolerance (90 ms), the wordplay EQ curves and the vocal threshold of the handoff.
- **Not thoroughly validated:** BPM and key reliability on reggaeton, and lyrics transcription in Spanish without stems.
- **Language.** What has already been written (chat, logs, history) is not re-translated when the language changes; only new content is. The reasons given by the AI depend on the model obeying the language instruction, and rare internal errors may come out in Spanish.
- **Quotas.** Free models change their limits often; fallback mode keeps the DJ from stopping, but without AI there is no chat.

## 11. Development

```bash
# Backend (requires ffmpeg with the rubberband filter; Debian/Ubuntu ships it)
cd backend
pip install -r requirements-dev.txt
pip install -r requirements-analysis.txt   # optional: Essentia and Whisper
pip install -r requirements-stems.txt      # optional: Demucs (install torch for CPU first)
AUTODJ_DATA_DIR=./data uvicorn autodj.api:app --reload --port 8000    # inline worker

# Frontend
cd frontend && npm install && npm start    # http://localhost:4200, proxied to :8000
```

**Tests:** `cd backend && python -m pytest` (or `python -m unittest discover -s tests -t .`). There are over 200 (plus one that verifies the translations): Camelot and scoring, strategies, analysis with synthetic songs, library with real ffmpeg, tempo and pitch adaptation, rendering (kick alignment included), stems, job queue, session (queue, history, failures, ending the session, chat → SoundCloud → queue) and the AI (provider switching, fallback mode, per-visitor limit).

All configuration is commented in [`.env.example`](../.env.example).

## 12. Third-party licenses

AutoDJ's code is MIT ([`LICENSE`](../LICENSE)). Dependencies keep their own, and some are *copyleft*:

| Dependency | License | How it is used |
| --- | --- | --- |
| [Essentia](https://essentia.upf.edu/documentation.html) | AGPL v3 (proprietary license available on request) | Optional, in the worker |
| [Rubber Band](https://breakfastquay.com/rubberband/license.html) | GPL v2 or later (commercial license available) | Via the system's ffmpeg |
| Demucs, faster-whisper, FastAPI, Angular | Permissive (MIT) | — |

Publishing this code does not redistribute those libraries, but **distributing prebuilt Docker images** (which do include them) may impose obligations: review them first. The music carries each artist's license, and AutoDJ always shows the author and the link to the original.
