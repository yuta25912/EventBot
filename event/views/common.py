from __future__ import annotations

from typing import Awaitable, Callable

import discord


class ConfirmView(discord.ui.View):
    """「本当に〜しますか？」用の 2 択ボタン (押せるのは本人だけ)"""

    def __init__(
        self,
        author_id: int,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        confirm_label: str,
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
    ):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.on_confirm = on_confirm
        self.confirm.label = confirm_label
        self.confirm.style = confirm_style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await self.on_confirm(interaction)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="やめました。", embed=None, view=None)


async def reply_error(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(f"⚠️ {text}", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ {text}", ephemeral=True)
