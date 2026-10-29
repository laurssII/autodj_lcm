# AutoDJ
[![CI](https://github.com/laurssII/autodj_lcm/actions/workflows/ci.yml/badge.svg)](https://github.com/laurssII/autodj_lcm/actions/workflows/ci.yml)
*[English version below](#autodj-english)*

Un DJ automático que funciona en el navegador. Un agente de IA decide en vivo qué canción suena después y cómo mezclarla (a compás, por EQ, con las voces separadas…), y atiende peticiones por chat: «ponme algo para saltar», «algo más oscuro», «pínchame esta».

## Vídeos de demostración
Vídeo de demostración de algunas funcionalidades https://youtu.be/os2vvJZMr3I
Vídeo de demostración de reproducción desde archivo local https://youtu.be/lqcweLgDVrA


## Probarlo

Necesitas [Docker](https://docs.docker.com/get-docker/) con Compose v2.24 o posterior.

```bash
git clone https://github.com/laurssII/autodj_lcm.git autodj && cd autodj
cp .env.example .env          # opcional: ahí van las claves de la IA
docker compose up --build
```

Abre <http://localhost:8080> y pulsa **Reproducción autónoma**. La interfaz está en español e inglés (selector **ES / EN** arriba a la derecha).

- **La primera vez tarda.** Construir las imágenes puede descargar varios GB (el worker incluye PyTorch y Demucs) y los modelos de IA se bajan al usarlos por primera vez. Después, el arranque es rápido.
- **La música empieza en cuanto hay una pista analizada.** Al arrancar se importan en segundo plano unas 12 pistas libres de Audius (no hace falta clave); mientras tanto puedes adjuntar tus propios archivos de audio.
- **Para empezar de cero** (borra biblioteca, análisis y modelos descargados): `docker compose down -v`.

## La IA (opcional, pero es lo divertido)

Sin claves, AutoDJ pincha igualmente en «modo reserva» (sorteo ponderado) pero **no entiende el chat**. Con una clave gratuita de [Groq](https://console.groq.com/keys) (sin tarjeta) ya decide y conversa; con las tres siguientes, aguanta más:

```bash
# .env
GROQ_API_KEY=…       # console.groq.com/keys
GEMINI_API_KEY=…     # aistudio.google.com/apikey
GITHUB_TOKEN=…       # token fine-grained con el permiso «Models: read»
```

Reinicia (`docker compose up -d`) y comprueba que responden con:
`docker compose run --rm backend python -m autodj.agent.check`

## Qué mirar

- El panel de **Observabilidad**: cada opción que vio la IA, la que eligió y por qué.
- Pídele cosas al **chat**, incluida una canción concreta que no esté en la biblioteca.
- **Mezclar ya**, la pestaña **Historial** y el botón **Terminar sesión** (borra todo lo de la sesión).

## Consideraciones

- **SoundCloud (solo uso personal).** Si pides una canción que no está en la biblioteca, o usas «Buscar en la web», AutoDJ la busca en SoundCloud y la descarga con [yt-dlp](https://github.com/yt-dlp/yt-dlp). SoundCloud no permite redistribuir su audio: úsalo en local, con música que tengas derecho a escuchar, y no lo despliegues en público tal cual. Si no lo usas, no se activa.
- **Música libre.** Las pistas del catálogo (Audius, y Jamendo si pones `JAMENDO_CLIENT_ID`) son de sus artistas; AutoDJ muestra siempre el autor y el enlace al original. Jamendo solo permite uso no comercial.
- **Recursos.** El worker puede usar hasta 3 CPU y 4 GB de RAM (`WORKER_CPUS`, `WORKER_MEM` en `.env`). Sin stems o sin worker, las mezclas siguen funcionando con EQ o fundidos.
- **Una sola sesión.** El backend tiene una sesión de DJ compartida por todo el que abra la web: está pensado para uso local, no para varios visitantes a la vez.
- **Claves.** Nunca se suben al repositorio (`.env` está en `.gitignore`).

## Más información

- [`docs/ESPECIFICACIONES.md`](docs/ESPECIFICACIONES.md) ([English](docs/SPECIFICATIONS.md)): cómo funciona por dentro, especificaciones y cómo ha evolucionado.
- Desarrollo sin Docker y tests: ver la sección «Desarrollo» de ese documento.

## Licencia

[MIT](LICENSE) para el código. La música y las dependencias de terceros tienen sus propias licencias.

---

<a id="autodj-english"></a>

# AutoDJ (English)

*[Versión en español arriba](#autodj)*

An automatic DJ that runs in the browser. An AI agent decides live which song plays next and how to mix it (bar-matched, EQ-based, with separated vocals…), and takes requests through chat: “play something to dance to”, “something darker”, “play this one”.

<!-- Demo video: uncomment and add the link
**▶ [Watch the video demo](LINK)**
-->

## Try it

You need [Docker](https://docs.docker.com/get-docker/) with Compose v2.24 or later.

```bash
git clone https://github.com/laurssII/autodj_lcm.git autodj && cd autodj
cp .env.example .env          # optional: AI keys go here
docker compose up --build
```

Open <http://localhost:8080> and click **Autonomous playback**. The interface comes in Spanish and English (**ES / EN** selector at the top right).

- **The first run takes a while.** Building the images can download several GB (the worker includes PyTorch and Demucs) and the AI models are fetched on first use. After that, startup is fast.
- **Music starts as soon as one track is analyzed.** On startup about 12 free Audius tracks are imported in the background (no key needed); meanwhile you can attach your own audio files.
- **To start from scratch** (deletes the library, analyses and downloaded models): `docker compose down -v`.

## The AI (optional, but it is the fun part)

Without keys, AutoDJ still plays in “fallback mode” (weighted draw) but **does not understand the chat**. A free [Groq](https://console.groq.com/keys) key (no card) is enough for it to decide and talk; with the next two it holds up longer:

```bash
# .env
GROQ_API_KEY=…       # console.groq.com/keys
GEMINI_API_KEY=…     # aistudio.google.com/apikey
GITHUB_TOKEN=…       # fine-grained token with the “Models: read” permission
```

Restart (`docker compose up -d`) and check that they answer with:
`docker compose run --rm backend python -m autodj.agent.check`

## What to look at

- The **Observability** panel: every option the AI saw, the one it picked and why.
- Ask the **chat** for things, including a specific song that is not in the library.
- **Mix now**, the **History** tab and the **End session** button (it wipes everything from the session).

## Notes

- **SoundCloud (personal use only).** If you ask for a song that is not in the library, or use “Search the web”, AutoDJ looks it up on SoundCloud and downloads it with [yt-dlp](https://github.com/yt-dlp/yt-dlp). SoundCloud does not allow redistributing its audio: use it locally, with music you have the right to listen to, and do not deploy it publicly as is. If you do not use it, it is never triggered.
- **Free music.** The catalog tracks (Audius, and Jamendo if you set `JAMENDO_CLIENT_ID`) belong to their artists; AutoDJ always shows the author and the link to the original. Jamendo only allows non-commercial use.
- **Resources.** The worker may use up to 3 CPUs and 4 GB of RAM (`WORKER_CPUS`, `WORKER_MEM` in `.env`). Without stems or without the worker, mixes still work with EQ or fades.
- **Single session.** The backend has one DJ session shared by everyone who opens the web page: it is meant for local use, not for several visitors at once.
- **Keys.** They are never committed to the repository (`.env` is in `.gitignore`).

## More information

- [`docs/SPECIFICATIONS.md`](docs/SPECIFICATIONS.md) ([Español](docs/ESPECIFICACIONES.md)): how it works inside, specifications and how it evolved.
- Development without Docker and tests: see the “Development” section of that document.

## License

[MIT](LICENSE) for the code. The music and third-party dependencies have their own licenses.
