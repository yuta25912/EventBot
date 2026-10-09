"""管理者連絡チャンネルでの承認フロー (内容変更・削除)"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord

from .. import errors, actions, embeds, perms, storage, timeutil
from .common import reply_error

log = logging.getLogger(__name__)


def _approval_view(guild_id: int, request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton(guild_id, request_id, "ok"))
    view.add_item(ApprovalButton(guild_id, request_id, "ng"))
    return view


def _request_embed(gd: storage.GuildData, req: dict[str, Any]) -> discord.Embed:
    ev = gd.get_event(req["event_id"]) or {}
    if req["type"] == "modify":
        embed = embeds.diff_embed(
            req["old"], req["new"], f"イベント変更申請 #{req['id']}", discord.Color.gold()
        )
    else:
        embed = discord.Embed(title=f"イベント削除申請 #{req['id']}", color=discord.Color.red())
        embeds.add_detail_fields(embed, ev)
        embed.add_field(name="状態", value=embeds.STATUS_LABELS.get(ev.get("status", ""), "-"), inline=True)
    embed.add_field(name="イベントID", value=str(req["event_id"]), inline=True)
    embed.add_field(name="申請者", value=f"<@{req['requester_id']}>", inline=True)

    status = req["status"]
    if status == "approved":
        embed.add_field(name="結果", value=f"✅ 確認済み（<@{req['decided_by']}>）", inline=False)
        embed.color = discord.Color.green()
    elif status == "rejected":
        embed.add_field(
            name="結果", value=f"❌ 拒否（<@{req['decided_by']}>）\n理由: {req.get('reason') or 'なし'}", inline=False
        )
        embed.color = discord.Color.dark_red()
    elif status == "stale":
        embed.add_field(name="結果", value=f"⚠️ 無効（{req.get('reason')}）", inline=False)
        embed.color = discord.Color.dark_grey()
    return embed


async def _post_request(guild: discord.Guild, gd: storage.GuildData, req: dict[str, Any]) -> bool:
    msg = await actions.send_admin(
        guild, gd, embed=_request_embed(gd, req), view=_approval_view(guild.id, req["id"]), mention=True
    )
    if msg is None:
        return False
    req["message_id"] = msg.id
    req["channel_id"] = msg.channel.id
    return True


async def submit_change_request(
    guild: discord.Guild, event_id: int, draft: dict[str, Any], requester_id: int
) -> tuple[bool, str]:
    from .editor import modify_block_reason

    gd = storage.get(guild.id)
    ev = gd.get_event(event_id)
    if ev is None:
        return False, "イベントが見つかりません。"
    if reason := modify_block_reason(gd, ev):
        return False, reason

    old = {k: ev.get(k) for k in embeds.EDIT_KEYS}
    new = {k: draft.get(k) for k in embeds.EDIT_KEYS}
    if old == new:
        return False, "変更がありません。"

    req = {
        "id": gd.new_request_id(),
        "type": "modify",
        "event_id": ev["id"],
        "requester_id": requester_id,
        "old": old,
        "new": new,
        "status": "pending",
        "created_at": timeutil.to_iso(timeutil.now()),
    }
    gd.requests[str(req["id"])] = req
    ev["pending_request_id"] = req["id"]
    gd.save()
    if not await _post_request(guild, gd, req):
        ev["pending_request_id"] = None
        req["status"] = "stale"
        req["reason"] = "管理者連絡チャンネルに送れませんでした"
        gd.save()
        return False, "管理者連絡チャンネルに送信できませんでした。設定を確認してください。"
    gd.save()
    return True, f"変更を申請しました（申請 #{req['id']}）。管理者の確認をお待ちください。"


async def submit_delete_request(guild: discord.Guild, event_id: int, requester_id: int) -> tuple[bool, str]:
    gd = storage.get(guild.id)
    ev = gd.get_event(event_id)
    if ev is None or ev["status"] == "deleted":
        return False, "イベントが見つかりません。"
    if ev.get("pending_request_id"):
        return False, "このイベントには承認待ちの申請があります。"
    req = {
        "id": gd.new_request_id(),
        "type": "delete",
        "event_id": ev["id"],
        "requester_id": requester_id,
        "status": "pending",
        "created_at": timeutil.to_iso(timeutil.now()),
    }
    gd.requests[str(req["id"])] = req
    ev["pending_request_id"] = req["id"]
    gd.save()
    if not await _post_request(guild, gd, req):
        ev["pending_request_id"] = None
        req["status"] = "stale"
        req["reason"] = "管理者連絡チャンネルに送れませんでした"
        gd.save()
        return False, "管理者連絡チャンネルに送信できませんでした。設定を確認してください。"
    gd.save()
    return True, f"削除を申請しました（申請 #{req['id']}）。管理者の確認をお待ちください。"


async def _notify_requester(guild: discord.Guild, req: dict[str, Any], ev: dict[str, Any]) -> None:
    kind = "変更" if req["type"] == "modify" else "削除"
    if req["status"] == "approved":
        embed = discord.Embed(
            title=f"イベント{kind}申請が確認されました", color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title=f"イベント{kind}申請が拒否されました",
            color=discord.Color.dark_red(),
            description=f"理由: {req.get('reason') or 'なし'}",
        )
    embed.add_field(name="イベント", value=f"`{ev['id']}` : {ev['name']}")
    await actions.dm(guild, req["requester_id"], embed=embed)


class RejectModal(discord.ui.Modal, title="拒否理由の入力"):
    reason = discord.ui.TextInput(label="拒否理由", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, guild: discord.Guild, request_id: int):
        super().__init__()
        self.guild = guild
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        gd = storage.get(self.guild.id)
        req = gd.requests.get(str(self.request_id))
        if req is None or req["status"] != "pending":
            return await reply_error(interaction, "この申請はすでに処理されています。")
        ev = gd.get_event(req["event_id"]) or {}
        req.update(status="rejected", decided_by=interaction.user.id, reason=self.reason.value)
        if ev.get("pending_request_id") == req["id"]:
            ev["pending_request_id"] = None
        gd.save()
        await interaction.response.edit_message(embed=_request_embed(gd, req), view=None)
        if ev:
            await _notify_requester(self.guild, req, ev)


class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"gev:req:(?P<g>\d+):(?P<r>\d+):(?P<a>ok|ng)"
):
    def __init__(self, guild_id: int, request_id: int, action: str):
        super().__init__(
            discord.ui.Button(
                label="確認" if action == "ok" else "拒否",
                style=discord.ButtonStyle.success if action == "ok" else discord.ButtonStyle.danger,
                custom_id=f"gev:req:{guild_id}:{request_id}:{action}",
            )
        )
        self.guild_id = guild_id
        self.request_id = request_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["g"]), int(match["r"]), match["a"])

    @errors.guard
    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            return await reply_error(interaction, "サーバーが見つかりません。")
        gd = storage.get(guild.id)
        req = gd.requests.get(str(self.request_id))
        if req is None:
            return await reply_error(interaction, "申請が見つかりません。")
        if req["status"] != "pending":
            await interaction.response.edit_message(embed=_request_embed(gd, req), view=None)
            return
        member = await perms.resolve_member(guild, interaction.user)
        if not perms.is_admin(member, gd):
            return await reply_error(interaction, "管理者のみ操作できます。")
        if interaction.user.id == req["requester_id"]:
            return await reply_error(interaction, "自分の申請は、ほかの管理者に確認してもらってください。")

        if self.action == "ng":
            return await interaction.response.send_modal(RejectModal(guild, req["id"]))

        ev = gd.get_event(req["event_id"])
        if ev is None:
            return await reply_error(interaction, "イベントが見つかりません。")

        # 確認
        if req["type"] == "modify" and ev["status"] != "scheduled":
            req.update(status="stale", reason=f"イベントが「{embeds.STATUS_LABELS.get(ev['status'])}」になっています")
            ev["pending_request_id"] = None
            gd.save()
            return await interaction.response.edit_message(embed=_request_embed(gd, req), view=None)

        req.update(status="approved", decided_by=interaction.user.id)
        ev["pending_request_id"] = None
        gd.save()
        await interaction.response.edit_message(embed=_request_embed(gd, req), view=None)
        try:
            if req["type"] == "modify":
                await actions.apply_changes(guild, ev, req["new"])
            else:
                await actions.delete_event(guild, ev)
        except Exception:
            log.exception("申請 #%s の反映に失敗しました", req["id"])
            await interaction.followup.send("⚠️ 反映中にエラーが発生しました。ログを確認してください。", ephemeral=True)
        await _notify_requester(guild, req, ev)
