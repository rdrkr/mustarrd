"""A provider client for M3U playlists, the non-Xtream way IPTV services are sold.

Plenty of providers hand out a single playlist URL rather than Xtream Codes
credentials: one ``#EXTINF`` line per channel, its stream URL on the next line,
and the archive described by attributes on the ``#EXTINF`` line. ``M3UClient``
reads such a playlist and answers the same calls ``XtreamClient`` does, so the
rest of the app - browsing, guide ingest, downloads, previews, schedules - runs
unchanged on either kind of account.

What maps onto what:

- **Channels.** Each entry becomes a ``get_live_streams`` dict in Xtream's
  shape. ``stream_id`` is the entry's ``tvg-id`` (stable across playlist
  refreshes, unlike the stream URL, which usually carries a rotating token),
  falling back to a hash of the channel name.
- **Categories.** ``group-title``.
- **Guide.** The playlist header's ``url-tvg`` (or ``x-tvg-url``), or the
  account's own EPG URL when one is set. The XMLTV ingest already matches
  guide channels to streams by ``epg_channel_id`` and by name.
- **Catchup.** The archive depth comes from ``catchup-days``, ``timeshift`` or
  ``tvg-rec``. The URL form comes from ``catchup`` / ``catchup-source`` when
  the playlist names one, and otherwise from the stream URL itself: a
  Flussonic-style ``…/index.m3u8`` gets Flussonic's archive form,
  ``…/index-<utc start>-<seconds>.m3u8``.
- **VOD.** Playlists carry none, so every VOD call is empty.

There are no credentials to authenticate with. ``authenticate`` fetches and
parses the playlist, and answers in Xtream's ``user_info`` shape so callers that
read ``max_connections`` or ``exp_date`` get ``None`` rather than a KeyError.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

PROVIDER_TYPE_XTREAM = "xtream"
PROVIDER_TYPE_M3U = "m3u"
PROVIDER_TYPES = (PROVIDER_TYPE_XTREAM, PROVIDER_TYPE_M3U)

PLAYLIST_TIMEOUT = aiohttp.ClientTimeout(total=60)
XMLTV_TIMEOUT = aiohttp.ClientTimeout(total=300)

# A parsed playlist is reused for this long. Every API call builds a fresh
# client, and a large playlist is half a megabyte, so without this a single page
# load would fetch it several times over. Short enough that a token the provider
# rotates is picked up long before it expires.
PLAYLIST_CACHE_SECONDS = 300

# Catchup forms. "flussonic" is the archive path form; "shift" and "append" add
# the window as query parameters or a templated suffix; "default" replaces the
# whole URL with catchup-source.
CATCHUP_FLUSSONIC = "flussonic"
CATCHUP_SHIFT = "shift"
CATCHUP_APPEND = "append"
CATCHUP_DEFAULT = "default"

_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_FLUSSONIC_HLS = re.compile(r"/(index|video|mono|playlist)\.m3u8$")
_FLUSSONIC_TS = re.compile(r"/mpegts$")


@dataclass(frozen=True)
class PlaylistChannel:
    """One ``#EXTINF`` entry of the playlist."""

    stream_id: str
    """Stable id: the entry's tvg-id, else a hash of its name."""

    name: str
    """Display name: the text after the EXTINF comma, else tvg-name."""

    url: str
    """The live stream URL, token and all."""

    epg_channel_id: str
    """tvg-id, which the guide's <channel id> uses."""

    logo: str
    """tvg-logo."""

    group: str
    """group-title, used as the category."""

    archive_days: int
    """How many days back the archive reaches; 0 when there is none."""

    catchup_type: str
    """One of the CATCHUP_* forms, or "" when there is no archive."""

    catchup_source: str
    """The catchup-source template, when the playlist gives one."""


@dataclass
class Playlist:
    """A parsed playlist: its channels, in order, and the guide URL it names."""

    channels: list[PlaylistChannel] = field(default_factory=list)
    """Every entry, in playlist order."""

    epg_url: str = ""
    """url-tvg / x-tvg-url from the #EXTM3U header."""

    by_id: dict[str, PlaylistChannel] = field(default_factory=dict)
    """The same channels keyed by stream_id."""


def _archive_days(attrs: dict[str, str]) -> int:
    """Read the archive depth from whichever attribute the provider used."""
    for key in ("catchup-days", "timeshift", "tvg-rec"):
        value = (attrs.get(key) or "").strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
    return 0


def _catchup_type(attrs: dict[str, str], url: str, days: int) -> str:
    """Work out which catchup URL form a channel uses.

    The playlist's own ``catchup`` attribute wins ("fs" is the common
    abbreviation for Flussonic). Without one, a channel that has an archive is
    Flussonic if its stream URL is shaped like a Flussonic stream, and gets the
    widely supported ``?utc=&lutc=`` shift form otherwise.
    """
    if not days and not attrs.get("catchup"):
        return ""
    declared = (attrs.get("catchup") or "").strip().lower()
    if declared in ("flussonic", "flussonic-hls", "flussonic-ts", "fs"):
        return CATCHUP_FLUSSONIC
    if declared in (CATCHUP_SHIFT, CATCHUP_APPEND, CATCHUP_DEFAULT):
        return declared
    path = urlsplit(url).path
    if _FLUSSONIC_HLS.search(path) or _FLUSSONIC_TS.search(path):
        return CATCHUP_FLUSSONIC
    return CATCHUP_SHIFT


def _fallback_id(name: str) -> str:
    """A stable id for an entry without tvg-id, from its name."""
    return "m3u-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def parse_playlist(text: str) -> Playlist:
    """Parse an extended M3U playlist.

    Entries without a URL line are skipped. A tvg-id seen twice keeps the
    first entry's id, and later duplicates get a numeric suffix, because two
    streams with the same id would make one of them unreachable.
    """
    playlist = Playlist()
    pending: Optional[tuple[dict[str, str], str]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = dict(_ATTR.findall(line))
            playlist.epg_url = (header.get("url-tvg") or header.get("x-tvg-url") or "").split(",")[0].strip()
            continue
        if line.startswith("#EXTINF"):
            meta, _, title = line.partition(",")
            pending = (dict(_ATTR.findall(meta)), title.strip())
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        attrs, title = pending
        pending = None
        name = title or attrs.get("tvg-name", "").strip() or line
        base_id = (attrs.get("tvg-id") or "").strip() or _fallback_id(name)
        stream_id = base_id
        n = 2
        while stream_id in playlist.by_id:
            stream_id = f"{base_id}-{n}"
            n += 1
        days = _archive_days(attrs)
        channel = PlaylistChannel(
            stream_id=stream_id,
            name=name,
            url=line,
            epg_channel_id=(attrs.get("tvg-id") or "").strip(),
            logo=(attrs.get("tvg-logo") or "").strip(),
            group=(attrs.get("group-title") or "").strip() or "Uncategorized",
            archive_days=days,
            catchup_type=_catchup_type(attrs, line, days),
            catchup_source=(attrs.get("catchup-source") or "").strip(),
        )
        playlist.channels.append(channel)
        playlist.by_id[stream_id] = channel
    return playlist


def category_id_for(group: str) -> str:
    """A stable category id for a group-title."""
    return "g-" + hashlib.sha1(group.encode("utf-8")).hexdigest()[:10]


def _fill_template(template: str, start: int, duration: int) -> str:
    """Substitute the catchup-source placeholders players commonly support.

    Both the ``{utc}`` and the ``${start}`` spellings are accepted, along with
    the strftime-style ``{Y}``/``{m}``/``{d}``/``{H}``/``{M}``/``{S}`` parts of
    the start time, in UTC.
    """
    end = start + duration
    now = int(time.time())
    started = datetime.fromtimestamp(start, tz=timezone.utc)
    values = {
        "utc": start, "start": start, "timestamp": start,
        "utcend": end, "end": end,
        "lutc": now, "now": now,
        "duration": duration,
        "offset": max(0, now - start),
        "Y": f"{started:%Y}", "m": f"{started:%m}", "d": f"{started:%d}",
        "H": f"{started:%H}", "M": f"{started:%M}", "S": f"{started:%S}",
    }
    out = template
    for key, value in values.items():
        out = out.replace("${" + key + "}", str(value)).replace("{" + key + "}", str(value))
    return out


def build_catchup_url(channel: PlaylistChannel, start: int, duration: int) -> str:
    """Build the archive URL for ``duration`` seconds of a channel from ``start``.

    Args:
        channel: The playlist entry.
        start: Window start, as a UTC epoch timestamp.
        duration: Window length in seconds.

    Raises:
        ValueError: When the channel has no archive.
    """
    if not channel.catchup_type:
        raise ValueError(f"Channel {channel.name!r} has no catchup archive")
    parts = urlsplit(channel.url)
    if channel.catchup_type == CATCHUP_DEFAULT and channel.catchup_source:
        return urljoin(channel.url, _fill_template(channel.catchup_source, start, duration))
    if channel.catchup_type == CATCHUP_APPEND and channel.catchup_source:
        return channel.url + _fill_template(channel.catchup_source, start, duration)
    if channel.catchup_type == CATCHUP_FLUSSONIC:
        if channel.catchup_source:
            return urljoin(channel.url, _fill_template(channel.catchup_source, start, duration))
        path = parts.path
        hls = _FLUSSONIC_HLS.search(path)
        if hls:
            path = path[: hls.start()] + f"/{hls.group(1)}-{start}-{duration}.m3u8"
        elif _FLUSSONIC_TS.search(path):
            path = path[: -len("/mpegts")] + f"/timeshift_abs-{start}.ts"
        else:
            raise ValueError(f"Channel {channel.name!r} is not a Flussonic stream URL")
        return urlunsplit(parts._replace(path=path))
    # Shift: the window as query parameters on the live URL.
    query = parts.query + ("&" if parts.query else "") + f"utc={start}&lutc={start + duration}"
    return urlunsplit(parts._replace(query=query))


_cache: dict[str, tuple[float, Playlist]] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_playlist_cache() -> None:
    """Forget every cached playlist, so the next call fetches afresh."""
    _cache.clear()


class M3UClient:
    """Answers the XtreamClient calls the app makes, from an M3U playlist."""

    provider_type = PROVIDER_TYPE_M3U

    def __init__(self, playlist_url: str, epg_url: Optional[str] = None):
        """Build a client.

        Args:
            playlist_url: The provider's playlist URL.
            epg_url: An XMLTV URL that overrides the playlist's own url-tvg.
        """
        self.server_url = playlist_url.strip()
        self.epg_url = (epg_url or "").strip()
        self._playlist: Optional[Playlist] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=PLAYLIST_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def load_playlist(self, force: bool = False) -> Playlist:
        """Fetch and parse the playlist, or reuse a recent parse of it.

        Args:
            force: Fetch even if a cached copy is still fresh - used when the
                provider rejects a stream URL whose token may have rotated.
        """
        cached = _cache.get(self.server_url)
        if not force and cached and time.monotonic() - cached[0] < PLAYLIST_CACHE_SECONDS:
            self._playlist = cached[1]
            return cached[1]
        lock = _locks.setdefault(self.server_url, asyncio.Lock())
        async with lock:
            cached = _cache.get(self.server_url)
            if not force and cached and time.monotonic() - cached[0] < PLAYLIST_CACHE_SECONDS:
                self._playlist = cached[1]
                return cached[1]
            session = await self._get_session()
            async with session.get(self.server_url) as response:
                if response.status != 200:
                    raise Exception(f"Failed to get playlist: HTTP {response.status}")
                text = await response.text(errors="replace")
            playlist = parse_playlist(text)
            if not playlist.channels:
                raise Exception("Playlist has no channels")
            _cache[self.server_url] = (time.monotonic(), playlist)
            self._playlist = playlist
            return playlist

    async def prepare(self) -> None:
        """Load the playlist so the synchronous URL builders can run."""
        await self.load_playlist()

    def _channel(self, stream_id: str) -> PlaylistChannel:
        if self._playlist is None:
            raise RuntimeError("M3UClient.prepare() must be awaited before building URLs")
        channel = self._playlist.by_id.get(str(stream_id))
        if channel is None:
            raise ValueError(f"Channel {stream_id} is not in the playlist")
        return channel

    async def authenticate(self) -> dict:
        """Validate the playlist by fetching it, answering in Xtream's shape."""
        playlist = await self.load_playlist(force=True)
        return {
            "user_info": {
                "auth": 1,
                "status": "Active",
                "exp_date": None,
                "max_connections": None,
                "active_cons": None,
            },
            "server_info": {
                "provider_type": PROVIDER_TYPE_M3U,
                "channels": len(playlist.channels),
                "catchup_channels": sum(1 for c in playlist.channels if c.archive_days),
                "epg_url": self.epg_url or playlist.epg_url,
            },
        }

    async def get_live_categories(self) -> list:
        """One category per group-title, in playlist order."""
        playlist = await self.load_playlist()
        seen: dict[str, dict] = {}
        for channel in playlist.channels:
            if channel.group not in seen:
                seen[channel.group] = {
                    "category_id": category_id_for(channel.group),
                    "category_name": channel.group,
                    "parent_id": 0,
                }
        return list(seen.values())

    async def get_live_streams(self, category_id: Optional[str] = None) -> list:
        """Every channel as an Xtream ``get_live_streams`` entry."""
        playlist = await self.load_playlist()
        streams = []
        for number, channel in enumerate(playlist.channels, start=1):
            cat_id = category_id_for(channel.group)
            if category_id and str(category_id) != cat_id:
                continue
            streams.append({
                "num": number,
                "name": channel.name,
                "stream_type": "live",
                "stream_id": channel.stream_id,
                "stream_icon": channel.logo,
                "epg_channel_id": channel.epg_channel_id or None,
                "added": None,
                "category_id": cat_id,
                "category_name": channel.group,
                "category_ids": [cat_id],
                "custom_sid": "",
                "tv_archive": 1 if channel.archive_days and channel.catchup_type else 0,
                "direct_source": "",
                "tv_archive_duration": channel.archive_days,
            })
        return streams

    async def get_epg(self, stream_id: str) -> list:
        """No per-channel guide endpoint exists; callers fall back to the ingested XMLTV."""
        return []

    async def get_short_epg(self, stream_id: str, limit: int = 10) -> list:
        """No per-channel guide endpoint exists; callers fall back to the ingested XMLTV."""
        return []

    async def get_xmltv(self) -> bytes:
        """Fetch the XMLTV guide: the account's EPG URL, else the playlist's url-tvg."""
        url = self.epg_url
        if not url:
            url = (await self.load_playlist()).epg_url
        if not url:
            return b""
        session = await self._get_session()
        async with session.get(url, timeout=XMLTV_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get XMLTV: HTTP {response.status}")
            return await response.read()

    async def get_vod_categories(self) -> list:
        return []

    async def get_vod_streams(self, category_id: Optional[str] = None) -> list:
        return []

    async def get_vod_info(self, vod_id: str) -> dict:
        return {}

    async def get_series_categories(self) -> list:
        return []

    async def get_series(self, category_id: Optional[str] = None) -> list:
        return []

    async def get_series_info(self, series_id: str) -> dict:
        return {}

    def build_timeshift_url(
        self,
        stream_id: str,
        start_time: datetime,
        duration_minutes: int,
        provider_start: Optional[str] = None,
        style: Optional[str] = None,
    ) -> str:
        """Build the archive URL for a window of a channel.

        ``provider_start`` and ``style`` are Xtream concepts - the provider's
        wall-clock start token and the path/query URL form - and are ignored:
        playlist archives are addressed by UTC timestamp.
        """
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        start = int(start_time.timestamp())
        return build_catchup_url(self._channel(stream_id), start, int(duration_minutes) * 60)

    def build_stream_url(self, stream_id: str, extension: str = "ts") -> str:
        """The channel's live URL, as the playlist gives it."""
        return self._channel(stream_id).url

    def build_vod_url(self, vod_id: str, extension: Optional[str] = None) -> str:
        raise ValueError("M3U playlists have no VOD")

    def build_series_url(self, episode_id: str, extension: Optional[str] = None) -> str:
        raise ValueError("M3U playlists have no series")

    async def refresh_url(self, url: str) -> Optional[str]:
        """Give a stream or archive URL the playlist's current token.

        Providers like this one sign every URL with a token that rotates. A
        download queued days ahead can outlive the token in its URL, so when
        the provider refuses it, the playlist is fetched again and the URL's
        query string is replaced with the one the same stream carries now.

        Returns:
            The refreshed URL, or None when no channel in the playlist shares
            the URL's host and stream directory.
        """
        playlist = await self.load_playlist(force=True)
        target = urlsplit(url)
        target_dir = target.path.rsplit("/", 1)[0]
        for channel in playlist.channels:
            live = urlsplit(channel.url)
            if live.netloc == target.netloc and live.path.rsplit("/", 1)[0] == target_dir:
                # The live URL's parameters (the token) replace their stale
                # copies; the archive's own (utc, lutc, ...) are kept.
                fresh = dict(parse_qsl(live.query, keep_blank_values=True))
                kept = [
                    (k, v) for k, v in parse_qsl(target.query, keep_blank_values=True)
                    if k not in fresh
                ]
                query = urlencode(list(fresh.items()) + kept, safe=":/")
                return urlunsplit(target._replace(query=query))
        return None
