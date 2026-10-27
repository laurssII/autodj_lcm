"""English text for every message the backend shows. The key is the Spanish original; placeholders must match."""

from __future__ import annotations

EN: dict[str, str] = {
    # strategies
    "Por compases": "Bar-matched",
    "EQ mixing": "EQ mixing",
    "Wordplay": "Wordplay",
    "Stems": "Stems",
    "Relevo de voz": "Vocal handoff",
    "Mezcla corta": "Short mix",
    "Corte con eco": "Echo cut",
    "Fundido": "Fade",
    "subida progresiva": "gradual build-up",
    "olas de energía": "energy waves",
    "mantener arriba": "keep it high",
    "no cabe una mezcla en los puntos de entrada y salida disponibles":
        "no mix fits between the available entry and exit points",
    "corte sobre el compás: la saliente se va con eco y la entrante arranca a su propio tempo":
        "cut on the bar: the outgoing track leaves with an echo and the incoming one starts at its own tempo",
    "enlaza en «{word}» dentro de una mezcla por EQ de {bars} compases":
        "links on “{word}” inside a {bars}-bar EQ mix",
    "no se detectaron compases suficientes en una de las dos": "not enough bars were detected in one of the two",
    "tempo demasiado distinto ({a} vs {b} BPM, {gap} % de diferencia; máximo {max} %)":
        "tempos too far apart ({a} vs {b} BPM, {gap} % difference; maximum {max} %)",
    "tonalidades que chocan ({ka} → {kb}, fiabilidad {ca}/{cb}) y no se arreglan con ±{n} semitonos":
        "clashing keys ({ka} → {kb}, confidence {ca}/{cb}) that ±{n} semitones cannot fix",
    "; tempo común {bpm} BPM": "; shared tempo {bpm} BPM",
    "; entrante {n} semitonos": "; incoming {n} semitones",
    "no hay worker con Rubber Band disponible (mezcla clásica: ±6 % de tempo)":
        "no worker with Rubber Band available (classic mix: ±6 % tempo)",
    "falta el tempo de una de las pistas": "the tempo of one of the tracks is missing",
    "entrante {n} semitonos durante la mezcla": "incoming {n} semitones during the mix",
    "tempo común {bpm} BPM": "shared tempo {bpm} BPM",
    " (doble/mitad)": " (double/half)",
    "fundido de {bars} compases con intercambio de graves": "{bars}-bar fade with a bass swap",
    "EQ de {bars} compases: graves cambian a mitad, luego se vacía la saliente":
        "{bars}-bar EQ: bass swaps halfway, then the outgoing track drains away",
    "stems de {bars} compases: bajo y armonía cambian a mitad, las voces no se pisan":
        "{bars}-bar stems: bass and harmony swap halfway, vocals never overlap",
    "relevo de voz de {bars} compases: la voz de la saliente sigue sobre la base de la entrante hasta el compás {bar}":
        "{bars}-bar vocal handoff: the outgoing vocal carries on over the incoming backing until bar {bar}",
    "el tempo no permite mezclar a compás": "the tempo does not allow a bar-matched mix",
    "fundido suave: {why}": "soft fade: {why}",
    " al llegar la entrante a su cuerpo": " when the incoming track reaches its body",
    "; el cambio coincide con el cuerpo de la entrante": "; the swap lands on the body of the incoming track",
    "enlaza en la palabra «{word}»": "links on the word “{word}”",
    "mezcla corta de {bars} compases: las tonalidades chocan, así que se solapa lo mínimo":
        "short {bars}-bar mix: the keys clash, so the overlap is kept to a minimum",

    # analysis, library, sources
    "la pista dura menos de 20 s": "the track is shorter than 20 s",
    "Escribe al menos dos letras para buscar.": "Type at least two letters to search.",
    "pista no encontrada": "track not found",
    "clip no encontrado": "clip not found",
    "no se pudo preparar el audio: {why}": "could not prepare the audio: {why}",
    "Ha ocurrido un error inesperado. Inténtalo de nuevo.": "An unexpected error occurred. Please try again.",
    "confianza baja en BPM o tonalidad": "low confidence in BPM or key",
    "formato no soportado: {ext}": "unsupported format: {ext}",
    "el archivo supera {mb} MB": "the file exceeds {mb} MB",
    "sin extensión": "no extension",
    "código Camelot inválido": "invalid Camelot code",
    "el análisis falló": "the analysis failed",
    "artista desconocido": "unknown artist",
    "Esta pista tiene protección DRM (SoundCloud Go+) y no se puede usar.":
        "This track has DRM protection (SoundCloud Go+) and cannot be used.",
    "Solo se pueden añadir enlaces de SoundCloud.": "Only SoundCloud links can be added.",
    "Ese enlace no es una pista de SoundCloud.": "That link is not a SoundCloud track.",
    "Esta pista solo ofrece una previsualización de 30 s (SoundCloud Go+).":
        "This track only offers a 30 s preview (SoundCloud Go+).",
    "No se pudo buscar en SoundCloud: {why}": "Could not search SoundCloud: {why}",

    # rendering
    "la pista tiene muy pocos compases detectados": "the track has too few detected bars",
    "el corte cae fuera de la rejilla": "the cut falls outside the grid",
    "no hay compases suficientes antes del solapamiento en A": "not enough bars before the overlap in A",
    "rejilla de compases irregular (A {a_ms} ms / {a_ok} válidos, B {b_ms} ms / {b_ok} válidos)":
        "irregular bar grid (A {a_ms} ms / {a_ok} valid, B {b_ms} ms / {b_ok} valid)",
    "el estirado de {k} sale desalineado ({ms} ms)": "the stretch of {k} comes out misaligned ({ms} ms)",
    "el trabajo de renderizado desapareció": "the render job disappeared",
    "el clip no estuvo listo en {n} s": "the clip was not ready after {n} s",
    "el renderizado falló": "the render failed",

    # mix options and descriptions
    "{where}, a los {at}": "{where}, at {at}",
    "la entrante entra desde su {at}": "the incoming track enters from its {at}",
    "justo tras el primer estribillo de la que suena (la recorta)":
        "right after the first chorus of the current track (cuts it short)",
    "la entrante llega a su cuerpo justo en el cambio de graves":
        "the incoming track reaches its body right at the bass swap",
    "la entrante empieza por su intro": "the incoming track starts from its intro",
    "la entrante empieza desde el principio": "the incoming track starts from the beginning",
    "{label} de {bars} compases": "{label}, {bars} bars",
    "empieza {where}": "starts {where}",
    "dura unos {n} s": "lasts about {n} s",
    "en el outro de la que suena (la deja casi entera)": "in the outro of the current track (leaves it almost whole)",
    "tempo de la entrante ajustado un {pct} %": "incoming tempo adjusted by {pct} %",
    " (armonía {h})": " (harmony {h})",
    "energía {n}": "energy {n}",
    "ánimo {n}": "mood {n}",
    "puntuación {n}": "score {n}",
    "en el tramo final de la que suena": "in the final stretch of the current track",
    "a mitad de la que suena (la recorta bastante)": "halfway through the current track (cuts it quite short)",

    # decisions
    "modo reserva": "fallback mode",
    "sorteo entre las de energía media": "draw among the mid-energy tracks",
    "decide la IA ({provider})": "the AI decides ({provider})",
    "única opción viable": "only viable option",
    "mezcla inmediata": "immediate mix",
    "buen arranque": "good opener",
    "encaja con el momento": "fits the moment",
    "es la primera de tu cola": "it is first in your queue",
    "sorteo ponderado entre las mejores": "weighted draw among the best",
    "es la única forma viable de mezclarla": "it is the only viable way to mix it",
    "es la única opción viable": "it is the only viable option",
    " (de tu cola)": " (from your queue)",
    "ya no daba tiempo a la variante elegida": "there was no longer time for the chosen variant",
    "petición del usuario": "user request",
    "  ← primera de la cola del usuario: suena sí o sí, tú eliges cómo mezclarla":
        "  ← first in the user's queue: it plays no matter what, you choose how to mix it",
    "La IA no pudo decidir: uso el modo reserva": "The AI could not decide: using fallback mode",
    "la IA está saturada o ha llegado a su límite de uso": "the AI is overloaded or has reached its usage limit",
    "la IA tarda demasiado en responder": "the AI is taking too long to answer",
    "la IA no está disponible": "the AI is not available",
    "Respondió {provider} tras fallar otros": "{provider} answered after others failed",
    "No hay candidatas válidas": "There are no valid candidates",
    "«{title}» aún se está analizando; elijo otra mientras tanto":
        "“{title}” is still being analyzed; I pick another one meanwhile",
    "la IA devolvió una opción que no existe ({what})": "the AI returned an option that does not exist ({what})",

    # adaptive mixing
    "«{title}» suena estirada (x{rate}) por una mezcla clásica anterior; la adaptativa necesita la pista a su tempo original":
        "“{title}” is playing stretched (x{rate}) because of a previous classic mix; the adaptive mix needs the track at its original tempo",
    "el renderizado de esta pareja falló antes": "rendering this pair failed before",
    "la mezcla adaptativa está desactivada (MIX_ADAPTIVE)": "adaptive mixing is disabled (MIX_ADAPTIVE)",
    "no hay ningún worker vivo (¿está en marcha el servicio worker?)":
        "no worker is alive (is the worker service running?)",
    "al worker le falta {what}": "the worker lacks {what}",
    "hay que rehacer el clip": "the clip has to be redone",
    "el clip llegó tarde": "the clip arrived late",
    "ya están los stems: misma canción, mezcla con stems": "the stems are ready: same song, stem mix",
    "Ajusto la mezcla decidida ({why}); la canción sigue siendo la elegida":
        "Adjusting the planned mix ({why}); the song stays the same",
    "Mezcla clásica: {why}": "Classic mix: {why}",
    "No se pudo renderizar la mezcla: {why}; uso la mezcla clásica":
        "Could not render the mix: {why}; using the classic mix",
    "El navegador no pudo cargar el clip de la mezcla; uso la mezcla clásica":
        "The browser could not load the mix clip; using the classic mix",
    "Preparando la mezcla ({label}, {bars} compases)…": "Preparing the mix ({label}, {bars} bars)…",
    "Mezcla: {label} en {at}s": "Mix: {label} at {at}s",

    # session log and events
    "Sesión iniciada · estilo: {style}": "Session started · style: {style}",
    "Primera canción: {title} · {by}": "First song: {title} · {by}",
    "IA no configurada (añade una clave en .env)": "AI not configured (add a key to .env)",
    "IA no configurada": "AI not configured",
    "Siguiente: {title}": "Next: {title}",
    "Suena: {title} — {artist}": "Now playing: {title} — {artist}",
    "Transición en marcha": "Transition under way",
    "No se pudo cargar {title}": "Could not load {title}",
    "No he podido cargar «{title}». Sigo con otra.": "I could not load “{title}”. Moving on to another one.",
    "Sin pistas para empezar: esperando al análisis": "No tracks to start with: waiting for the analysis",
    "La biblioteca todavía no tiene pistas listas. Arranco en cuanto se analice la primera; mientras, puedes adjuntar una tuya.":
        "The library has no ready tracks yet. I will start as soon as the first one is analyzed; meanwhile you can attach one of your own.",
    "No encuentro la pista que has adjuntado. Arranco yo con algo.":
        "I cannot find the track you attached. I will start with something myself.",
    "No he podido analizar «{title}». Arranco yo con algo.":
        "I could not analyze “{title}”. I will start with something myself.",
    "Analizando «{title}»… arranco en cuanto termine.": "Analyzing “{title}”… I will start as soon as it finishes.",
    "«{title}» ya está analizada: suena a continuación.": "“{title}” is analyzed: it plays next.",
    "«{title}» no se pudo analizar": "“{title}” could not be analyzed",
    "Mezcla inmediata pedida desde la interfaz": "Immediate mix requested from the interface",
    " · motivo: {why}": " · reason: {why}",
    " y ": " and ",

    # chat and queue actions
    "«{title}» en la posición {pos} de la cola{ready}": "“{title}” at position {pos} of the queue{ready}",
    " (en cuanto termine su análisis)": " (as soon as its analysis finishes)",
    "quitada de la cola": "removed from the queue",
    "{n} resultados": "{n} results",
    "{n} resultados en SoundCloud": "{n} results on SoundCloud",
    "SoundCloud no devuelve nada reproducible": "SoundCloud returns nothing playable",
    "mezclo en el próximo inicio de frase": "I will mix at the next phrase start",
    "ambiente ajustado": "mood adjusted",
    "no pondré más a {value}": "I will not play {value} any more",
    "canción vetada": "song vetoed",
    "estrategia preferida: {strategy}": "preferred strategy: {strategy}",
    "no existe esa pista": "that track does not exist",
    "no existe esa pista (usa su id)": "that track does not exist (use its id)",
    "«{title}» ya está en la cola": "“{title}” is already in the queue",
    "«{title}» ya está sonando": "“{title}” is already playing",
    "Añadida a la cola: {title}": "Added to the queue: {title}",
    "no está en la cola": "it is not in the queue",
    "ya está entrando en la mezcla": "it is already entering the mix",
    "Cola reordenada": "Queue reordered",
    "Ahora mismo no puedo entender mensajes de texto; sigo pinchando por mi cuenta.":
        "I cannot understand text messages right now; I keep spinning on my own.",
    " (Para hablar conmigo por chat, añade una clave de IA en el archivo .env, por ejemplo GROQ_API_KEY.)":
        " (To chat with me, add an AI key to the .env file, for example GROQ_API_KEY.)",
    "herramienta desconocida: {name}": "unknown tool: {name}",
    "hace falta algo que buscar": "something to search for is needed",
    "no se pudo añadir la pista": "could not add the track",
    "no suena nada todavía": "nothing is playing yet",
    "ya estoy mezclando": "I am already mixing",
    "Herramienta {name}": "Tool {name}",
    "No he podido entender bien la petición. ¿Me la dices de otra forma?":
        "I could not quite understand the request. Could you say it another way?",
    "Hecho: {what}": "Done: {what}",
    "Hecho: {msg}.": "Done: {msg}.",
    "No he podido añadirla: {msg}.": "I could not add it: {msg}.",
    "La IA no responde ahora mismo ({why}), pero he añadido tu pista a la cola ({msg}).":
        "The AI is not answering right now ({why}), but I added your track to the queue ({msg}).",
    "La IA no responde ahora mismo ({why}). La música sigue; prueba de nuevo en un rato.":
        "The AI is not answering right now ({why}). The music goes on; try again in a while.",
    "La IA no respondió al chat": "The AI did not answer the chat",

    "yt-dlp no generó el archivo de audio": "yt-dlp did not produce the audio file",
    "ffmpeg no está instalado": "ffmpeg is not installed",
    "ffmpeg no devolvió audio": "ffmpeg returned no audio",

    # LLM providers and limits
    "sin conexión: {what}": "no connection: {what}",
    "sin conexión o tiempo agotado: {what}": "no connection or timed out: {what}",
    "presupuesto diario de llamadas agotado ({n})": "daily call budget exhausted ({n})",
    "no hay ningún proveedor de IA con clave configurada": "no AI provider has a key configured",
    "todos los proveedores están enfriando (el primero vuelve en {n} s)":
        "all providers are cooling down (the first returns in {n} s)",
    "tardó más de {n} s": "took longer than {n} s",
    "error inesperado: {what}: {why}": "unexpected error: {what}: {why}",
    "ningún proveedor respondió: {what}": "no provider answered: {what}",
    "Has llegado al límite de {n} mensajes por hoy. ¡Mañana más!":
        "You have reached the limit of {n} messages for today. More tomorrow!",
    "Vas muy rápido: como mucho {n} mensajes por minuto. Prueba otra vez en {wait} s; la música sigue.":
        "You are going too fast: at most {n} messages per minute. Try again in {wait} s; the music goes on.",

    # reasons for re-planning
    "ya están los stems de la pareja": "the stems for the pair are ready",
    "el usuario pidió mezclar ya": "the user asked to mix now",
    "empieza una canción nueva": "a new song starts",
    "falló el clip de la mezcla": "the mix clip failed",
    "falló la carga de la siguiente": "loading the next track failed",
    "cambió la cabeza de la cola": "the head of the queue changed",
    "la pista pedida terminó de analizarse": "the requested track finished analysis",
    "el usuario cambió el ambiente": "the user changed the mood",
    "el usuario vetó lo que suena": "the user vetoed what is playing",
    "el usuario vetó la siguiente": "the user vetoed the next track",
    "cambió la estrategia preferida": "the preferred strategy changed",
}
