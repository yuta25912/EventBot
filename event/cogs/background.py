"""毎朝の DM、参加時間の計測、Discord 側でのイベント操作の同期"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from .. import actions, storage

log = logging.getLogger(__name__)


class Background(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._resumed = False
        self.daily_dm.start()
        self.checkpoint.start()

    async def cog_unload(self) -> None:
        self.daily_dm.cancel()
        self.checkpoint.cancel()

    # ------------------------------------------------------------ 起動時

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._resumed:
            return
        self._resumed = True
        for guild in self.bot.guilds:
            gd = storage.get(guild.id)
            # 開催中のイベントの計測を、今 VC にいる人から再開する
            active = actions.events_of(gd, "active")
            for ev in active:
                actions.attendance_resume(guild, ev)
            if active:
                gd.save()
            # 9 時を過ぎてから起動した場合の取りこぼし
            await actions.send_daily_panels(guild)
            try:
                await actions.refresh_announcements(guild)
            except discord.HTTPException:
                log.exception("guild %s: 告知メッセージの更新に失敗しました", guild.id)

    # ------------------------------------------------------------ 定期処理

    # 送る時刻はイベントごとに違う (9:00 か開始 1 時間前) ので毎分確認する
    @tasks.loop(minutes=1)
    async def daily_dm(self) -> None:
        for guild in self.bot.guilds:
            await actions.send_daily_panels(guild)

    @daily_dm.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def checkpoint(self) -> None:
        for guild in self.bot.guilds:
            gd = storage.get(guild.id)
            active = actions.events_of(gd, "active")
            for ev in active:
                actions.attendance_checkpoint(ev)
            if active:
                gd.save()

    @checkpoint.before_loop
    async def _before_checkpoint(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ VC 入退室

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or before.channel == after.channel:
            return
        gd = storage.get(member.guild.id)
        changed = False
        for ev in actions.events_of(gd, "active"):
            if before.channel and before.channel.id == ev["vc_id"]:
                actions.attendance_leave(ev, member.id)
                changed = True
            if after.channel and after.channel.id == ev["vc_id"]:
                actions.attendance_join(ev, member.id)
                changed = True
        if changed:
            gd.save()

    # ------------------------------------------------------------ Discord 上で直接操作された場合

    def _find(self, se: discord.ScheduledEvent) -> dict | None:
        gd = storage.get(se.guild.id)
        for ev in gd.events.values():
            if ev.get("discord_event_id") == se.id:
                return ev
        return None

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        ev = self._find(after)
        if ev is None or before.status == after.status:
            return
        try:
            if after.status == discord.EventStatus.active and ev["status"] == "scheduled":
                await actions.start_event(after.guild, ev, from_discord=True)
            elif after.status == discord.EventStatus.completed and ev["status"] == "active":
                await actions.end_event(after.guild, ev, from_discord=True)
            elif after.status == discord.EventStatus.cancelled and ev["status"] == "scheduled":
                await actions.cancel_event(after.guild, ev, None, from_discord=True)
        except Exception:
            log.exception("Discord イベントの状態同期に失敗しました (event %s)", ev["id"])

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, se: discord.ScheduledEvent) -> None:
        ev = self._find(se)
        if ev is None:
            return
        gd = storage.get(se.guild.id)
        ev["discord_event_id"] = None
        gd.save()
        log.info("Discord イベントが直接削除されました (event %s)", ev["id"])
        try:
            await actions.refresh_announcements(se.guild)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Background(bot))
