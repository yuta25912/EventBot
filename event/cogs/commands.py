"""スラッシュコマンド (/admin, /host)"""

from __future__ import annotations

from typing import Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from .. import actions, embeds, errors, perms, storage, timeutil
from ..views.approval import submit_delete_request
from ..views.common import ConfirmView, reply_error
from ..views.editor import EventEditor, EventModal, modify_block_reason, open_modify_editor
from ..views.rolepanel import build_role_panel


def _event_choices(
    interaction: discord.Interaction, current: str, statuses: tuple[str, ...], *, only_own: bool
) -> list[app_commands.Choice[str]]:
    guild = interaction.guild
    if guild is None:
        return []
    gd = storage.get(guild.id)
    is_admin = perms.is_admin(interaction.user if isinstance(interaction.user, discord.Member) else None, gd)
    evs = [e for e in gd.events.values() if e["status"] in statuses]
    if only_own and not is_admin:
        evs = [e for e in evs if e["host_id"] == interaction.user.id]
    evs.sort(key=lambda e: e["start"])
    cur = current.strip().lower()
    out = []
    for e in evs:
        label = f"{e['id']} : {e['name']}"
        if cur and cur not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=str(e["id"])))
    return out[:25]


def _get_event(gd: storage.GuildData, value: str) -> dict[str, Any] | None:
    # 候補を選ばずに「3 : 名前」や「3」と打たれても受け付ける
    head = value.split(":", 1)[0].strip()
    return gd.get_event(head) if head.isdigit() else None


class EventCommands(commands.Cog):
    admin = app_commands.Group(name="admin", description="管理者用コマンド", guild_only=True)
    host = app_commands.Group(name="host", description="主催者用コマンド", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------ 権限

    async def _require_admin(self, interaction: discord.Interaction) -> storage.GuildData | None:
        gd = storage.get(interaction.guild.id)  # type: ignore[union-attr]
        if not perms.is_admin(interaction.user, gd):  # type: ignore[arg-type]
            await reply_error(interaction, "管理者のみ使えるコマンドです。")
            return None
        return gd

    async def _require_config(self, interaction: discord.Interaction) -> storage.GuildData | None:
        gd = storage.get(interaction.guild.id)  # type: ignore[union-attr]
        if not perms.can_configure(interaction.user, gd):  # type: ignore[arg-type]
            await reply_error(interaction, "管理者のみ使えるコマンドです。")
            return None
        return gd

    # ------------------------------------------------------------ 設定

    @admin.command(name="config_admin_role", description="管理者ロールを設定します")
    @app_commands.rename(role="ロール")
    async def config_admin_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        gd.config["admin_role_id"] = role.id
        gd.save()
        await interaction.response.send_message(f"管理者ロールを {role.mention} にしました。", ephemeral=True)

    @admin.command(name="config_admin_channel", description="管理者連絡チャンネルを設定します")
    @app_commands.rename(channel="チャンネル")
    async def config_admin_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        if missing := errors.missing_perms(channel):
            return await reply_error(interaction, errors.missing_text(channel, missing))
        gd.config["admin_channel_id"] = channel.id
        gd.save()
        await interaction.response.send_message(f"管理者連絡チャンネルを {channel.mention} にしました。", ephemeral=True)

    @admin.command(name="config_schedule_channel", description="イベント予定チャンネル（告知 Embed を日時順に並べる）を設定します")
    @app_commands.rename(channel="チャンネル")
    async def config_schedule_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        if missing := errors.missing_perms(channel):
            return await reply_error(interaction, errors.missing_text(channel, missing))
        gd.config["schedule_channel_id"] = channel.id
        gd.save()
        await interaction.response.send_message(
            f"イベント予定チャンネルを {channel.mention} にしました。告知メッセージを出し直します。", ephemeral=True
        )
        await actions.refresh_announcements(interaction.guild)  # type: ignore[arg-type]

    @admin.command(name="config_event_channel", description="イベントチャンネル（開始・終了のお知らせ）を設定します")
    @app_commands.rename(channel="チャンネル")
    async def config_event_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        if missing := errors.missing_perms(channel):
            return await reply_error(interaction, errors.missing_text(channel, missing))
        gd.config["event_channel_id"] = channel.id
        gd.save()
        await interaction.response.send_message(
            f"イベントチャンネル（開始・終了のお知らせ）を {channel.mention} にしました。", ephemeral=True
        )

    @admin.command(name="config_game_add", description="ゲームを追加します（ロールも設定できます）")
    @app_commands.rename(name="ゲーム名", role="ロール")
    @app_commands.describe(role="開始時にメンションするロール（省略可）")
    async def config_game_add(
        self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100], role: Optional[discord.Role] = None
    ) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        if name == embeds.OTHER_GAME:
            return await reply_error(interaction, f"「{embeds.OTHER_GAME}」は登録できません。")
        if name not in gd.config["games"] and len(gd.config["games"]) >= 24:
            return await reply_error(interaction, "ゲームは 24 個まで登録できます。")
        gd.config["games"][name] = role.id if role else None
        gd.save()
        await interaction.response.send_message(
            f"ゲーム「{name}」を登録しました（ロール: {role.mention if role else 'なし'}）。", ephemeral=True
        )

    async def _game_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        games = list(storage.get(interaction.guild.id).config["games"].keys())  # type: ignore[union-attr]
        return [app_commands.Choice(name=g, value=g) for g in games if current.lower() in g.lower()][:25]

    @admin.command(name="config_game_remove", description="ゲームを削除します")
    @app_commands.rename(name="ゲーム名")
    @app_commands.autocomplete(name=_game_autocomplete)
    async def config_game_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        if gd.config["games"].pop(name, "missing") == "missing":
            return await reply_error(interaction, f"ゲーム「{name}」は登録されていません。")
        gd.save()
        await interaction.response.send_message(f"ゲーム「{name}」を削除しました。", ephemeral=True)

    @admin.command(name="config_show", description="現在の設定を表示します")
    async def config_show(self, interaction: discord.Interaction) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        c = gd.config

        def ch(i: int | None) -> str:
            return f"<#{i}>" if i else "未設定"

        embed = discord.Embed(title="設定", color=discord.Color.blurple())
        embed.add_field(
            name="管理者ロール",
            value=f"<@&{c['admin_role_id']}>" if c.get("admin_role_id") else "未設定（サーバー管理者権限で判定）",
            inline=False,
        )
        embed.add_field(name="管理者連絡チャンネル", value=ch(c.get("admin_channel_id")), inline=False)
        embed.add_field(name="イベント予定チャンネル", value=ch(c.get("schedule_channel_id")), inline=False)
        embed.add_field(name="イベントチャンネル（開始・終了）", value=ch(c.get("event_channel_id")), inline=False)
        games = "\n".join(f"・{g}: {f'<@&{r}>' if r else 'ロールなし'}" for g, r in c["games"].items()) or "なし"
        embed.add_field(name="ゲームとロール", value=games, inline=False)
        embed.add_field(name="Test鯖", value="はい" if c.get("test_server") else "いいえ", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="rolepanel", description="（Test鯖のみ）ロールパネルをこのチャンネルに設置します")
    async def rolepanel(self, interaction: discord.Interaction) -> None:
        if not (gd := await self._require_config(interaction)):
            return
        if not gd.config.get("test_server"):
            return await reply_error(interaction, "Test鯖 でのみ使えます。")
        embed, view = build_role_panel(interaction.guild)  # type: ignore[arg-type]
        if view is None:
            return await reply_error(interaction, embed.description or "ロールがありません。")
        channel = interaction.channel
        if not isinstance(channel, discord.abc.GuildChannel):
            return await reply_error(interaction, "このチャンネルには設置できません。")
        if missing := errors.missing_perms(channel, "view_channel", "send_messages", "embed_links"):
            return await reply_error(interaction, errors.missing_text(channel, missing))
        if not channel.guild.me.guild_permissions.manage_roles:
            return await reply_error(interaction, f"Bot に権限が足りません: **{errors.PERM_LABELS['manage_roles']}**")
        await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
        await interaction.response.send_message("ロールパネルを設置しました。", ephemeral=True)

    # ------------------------------------------------------------ イベント (admin)

    async def _dt_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return timeutil.autocomplete(current)

    async def _create_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        games = list(storage.get(interaction.guild.id).config["games"].keys())[:24] + [embeds.OTHER_GAME]  # type: ignore[union-attr]
        return [app_commands.Choice(name=g, value=g) for g in games if current.lower() in g.lower()][:25]

    @admin.command(name="event_create", description="イベントを作成します")
    @app_commands.rename(start="日時", host="主催", vc="vc", game="ゲーム", end="終了日時")
    @app_commands.describe(
        start="開始日時（例: 2026-10-12 21:00）",
        host="主催するユーザー",
        vc="使うボイスチャンネル",
        game="ゲーム（「その他」なら次の画面で入力）",
        end="終了日時（省略可）",
    )
    @app_commands.autocomplete(start=_dt_autocomplete, end=_dt_autocomplete, game=_create_game_autocomplete)
    async def event_create(
        self,
        interaction: discord.Interaction,
        start: str,
        host: discord.Member,
        vc: Union[discord.VoiceChannel, discord.StageChannel],
        game: str,
        end: Optional[str] = None,
    ) -> None:
        if not (gd := await self._require_admin(interaction)):
            return
        for key, label, cmd in (
            ("schedule_channel_id", "イベント予定チャンネル", "config_schedule_channel"),
            ("event_channel_id", "イベントチャンネル", "config_event_channel"),
        ):
            target = interaction.guild.get_channel(gd.config.get(key) or 0)  # type: ignore[union-attr]
            if target is None:
                return await reply_error(interaction, f"{label}が未設定です（`/admin {cmd}`）。")
            if missing := errors.missing_perms(target):
                return await reply_error(interaction, errors.missing_text(target, missing))
        if missing := errors.missing_perms(vc, "view_channel", "connect", "create_events", "manage_events"):
            return await reply_error(interaction, errors.missing_text(vc, missing))
        if game != embeds.OTHER_GAME and game not in gd.config["games"]:
            return await reply_error(interaction, f"ゲーム「{game}」は登録されていません。候補から選んでください。")
        if host.bot:
            return await reply_error(interaction, "Bot は主催にできません。")
        start_dt = timeutil.parse(start)
        if start_dt is None:
            return await reply_error(interaction, "日時の形式が正しくありません（例: 2026-10-12 21:00）。")
        if start_dt <= timeutil.now():
            return await reply_error(interaction, "開始日時は今より後にしてください。")
        end_dt = None
        if end:
            end_dt = timeutil.parse(end)
            if end_dt is None or end_dt <= start_dt:
                return await reply_error(interaction, "終了日時が正しくありません（開始より後の日時を入力してください）。")

        guild = interaction.guild
        assert guild is not None
        draft: dict[str, Any] = {
            "name": None,
            "description": None,
            "game": game,
            "game_custom": None,
            "version": None,
            "start": timeutil.to_iso(start_dt),
            "end": timeutil.to_iso(end_dt),
            "host_id": host.id,
            "vc_id": vc.id,
        }

        async def done(inter: discord.Interaction, d: dict[str, Any], error: str | None) -> None:
            view = EventEditor(guild, d, mode="create", author_id=inter.user.id, note=error)
            await inter.response.send_message(embed=view.embed(), view=view, ephemeral=True)

        await interaction.response.send_modal(EventModal(draft, include_time=False, on_done=done, title="イベント作成"))

    @admin.command(name="event_list", description="イベントの一覧を表示します")
    @app_commands.rename(show_all="すべて")
    @app_commands.describe(show_all="終了・キャンセル済みも表示する")
    async def event_list(self, interaction: discord.Interaction, show_all: bool = False) -> None:
        if not (gd := await self._require_admin(interaction)):
            return
        statuses = ("scheduled", "active", "ended", "cancelled") if show_all else ("scheduled", "active")
        evs = sorted((e for e in gd.events.values() if e["status"] in statuses), key=lambda e: e["start"])
        lines = [embeds.event_line(e) for e in evs] or ["イベントはありません。"]
        text = ""
        for line in lines:
            if len(text) + len(line) > 3900:
                text += "\n…"
                break
            text += line + "\n"
        embed = discord.Embed(
            title="イベント一覧" + ("（すべて）" if show_all else "（今後）"), description=text, color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _delete_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _event_choices(interaction, current, ("scheduled", "active", "ended", "cancelled"), only_own=False)

    @admin.command(name="event_delete", description="イベントの削除を申請します（ほかの管理者の確認が必要）")
    @app_commands.rename(event="イベント")
    @app_commands.autocomplete(event=_delete_autocomplete)
    async def event_delete(self, interaction: discord.Interaction, event: str) -> None:
        if not (gd := await self._require_admin(interaction)):
            return
        ev = _get_event(gd, event)
        if ev is None or ev["status"] == "deleted":
            return await reply_error(interaction, "イベントが見つかりません。")
        if ev.get("pending_request_id"):
            return await reply_error(interaction, "このイベントには承認待ちの申請があります。")
        guild = interaction.guild
        assert guild is not None

        async def confirmed(inter: discord.Interaction) -> None:
            await inter.response.edit_message(content="申請しています…", embed=None, view=None)
            ok, message = await submit_delete_request(guild, ev["id"], inter.user.id)
            await inter.edit_original_response(content=("✅ " if ok else "⚠️ ") + message)

        embed = discord.Embed(title="本当に削除申請しますか？", color=discord.Color.red())
        embeds.add_detail_fields(embed, ev)
        await interaction.response.send_message(
            embed=embed, view=ConfirmView(interaction.user.id, confirmed, "削除を申請する"), ephemeral=True
        )

    # ------------------------------------------------------------ イベント (host)

    async def _modify_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _event_choices(interaction, current, ("scheduled",), only_own=True)

    async def _panel_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _event_choices(interaction, current, ("scheduled", "active"), only_own=True)

    @host.command(name="event_modify", description="イベントの内容変更を申請します")
    @app_commands.rename(event="イベント")
    @app_commands.autocomplete(event=_modify_autocomplete)
    async def event_modify(self, interaction: discord.Interaction, event: str) -> None:
        gd = storage.get(interaction.guild.id)  # type: ignore[union-attr]
        ev = _get_event(gd, event)
        if ev is None or ev["status"] == "deleted":
            return await reply_error(interaction, "イベントが見つかりません。")
        if not perms.is_host_or_admin(interaction.user, gd, ev):  # type: ignore[arg-type]
            return await reply_error(interaction, "このイベントの主催者か管理者のみ使えます。")
        if reason := modify_block_reason(gd, ev):
            return await reply_error(interaction, reason)
        await open_modify_editor(interaction, interaction.guild, ev, in_guild=True)  # type: ignore[arg-type]

    @host.command(name="event_panel", description="イベント管理パネルを表示します")
    @app_commands.rename(event="イベント", send_to="送り先")
    @app_commands.choices(
        send_to=[
            app_commands.Choice(name="このチャンネル", value="here"),
            app_commands.Choice(name="DM", value="dm"),
        ]
    )
    @app_commands.autocomplete(event=_panel_autocomplete)
    async def event_panel(
        self, interaction: discord.Interaction, event: str, send_to: app_commands.Choice[str]
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        gd = storage.get(guild.id)
        ev = _get_event(gd, event)
        if ev is None or ev["status"] == "deleted":
            return await reply_error(interaction, "イベントが見つかりません。")
        if not perms.is_host_or_admin(interaction.user, gd, ev):  # type: ignore[arg-type]
            return await reply_error(interaction, "このイベントの主催者か管理者のみ使えます。")
        if send_to.value == "dm":
            try:
                await actions.send_panel(guild, ev, interaction.user)
            except discord.HTTPException:
                return await reply_error(interaction, "DM を送れませんでした。DM の受信設定を確認してください。")
            await interaction.response.send_message("DM に管理パネルを送りました。", ephemeral=True)
        else:
            await interaction.response.send_message("管理パネルを表示します。", ephemeral=True)
            await actions.send_panel(guild, ev, interaction.channel)  # type: ignore[arg-type]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventCommands(bot))
