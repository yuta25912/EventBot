"""権限チェック"""

from __future__ import annotations

from typing import Any

import discord

from . import storage


def is_admin(member: discord.Member | None, gd: storage.GuildData) -> bool:
    """管理者ロールが設定されていればそのロール、未設定ならサーバー管理者権限"""
    if member is None:
        return False
    role_id = gd.config.get("admin_role_id")
    if role_id:
        return any(r.id == role_id for r in member.roles)
    return member.guild_permissions.administrator


def can_configure(member: discord.Member | None, gd: storage.GuildData) -> bool:
    """設定コマンド用。ロック防止のためサーバー管理者権限でも可"""
    if member is None:
        return False
    return is_admin(member, gd) or member.guild_permissions.administrator


def is_host_or_admin(member: discord.Member | None, gd: storage.GuildData, event: dict[str, Any]) -> bool:
    if member is None:
        return False
    return member.id == event.get("host_id") or is_admin(member, gd)


async def resolve_member(guild: discord.Guild, user: discord.abc.User) -> discord.Member | None:
    """DM からのインタラクションでもギルドメンバーを取得する"""
    if isinstance(user, discord.Member) and user.guild.id == guild.id:
        return user
    member = guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            return None
    return member
