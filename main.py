"""Discord Spam / Nuke Blocker エントリポイント。"""

from __future__ import annotations

import asyncio
import os
import shutil

import discord
import yaml
from discord.ext import commands


async def mobile_identify(self) -> None:
    """Botをモバイルとして識別させるためのカスタムIdentify。"""
    # Identifyペイロードを組み立てる
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "token": self.token,
            "properties": {
                "$os": "Discord Android",
                "$browser": "Discord Android",
                "$device": "Discord Android",
            },
            "compress": True,
            "large_threshold": 250,
            "intents": self._connection.intents.value,
        },
    }
    # シャード情報があれば付与する
    if self.shard_id is not None and self.shard_count is not None:
        payload["d"]["shard"] = [self.shard_id, self.shard_count]
    # プレゼンスがあれば付与する
    state = self._connection
    if state._activity is not None or state._status is not None:
        payload["d"]["presence"] = {
            "status": state._status,
            "game": state._activity,
            "since": 0,
            "afk": False,
        }
    # before_identifyフックを呼ぶ
    await self.call_hooks("before_identify", self.shard_id, initial=self._initial_identify)
    # JSONとして送る
    await self.send_as_json(payload)


def load_config() -> dict:
    """config.yamlを読み込む（無ければdefaultからコピー）。"""
    # config.yamlが無ければテンプレから作る
    if not os.path.exists("config.yaml"):
        if os.path.exists("config.default.yaml"):
            shutil.copy("config.default.yaml", "config.yaml")
            print("✅ config.default.yaml から config.yaml を作成しました")
            print("⚠️  config.yaml を編集して bot_token などを設定してください")
        else:
            print("❌ エラー: config.default.yaml が見つかりません")
            raise SystemExit(1)
    # YAMLを読む
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def setup_error_handler(bot: commands.Bot) -> None:
    """グローバルコマンドエラーハンドラを設定する。"""

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        # 未知コマンドは無視する
        if isinstance(error, commands.CommandNotFound):
            return
        # 引数不足
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ 必要な引数が不足しています: {error.param.name}")
            return
        # 権限不足
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ このコマンドを実行する権限がありません")
            return
        # その他
        await ctx.send(f"❌ エラーが発生しました: {error}")
        print(f"Error: {error}")


# Gateway Intents（メッセージ・メンバー・モデレーション）
intents = discord.Intents.default()
# メッセージ本文を読む
intents.message_content = True
# 参加・キック検知用
intents.members = True
# ギルドイベント用
intents.guilds = True
# モデレーション関連（BAN等）※対応バージョンのみ
if hasattr(discord.Intents, "moderation"):
    intents.moderation = True

# プレフィックス付きBotを作る
bot = commands.Bot(command_prefix="!!!", intents=intents)


@bot.event
async def on_ready() -> None:
    """起動完了時のログとプレゼンス設定。"""
    # 起動バナーを出す
    print("=" * 50)
    print(f"🤖 {bot.user} でログインしました")
    print("📱 モバイルステータスで表示されています")
    print(f"🌐 {len(bot.guilds)} サーバーに接続中")
    print("=" * 50)
    # ステータスを設定する
    await bot.change_presence(
        activity=discord.Game(name="spam/nuke blocker"),
    )


# エラーハンドラをセットする
setup_error_handler(bot)


async def load_extensions() -> None:
    """スパム／ハニーポット／ニュークCogを読み込む。"""
    # 読み込む拡張一覧
    extensions = [
        "spamblocker.spam.spam_cog",
        "spamblocker.honeypot.honeypot_cog",
        "spamblocker.nuke.nuke_cog",
    ]
    # 順にロードする
    for ext in extensions:
        try:
            await bot.load_extension(ext)
            print(f"✅ {ext} を読み込みました")
        except Exception as e:
            print(f"❌ {ext} の読み込みに失敗しました: {e}")


if __name__ == "__main__":
    # 設定を読む
    config = load_config()
    # トークンを取る
    token = config.get("bot_token")
    # 未設定なら終了する
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("❌ エラー: config.yaml に bot_token が設定されていません")
        raise SystemExit(1)
    # モバイルIdentifyを有効化する
    discord.gateway.DiscordWebSocket.identify = mobile_identify

    async def main() -> None:
        # Cogを載せてから起動する
        async with bot:
            await load_extensions()
            await bot.start(token)

    # イベントループを回す
    asyncio.run(main())
