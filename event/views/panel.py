"""主催者向けの管理パネル (毎朝の DM / /host event_panel)"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord

from .. import errors, actions, embeds, perms, storage
from .common import ConfirmView, reply_error
from .editor import modify_block_reason, open_modify_editor

log = logging.getLogger(__name__)

_BUTTONS = {
    "start": ("開始", discord.ButtonStyle.success),
    "modify": ("内容変更", discord.ButtonStyle.primary),
    "cancel": ("イベントキャンセル", discord.ButtonStyle.danger),
    "end": ("終了", discord.ButtonStyle.danger),
}
_BY_STATUS = {
    "scheduled": ("start", "modify", "cancel"),
    "active": ("end",),
}


def build_panel_view(guild_id: int, ev: dict[str, Any]) -> discord.ui.View | None:
    acts = _BY_STATUS.get(ev["status"])
    if not acts:
        return None
    view = discord.ui.View(timeout=None)
    for a in acts:
        view.add_item(PanelButton(guild_id, ev["id"], a))
    return view


async def _refresh_panel(message: discord.Message | None, guild_id: int, ev: dict[str, Any]) -> None:
    if message is None:
        return
    try:
        await message.edit(embed=embeds.panel_embed(ev), view=build_panel_view(guild_id, ev))
    except discord.HTTPException:
        pass


class PanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gev:panel:(?P<g>\d+):(?P<e>\d+):(?P<a>start|modify|cancel|end)",
):
    def __init__(self, guild_id: int, event_id: int, action: str):
        label, style = _BUTTONS[action]
        super().__init__(
            discord.ui.Button(label=label, style=style, custom_id=f"gev:panel:{guild_id}:{event_id}:{action}")
        )
        self.guild_id = guild_id
        self.event_id = event_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["g"]), int(match["e"]), match["a"])

    @errors.guard
    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            return await reply_error(interaction, "サーバーが見つかりません。")
        gd = storage.get(guild.id)
        ev = gd.get_event(self.event_id)
        if ev is None:
            return await reply_error(interaction, "イベントが見つかりません。")
        member = await perms.resolve_member(guild, interaction.user)
        if not perms.is_host_or_admin(member, gd, ev):
            return await reply_error(interaction, "このイベントの主催者か管理者のみ操作できます。")

        # 状態が変わっていたら、押されたパネルを最新にしてから知らせる
        if self.action not in _BY_STATUS.get(ev["status"], ()):
            await _refresh_panel(interaction.message, guild.id, ev)
            return await reply_error(
                interaction, f"このイベントは現在「{embeds.STATUS_LABELS.get(ev['status'])}」です。"
            )

        if self.action == "start":
            await interaction.response.defer()
            if ev["status"] != "scheduled":
                return
            await actions.start_event(guild, ev)
            await interaction.edit_original_response(embed=embeds.panel_embed(ev), view=build_panel_view(guild.id, ev))

        elif self.action == "end":
            await interaction.response.defer()
            if ev["status"] != "active":
                return
            await actions.end_event(guild, ev)
            await interaction.edit_original_response(embed=embeds.panel_embed(ev), view=None)

        elif self.action == "modify":
            if reason := modify_block_reason(gd, ev):
                return await reply_error(interaction, reason)
            await open_modify_editor(interaction, guild, ev, in_guild=interaction.guild is not None)

        elif self.action == "cancel":
            panel_message = interaction.message

            async def do_cancel(inter: discord.Interaction) -> None:
                if ev["status"] != "scheduled":
                    return await inter.response.edit_message(content="このイベントはキャンセルできません。", view=None)
                await inter.response.edit_message(content="キャンセルしています…", view=None)
                if ev.get("pending_request_id"):
                    req = gd.requests.get(str(ev["pending_request_id"]))
                    if req and req["status"] == "pending":
                        req.update(status="stale", reason="イベントがキャンセルされました")
                    ev["pending_request_id"] = None
                await actions.cancel_event(guild, ev, inter.user.id)
                await _refresh_panel(panel_message, guild.id, ev)
                await inter.edit_original_response(content="イベントをキャンセルしました。")

            await interaction.response.send_message(
                f"**{ev['name']}** をキャンセルしますか？",
                view=ConfirmView(interaction.user.id, do_cancel, "キャンセルする"),
                ephemeral=True,
            )
