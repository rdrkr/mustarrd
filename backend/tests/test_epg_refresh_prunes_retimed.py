"""
Regression test: a refresh after the provider retimes a programme leaves the
old timing in the guide.

Bug: a guide row's epg_id is channel:start:stop, and a non-force refresh only
upserts on it. When the provider moved a programme, the next refresh inserted
the new timing as a new row and kept the old one, so the guide showed the
programme twice, once at a time it no longer airs. A daily refresh of a
week-ahead guide stacked every revision on top of the last.

Fix: a refresh stamps the rows it writes with ingested_at, and once a file
has parsed to the end, deletes rows on each channel it covered, inside the
span of start times it covered, that it did not write. Older archive rows
outside that span, and everything after a truncated file, are kept.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import models  # noqa: F401  (registers every table on Base.metadata)
from database import Base
from models import AppSettings, EPGProgram, XtreamAccount
from services.epg_ingest_manager import EPGIngestManager

ACCOUNT_ID = 1
CHANNEL_ID = "101"
CHANNELS = [{
    "stream_id": CHANNEL_ID,
    "name": "News",
    "epg_channel_id": "news.il",
    "tv_archive": 1,
    "tv_archive_duration": 7,
}]

# Tomorrow, on the hour: inside the refresh window and in the future.
BASE = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    minute=0, second=0, microsecond=0
)


def _xmltv(programmes, truncated=False) -> bytes:
    """Build an XMLTV document for one channel.

    Args:
        programmes: (title, start offset in minutes, stop offset in minutes).
        truncated: End the document mid-tag, the way a half-served file does.

    Returns:
        The document as UTF-8 bytes.
    """
    fmt = "%Y%m%d%H%M%S +0000"
    body = "".join(
        f'<programme start="{(BASE + timedelta(minutes=a)).strftime(fmt)}" '
        f'stop="{(BASE + timedelta(minutes=b)).strftime(fmt)}" channel="news.il">'
        f"<title>{title}</title></programme>"
        for title, a, b in programmes
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?><tv>'
        '<channel id="news.il"><display-name>News</display-name></channel>'
        f"{body}"
    )
    doc += '<programme start="2026' if truncated else "</tv>"
    return doc.encode("utf-8")


class RefreshPrunesRetimedProgrammesTests(unittest.IsolatedAsyncioTestCase):
    """A refresh replaces the provider's previous timings instead of adding to them."""

    async def asyncSetUp(self):
        """Create an in-memory database holding one Xtream account."""
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.session_maker() as session:
            async with session.begin():
                session.add(XtreamAccount(
                    id=ACCOUNT_ID,
                    name="Test",
                    server_url="http://example.com",
                    username="user",
                    password="pass",
                    guide_offset_hours=0,
                    # Skip the API backfill: this is about the XMLTV merge.
                    last_epg_backfill_at=datetime.now(timezone.utc).replace(tzinfo=None),
                ))
                session.add(AppSettings(id=1))

    async def asyncTearDown(self):
        """Dispose of the in-memory database."""
        await self.engine.dispose()

    async def _refresh(self, xmltv: bytes, force: bool = False) -> None:
        """Run a full account refresh against a provider serving ``xmltv``."""
        client = MagicMock()
        client.get_live_streams = AsyncMock(return_value=CHANNELS)
        client.get_vod_categories = AsyncMock(return_value=[])
        client.authenticate = AsyncMock(return_value={})
        client.get_xmltv = AsyncMock(return_value=xmltv)
        client.get_epg = AsyncMock(return_value=[])
        client.close = AsyncMock()
        async with self.session_maker() as session:
            account = await session.get(XtreamAccount, ACCOUNT_ID)
        with (
            patch("services.epg_ingest_manager.async_session_maker", self.session_maker),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await EPGIngestManager()._refresh_account(account, force=force)

    async def _guide(self) -> list[tuple[str, int]]:
        """Return the stored guide as (title, minutes after BASE), in start order."""
        async with self.session_maker() as session:
            rows = (await session.execute(
                select(EPGProgram.title, EPGProgram.start_timestamp)
                .order_by(EPGProgram.start_timestamp)
            )).all()
        base_ts = int(BASE.timestamp())
        return [(title, (ts - base_ts) // 60) for title, ts in rows]

    async def test_retimed_programme_replaces_the_old_timing(self):
        """The provider moves the news 10 minutes later: one news row, at the new time."""
        await self._refresh(_xmltv([("Morning", 0, 60), ("News", 60, 120), ("Late", 120, 180)]))
        await self._refresh(_xmltv([("Morning", 0, 70), ("News", 70, 120), ("Late", 120, 180)]))

        self.assertEqual(await self._guide(), [("Morning", 0), ("News", 70), ("Late", 120)])

    async def test_rows_before_the_new_file_are_kept(self):
        """Archive the newer file no longer lists stays in the guide."""
        await self._refresh(_xmltv([("Old", -120, -60), ("News", 60, 120)]))
        await self._refresh(_xmltv([("News", 60, 120), ("Late", 120, 180)]))

        self.assertEqual(await self._guide(), [("Old", -120), ("News", 60), ("Late", 120)])

    async def test_truncated_file_prunes_nothing(self):
        """A half-served file cannot be trusted to list everything still current."""
        await self._refresh(_xmltv([("Morning", 0, 60), ("News", 60, 120), ("Late", 120, 180)]))
        await self._refresh(_xmltv([("Morning", 0, 70), ("Late", 120, 180)], truncated=True))

        self.assertIn(("News", 60), await self._guide())


if __name__ == "__main__":
    unittest.main()
