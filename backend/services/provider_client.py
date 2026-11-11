"""Pick the right provider client for an account.

An account is either an Xtream Codes login (server, username, password) or an
M3U playlist URL. Both clients answer the same calls, so every caller that used
to build an ``XtreamClient`` directly asks this module instead and never needs
to know which kind of account it holds.
"""

from __future__ import annotations

from typing import Union

from services.m3u_client import PROVIDER_TYPE_M3U, M3UClient
from services.xtream_client import XtreamClient

ProviderClient = Union[XtreamClient, M3UClient]


def is_m3u_account(account) -> bool:
    """Whether an account is an M3U playlist rather than an Xtream login."""
    return (getattr(account, "provider_type", None) or "").lower() == PROVIDER_TYPE_M3U


def make_provider_client(account, password: str = "", xtream_cls=XtreamClient) -> ProviderClient:
    """Build the client for an account (an XtreamAccount row or anything shaped like one).

    Args:
        account: Needs ``server_url``; ``username`` for Xtream; ``provider_type``
            and ``epg_url`` for M3U.
        password: The decrypted password. Ignored for M3U accounts.
        xtream_cls: The Xtream client class to build. Callers pass the name they
            imported, so a test that patches ``<module>.XtreamClient`` still
            intercepts the client that module builds.
    """
    if is_m3u_account(account):
        return M3UClient(account.server_url, getattr(account, "epg_url", None))
    return xtream_cls(account.server_url, account.username, password)
