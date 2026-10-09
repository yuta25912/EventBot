"""日時の入力・表示 (JST 固定)"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"

_FULL = re.compile(r"^\s*(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})\s*$")
_DATE_ONLY = re.compile(r"^\s*(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})\s*$")
_DATE_HOUR = re.compile(r"^\s*(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):?\d?\s*$")


def now() -> datetime:
    return datetime.now(JST)


def _resolve_year(year: str | None, month: int, day: int) -> int:
    if year:
        return int(year)
    today = now()
    y = today.year
    try:
        if datetime(y, month, day, tzinfo=JST) < today - timedelta(days=1):
            y += 1
    except ValueError:
        pass
    return y


def normalize(text: str) -> str:
    """全角を半角にし、候補の表示名に入っている曜日 `(土)` を取り除く"""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\([^)]*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse(text: str) -> datetime | None:
    """`2026-10-12 21:00` / `10/12 21:00` / `2026-10-12 (月) 21:00` などを JST の datetime にする"""
    m = _FULL.match(normalize(text))
    if not m:
        return None
    month, day = int(m[2]), int(m[3])
    try:
        return datetime(_resolve_year(m[1], month, day), month, day, int(m[4]), int(m[5]), tzinfo=JST)
    except ValueError:
        return None


def to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(JST) if s else None


def fmt(dt: datetime | None) -> str:
    if dt is None:
        return "未設定"
    dt = dt.astimezone(JST)
    return f"{dt:%Y-%m-%d} ({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def fmt_iso(s: str | None) -> str:
    return fmt(from_iso(s))


def input_value(dt: datetime | None) -> str:
    """Modal の初期値用"""
    return f"{dt.astimezone(JST):%Y-%m-%d %H:%M}" if dt else ""


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}時間{m}分"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


def autocomplete(current: str) -> list[discord.app_commands.Choice[str]]:
    """日時入力の候補 (補助用。直接入力も受け付ける)"""
    base = now()
    cands: list[datetime] = []

    current = normalize(current)
    dt = parse(current)
    if dt:
        cands = [dt]
    elif m := _DATE_ONLY.match(current):
        try:
            month, day = int(m[2]), int(m[3])
            d = datetime(_resolve_year(m[1], month, day), month, day, tzinfo=JST)
            cands = [d + timedelta(hours=10, minutes=30 * i) for i in range(25)]
        except ValueError:
            cands = []
    elif m := _DATE_HOUR.match(current):
        try:
            month, day = int(m[2]), int(m[3])
            d = datetime(_resolve_year(m[1], month, day), month, day, int(m[4]), tzinfo=JST)
            cands = [d, d + timedelta(minutes=30)]
        except ValueError:
            cands = []
    else:
        today = base.replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(8):
            for h in (20, 21, 22):
                cands.append(today + timedelta(days=i, hours=h))

    choices = [
        discord.app_commands.Choice(name=fmt(c), value=input_value(c))
        for c in cands
        if c > base
    ]
    return choices[:25]
