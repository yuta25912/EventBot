"""エラーを、コンソールではなく操作した人に表示する"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

log = logging.getLogger(__name__)

PERM_LABELS = {
    "view_channel": "チャンネルを見る",
    "send_messages": "メッセージを送信",
    "embed_links": "埋め込みリンク",
    "read_message_history": "メッセージ履歴を読む",
    "mention_everyone": "すべてのロールにメンション",
    "manage_roles": "ロールの管理",
    "manage_events": "イベントの管理",
    "create_events": "イベントの作成",
    "connect": "接続",
}

# 告知・管理者チャンネルなど、Bot がメッセージを送るチャンネルに必要な権限
SEND_PERMS = ("view_channel", "send_messages", "embed_links", "read_message_history")


def missing_perms(channel: discord.abc.GuildChannel, *names: str) -> list[str]:
    """channel で Bot に足りない権限の表示名を返す"""
    perms = channel.permissions_for(channel.guild.me)
    return [PERM_LABELS.get(n, n) for n in (names or SEND_PERMS) if not getattr(perms, n)]


def missing_text(channel: discord.abc.GuildChannel, missing: list[str]) -> str:
    return f"Bot に {channel.mention} での権限が足りません: **{'、'.join(missing)}**"


def _message_for(error: BaseException) -> str:
    if isinstance(error, discord.Forbidden):
        return (
            "Bot の権限が足りないため実行できませんでした。"
            "チャンネルやロールの権限設定を確認してください。\n"
            f"（Discord からの応答: {error.text or error}）"
        )
    if isinstance(error, discord.NotFound):
        return "対象のメッセージやチャンネルが見つかりませんでした（削除された可能性があります）。"
    return "エラーが発生しました。時間をおいてもう一度試すか、管理者に連絡してください。"


async def report(interaction: discord.Interaction, error: BaseException) -> None:
    if isinstance(error, (app_commands.CommandInvokeError, app_commands.TransformerError)):
        error = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    if isinstance(error, app_commands.CheckFailure):
        text = "このコマンドを使う権限がありません。"
    else:
        text = _message_for(error)

    if isinstance(error, discord.Forbidden):
        # 権限不足は想定内なので 1 行だけ残す
        log.warning("権限不足: %s (%s)", error.text, interaction.command or interaction.data)
    else:
        log.error("インタラクション処理中のエラー", exc_info=error)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ {text}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {text}", ephemeral=True)
    except discord.HTTPException:
        pass


def install(tree: app_commands.CommandTree) -> None:
    """コマンド・ボタン・Modal すべてのエラーを report() に流す"""

    @tree.error
    async def _on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await report(interaction, error)

    async def _view_on_error(self, interaction, error, item) -> None:  # type: ignore[no-untyped-def]
        await report(interaction, error)

    async def _modal_on_error(self, interaction, error) -> None:  # type: ignore[no-untyped-def]
        await report(interaction, error)

    discord.ui.View.on_error = _view_on_error  # type: ignore[method-assign]
    discord.ui.Modal.on_error = _modal_on_error  # type: ignore[method-assign]


def guard(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """永続ボタン (DynamicItem) の callback 用。

    DynamicItem のエラーは View.on_error を通らずログに出るだけなので、ここで受け止めて表示する。
    """

    @functools.wraps(func)
    async def wrapper(self: Any, interaction: discord.Interaction) -> None:
        try:
            await func(self, interaction)
        except Exception as e:
            await report(interaction, e)

    return wrapper
