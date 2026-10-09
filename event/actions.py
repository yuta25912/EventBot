"""イベント操作の本体 (コマンド・ボタンの両方から呼ばれる)"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import discord

from . import embeds, storage, timeutil

log = logging.getLogger(__name__)

# 参加者とみなす最低滞在秒数
MIN_ATTEND_SECONDS = 10
# 管理 DM を送る時刻: 当日 9:00 と 開始 1 時間前 の早いほう
DAILY_DM_HOUR = 9
DM_BEFORE_START = timedelta(hours=1)


# ---------------------------------------------------------------- 共通


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
    return member


async def send_admin(
    guild: discord.Guild,
    gd: storage.GuildData,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
    mention: bool = False,
) -> discord.Message | None:
    """管理者連絡チャンネルへ送る"""
    channel = guild.get_channel(gd.config.get("admin_channel_id") or 0)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("guild %s: 管理者連絡チャンネルが未設定です", guild.id)
        return None
    content = None
    allowed = discord.AllowedMentions.none()
    role = guild.get_role(gd.config.get("admin_role_id") or 0)
    if mention and role:
        content = role.mention
        allowed = discord.AllowedMentions(roles=[role])
    kwargs: dict[str, Any] = {"content": content, "embed": embed, "allowed_mentions": allowed}
    if view is not None:
        kwargs["view"] = view
    return await channel.send(**kwargs)


async def dm(guild: discord.Guild, user_id: int, **kwargs: Any) -> bool:
    member = await fetch_member(guild, user_id)
    if member is None:
        return False
    try:
        await member.send(**kwargs)
        return True
    except discord.HTTPException:
        return False


def events_of(gd: storage.GuildData, *statuses: str) -> list[dict[str, Any]]:
    return [e for e in gd.events.values() if e["status"] in statuses]


# ---------------------------------------------------------------- Discord のイベント


def discord_event_url(guild_id: int, ev: dict[str, Any]) -> str | None:
    if not ev.get("discord_event_id"):
        return None
    return f"https://discord.com/events/{guild_id}/{ev['discord_event_id']}"


def _discord_description(guild: discord.Guild, ev: dict[str, Any]) -> str:
    host = guild.get_member(ev["host_id"])
    lines = []
    if ev.get("description"):
        lines += [ev["description"], ""]
    lines.append(f"ゲーム: {embeds.game_display(ev)}")
    if embeds.needs_version(ev) and ev.get("version"):
        lines.append(f"バージョン: {ev['version']}")
    lines.append(f"主催: {host.display_name if host else ev['host_id']}")
    text = "\n".join(lines)
    return text[:1000]


async def _get_discord_event(guild: discord.Guild, ev: dict[str, Any]) -> discord.ScheduledEvent | None:
    eid = ev.get("discord_event_id")
    if not eid:
        return None
    se = guild.get_scheduled_event(eid)
    if se is None:
        try:
            se = await guild.fetch_scheduled_event(eid)
        except discord.HTTPException:
            return None
    return se


async def _create_discord_event(guild: discord.Guild, ev: dict[str, Any]) -> discord.ScheduledEvent:
    vc = guild.get_channel(ev["vc_id"])
    kwargs: dict[str, Any] = {
        "name": ev["name"][:100],
        "description": _discord_description(guild, ev),
        "start_time": timeutil.from_iso(ev["start"]),
        "channel": vc,
        "entity_type": discord.EntityType.stage_instance
        if isinstance(vc, discord.StageChannel)
        else discord.EntityType.voice,
        "privacy_level": discord.PrivacyLevel.guild_only,
    }
    if ev.get("end"):
        kwargs["end_time"] = timeutil.from_iso(ev["end"])
    return await guild.create_scheduled_event(**kwargs)


async def _sync_discord_event(guild: discord.Guild, ev: dict[str, Any]) -> None:
    se = await _get_discord_event(guild, ev)
    if se is None:
        return
    kwargs: dict[str, Any] = {
        "name": ev["name"][:100],
        "description": _discord_description(guild, ev),
    }
    vc = guild.get_channel(ev["vc_id"])
    if vc:
        kwargs["channel"] = vc
    if se.status == discord.EventStatus.scheduled:
        kwargs["start_time"] = timeutil.from_iso(ev["start"])
    if ev.get("end"):
        kwargs["end_time"] = timeutil.from_iso(ev["end"])
    try:
        await se.edit(**kwargs)
    except discord.HTTPException:
        log.exception("Discord イベントの更新に失敗しました (event %s)", ev["id"])


# ---------------------------------------------------------------- 告知メッセージ (並べ替え)

_announce_locks: dict[int, asyncio.Lock] = {}


def _announce_order(gd: storage.GuildData) -> list[dict[str, Any]]:
    """上から [終了/キャンセル済み] [開催予定] [開催中]、それぞれ日時順"""

    def key(e: dict[str, Any]) -> tuple[str, int]:
        return (e["start"], e["id"])

    done = sorted(events_of(gd, "ended", "cancelled"), key=key)
    sched = sorted(events_of(gd, "scheduled"), key=key)
    active = sorted(events_of(gd, "active"), key=key)
    return done + sched + active


def _embed_hash(embed: discord.Embed) -> str:
    raw = json.dumps(embed.to_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


async def refresh_announcements(guild: discord.Guild) -> None:
    lock = _announce_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        gd = storage.get(guild.id)
        try:
            await _refresh(guild, gd)
        except discord.NotFound:
            # 誰かが告知メッセージを消した → 全部出し直して順番を保つ
            log.warning("guild %s: 告知メッセージが見つからないため再投稿します", guild.id)
            await _clear_announcements(guild, gd)
            await _refresh(guild, gd)
        finally:
            gd.save()


async def _clear_announcements(guild: discord.Guild, gd: storage.GuildData) -> None:
    channel = guild.get_channel(gd.data.get("announcement_channel_id") or 0)
    for slot in gd.data["announcements"]:
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.get_partial_message(slot["message_id"]).delete()
            except discord.HTTPException:
                pass
    gd.data["announcements"] = []


async def _refresh(guild: discord.Guild, gd: storage.GuildData) -> None:
    channel = guild.get_channel(gd.config.get("schedule_channel_id") or 0)
    if not isinstance(channel, discord.TextChannel):
        return
    if gd.data.get("announcement_channel_id") != channel.id:
        # チャンネルが変わったら、新しいチャンネルで出し直す (古いメッセージはそのまま)
        gd.data["announcements"] = []
        gd.data["announcement_channel_id"] = channel.id

    slots: list[dict[str, Any]] = gd.data["announcements"]
    order = _announce_order(gd)
    for i, ev in enumerate(order):
        embed = embeds.announcement_embed(ev, discord_event_url(guild.id, ev))
        h = _embed_hash(embed)
        if i < len(slots):
            slot = slots[i]
            if slot.get("event_id") != ev["id"] or slot.get("hash") != h:
                await channel.get_partial_message(slot["message_id"]).edit(content=None, embed=embed)
                slot["event_id"], slot["hash"] = ev["id"], h
        else:
            msg = await channel.send(embed=embed)
            slots.append({"message_id": msg.id, "event_id": ev["id"], "hash": h})
        ev["message_id"] = slots[i]["message_id"]

    # 余ったメッセージ (削除されたイベントの分) は下から消す
    while len(slots) > len(order):
        slot = slots.pop()
        try:
            await channel.get_partial_message(slot["message_id"]).delete()
        except discord.NotFound:
            pass


# ---------------------------------------------------------------- 参加時間の計測


def _vc_members(guild: discord.Guild, ev: dict[str, Any]) -> list[discord.Member]:
    vc = guild.get_channel(ev["vc_id"])
    if not isinstance(vc, (discord.VoiceChannel, discord.StageChannel)):
        return []
    return [m for m in vc.members if not m.bot]


def attendance_start(guild: discord.Guild, ev: dict[str, Any]) -> None:
    t = time.time()
    ev["attendance"] = {}
    ev["sessions"] = {str(m.id): t for m in _vc_members(guild, ev)}


def attendance_join(ev: dict[str, Any], user_id: int) -> None:
    ev.setdefault("sessions", {}).setdefault(str(user_id), time.time())


def attendance_leave(ev: dict[str, Any], user_id: int) -> None:
    joined = ev.setdefault("sessions", {}).pop(str(user_id), None)
    if joined is not None:
        att = ev.setdefault("attendance", {})
        att[str(user_id)] = att.get(str(user_id), 0) + (time.time() - joined)


def attendance_checkpoint(ev: dict[str, Any]) -> None:
    """在室中の人の時間をいったん確定させる (再起動に備えた定期保存用)"""
    t = time.time()
    att = ev.setdefault("attendance", {})
    sessions = ev.setdefault("sessions", {})
    for uid, joined in sessions.items():
        att[uid] = att.get(uid, 0) + (t - joined)
        sessions[uid] = t


def attendance_resume(guild: discord.Guild, ev: dict[str, Any]) -> None:
    """再起動後: 最後の定期保存までの時間は確定済み。今 VC にいる人から数え直す"""
    t = time.time()
    ev["sessions"] = {str(m.id): t for m in _vc_members(guild, ev)}


def attendance_finish(ev: dict[str, Any]) -> list[dict[str, Any]]:
    for uid in list(ev.get("sessions", {})):
        attendance_leave(ev, int(uid))
    rows = [
        {"user_id": int(uid), "seconds": round(sec)}
        for uid, sec in ev.get("attendance", {}).items()
        if sec >= MIN_ATTEND_SECONDS
    ]
    rows.sort(key=lambda r: r["seconds"], reverse=True)
    return rows


# ---------------------------------------------------------------- 管理パネル (DM)


async def send_panel(guild: discord.Guild, ev: dict[str, Any], dest: discord.abc.Messageable) -> discord.Message:
    from .views.panel import build_panel_view

    view = build_panel_view(guild.id, ev)
    kwargs: dict[str, Any] = {"embed": embeds.panel_embed(ev)}
    if view is not None:
        kwargs["view"] = view
    return await dest.send(**kwargs)


def dm_send_at(start: datetime) -> datetime:
    """管理 DM を送る時刻。9 時より前に始まるイベントでも間に合うようにする"""
    morning = start.replace(hour=DAILY_DM_HOUR, minute=0, second=0, microsecond=0)
    return min(morning, start - DM_BEFORE_START)


def _dm_key(ev: dict[str, Any]) -> str:
    # 日時か主催が変わったら送り直す
    return f"{ev.get('start')}|{ev.get('host_id')}"


async def maybe_send_daily_panel(guild: discord.Guild, gd: storage.GuildData, ev: dict[str, Any]) -> None:
    """送る時刻を過ぎていて、まだ送っていなければ主催者に管理 DM を送る"""
    if ev["status"] != "scheduled":
        return
    start = timeutil.from_iso(ev["start"])
    if start is None or timeutil.now() < dm_send_at(start):
        return
    if ev.get("dm_sent_key") == _dm_key(ev):
        return
    ev["dm_sent_key"] = _dm_key(ev)
    gd.save()

    member = await fetch_member(guild, ev["host_id"])
    sent = False
    if member is not None:
        try:
            await send_panel(guild, ev, member)
            sent = True
        except discord.HTTPException:
            pass
    if not sent:
        embed = discord.Embed(
            title="主催者に DM を送れませんでした",
            color=discord.Color.orange(),
            description=(
                f"<@{ev['host_id']}> さんに本日のイベント管理 DM を送れませんでした。\n"
                f"`/host event_panel` で管理画面を表示できます。"
            ),
        )
        embed.add_field(name="イベント", value=f"`{ev['id']}` : {ev['name']}")
        await send_admin(guild, gd, embed=embed)


async def send_daily_panels(guild: discord.Guild) -> None:
    gd = storage.get(guild.id)
    for ev in events_of(gd, "scheduled"):
        try:
            await maybe_send_daily_panel(guild, gd, ev)
        except Exception:
            log.exception("管理 DM の送信に失敗しました (event %s)", ev["id"])


# ---------------------------------------------------------------- イベントの状態変更


async def create_event(guild: discord.Guild, draft: dict[str, Any], created_by: int) -> dict[str, Any]:
    gd = storage.get(guild.id)
    ev: dict[str, Any] = {k: draft.get(k) for k in embeds.EDIT_KEYS}
    se = await _create_discord_event(guild, ev)  # 失敗したら例外のまま呼び出し元へ
    ev.update(
        id=gd.new_event_id(),
        status="scheduled",
        discord_event_id=se.id,
        created_by=created_by,
        created_at=timeutil.to_iso(timeutil.now()),
        pending_request_id=None,
    )
    gd.events[str(ev["id"])] = ev
    gd.save()
    await refresh_announcements(guild)
    await maybe_send_daily_panel(guild, gd, ev)
    return ev


async def start_event(guild: discord.Guild, ev: dict[str, Any], *, from_discord: bool = False) -> None:
    gd = storage.get(guild.id)
    ev["status"] = "active"
    ev["started_at"] = timeutil.to_iso(timeutil.now())
    attendance_start(guild, ev)
    gd.save()

    if not from_discord:
        se = await _get_discord_event(guild, ev)
        if se and se.status == discord.EventStatus.scheduled:
            try:
                await se.start()
            except discord.HTTPException:
                log.exception("Discord イベントの開始に失敗しました (event %s)", ev["id"])

    channel = guild.get_channel(gd.config.get("event_channel_id") or 0)
    if isinstance(channel, discord.TextChannel):
        role_id = gd.config["games"].get(ev.get("game")) if ev.get("game") != embeds.OTHER_GAME else None
        role = guild.get_role(role_id or 0)
        text = f"🔴 **{ev['name']}** が始まりました！ -> <#{ev['vc_id']}>"
        if role:
            text = f"{role.mention}\n{text}"
        msg = await channel.send(
            text, allowed_mentions=discord.AllowedMentions(roles=[role] if role else [])
        )
        ev["start_notice"] = {"channel_id": channel.id, "message_id": msg.id}
        gd.save()

    await refresh_announcements(guild)


async def _delete_start_notice(guild: discord.Guild, ev: dict[str, Any]) -> None:
    notice = ev.pop("start_notice", None)
    if not notice:
        return
    channel = guild.get_channel(notice["channel_id"])
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.get_partial_message(notice["message_id"]).delete()
        except discord.HTTPException:
            pass


async def end_event(guild: discord.Guild, ev: dict[str, Any], *, from_discord: bool = False) -> None:
    gd = storage.get(guild.id)
    ev["result"] = attendance_finish(ev)
    ev["status"] = "ended"
    ev["ended_at"] = timeutil.to_iso(timeutil.now())
    gd.save()

    if not from_discord:
        se = await _get_discord_event(guild, ev)
        if se and se.status == discord.EventStatus.active:
            try:
                await se.end()
            except discord.HTTPException:
                log.exception("Discord イベントの終了に失敗しました (event %s)", ev["id"])

    channel = guild.get_channel(gd.config.get("event_channel_id") or 0)
    if isinstance(channel, discord.TextChannel):
        embed = discord.Embed(
            title="参加者（滞在時間順）",
            color=discord.Color.dark_grey(),
            description=embeds.participants_text(ev),
        )
        await channel.send(
            f"✅ **{ev['name']}** が終了しました！ 参加ありがとうございました。",
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    await refresh_announcements(guild)


async def cancel_event(
    guild: discord.Guild, ev: dict[str, Any], by_user_id: int | None, *, from_discord: bool = False
) -> None:
    gd = storage.get(guild.id)
    ev["status"] = "cancelled"
    ev["cancelled_at"] = timeutil.to_iso(timeutil.now())
    ev["cancelled_by"] = by_user_id
    gd.save()

    if not from_discord:
        se = await _get_discord_event(guild, ev)
        if se and se.status == discord.EventStatus.scheduled:
            try:
                await se.cancel()
            except discord.HTTPException:
                log.exception("Discord イベントのキャンセルに失敗しました (event %s)", ev["id"])

    await refresh_announcements(guild)

    embed = discord.Embed(title="イベントがキャンセルされました", color=discord.Color.dark_grey())
    embed.add_field(name="イベント", value=f"`{ev['id']}` : {ev['name']}", inline=False)
    embed.add_field(name="開始日時", value=timeutil.fmt_iso(ev["start"]), inline=True)
    embed.add_field(name="主催", value=f"<@{ev['host_id']}>", inline=True)
    embed.add_field(
        name="キャンセルした人",
        value=f"<@{by_user_id}>" if by_user_id else "Discord 上で直接キャンセル",
        inline=True,
    )
    await send_admin(guild, gd, embed=embed, mention=False)


async def apply_changes(guild: discord.Guild, ev: dict[str, Any], new: dict[str, Any]) -> None:
    gd = storage.get(guild.id)
    ev.update(new)
    gd.save()
    await _sync_discord_event(guild, ev)
    await refresh_announcements(guild)
    await maybe_send_daily_panel(guild, gd, ev)


async def delete_event(guild: discord.Guild, ev: dict[str, Any]) -> None:
    gd = storage.get(guild.id)
    se = await _get_discord_event(guild, ev)
    if se:
        try:
            await se.delete()
        except discord.HTTPException:
            log.exception("Discord イベントの削除に失敗しました (event %s)", ev["id"])
    await _delete_start_notice(guild, ev)
    ev["status"] = "deleted"
    ev["deleted_at"] = timeutil.to_iso(timeutil.now())
    ev.pop("message_id", None)
    gd.save()
    await refresh_announcements(guild)
