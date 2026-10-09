"""イベントの作成・編集画面 (event_create / event_modify / DM の内容変更で共通)"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import discord

from .. import actions, embeds, storage, timeutil

log = logging.getLogger(__name__)

DoneCallback = Callable[[discord.Interaction, dict[str, Any], str | None], Awaitable[None]]


def validate(guild: discord.Guild, d: dict[str, Any]) -> str | None:
    if not (d.get("name") or "").strip():
        return "企画名を入力してください。"
    if d.get("game") == embeds.OTHER_GAME and not (d.get("game_custom") or "").strip():
        return "ゲーム名を入力してください（「その他」を選んだ場合）。"
    start = timeutil.from_iso(d.get("start"))
    if start is None:
        return "開始日時を入力してください。"
    if start <= timeutil.now():
        return "開始日時は今より後にしてください。"
    end = timeutil.from_iso(d.get("end"))
    if end is not None and end <= start:
        return "終了日時は開始日時より後にしてください。"
    if not d.get("host_id"):
        return "主催を選んでください。"
    if not isinstance(guild.get_channel(d.get("vc_id") or 0), (discord.VoiceChannel, discord.StageChannel)):
        return "VC を選んでください。"
    return None


class EventModal(discord.ui.Modal):
    """テキスト項目の入力。Modal は 5 項目までなので、必要なものだけ出す"""

    def __init__(self, draft: dict[str, Any], *, include_time: bool, on_done: DoneCallback, title: str):
        super().__init__(title=title[:45], timeout=600)
        self.draft = draft
        self.on_done = on_done

        self.name_input = discord.ui.TextInput(label="企画名", max_length=100, default=draft.get("name") or None)
        self.desc_input = discord.ui.TextInput(
            label="企画内容（省略可）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=800,
            default=draft.get("description") or None,
        )
        self.add_item(self.name_input)
        self.add_item(self.desc_input)

        self.start_input = self.end_input = None
        if include_time:
            self.start_input = discord.ui.TextInput(
                label="開始日時（例: 2026-10-12 21:00）",
                max_length=20,
                default=timeutil.input_value(timeutil.from_iso(draft.get("start"))) or None,
            )
            self.end_input = discord.ui.TextInput(
                label="終了日時（省略可）",
                max_length=20,
                required=False,
                default=timeutil.input_value(timeutil.from_iso(draft.get("end"))) or None,
            )
            self.add_item(self.start_input)
            self.add_item(self.end_input)

        self.game_input = self.version_input = None
        if draft.get("game") == embeds.OTHER_GAME:
            self.game_input = discord.ui.TextInput(
                label="ゲーム名", max_length=100, default=draft.get("game_custom") or None
            )
            self.add_item(self.game_input)
        elif embeds.needs_version(draft):
            self.version_input = discord.ui.TextInput(
                label="バージョン（例: Java 1.21.1）",
                max_length=50,
                required=False,
                default=draft.get("version") or None,
            )
            self.add_item(self.version_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        d = self.draft
        error: str | None = None
        d["name"] = self.name_input.value.strip()
        d["description"] = self.desc_input.value.strip() or None
        if self.game_input is not None:
            d["game_custom"] = self.game_input.value.strip()
        if self.version_input is not None:
            d["version"] = self.version_input.value.strip() or None
        if self.start_input is not None and self.end_input is not None:
            start = timeutil.parse(self.start_input.value)
            if start is None:
                error = "開始日時の形式が正しくありません（例: 2026-10-12 21:00）。"
            else:
                d["start"] = timeutil.to_iso(start)
            if self.end_input.value.strip():
                end = timeutil.parse(self.end_input.value)
                if end is None:
                    error = "終了日時の形式が正しくありません（例: 2026-10-12 23:00）。"
                else:
                    d["end"] = timeutil.to_iso(end)
            else:
                d["end"] = None
        await self.on_done(interaction, d, error)


class EventEditor(discord.ui.View):
    """
    mode="create": 確認画面 [編集][完了] → 編集画面 (セレクト + [内容を編集][完了])
    mode="modify": 編集画面 (セレクト + [内容を編集][変更を申請][やめる])
    """

    def __init__(
        self,
        guild: discord.Guild,
        draft: dict[str, Any],
        *,
        mode: str,
        author_id: int,
        in_guild: bool = True,
        event_id: int | None = None,
        note: str | None = None,
    ):
        super().__init__(timeout=14 * 60)
        self.guild = guild
        self.draft = draft
        self.mode = mode
        self.author_id = author_id
        self.in_guild = in_guild
        self.event_id = event_id
        self.stage = "summary" if mode == "create" else "edit"
        self.note = note
        self._build()

    # ---------- 表示

    def embed(self) -> discord.Embed:
        if self.mode == "create":
            title = "イベント作成の確認" if self.stage == "summary" else "イベント作成: 編集中"
        else:
            title = f"イベント変更 (ID: {self.event_id})"
        note = f"⚠️ {self.note}" if self.note else None
        if self.mode == "modify" and not note:
            note = "変更したら「変更を申請」を押してください。管理者の確認後に反映されます。"
        if self.mode == "modify" and not self.in_guild:
            note = (note or "") + "\n※主催の変更はサーバー内で `/host event_modify` を使ってください。"
        return embeds.draft_embed(self.draft, title, note)

    def _button(self, label: str, style: discord.ButtonStyle, cb: Callable, row: int) -> None:
        b = discord.ui.Button(label=label, style=style, row=row)
        b.callback = cb  # type: ignore[method-assign]
        self.add_item(b)

    def _build(self) -> None:
        self.clear_items()
        if self.stage == "summary":
            self._button("編集", discord.ButtonStyle.secondary, self._to_edit, 0)
            self._button("完了", discord.ButtonStyle.success, self._complete, 0)
            return

        gd = storage.get(self.guild.id)
        # ゲーム
        games = list(gd.config["games"].keys())
        cur_game = self.draft.get("game")
        if cur_game and cur_game not in games and cur_game != embeds.OTHER_GAME:
            games.insert(0, cur_game)
        games = games[:24] + [embeds.OTHER_GAME]
        game_select = discord.ui.Select(
            placeholder="ゲームを選ぶ",
            row=0,
            options=[discord.SelectOption(label=g, value=g, default=g == cur_game) for g in games],
        )
        game_select.callback = self._on_game  # type: ignore[method-assign]
        self.add_item(game_select)

        # VC
        vcs: list[discord.abc.GuildChannel] = [*self.guild.voice_channels, *self.guild.stage_channels]
        cur_vc = self.draft.get("vc_id")
        vcs.sort(key=lambda c: c.id != cur_vc)
        vc_select = discord.ui.Select(
            placeholder="VC を選ぶ",
            row=1,
            options=[
                discord.SelectOption(label=c.name[:100], value=str(c.id), default=c.id == cur_vc) for c in vcs[:25]
            ]
            or [discord.SelectOption(label="VC がありません", value="0")],
        )
        vc_select.callback = self._on_vc  # type: ignore[method-assign]
        self.add_item(vc_select)

        # 主催 (DM ではメンバー一覧が出せないのでサーバー内のみ)
        if self.in_guild:
            host_select = discord.ui.UserSelect(
                placeholder="主催を選ぶ",
                row=2,
                default_values=[discord.Object(id=self.draft["host_id"])] if self.draft.get("host_id") else [],
            )
            host_select.callback = self._on_host  # type: ignore[method-assign]
            self.add_item(host_select)

        self._button("内容を編集", discord.ButtonStyle.primary, self._open_modal, 3)
        if self.mode == "create":
            self._button("完了", discord.ButtonStyle.success, self._complete, 3)
        else:
            self._button("変更を申請", discord.ButtonStyle.success, self._submit_request, 3)
            self._button("やめる", discord.ButtonStyle.secondary, self._abort, 3)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    # ---------- コールバック

    async def _to_edit(self, interaction: discord.Interaction) -> None:
        self.stage = "edit"
        self.note = None
        await self._rerender(interaction)

    async def _on_game(self, interaction: discord.Interaction) -> None:
        value = interaction.data["values"][0]  # type: ignore[index]
        self.draft["game"] = value
        if value != embeds.OTHER_GAME:
            self.draft["game_custom"] = None
        if not embeds.needs_version(self.draft):
            self.draft["version"] = None
        self.note = None
        if value == embeds.OTHER_GAME or embeds.needs_version(self.draft):
            self.note = "「内容を編集」から" + ("ゲーム名" if value == embeds.OTHER_GAME else "バージョン") + "を入力してください。"
        await self._rerender(interaction)

    async def _on_vc(self, interaction: discord.Interaction) -> None:
        value = int(interaction.data["values"][0])  # type: ignore[index]
        if value:
            self.draft["vc_id"] = value
        self.note = None
        await self._rerender(interaction)

    async def _on_host(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.data["values"][0])  # type: ignore[index]
        member = self.guild.get_member(user_id)
        if member is not None and member.bot:
            self.note = "Bot は主催にできません。"
        else:
            self.draft["host_id"] = user_id
            self.note = None
        await self._rerender(interaction)

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        async def done(inter: discord.Interaction, _: dict[str, Any], error: str | None) -> None:
            self.note = error
            await self._rerender(inter)

        await interaction.response.send_modal(
            EventModal(self.draft, include_time=True, on_done=done, title="イベント内容の編集")
        )

    async def _complete(self, interaction: discord.Interaction) -> None:
        error = validate(self.guild, self.draft)
        if error:
            self.note = error
            return await self._rerender(interaction)
        await interaction.response.defer()
        try:
            ev = await actions.create_event(self.guild, self.draft, interaction.user.id)
        except discord.HTTPException as e:
            log.exception("イベント作成に失敗しました")
            self.note = f"Discord イベントの作成に失敗しました: {e.text or e}"
            self._build()
            await interaction.edit_original_response(embed=self.embed(), view=self)
            return
        self.stop()
        embed = embeds.draft_embed(ev, f"✅ イベントを作成しました (ID: {ev['id']})")
        url = actions.discord_event_url(self.guild.id, ev)
        if url:
            embed.add_field(name="Discord イベント", value=url, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)

    async def _submit_request(self, interaction: discord.Interaction) -> None:
        from .approval import submit_change_request

        error = validate(self.guild, self.draft)
        if error:
            self.note = error
            return await self._rerender(interaction)
        await interaction.response.defer()
        ok, message = await submit_change_request(self.guild, self.event_id or 0, self.draft, interaction.user.id)
        if not ok:
            self.note = message
            self._build()
            await interaction.edit_original_response(embed=self.embed(), view=self)
            return
        self.stop()
        await interaction.edit_original_response(content=f"✅ {message}", embed=None, view=None)

    async def _abort(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="変更をやめました。", embed=None, view=None)


def draft_from_event(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: ev.get(k) for k in embeds.EDIT_KEYS}


async def open_modify_editor(
    interaction: discord.Interaction, guild: discord.Guild, ev: dict[str, Any], *, in_guild: bool
) -> None:
    view = EventEditor(
        guild, draft_from_event(ev), mode="modify", author_id=interaction.user.id, in_guild=in_guild, event_id=ev["id"]
    )
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


def modify_block_reason(gd: storage.GuildData, ev: dict[str, Any]) -> str | None:
    if ev["status"] != "scheduled":
        return f"このイベントは「{embeds.STATUS_LABELS.get(ev['status'])}」のため変更できません。"
    if ev.get("pending_request_id"):
        return "このイベントには承認待ちの申請があります。結果が出るまで新しい申請は送れません。"
    if not gd.config.get("admin_channel_id"):
        return "管理者連絡チャンネルが設定されていません。"
    return None
