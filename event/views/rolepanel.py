"""Test鯖用のロールパネル (ボタンでロールを付け外しする)"""

from __future__ import annotations

import re

import discord

from .. import errors, storage
from .common import reply_error


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gev:role:(?P<r>\d+)"):
    def __init__(self, role_id: int, label: str = "ロール"):
        super().__init__(
            discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"gev:role:{role_id}")
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["r"]), item.label or "ロール")

    @errors.guard
    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await reply_error(interaction, "サーバー内で使ってください。")
        if not storage.get(guild.id).config.get("test_server"):
            return await reply_error(interaction, "このサーバーではロールパネルは無効です。")
        role = guild.get_role(self.role_id)
        if role is None:
            return await reply_error(interaction, "ロールが見つかりません。")
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="ロールパネル")
                text = f"{role.mention} を外しました。"
            else:
                await interaction.user.add_roles(role, reason="ロールパネル")
                text = f"{role.mention} を付けました。"
        except discord.Forbidden:
            return await reply_error(interaction, "Bot にロールを操作する権限がありません（ロールの順番を確認してください）。")
        await interaction.response.send_message(text, ephemeral=True)


def build_role_panel(guild: discord.Guild) -> tuple[discord.Embed, discord.ui.View | None]:
    gd = storage.get(guild.id)
    roles: list[tuple[str, discord.Role]] = []
    admin_role = guild.get_role(gd.config.get("admin_role_id") or 0)
    if admin_role:
        roles.append((f"管理者: {admin_role.name}", admin_role))
    for game, role_id in gd.config["games"].items():
        role = guild.get_role(role_id or 0)
        if role and role not in [r for _, r in roles]:
            roles.append((f"{game}: {role.name}", role))

    embed = discord.Embed(
        title="ロールパネル（テスト用）",
        color=discord.Color.teal(),
        description="ボタンを押すとロールを付けたり外したりできます。",
    )
    if not roles:
        embed.description = "付け外しできるロールがありません。管理者ロールやゲームロールを設定してください。"
        return embed, None
    view = discord.ui.View(timeout=None)
    for label, role in roles[:25]:
        view.add_item(RoleButton(role.id, label))
    return embed, view
