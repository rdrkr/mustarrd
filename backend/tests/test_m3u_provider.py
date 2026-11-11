"""M3U playlist accounts: parsing, catchup URLs, the HLS reader, the client factory.

The playlist shapes are taken from a real Flussonic-based provider (TV Team /
hls.gd): channels carry ``tvg-id``, ``group-title`` and ``timeshift="7"``, the
header names the guide in ``url-tvg``, and every stream and segment URL is
signed with a ``token`` query parameter. The HLS tests run against a local
aiohttp server, so the reader is exercised over real HTTP.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from aiohttp import web
from aiohttp.test_utils import TestServer

from services import m3u_client
from services.hls_source import (
    HLSProgress,
    HLSStatusError,
    is_hls_url,
    iter_hls,
    parse_media_playlist,
    pick_variant,
)
from services.m3u_client import (
    CATCHUP_APPEND,
    CATCHUP_FLUSSONIC,
    CATCHUP_SHIFT,
    M3UClient,
    build_catchup_url,
    parse_playlist,
)
from services.provider_client import is_m3u_account, make_provider_client
from services.xtream_client import XtreamClient

PLAYLIST = """#EXTM3U generation-date="2026.11.11" url-tvg="https://epg.example/3.1.xml.gz"
#EXTINF:0 tvg-id="ch2934" tvg-name="IL: Channel" group-title="15. Israel" tvg-logo="https://img/2934.png"  timeshift="7", IL: Channel
https://3.hls.example/ch2934/index.m3u8?token=abc
#EXTINF:0 tvg-id="ch1" group-title="News", News One
https://live.example/news/playlist.m3u8?token=n1
#EXTINF:-1 tvg-id="ch2" catchup="shift" catchup-days="3" group-title="News", Shifted
http://other.example/live/ch2.ts
#EXTINF:-1 catchup="append" catchup-days="2" catchup-source="?start={utc}&dur={duration}", No Id
http://append.example/stream.m3u8
#EXTINF:0 tvg-id="ch1", Duplicate Id
https://live.example/dup/index.m3u8
#EXTINF:0, Missing URL line
"""


class PlaylistParsingTests(unittest.TestCase):
    def setUp(self):
        self.playlist = parse_playlist(PLAYLIST)

    def test_guide_url_comes_from_the_header(self):
        self.assertEqual(self.playlist.epg_url, "https://epg.example/3.1.xml.gz")

    def test_entries_without_a_url_are_dropped(self):
        self.assertEqual(len(self.playlist.channels), 5)

    def test_flussonic_channel_is_detected_from_its_url(self):
        ch = self.playlist.by_id["ch2934"]
        self.assertEqual(ch.name, "IL: Channel")
        self.assertEqual(ch.group, "15. Israel")
        self.assertEqual(ch.archive_days, 7)
        self.assertEqual(ch.catchup_type, CATCHUP_FLUSSONIC)

    def test_channel_without_archive_has_no_catchup(self):
        self.assertEqual(self.playlist.by_id["ch1"].catchup_type, "")

    def test_declared_catchup_types_win(self):
        self.assertEqual(self.playlist.by_id["ch2"].catchup_type, CATCHUP_SHIFT)
        no_id = next(c for c in self.playlist.channels if c.name == "No Id")
        self.assertEqual(no_id.catchup_type, CATCHUP_APPEND)
        self.assertTrue(no_id.stream_id.startswith("m3u-"))

    def test_duplicate_ids_stay_unique(self):
        self.assertIn("ch1-2", self.playlist.by_id)
        self.assertEqual(self.playlist.by_id["ch1-2"].name, "Duplicate Id")


class CatchupUrlTests(unittest.TestCase):
    def setUp(self):
        self.playlist = parse_playlist(PLAYLIST)

    def test_flussonic_archive_form_keeps_the_token(self):
        url = build_catchup_url(self.playlist.by_id["ch2934"], 1794304600, 3600)
        self.assertEqual(url, "https://3.hls.example/ch2934/index-1794304600-3600.m3u8?token=abc")

    def test_shift_form_appends_the_window(self):
        url = build_catchup_url(self.playlist.by_id["ch2"], 1000, 60)
        self.assertEqual(url, "http://other.example/live/ch2.ts?utc=1000&lutc=1060")

    def test_append_form_fills_the_template(self):
        no_id = next(c for c in self.playlist.channels if c.name == "No Id")
        self.assertEqual(
            build_catchup_url(no_id, 1000, 60),
            "http://append.example/stream.m3u8?start=1000&dur=60",
        )

    def test_channel_without_archive_refuses(self):
        with self.assertRaises(ValueError):
            build_catchup_url(self.playlist.by_id["ch1"], 1000, 60)


class M3UClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        m3u_client.clear_playlist_cache()
        self.hits = 0

        async def playlist(_request):
            self.hits += 1
            body = PLAYLIST.replace("token=abc", f"token=fresh{self.hits}")
            return web.Response(text=body)

        app = web.Application()
        app.router.add_get("/pl.m3u8", playlist)
        self.server = TestServer(app)
        await self.server.start_server()
        self.url = str(self.server.make_url("/pl.m3u8"))

    async def asyncTearDown(self):
        await self.server.close()
        m3u_client.clear_playlist_cache()

    async def test_streams_have_the_xtream_shape(self):
        client = M3UClient(self.url)
        try:
            streams = await client.get_live_streams()
        finally:
            await client.close()
        ch = next(s for s in streams if s["stream_id"] == "ch2934")
        self.assertEqual(ch["tv_archive"], 1)
        self.assertEqual(ch["tv_archive_duration"], 7)
        self.assertEqual(ch["epg_channel_id"], "ch2934")
        self.assertEqual(ch["category_name"], "15. Israel")

    async def test_category_filter(self):
        client = M3UClient(self.url)
        try:
            categories = await client.get_live_categories()
            news = next(c for c in categories if c["category_name"] == "News")
            streams = await client.get_live_streams(news["category_id"])
        finally:
            await client.close()
        self.assertEqual({s["name"] for s in streams}, {"News One", "Shifted"})

    async def test_playlist_is_cached_between_clients(self):
        for _ in range(3):
            client = M3UClient(self.url)
            await client.get_live_streams()
            await client.close()
        self.assertEqual(self.hits, 1)

    async def test_authenticate_answers_in_xtream_shape(self):
        client = M3UClient(self.url)
        try:
            data = await client.authenticate()
        finally:
            await client.close()
        self.assertEqual(data["user_info"]["auth"], 1)
        self.assertIsNone(data["user_info"]["max_connections"])
        self.assertEqual(data["server_info"]["channels"], 5)

    async def test_timeshift_url_uses_utc_epoch_and_ignores_provider_start(self):
        client = M3UClient(self.url)
        try:
            await client.prepare()
        finally:
            await client.close()
        start = datetime(2026, 11, 10, 9, 56, 40, tzinfo=timezone.utc)
        url = client.build_timeshift_url("ch2934", start, 61, provider_start="2026-11-10:11-56", style="path")
        self.assertIn(f"/ch2934/index-{int(start.timestamp())}-3660.m3u8?token=fresh1", url)

    async def test_refresh_url_swaps_only_the_token(self):
        client = M3UClient(self.url)
        try:
            await client.prepare()
            stale = "https://3.hls.example/ch2934/index-1000-60.m3u8?token=expired"
            refreshed = await client.refresh_url(stale)
        finally:
            await client.close()
        self.assertEqual(refreshed, "https://3.hls.example/ch2934/index-1000-60.m3u8?token=fresh2")

    async def test_refresh_url_unknown_stream(self):
        client = M3UClient(self.url)
        try:
            self.assertIsNone(await client.refresh_url("https://nowhere.example/x/index.m3u8?token=1"))
        finally:
            await client.close()

    async def test_vod_is_empty(self):
        client = M3UClient(self.url)
        self.assertEqual(await client.get_vod_categories(), [])
        self.assertEqual(await client.get_epg("ch2934"), [])


class FactoryTests(unittest.TestCase):
    def test_m3u_account_gets_the_playlist_client(self):
        account = SimpleNamespace(provider_type="m3u", server_url="https://p/pl.m3u8", epg_url="https://e/x.xml", username="")
        client = make_provider_client(account)
        self.assertIsInstance(client, M3UClient)
        self.assertEqual(client.epg_url, "https://e/x.xml")
        self.assertTrue(is_m3u_account(account))

    def test_xtream_is_the_default(self):
        account = SimpleNamespace(provider_type=None, server_url="http://x", username="u")
        self.assertIsInstance(make_provider_client(account, "p"), XtreamClient)
        self.assertFalse(is_m3u_account(account))


SEGMENT_BYTES = {0: b"A" * 1000, 1: b"B" * 500, 2: b"C" * 700}


class HLSReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.live_polls = 0
        self.segment_failures = {1: 1}

        async def master(_request):
            return web.Response(text=(
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3000000\nhigh/index.m3u8\n"
            ))

        async def archive(_request):
            return web.Response(text=(
                "#EXTM3U\n#EXT-X-TARGETDURATION:7\n#EXT-X-MEDIA-SEQUENCE:100\n"
                "#EXT-X-PLAYLIST-TYPE:EVENT\n"
                "#EXTINF:6.0,\nseg0.ts?token=t\n"
                "#EXTINF:6.0,\nseg1.ts?token=t\n"
                "#EXTINF:3.0,\nseg2.ts?token=t\n"
                "#EXT-X-ENDLIST\n"
            ))

        async def live(_request):
            self.live_polls += 1
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{self.live_polls - 1}"]
            lines += ["#EXTINF:1.0,", f"seg{self.live_polls - 1}.ts"]
            if self.live_polls >= 3:
                lines.append("#EXT-X-ENDLIST")
            return web.Response(text="\n".join(lines))

        async def segment(request):
            n = int(request.match_info["n"])
            if self.segment_failures.get(n):
                self.segment_failures[n] -= 1
                return web.Response(status=502)
            return web.Response(body=SEGMENT_BYTES[n], content_type="video/mp2t")

        async def forbidden(_request):
            return web.Response(status=403)

        app = web.Application()
        app.router.add_get("/master.m3u8", master)
        app.router.add_get("/high/index.m3u8", archive)
        app.router.add_get("/archive.m3u8", archive)
        app.router.add_get("/live.m3u8", live)
        app.router.add_get("/high/seg{n}.ts", segment)
        app.router.add_get("/seg{n}.ts", segment)
        app.router.add_get("/denied.m3u8", forbidden)
        self.server = TestServer(app)
        await self.server.start_server()

    async def asyncTearDown(self):
        await self.server.close()

    async def _read(self, path, **kwargs):
        out = b""
        async for chunk in iter_hls(str(self.server.make_url(path)), **kwargs):
            out += chunk
        return out

    async def test_archive_is_concatenated_in_order_with_a_retried_segment(self):
        import services.hls_source as hls
        hls.SEGMENT_RETRY_DELAY, saved = 0.01, hls.SEGMENT_RETRY_DELAY
        try:
            progress = HLSProgress()
            data = await self._read("/archive.m3u8", progress=progress)
        finally:
            hls.SEGMENT_RETRY_DELAY = saved
        self.assertEqual(data, SEGMENT_BYTES[0] + SEGMENT_BYTES[1] + SEGMENT_BYTES[2])
        self.assertEqual((progress.segments_done, progress.segments_total), (3, 3))
        self.assertAlmostEqual(progress.fraction, 1.0)

    async def test_master_playlist_follows_the_best_variant(self):
        self.segment_failures = {}
        data = await self._read("/master.m3u8")
        self.assertEqual(len(data), 2200)

    async def test_growing_playlist_is_polled_until_it_ends(self):
        self.segment_failures = {}
        data = await self._read("/live.m3u8")
        self.assertEqual(data, SEGMENT_BYTES[0] + SEGMENT_BYTES[1] + SEGMENT_BYTES[2])

    async def test_refused_playlist_raises_with_its_status(self):
        with self.assertRaises(HLSStatusError) as caught:
            await self._read("/denied.m3u8")
        self.assertEqual(caught.exception.status, 403)


class HLSParsingTests(unittest.TestCase):
    def test_relative_segments_resolve_against_the_playlist(self):
        playlist = parse_media_playlist(
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n#EXTINF:6.0,\ndvr-2026/11/10/a.ts?token=x\n#EXT-X-ENDLIST\n",
            "https://3.hls.example/ch2934/index-1-60.m3u8?token=y",
        )
        self.assertTrue(playlist.ended)
        self.assertEqual(
            playlist.segments,
            [(5, "https://3.hls.example/ch2934/dvr-2026/11/10/a.ts?token=x", 6.0)],
        )

    def test_media_playlist_is_not_a_master(self):
        self.assertIsNone(pick_variant("#EXTM3U\n#EXTINF:6,\na.ts\n", "http://x/"))

    def test_is_hls_url(self):
        self.assertTrue(is_hls_url("https://x/ch/index-1-2.m3u8?token=a"))
        self.assertFalse(is_hls_url("http://x/timeshift/u/p/60/2026-01-01:10-00/1.ts"))
        self.assertFalse(is_hls_url(None))


if __name__ == "__main__":
    unittest.main()
