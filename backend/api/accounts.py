import asyncio
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from auth import require_admin, require_admin_or_download_user, AuthContext
from database import get_session
from models import AppSettings, EPGProgram, XtreamAccount, ScheduledRecording, ScheduledStatus, Download, DownloadStatus
from services.download_manager import download_manager
from services.recording_rule_service import recording_rule_service
from services.epg_ingest_manager import epg_ingest_manager, friendly_connection_error
from services.epg_service import epg_service
from services import notifications as notification_service
from services.account_credentials import (
    encrypt_account_password,
    resolve_account_password_with_migration,
)
from services.xtream_client import (
    XtreamClient,
    TIMESHIFT_STYLE_AUTO,
    TIMESHIFT_STYLE_SETTINGS,
    parse_expiration_date,
)
from services.provider_client import is_m3u_account, make_provider_client
from services.m3u_client import PROVIDER_TYPE_XTREAM, PROVIDER_TYPES, clear_playlist_cache


router = APIRouter()
logger = logging.getLogger(__name__)


def _log_task_result(task: asyncio.Task):
    try:
        task.result()
    except Exception as exc:
        logger.exception("EPG refresh task failed: %s", exc)


def _validate_catchup_url_style(value: str | None) -> str | None:
    """Accept only the three styles the app knows, normalised."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in TIMESHIFT_STYLE_SETTINGS:
        raise ValueError(
            "catchup_url_style must be one of: " + ", ".join(TIMESHIFT_STYLE_SETTINGS)
        )
    return normalized


def _validate_provider_type(value: str | None) -> str | None:
    """Accept only the provider kinds the app knows, normalised."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in PROVIDER_TYPES:
        raise ValueError("provider_type must be one of: " + ", ".join(PROVIDER_TYPES))
    return normalized


def _validate_guide_timezone(value: str | None) -> str | None:
    """Accept an IANA time zone name, or nothing; an empty string clears it."""
    if value is None or not value.strip():
        return None
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {name}")
    return name


class AccountCreate(BaseModel):
    name: str
    # The Xtream server URL, or for an M3U account the playlist URL.
    server_url: str
    # Xtream only; an M3U playlist carries its own authorisation.
    username: str = ""
    password: str = ""
    provider_type: str = PROVIDER_TYPE_XTREAM
    # M3U only: an XMLTV URL overriding the playlist's own url-tvg.
    epg_url: str | None = None
    guide_offset_hours: int = 0
    guide_timezone: str | None = None
    catchup_url_style: str = TIMESHIFT_STYLE_AUTO

    _check_style = field_validator("catchup_url_style")(_validate_catchup_url_style)
    _check_provider = field_validator("provider_type")(_validate_provider_type)
    _check_timezone = field_validator("guide_timezone")(_validate_guide_timezone)

    @model_validator(mode="after")
    def _require_credentials(self):
        """An Xtream login needs a username and password; a playlist needs neither."""
        if self.provider_type == PROVIDER_TYPE_XTREAM and not (self.username and self.password):
            raise ValueError("Xtream accounts need a username and password")
        if not self.server_url.strip().lower().startswith(("http://", "https://")):
            raise ValueError("server_url must be an http(s) URL")
        return self


class AccountUpdate(BaseModel):
    name: str | None = None
    server_url: str | None = None
    username: str | None = None
    password: str | None = None
    provider_type: str | None = None
    epg_url: str | None = None
    is_active: bool | None = None
    guide_offset_hours: int | None = None
    guide_timezone: str | None = None
    catchup_url_style: str | None = None

    _check_style = field_validator("catchup_url_style")(_validate_catchup_url_style)
    _check_provider = field_validator("provider_type")(_validate_provider_type)
    _check_timezone = field_validator("guide_timezone")(_validate_guide_timezone)


@router.get("")
async def list_accounts(
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """List all Xtream accounts."""
    result = await session.execute(select(XtreamAccount).order_by(XtreamAccount.name))
    accounts = result.scalars().all()
    return [acc.to_dict() for acc in accounts]


@router.get("/public")
async def list_accounts_public(
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """List minimal account info for non-admin browsing flows."""
    result = await session.execute(select(XtreamAccount).order_by(XtreamAccount.name))
    accounts = result.scalars().all()
    return [
        {
            "id": acc.id,
            "name": acc.name,
            "is_active": acc.is_active,
            "guide_offset_hours": int(acc.guide_offset_hours or 0),
            "guide_timezone": acc.guide_timezone,
        }
        for acc in accounts
    ]


def _record_connection_check(account: XtreamAccount, ok: bool, error: str | None = None) -> None:
    account.last_connection_ok = ok
    account.last_connection_checked_at = datetime.now(timezone.utc)
    account.last_connection_error = None if ok else error


async def _sync_account_notifications(session: AsyncSession, account: XtreamAccount) -> None:
    """Re-check the bell for this account after a save. Never fails the save."""
    try:
        await notification_service.sync_account_expiry(session, account)
        await notification_service.sync_account_connection(session, account)
        await session.commit()
    except Exception:
        logger.exception("Could not update notifications for account_id=%s", account.id)
        await session.rollback()


@router.post("")
async def create_account(
    account: AccountCreate,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Create a new Xtream or M3U account (validates connection)."""
    # Test connection first
    client = make_provider_client(account, account.password, XtreamClient)
    try:
        auth_data = await client.authenticate()
    except Exception:
        logger.exception(
            "Account validation failed during create account_name=%s server_url=%s username=%s",
            account.name,
            account.server_url,
            account.username,
        )
        raise HTTPException(status_code=400, detail="Provider connection failed")
    finally:
        await client.close()

    # Extract account info from auth response
    user_info = auth_data.get("user_info", {})
    server_info = auth_data.get("server_info", {})

    exp_date = parse_expiration_date(user_info)

    # Create account
    db_account = XtreamAccount(
        name=account.name,
        server_url=account.server_url,
        username=account.username,
        password="",
        password_encrypted=encrypt_account_password(account.password or ""),
        provider_type=account.provider_type,
        epg_url=(account.epg_url or "").strip() or None,
        max_connections=user_info.get("max_connections"),
        active_connections=user_info.get("active_cons"),
        expiration_date=exp_date,
        guide_offset_hours=int(account.guide_offset_hours or 0),
        guide_timezone=account.guide_timezone,
        catchup_url_style=account.catchup_url_style,
    )

    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)
    await _sync_account_notifications(session, db_account)

    refresh_task = asyncio.create_task(epg_ingest_manager.refresh_account_by_id(db_account.id))
    refresh_task.add_done_callback(_log_task_result)

    return db_account.to_dict()


@router.get("/{account_id}")
async def get_account(
    account_id: int,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Get a specific account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return account.to_dict()


@router.put("/{account_id}")
async def update_account(
    account_id: int,
    update_data: AccountUpdate,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Update an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for field, value in update_dict.items():
        if field == "password":
            if value is None:
                continue
            account.password_encrypted = encrypt_account_password(value)
            account.password = ""
            continue
        if field == "guide_offset_hours" and value is not None:
            value = int(value)
        if field == "catchup_url_style":
            if value is None:
                continue
            # Changing the picker also clears what probing remembered, so it
            # doubles as the way to force a fresh probe.
            if value != (account.catchup_url_style or "auto"):
                account.catchup_url_style_resolved = None
        setattr(account, field, value)

    await session.commit()
    await session.refresh(account)
    # A changed playlist URL must not be answered from the old one's cache.
    clear_playlist_cache()
    await _sync_account_notifications(session, account)

    return account.to_dict()


@router.delete("/{account_id}")
async def delete_account(
    account_id: int,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Delete an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    settings_result = await session.execute(select(AppSettings))
    app_settings = settings_result.scalar_one_or_none()
    if app_settings and app_settings.default_account_id == account.id:
        app_settings.default_account_id = None

    active_schedule_statuses = [
        ScheduledStatus.SCHEDULED.value,
        ScheduledStatus.QUEUED.value,
        ScheduledStatus.DOWNLOADING.value,
        ScheduledStatus.PROCESSING.value,
        ScheduledStatus.PAUSED_LOW_SPACE.value,
    ]
    sched_result = await session.execute(
        select(ScheduledRecording).where(
            ScheduledRecording.account_id == account.id,
            ScheduledRecording.status.in_(active_schedule_statuses),
        )
    )
    for sched in sched_result.scalars().all():
        sched.status = ScheduledStatus.CANCELLED.value
        sched.status_message = "Account deleted"

    active_download_statuses = [
        DownloadStatus.PENDING.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.PROCESSING.value,
    ]
    dl_result = await session.execute(
        select(Download).where(
            Download.account_id == account.id,
            Download.status.in_(active_download_statuses),
        )
    )
    active_dl_ids = [d.id for d in dl_result.scalars().all()]

    await session.execute(
        delete(EPGProgram).where(EPGProgram.account_id == account.id)
    )

    deleted_account_id = account.id
    # The account's recording rules go with it: there is no guide left for
    # them to match against.
    async with recording_rule_service.lock:
        await recording_rule_service.delete_rules(session, account_id=account.id)
        await session.delete(account)
        await session.commit()
    epg_service.clear_cache()

    # Best effort: the hourly sweep also removes notifications whose account
    # is gone, so a failure here only delays their disappearance.
    try:
        await notification_service.clear_for_account(session, deleted_account_id)
        await session.commit()
    except Exception:
        logger.exception("Could not clear notifications for deleted account_id=%s", deleted_account_id)
        await session.rollback()

    for dl_id in active_dl_ids:
        await download_manager.cancel_download(dl_id)

    return {"status": "deleted"}


@router.post("/{account_id}/test")
async def test_account(
    account_id: int,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Test connection to an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        password = await resolve_account_password_with_migration(session, account)
    except ValueError:
        if is_m3u_account(account):
            password = ""
        else:
            logger.exception("Account credentials missing or invalid account_id=%s", account_id)
            raise HTTPException(status_code=400, detail="Account credentials are not configured")

    client = make_provider_client(account, password, XtreamClient)
    try:
        auth_data = await client.authenticate()

        # Update cached info
        user_info = auth_data.get("user_info", {})

        account.max_connections = user_info.get("max_connections")
        account.active_connections = user_info.get("active_cons")
        account.expiration_date = parse_expiration_date(user_info)
        account.last_used = datetime.utcnow()
        _record_connection_check(account, ok=True)

        await session.commit()
        await _sync_account_notifications(session, account)

        return {
            "status": "connected",
            "user_info": user_info,
            "server_info": auth_data.get("server_info", {}),
        }

    except Exception as exc:
        logger.exception("Account test failed account_id=%s", account_id)
        # The Test button is a connection check like a guide refresh, so it
        # updates the same status and the bell's "can't connect" entry.
        try:
            _record_connection_check(account, ok=False, error=friendly_connection_error(exc))
            await session.commit()
            await _sync_account_notifications(session, account)
        except Exception:
            logger.exception("Could not record failed test for account_id=%s", account_id)
            await session.rollback()
        raise HTTPException(status_code=400, detail="Provider connection failed")
    finally:
        await client.close()
