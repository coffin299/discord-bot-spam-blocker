"""ハニーポットCog（罠チャンネル監視）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

from spamblocker.common.config import ensure_data_dir, id_set, load_config
from spamblocker.common.logging_util import send_mod_log

# ストライク永続ファイル名
STRIKES_FILE = os.path.join("data", "honeypot_strikes.json")


class HoneypotCog(commands.Cog):
    """罠チャンネルへの投稿でKICK/BANし、メッセージを掃除する。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot参照を保持する
        self.bot = bot
        # 設定を読み込む
        self.config = load_config()
        # データディレクトリを用意する
        ensure_data_dir()
        # ストライク履歴を読み込む
        self.strikes: dict[str, Any] = self._load_strikes()

    def reload(self) -> None:
        """設定を再読込する。"""
        # 最新のYAMLを反映する
        self.config = load_config()

    def _hp(self) -> dict[str, Any]:
        """ハニーポット設定節を返す。"""
        # 未定義なら空辞書
        return self.config.get("honeypot") or {}

    def _enabled(self) -> bool:
        """ハニーポット有効フラグ（トップレベル優先）。"""
        # token直下のスイッチを優先する
        if "honeypot_enabled" in self.config:
            return bool(self.config.get("honeypot_enabled"))
        # 後方互換でネストも見る
        return bool(self._hp().get("enabled", False))

    def _purge_enabled(self) -> bool:
        """メッセージ掃除フラグ（トップレベル優先）。"""
        # token直下のスイッチを優先する
        if "honeypot_purge_user_messages" in self.config:
            return bool(self.config.get("honeypot_purge_user_messages"))
        # ネストの既定はTrue
        return bool(self._hp().get("purge_user_messages", True))

    def _load_strikes(self) -> dict[str, Any]:
        """ストライクJSONを読み込む。"""
        # ファイルが無ければ空
        if not os.path.exists(STRIKES_FILE):
            return {}
        try:
            with open(STRIKES_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            # 壊れていれば初期化する
            return {}

    def _save_strikes(self) -> None:
        """ストライクJSONを保存する。"""
        # ディレクトリを確保する
        ensure_data_dir()
        # 原子的に近い形で書き出す
        with open(STRIKES_FILE, "w", encoding="utf-8") as f:
            json.dump(self.strikes, f, ensure_ascii=False, indent=2)

    def _exempt_ids(self) -> set[str]:
        """処罰除外ID集合を返す。"""
        # 管理者・WLユーザー・許可BOT・ハニーポット用WL・自BOTを除外する
        ids = set()
        ids |= id_set(self.config.get("admin_ids"))
        ids |= id_set(self.config.get("whitelisted_users"))
        ids |= id_set(self.config.get("allowed_bots"))
        ids |= id_set(self._hp().get("whitelist_ids"))
        if self.bot.user:
            ids.add(str(self.bot.user.id))
        return ids

    def _strike_key(self, guild_id: int, user_id: int) -> str:
        """ギルド×ユーザーのキーを作る。"""
        # 文字列キーで永続化する
        return f"{guild_id}:{user_id}"

    def _increment_strike(self, guild_id: int, user_id: int) -> int:
        """作動回数を増やし、最新回数を返す。"""
        # キーを取得する
        key = self._strike_key(guild_id, user_id)
        # 既存レコードを取る
        record = self.strikes.get(key) or {"count": 0, "updated_at": None}
        # 日数リセットが設定されていれば確認する
        reset_days = self._hp().get("strike_reset_days")
        if reset_days and record.get("updated_at"):
            try:
                # 最終更新からの経過日を見る
                last = datetime.fromisoformat(record["updated_at"])
                now = datetime.now(timezone.utc)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                # 期限超えならカウンタをリセットする
                if (now - last).days >= int(reset_days):
                    record["count"] = 0
            except (ValueError, TypeError):
                # 日付パース失敗時はそのまま進める
                pass
        # 回数を1増やす
        record["count"] = int(record.get("count", 0)) + 1
        # 更新時刻を記録する
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        # 保存する
        self.strikes[key] = record
        self._save_strikes()
        # 最新回数を返す
        return int(record["count"])

    async def _log(self, guild: discord.Guild, text: str) -> None:
        """ログチャンネルへ通知する。"""
        # 個別 → 全体 mod_log の順
        await send_mod_log(
            guild,
            self.config,
            text,
            override_channel_id=self._hp().get("log_channel_id"),
        )

    async def _purge_user_messages(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> int:
        """対象ユーザーのメッセージを可能な範囲で削除する。"""
        # 削除件数カウンタ
        deleted = 0
        # テキスト系チャンネルを走査する
        for channel in guild.text_channels:
            # 閲覧・管理権限が無ければスキップする
            me = guild.me
            if me is None:
                continue
            perms = channel.permissions_for(me)
            if not (perms.manage_messages and perms.read_message_history):
                continue
            try:
                # 直近メッセージから当該ユーザー分を一括削除する
                purged = await channel.purge(
                    limit=200,
                    check=lambda m, uid=user_id: m.author.id == uid,
                    bulk=True,
                )
                deleted += len(purged)
            except discord.HTTPException:
                # 権限やレート制限は次チャンネルへ
                continue
        # 削除件数を返す
        return deleted

    async def _punish(
        self,
        member: discord.Member,
        count: int,
    ) -> str:
        """設定に従いKICKまたはBANし、実施した処置名を返す。"""
        # 理由文を用意する
        reason = str(self._hp().get("reason") or "ハニーポット作動")
        action = str(self._hp().get("action") or "kick").lower()
        # 即BANモード
        if action == "ban":
            await member.ban(reason=reason, delete_message_seconds=0)
            return "BAN"
        # KICKモード: 許容回数を超えたらBAN
        max_kick = int(self._hp().get("max_kick_before_ban", 3))
        if count > max_kick:
            ban_reason = f"{reason} (再加入超過: {count}回)"
            await member.ban(reason=ban_reason, delete_message_seconds=0)
            return "BAN"
        # 許容内はKICK
        await member.kick(reason=reason)
        return "KICK"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """罠チャンネルへの投稿を即処罰する。"""
        # 機能無効なら何もしない
        if not self._enabled():
            return
        # ギルド外は無視する
        if not message.guild or not isinstance(message.author, discord.Member):
            return
        # 自BOTは無視する
        if message.author == self.bot.user:
            return
        # 対象チャンネルでなければ無視する
        channel_ids = id_set(self._hp().get("channel_ids"))
        if not channel_ids or str(message.channel.id) not in channel_ids:
            return
        # 除外対象は無視する
        if str(message.author.id) in self._exempt_ids():
            return

        # トリガーメッセージを先に消す
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        # ストライクを加算する
        count = self._increment_strike(message.guild.id, message.author.id)
        # 処罰する
        try:
            action_done = await self._punish(message.author, count)
        except discord.HTTPException as e:
            await self._log(
                message.guild,
                f"🍯 ハニーポット処罰失敗: {message.author} ({message.author.id}) — {e}",
            )
            return

        # メッセージ掃除
        purged = 0
        if self._purge_enabled():
            purged = await self._purge_user_messages(
                message.guild,
                message.author.id,
            )

        # ログする
        await self._log(
            message.guild,
            (
                f"🍯 ハニーポット作動: {message.author} (`{message.author.id}`) "
                f"→ **{action_done}** (累計{count}回) "
                f"削除メッセージ約{purged}件 / ch=<#{message.channel.id}>"
            ),
        )


async def setup(bot: commands.Bot) -> None:
    """Cogを登録する。"""
    # ハニーポットCogを追加する
    await bot.add_cog(HoneypotCog(bot))
