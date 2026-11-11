"""Read an HLS stream as one continuous MPEG-TS byte stream.

Xtream providers serve a catchup recording as a single ``.ts`` response, and
the download and preview code was written around that: open one URL, read its
body. Playlist providers serve the same recording as HLS instead - an ``.m3u8``
listing a few thousand six-second ``.ts`` segments, each signed with its own
token. Concatenated in order, MPEG-TS segments are a valid MPEG-TS file, so
``iter_hls`` turns the playlist into exactly the byte stream the existing code
expects, and nothing downstream has to know the difference.

It handles:

- **Master playlists.** The highest-bandwidth variant is followed.
- **Finished playlists** (``#EXT-X-ENDLIST``), which is what an archive window
  in the past returns. Every segment is read once and the stream ends.
- **Growing playlists**, which is what a live channel, or an archive window that
  reaches past "now", returns. The playlist is polled for new segments until it
  ends, or until no new segment has appeared for ``stall_seconds``.
- **Flaky segments.** A failed segment fetch is retried a few times before the
  whole stream fails.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp

SEGMENT_RETRIES = 3
SEGMENT_RETRY_DELAY = 2.0
DEFAULT_STALL_SECONDS = 90.0
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
CHUNK_SIZE = 256 * 1024

_STREAM_INF = re.compile(r"BANDWIDTH=(\d+)")


class HLSStatusError(Exception):
    """The provider refused a playlist or segment with an HTTP error status."""

    def __init__(self, status: int, reason: str = ""):
        super().__init__(f"HTTP {status} {reason}".strip())
        self.status = status
        self.reason = reason


def is_hls_url(url: Optional[str]) -> bool:
    """Whether a URL names an HLS playlist rather than a single media file."""
    if not url:
        return False
    return urlsplit(url).path.lower().endswith(".m3u8")


@dataclass
class MediaPlaylist:
    """The parts of a media playlist the reader needs."""

    segments: list[tuple[int, str, float]]
    """(media sequence number, absolute URL, duration in seconds), in order."""

    ended: bool
    """Whether the playlist carries #EXT-X-ENDLIST."""

    target_duration: float
    """#EXT-X-TARGETDURATION, the longest a segment may be."""


@dataclass
class HLSProgress:
    """How far through the stream the reader is, for progress reporting."""

    segments_done: int = 0
    """Segments fully read so far."""

    segments_total: int = 0
    """Segments the playlist lists, once it has ended; 0 while it is still growing."""

    seconds_done: float = 0.0
    """Media seconds read so far."""

    seconds_total: float = 0.0
    """Media seconds the playlist lists so far."""

    @property
    def fraction(self) -> float:
        """0..1 when the total is known, else 0."""
        if not self.seconds_total:
            return 0.0
        return min(1.0, self.seconds_done / self.seconds_total)


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    """Parse a media playlist's segments, resolving their URLs against ``base_url``."""
    sequence = 0
    target = 6.0
    ended = False
    duration: Optional[float] = None
    segments: list[tuple[int, str, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif not line.startswith("#"):
            segments.append((sequence, urljoin(base_url, line), duration or 0.0))
            sequence += 1
            duration = None
    return MediaPlaylist(segments=segments, ended=ended, target_duration=target)


def pick_variant(text: str, base_url: str) -> Optional[str]:
    """Return the highest-bandwidth variant of a master playlist, or None if it is not one."""
    best: tuple[int, str] | None = None
    bandwidth: Optional[int] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            match = _STREAM_INF.search(line)
            bandwidth = int(match.group(1)) if match else 0
        elif line and not line.startswith("#") and bandwidth is not None:
            if best is None or bandwidth > best[0]:
                best = (bandwidth, urljoin(base_url, line))
            bandwidth = None
    return best[1] if best else None


async def _fetch_text(http: aiohttp.ClientSession, url: str) -> str:
    async with http.get(url) as response:
        if response.status != 200:
            raise HLSStatusError(response.status, response.reason or "")
        return await response.text(errors="replace")


async def _load_media_playlist(http: aiohttp.ClientSession, url: str) -> tuple[str, MediaPlaylist]:
    """Fetch a playlist, following a master playlist to its best variant."""
    text = await _fetch_text(http, url)
    variant = pick_variant(text, url)
    if variant:
        url = variant
        text = await _fetch_text(http, url)
    return url, parse_media_playlist(text, url)


async def iter_hls(
    url: str,
    *,
    progress: Optional[HLSProgress] = None,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    is_cancelled: Callable[[], bool] = lambda: False,
    http: Optional[aiohttp.ClientSession] = None,
) -> AsyncIterator[bytes]:
    """Yield the stream's MPEG-TS bytes, segment after segment.

    Args:
        url: The playlist URL.
        progress: Updated in place as segments are read, if given.
        stall_seconds: How long a growing playlist may go without a new segment
            before the stream is treated as finished.
        is_cancelled: Polled between chunks; True stops the stream with
            asyncio.CancelledError.
        http: A session to use; one is opened (and closed) when omitted.

    Raises:
        HLSStatusError: The playlist was refused, or a segment kept failing.
    """
    own_session = http is None
    session = http or aiohttp.ClientSession(timeout=FETCH_TIMEOUT)
    progress = progress if progress is not None else HLSProgress()
    try:
        media_url, playlist = await _load_media_playlist(session, url)
        next_sequence: Optional[int] = None
        last_new = asyncio.get_running_loop().time()
        while True:
            fresh = [s for s in playlist.segments if next_sequence is None or s[0] >= next_sequence]
            progress.seconds_total = progress.seconds_done + sum(s[2] for s in fresh)
            if playlist.ended:
                progress.segments_total = progress.segments_done + len(fresh)
            for sequence, segment_url, duration in fresh:
                async for chunk in _iter_segment(session, segment_url, is_cancelled):
                    yield chunk
                progress.segments_done += 1
                progress.seconds_done += duration
                next_sequence = sequence + 1
                last_new = asyncio.get_running_loop().time()
            if playlist.ended:
                return
            if asyncio.get_running_loop().time() - last_new > stall_seconds:
                return
            await asyncio.sleep(max(1.0, playlist.target_duration / 2))
            if is_cancelled():
                raise asyncio.CancelledError()
            playlist = parse_media_playlist(await _fetch_text(session, media_url), media_url)
    finally:
        if own_session:
            await session.close()


async def _iter_segment(
    http: aiohttp.ClientSession,
    url: str,
    is_cancelled: Callable[[], bool],
) -> AsyncIterator[bytes]:
    """Yield one segment's bytes, retrying the whole segment on a failure.

    A segment is retried only while none of it has been yielded; a failure
    part-way through would otherwise duplicate the bytes already passed on.
    """
    attempt = 0
    while True:
        yielded = False
        try:
            async with http.get(url) as response:
                if response.status != 200:
                    raise HLSStatusError(response.status, response.reason or "")
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    if is_cancelled():
                        raise asyncio.CancelledError()
                    yielded = True
                    yield chunk
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, HLSStatusError) as exc:
            client_error = isinstance(exc, HLSStatusError) and 400 <= exc.status < 500
            if yielded or client_error or attempt >= SEGMENT_RETRIES:
                raise
            attempt += 1
            await asyncio.sleep(SEGMENT_RETRY_DELAY * attempt)
