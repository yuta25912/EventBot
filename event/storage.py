"""ギルドごとのデータ保存 (data/data_{guild_id}.json)"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DEFAULT_DATA: dict[str, Any] = {
    "config": {
        "admin_role_id": None,
        "admin_channel_id": None,
        # イベント予定: 告知 Embed を日時順に並べるチャンネル
        "schedule_channel_id": None,
        # イベント: 開始・終了のお知らせを送るチャンネル
        "event_channel_id": None,
        # ゲーム名 -> ロールID (None ならロールなし)
        "games": {"Minecraft": None},
        "test_server": False,
    },
    "next_event_id": 1,
    "next_request_id": 1,
    # 内部イベントID(str) -> イベント
    "events": {},
    # 申請ID(str) -> 申請
    "requests": {},
    # イベントチャンネルの告知メッセージ (上から順)
    # [{"message_id": int, "event_id": int | None, "hash": str}]
    "announcements": [],
}


class GuildData:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.path = DATA_DIR / f"data_{guild_id}.json"
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        data = copy.deepcopy(DEFAULT_DATA)
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                loaded = json.load(f)
            config = loaded.pop("config", {})
            # 旧形式: event_channel_id に告知 Embed のチャンネルが入っていた
            if "schedule_channel_id" not in config and config.get("event_channel_id"):
                config["schedule_channel_id"] = config.pop("event_channel_id")
            # 新しく増えたキーを補完する
            data["config"].update(config)
            data.update(loaded)
        return data

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 便利アクセサ ----
    @property
    def config(self) -> dict[str, Any]:
        return self.data["config"]

    @property
    def events(self) -> dict[str, dict[str, Any]]:
        return self.data["events"]

    @property
    def requests(self) -> dict[str, dict[str, Any]]:
        return self.data["requests"]

    def get_event(self, event_id: int | str) -> dict[str, Any] | None:
        return self.events.get(str(event_id))

    def new_event_id(self) -> int:
        eid = self.data["next_event_id"]
        self.data["next_event_id"] = eid + 1
        return eid

    def new_request_id(self) -> int:
        rid = self.data["next_request_id"]
        self.data["next_request_id"] = rid + 1
        return rid


_cache: dict[int, GuildData] = {}


def get(guild_id: int) -> GuildData:
    gd = _cache.get(guild_id)
    if gd is None:
        gd = GuildData(guild_id)
        _cache[guild_id] = gd
    return gd
