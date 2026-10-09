"""起動ファイル"""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from event import errors
from event.views.approval import ApprovalButton
from event.views.panel import PanelButton
from event.views.rolepanel import RoleButton

log = logging.getLogger("event")


class EventBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # Developer Portal で SERVER MEMBERS INTENT を ON にすること
        intents.voice_states = True
        intents.guild_scheduled_events = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self) -> None:
        errors.install(self.tree)
        # 再起動後もボタンが動くようにする
        self.add_dynamic_items(PanelButton, ApprovalButton, RoleButton)
        await self.load_extension("event.cogs.commands")
        await self.load_extension("event.cogs.background")

    async def _sync(self, guild: discord.Guild) -> None:
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("%s (%s): コマンド %d 個を同期しました", guild.name, guild.id, len(synced))
        except discord.HTTPException:
            log.exception("%s: コマンドの同期に失敗しました", guild.id)

    async def on_ready(self) -> None:
        log.info("ログインしました: %s", self.user)
        for guild in self.guilds:
            await self._sync(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync(guild)


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(".env に DISCORD_TOKEN を設定してください")
    discord.utils.setup_logging(level=logging.INFO)
    EventBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
