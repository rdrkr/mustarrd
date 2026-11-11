"""Large guides are staged on disk, never held whole in memory.

A seven-day XMLTV feed (epg.team's 7.7.xml) is 567 MB of XML with 1.28 million
programmes. Holding it - plus the copies that CDATA-stripping and entity
sanitising each made - got the process OOM-killed in a 2 GB container and left
it crash-looping, since every restart retried the refresh. These tests pin the
streaming preparation: same sanitising and same programme count as the
in-memory path, whatever the chunk boundaries and compression.
"""
import gzip
import os
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.epg_ingest_manager as eim
from services.epg_ingest_manager import EPGIngestManager, _prepare_xmltv_file, _sanitize_html_entities

DOC = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'
    b'<channel id="ch2934"><display-name>IL: Channel &amp; Co</display-name></channel>\n'
    + b"".join(
        b'<programme channel="ch2934" start="20261108%02d0000 +0000" stop="20261108%02d3000 +0000">'
        b"<title>\xd7\xa4\xd7\xa8\xd7\xa7 %d &nbsp;R&B</title>"
        b"<desc><![CDATA[fake <programme> inside CDATA\n spanning lines]]></desc></programme>\n"
        % (h % 24, h % 24, h)
        for h in range(40)
    )
    + b"</tv>\n"
)


class PrepareXmltvFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.raw = os.path.join(self.dir, "raw")
        self.out = os.path.join(self.dir, "out.xml")

    def _prepare(self, data: bytes, chunk: int = 97):
        with open(self.raw, "wb") as f:
            f.write(data)
        with patch.object(eim, "_STREAM_CHUNK", chunk):
            return _prepare_xmltv_file(self.raw, self.out)

    def test_matches_the_in_memory_sanitiser_at_any_chunk_size(self):
        for chunk in (7, 64, 97, 4096):
            with self.subTest(chunk=chunk):
                size, programmes = self._prepare(DOC, chunk)
                written = Path(self.out).read_bytes()
                self.assertEqual(written, _sanitize_html_entities(DOC))
                self.assertEqual(size, len(written))
                self.assertEqual(programmes, 40)

    def test_gzip_and_deflate_are_inflated(self):
        for data in (gzip.compress(DOC), zlib.compress(DOC), zlib.compress(DOC)[2:-4]):
            with self.subTest(head=data[:2]):
                _, programmes = self._prepare(data)
                self.assertEqual(Path(self.out).read_bytes(), _sanitize_html_entities(DOC))
                self.assertEqual(programmes, 40)

    def test_empty_and_corrupt_guides_are_reported_as_nothing(self):
        self.assertIsNone(self._prepare(b""))
        self.assertIsNone(self._prepare(b"\x1f\x8b\x08\x00garbage"))


class StreamingIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_streaming_client_is_parsed_from_disk(self):
        class StreamingClient:
            async def download_xmltv(self, path):
                with open(path, "wb") as f:
                    f.write(gzip.compress(DOC))

        manager = EPGIngestManager()
        work = tempfile.mkdtemp()
        source, size, programmes = await manager._fetch_xmltv(StreamingClient(), work)
        self.assertIsInstance(source, str)
        self.assertEqual(programmes, 40)
        self.assertFalse(os.path.exists(os.path.join(work, "raw")))

        maps = manager._build_channel_maps([
            {"stream_id": "ch2934", "name": "IL: Channel & Co", "epg_channel_id": "ch2934",
             "tv_archive": 1, "tv_archive_duration": 7},
        ])
        mapping, ok = manager._scan_channel_map(source, maps)
        self.assertTrue(ok)
        from datetime import datetime, timezone
        programs = list(manager._iter_programs(source, maps, datetime(2026, 11, 9, tzinfo=timezone.utc), xmltv_to_stream=mapping))
        self.assertEqual(len(programs), 40)
        self.assertTrue(programs[0]["title"].startswith("פרק"))

    async def test_a_client_with_nothing_to_stream_gives_no_source(self):
        class EmptyClient:
            async def download_xmltv(self, path):
                return None

        source, _, _ = await EPGIngestManager()._fetch_xmltv(EmptyClient(), tempfile.mkdtemp())
        self.assertIsNone(source)


if __name__ == "__main__":
    unittest.main()
