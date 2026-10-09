"""Embed の組み立て"""

from __future__ import annotations

from typing import Any, Callable

import discord

from . import timeutil

OTHER_GAME = "その他"
MINECRAFT = "Minecraft"

# 編集できるキー (イベントにもドラフトにも同じ形で入っている)
EDIT_KEYS = ("name", "description", "game", "game_custom", "version", "start", "end", "host_id", "vc_id")

STATUS_LABELS = {
    "scheduled": "開始前",
    "active": "開催中",
    "ended": "終了",
    "cancelled": "キャンセル",
    "deleted": "削除済み",
}


def game_display(d: dict[str, Any]) -> str:
    if d.get("game") == OTHER_GAME:
        return d.get("game_custom") or "(未入力)"
    return d.get("game") or "未設定"


def needs_version(d: dict[str, Any]) -> bool:
    return d.get("game") == MINECRAFT


# (表示名, 値の取り出し方) — 差分表示でも使う
DISPLAY_FIELDS: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
    ("企画名", lambda d: d.get("name") or "(未入力)"),
    ("企画内容", lambda d: d.get("description") or "なし"),
    ("ゲーム", game_display),
    ("バージョン", lambda d: (d.get("version") or "未入力") if needs_version(d) else "-"),
    ("開始日時", lambda d: timeutil.fmt_iso(d.get("start"))),
    ("終了日時", lambda d: timeutil.fmt_iso(d.get("end"))),
    ("主催", lambda d: f"<@{d['host_id']}>" if d.get("host_id") else "未設定"),
    ("VC", lambda d: f"<#{d['vc_id']}>" if d.get("vc_id") else "未設定"),
]

_INLINE = {"ゲーム", "バージョン", "開始日時", "終了日時", "主催", "VC"}


def _clip(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def add_detail_fields(embed: discord.Embed, d: dict[str, Any], skip: set[str] | None = None) -> None:
    for label, getter in DISPLAY_FIELDS:
        if skip and label in skip:
            continue
        if label == "バージョン" and not needs_version(d):
            continue
        embed.add_field(name=label, value=_clip(getter(d)), inline=label in _INLINE)


def draft_embed(d: dict[str, Any], title: str, note: str | None = None) -> discord.Embed:
    """作成・編集中の確認用"""
    embed = discord.Embed(title=title, color=discord.Color.blurple(), description=note)
    add_detail_fields(embed, d)
    return embed


def diff_embed(old: dict[str, Any], new: dict[str, Any], title: str, color: discord.Color) -> discord.Embed:
    """変更箇所に ※変更あり を付け、変更前 -> **変更後** で表示する"""
    embed = discord.Embed(title=title, color=color)
    for label, getter in DISPLAY_FIELDS:
        before, after = getter(old), getter(new)
        if label == "バージョン" and not needs_version(old) and not needs_version(new):
            continue
        if before != after:
            embed.add_field(name=f"{label} ※変更あり", value=_clip(f"{before} -> **{after}**"), inline=False)
        else:
            embed.add_field(name=label, value=_clip(after), inline=False)
    return embed


def participants_text(event: dict[str, Any]) -> str:
    rows = event.get("result") or []
    if not rows:
        return "参加者なし"
    lines = [f"{i}. <@{r['user_id']}> — {timeutil.fmt_duration(r['seconds'])}" for i, r in enumerate(rows, 1)]
    text = "\n".join(lines)
    return text if len(text) <= 3500 else text[:3490] + "\n…"


def announcement_embed(event: dict[str, Any], discord_event_url: str | None) -> discord.Embed:
    status = event["status"]
    name = event["name"]
    if status == "active":
        embed = discord.Embed(title=f"🔴 開催中: {name}", color=discord.Color.red())
    elif status == "ended":
        embed = discord.Embed(
            title=f"✅ 終了: {name}",
            color=discord.Color.dark_grey(),
            description=f"**参加者** (滞在時間順)\n{participants_text(event)}",
        )
    elif status == "cancelled":
        embed = discord.Embed(
            title=f"❌ 取り消されました: {name}",
            color=discord.Color.darker_grey(),
            description="このイベントは取り消されました。",
        )
    else:
        embed = discord.Embed(title=f"📅 {name}", color=discord.Color.blue())

    if status == "cancelled":
        embed.add_field(name="開始日時", value=timeutil.fmt_iso(event.get("start")), inline=True)
        embed.add_field(name="主催", value=f"<@{event['host_id']}>", inline=True)
    else:
        add_detail_fields(embed, event, skip={"企画名"})
        if discord_event_url and status in ("scheduled", "active"):
            embed.add_field(name="Discord イベント", value=discord_event_url, inline=False)
    embed.set_footer(text=f"イベントID: {event['id']}")
    return embed


def panel_embed(event: dict[str, Any]) -> discord.Embed:
    """主催者向けの管理 Embed"""
    status = event["status"]
    color = {
        "scheduled": discord.Color.blue(),
        "active": discord.Color.red(),
        "ended": discord.Color.dark_grey(),
    }.get(status, discord.Color.darker_grey())
    embed = discord.Embed(title=f"イベント管理: {event['name']}", color=color)
    embed.add_field(name="状態", value=STATUS_LABELS.get(status, status), inline=False)
    add_detail_fields(embed, event, skip={"企画名"})
    if status == "ended":
        embed.add_field(name="参加者", value=_clip(participants_text(event)), inline=False)
    if event.get("pending_request_id"):
        embed.add_field(name="承認待ち", value="内容変更の申請が承認待ちです", inline=False)
    embed.set_footer(text=f"イベントID: {event['id']}")
    return embed


def event_line(event: dict[str, Any]) -> str:
    return (
        f"`{event['id']}` : **{event['name']}** — {timeutil.fmt_iso(event.get('start'))} "
        f"— <@{event['host_id']}> — {STATUS_LABELS.get(event['status'], event['status'])}"
    )
