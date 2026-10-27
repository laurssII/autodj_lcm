"""Fuentes remotas: Audius, Jamendo y buscador de SoundCloud (sin red: yt-dlp y httpx simulados)."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

from autodj import sources
from autodj.config import Settings


class FakeYDL:
    """Imita lo mínimo de yt_dlp.YoutubeDL que usa sources.py."""

    calls: list[tuple[str, bool]] = []
    result: dict | None = None
    by_target: dict = {}  # objetivo → info (o excepción a lanzar)
    fail: Exception | None = None
    opts_seen: list[dict] = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, target, download=True):
        FakeYDL.calls.append((target, download))
        FakeYDL.opts_seen.append(self.opts)
        if FakeYDL.fail:
            raise FakeYDL.fail
        if download:
            # yt-dlp escribe el audio ya convertido a mp3 siguiendo outtmpl, y puede dejar restos
            Path(self.opts["outtmpl"].replace("%(ext)s", "mp3")).write_bytes(b"mp3")
            Path(self.opts["outtmpl"].replace("%(ext)s", "webm")).write_bytes(b"resto")
            return {"id": "1"}
        out = FakeYDL.by_target.get(target, FakeYDL.result)
        if isinstance(out, Exception):
            raise out
        return out


def _entry(i, **kw):
    return {"id": i, "title": f"Tema {i}", "uploader": "Artista", "duration": 200,
            "webpage_url": f"https://soundcloud.com/artista/tema-{i}", "thumbnail": "https://img/x.jpg", **kw}


class SoundcloudTest(unittest.TestCase):
    def setUp(self):
        FakeYDL.calls, FakeYDL.result, FakeYDL.fail = [], None, None
        FakeYDL.by_target, FakeYDL.opts_seen = {}, []
        sources._search_cache.clear()
        sources._resolved.clear()
        self._orig = sys.modules.get("yt_dlp")
        sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYDL)

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = self._orig

    def _serve(self, ids, drm=(), preview=()):
        """Prepara la búsqueda plana (solo URLs) y la respuesta de cada pista al resolverla."""
        FakeYDL.result = {"entries": [{"url": _entry(i)["webpage_url"]} for i in ids]}
        for i in ids:
            url = _entry(i)["webpage_url"]
            if i in drm:
                FakeYDL.by_target[url] = RuntimeError("ERROR: [soundcloud] 24: This video is DRM protected")
            elif i in preview:
                FakeYDL.by_target[url] = _entry(i, formats=[{"url": "https://cf-preview/preview/x.mp3"}])
            else:
                FakeYDL.by_target[url] = _entry(i, formats=[{"url": "https://cf/x.mp3"}])

    def test_las_pistas_con_drm_no_aparecen(self):
        self._serve([1, 2, 3, 4], drm=(2,), preview=(4,))
        res = sources.search_mainstream("tema artista", limit=5)
        self.assertEqual([r["id"] for r in res], ["soundcloud:1", "soundcloud:3"])  # mantiene el orden
        self.assertEqual(res[0]["url"], "https://soundcloud.com/artista/tema-1")

    def test_busca_una_vez_y_comprueba_cada_pista_sin_descargar(self):
        self._serve(range(1, 11))
        res = sources.search_mainstream("x", limit=8)
        self.assertEqual(len(res), 8)  # nunca más de las pedidas
        search, *checks = FakeYDL.calls
        self.assertEqual(search, ("scsearch12:x", False))  # 8 + 4 de margen por si se descartan
        self.assertEqual(len(checks), 10)
        self.assertTrue(all(download is False for _, download in FakeYDL.calls))  # nada se descarga
        self.assertEqual(FakeYDL.opts_seen[0]["extract_flat"], "in_playlist")
        self.assertIn("socket_timeout", FakeYDL.opts_seen[0])

    def test_busqueda_repetida_sale_de_la_cache_y_no_comparte_objetos(self):
        self._serve([1])
        first = sources.search_mainstream("Tema ", limit=8)
        first[0]["in_library"] = True  # la API añade campos: no debe contaminar la caché
        calls = len(FakeYDL.calls)
        second = sources.search_mainstream("tema", limit=8)
        self.assertEqual(len(FakeYDL.calls), calls)
        self.assertNotIn("in_library", second[0])

    def test_anadir_una_pista_ya_comprobada_no_vuelve_a_llamar_a_soundcloud(self):
        self._serve([1])
        meta = sources.search_mainstream("x")[0]
        calls = len(FakeYDL.calls)
        self.assertEqual(sources.resolve_soundcloud_url(meta["url"])["id"], meta["id"])
        self.assertEqual(len(FakeYDL.calls), calls)

    def test_miniatura_desde_la_lista_thumbnails(self):
        self._serve([1])
        FakeYDL.by_target[_entry(1)["webpage_url"]] = _entry(1, thumbnail=None, thumbnails=[
            {"url": "https://i1.sndcdn.com/a-large.jpg"}, {"url": "https://i1.sndcdn.com/a-t200x200.jpg"}])
        self.assertEqual(sources.search_mainstream("x")[0]["thumbnail"], "https://i1.sndcdn.com/a-t200x200.jpg")

    def test_resultados_repetidos_se_unifican(self):
        FakeYDL.result = {"entries": [{"url": "https://soundcloud.com/a/uno"}, {"url": "https://on.soundcloud.com/zz"}]}
        FakeYDL.by_target = {"https://soundcloud.com/a/uno": _entry(5), "https://on.soundcloud.com/zz": _entry(5)}
        self.assertEqual(len(sources.search_mainstream("x")), 1)

    def test_busqueda_sin_resultados_y_con_error(self):
        FakeYDL.result = {"entries": []}
        self.assertEqual(sources.search_mainstream("nada"), [])
        FakeYDL.fail = RuntimeError("sin red")
        with self.assertRaises(sources.MainstreamError):
            sources.search_mainstream("otra")

    def test_resolver_devuelve_metadatos(self):
        FakeYDL.result = _entry(7)
        meta = sources.resolve_soundcloud_url("https://soundcloud.com/artista/tema-7")
        self.assertEqual(meta["id"], "soundcloud:7")
        self.assertEqual(meta["title"], "Tema 7")

    def test_resolver_rechaza_drm_con_mensaje_claro(self):
        FakeYDL.fail = RuntimeError("ERROR: [soundcloud] 24: This video is DRM protected")
        with self.assertRaises(sources.MainstreamError) as cm:
            sources.resolve_soundcloud_url("https://soundcloud.com/a/b")
        self.assertIn("DRM", str(cm.exception))

    def test_resolver_rechaza_previsualizaciones(self):
        FakeYDL.result = _entry(8, formats=[{"url": "https://cf/preview/a.mp3"}, {"url": "https://cf/preview/b.mp3"}])
        with self.assertRaises(sources.MainstreamError):
            sources.resolve_soundcloud_url("https://soundcloud.com/a/b")

    def test_resolver_rechaza_otros_sitios_y_listas(self):
        for url in ("https://youtube.com/watch?v=x", "file:///etc/passwd", "https://evilsoundcloud.com/a",
                    "https://soundcloud.com.evil.io/a"):
            with self.assertRaises(sources.MainstreamError, msg=url):
                sources.resolve_soundcloud_url(url)
        self.assertEqual(FakeYDL.calls, [])  # ni siquiera se llama a yt-dlp
        FakeYDL.result = {"entries": [_entry(1)]}
        with self.assertRaises(sources.MainstreamError):
            sources.resolve_soundcloud_url("https://soundcloud.com/artista/sets/disco")

    def test_acepta_subdominios_de_soundcloud(self):
        self.assertTrue(sources.is_soundcloud_url("https://on.soundcloud.com/abc"))
        self.assertTrue(sources.is_soundcloud_url("https://m.soundcloud.com/a/b"))

    def test_descarga_por_url_deja_un_mp3_y_limpia_restos(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "soundcloud_123.mp3"
            out = sources.download(Settings(), "soundcloud", "https://soundcloud.com/a/b", dest)
            self.assertEqual(out, dest)
            self.assertEqual(dest.read_bytes(), b"mp3")
            self.assertEqual([p.name for p in Path(d).iterdir()], ["soundcloud_123.mp3"])
        self.assertEqual(FakeYDL.calls, [("https://soundcloud.com/a/b", True)])

    def test_descarga_con_el_formato_antiguo_busca_por_nombre(self):
        with tempfile.TemporaryDirectory() as d:
            sources.download(Settings(), "soundcloud", "Artista|||Tema", Path(d) / "t.mp3")
        self.assertEqual(FakeYDL.calls, [("scsearch1:Artista - Tema", True)])

    def test_descarga_fallida_no_deja_archivos(self):
        FakeYDL.fail = RuntimeError("sin red")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                sources.download(Settings(), "soundcloud", "https://soundcloud.com/a/b", Path(d) / "t.mp3")
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_descarga_no_acepta_urls_ajenas(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                sources.download(Settings(), "soundcloud", "https://example.com/x", Path(d) / "t.mp3")


class FakeResponse:
    def __init__(self, payload=None, bad_json=False):
        self.payload, self.bad_json = payload, bad_json

    def raise_for_status(self):
        pass

    def json(self):
        if self.bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self.payload


class FakeClient:
    def __init__(self, responder):
        self.responder, self.urls = responder, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        self.urls.append(url)
        return self.responder(url, params or {})


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self._client = sources._client

    def tearDown(self):
        sources._client = self._client

    def _patch(self, responder) -> FakeClient:
        fc = FakeClient(responder)
        sources._client = lambda settings: fc
        return fc

    def test_audius_usa_la_api_y_no_se_rompe_con_respuestas_que_no_son_json(self):
        def responder(url, params):
            if params["genre"] == "Pop":
                return FakeResponse(bad_json=True)
            if params["genre"] == "Latin":
                return FakeResponse({"data": [
                    {"id": "abc", "title": "Tema", "duration": 200, "is_streamable": True,
                     "user": {"name": "Artista"}, "permalink": "/artista/tema", "genre": "Latin"},
                    {"id": "corta", "title": "Intro", "duration": 20},
                ]})
            return FakeResponse({"data": []})

        fc = self._patch(responder)
        tracks = sources.audius_tracks(Settings(), 10)
        self.assertEqual([t.id for t in tracks], ["audius:abc"])
        self.assertEqual(tracks[0].credit_url, "https://audius.co/artista/tema")
        self.assertTrue(all(u.startswith("https://api.audius.co/v1/tracks/trending") for u in fc.urls))

    def test_urls_de_stream_y_jamendo_apuntan_a_las_apis(self):
        s = Settings()
        self.assertEqual(sources.audius_stream_url(s, "abc"),
                         f"https://api.audius.co/v1/tracks/abc/stream?app_name={s.audius_app_name}")
        self.assertTrue(sources.JAMENDO_API.startswith("https://api.jamendo.com/"))

    def test_jamendo_sin_client_id_no_hace_peticiones(self):
        fc = self._patch(lambda u, p: FakeResponse({"results": []}))
        s = Settings()
        s.jamendo_client_id = ""
        self.assertEqual(sources.jamendo_tracks(s), [])
        self.assertEqual(fc.urls, [])

    def test_jamendo_con_client_id(self):
        def responder(url, params):
            return FakeResponse({"results": [
                {"id": "9", "name": "Tema", "artist_name": "Artista", "duration": 180,
                 "audio": "https://mp3/9.mp3", "shareurl": "https://jamendo/9", "license_ccurl": "cc"}]})

        fc = self._patch(responder)
        s = Settings()
        s.jamendo_client_id = "x"
        tracks = sources.jamendo_tracks(s, 5)
        self.assertEqual([t.id for t in tracks], ["jamendo:9"])
        self.assertTrue(fc.urls[0].startswith("https://api.jamendo.com/v3.0/tracks/"))


if __name__ == "__main__":
    unittest.main()
