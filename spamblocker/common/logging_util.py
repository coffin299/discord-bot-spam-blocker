"""モデレーションログ送信用の共通処理。"""

from __future__ import annotations

from typing import Any

import discord


async def send_mod_log(
    guild: discord.Guild,
    config: dict[str, Any],
    text: str,
    *,
    override_channel_id: Any = None,
) -> None:
    """指定チャンネルへモデレーションログを送る。未設定ならコンソールのみ。"""
    # 個別上書き → 全体の mod_log_channel_id の順で決める
    channel_id = override_channel_id or config.get("mod_log_channel_id")
    # 未設定なら標準出力だけにする
    if not channel_id:
        print(text)
        return
    try:
        # 文字列IDでも動くようにする
        cid = int(channel_id)
    except (TypeError, ValueError):
        print(text)
        return
    # ギルド内チャンネルを取得する
    channel = guild.get_channel(cid)
    if channel is None:
        print(text)
        return
    try:
        # ログ本文を送信する
        await channel.send(text)
    except discord.HTTPException as e:
        # 失敗時はコンソールへフォールバックする
        print(f"モデレーションログ送信失敗: {e}")
        print(text)
