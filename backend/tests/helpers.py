from __future__ import annotations

from autodj.models import Segment, Track, Word


def make_track(tid: str, bpm: float = 100.0, camelot: str = "8A", duration: float = 200.0,
               energy: float = 6.0, artist: str | None = None, genre: str = "pop",
               mood: float = 0.0, words: list[tuple[str, float]] | None = None,
               drop_at: float | None = 60.0, phrase_offset: int = 0, intro_end: float | None = None,
               outro_start: float | None = None) -> Track:
    beat = 60.0 / bpm
    beats = [round(i * beat, 4) for i in range(int(duration / beat))]
    downbeats = beats[::4]
    segs = [Segment("intro", 0.0, 16 * 4 * beat, 0.3)]
    if drop_at is not None:
        segs.append(Segment("drop", drop_at, drop_at + 30, 0.9, True))
    segs.append(Segment("outro", duration - 30, duration, 0.3))
    return Track(
        id=tid, source="local", stream_ref=f"/x/{tid}.mp3", title=f"Song {tid}",
        artist=artist or f"Artist {tid}", genre=genre, duration=duration, status="ready",
        bpm=bpm, bpm_conf=0.9, camelot=camelot, key_conf=1.0, energy=energy, mood=mood,
        loudness_lufs=-10.0, beats=beats, downbeats=downbeats, segments=segs,
        phrase_offset=phrase_offset, intro_end=intro_end, outro_start=outro_start,
        lyrics_words=[Word(w, t, t + 0.4, 0.9) for w, t in (words or [])],
        lyrics_status="ready" if words else "none",
    )
