"""スパムブロック用Cog。"""

from __future__ import annotations

import re

import discord
from discord.ext import commands

from spamblocker.common.config import id_set, load_config, save_config
from spamblocker.common.logging_util import send_mod_log

# 疑わしいリンク照合時にスキップする公式系ホスト
SAFE_LINK_HOSTS = (
    "discord.com",
    "discord.gg",
    "discord.gift",
    "discordapp.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "steamcommunity.com",
    "store.steampowered.com",
)


class SpamBlockerCog(commands.Cog):
    """未許可BOT制御とメッセージスパム検知を行う。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot本体を保持する
        self.bot = bot
        # 起動時に設定を読み込む
        self.config = load_config()
        # 許可BOTと監視対象をログに出す
        print(f'許可されたBOT: {self.config.get("allowed_bots", [])}')
        print(f'監視対象サーバー: {self.config.get("monitored_guilds", [])}')
        print(f'監視対象チャンネル: {self.config.get("monitored_channels", [])}')

    def reload(self) -> None:
        """設定を再読込する。"""
        # ディスク上の最新configを反映する
        self.config = load_config()

    def _is_monitored(self, message: discord.Message) -> bool:
        """監視対象のサーバー・チャンネルか判定する。"""
        # DMは対象外
        if not message.guild:
            return False
        # 監視ギルドが指定されていればその中だけ見る
        monitored_guilds = id_set(self.config.get("monitored_guilds"))
        if monitored_guilds and str(message.guild.id) not in monitored_guilds:
            return False
        # 監視チャンネルが指定されていればその中だけ見る
        monitored_channels = id_set(self.config.get("monitored_channels"))
        if monitored_channels and str(message.channel.id) not in monitored_channels:
            return False
        # 条件を満たせば監視対象
        return True

    def _is_exempt_user(self, author: discord.abc.User) -> bool:
        """スパム判定から除外するユーザーか判定する。"""
        # 自BOTは除外する
        if author.id == self.bot.user.id:
            return True
        # 管理者IDは除外する
        if str(author.id) in id_set(self.config.get("admin_ids")):
            return True
        # ホワイトリストユーザーは除外する
        if str(author.id) in id_set(self.config.get("whitelisted_users")):
            return True
        # MemberならDiscord管理者権限も除外する
        if isinstance(author, discord.Member) and author.guild_permissions.administrator:
            return True
        # それ以外は対象
        return False

    def _allowed_bots_empty(self) -> bool:
        """許可BOTリストが空（＝全BOT許可）か判定する。"""
        # 空または未設定なら全許可モード
        return not self.config.get("allowed_bots")

    def _is_allowed_bot(self, author: discord.abc.User) -> bool:
        """許可されたBOTか判定する。"""
        # 人間はここでは扱わない
        if not author.bot:
            return False
        # リスト空なら全BOT許可
        if self._allowed_bots_empty():
            return True
        # リスト内なら許可
        return str(author.id) in id_set(self.config.get("allowed_bots"))

    def _collect_text(self, message: discord.Message) -> str:
        """本文とembedから検査用テキストを組み立てる。"""
        # 本文をベースにする
        parts = [message.content or ""]
        # embedのタイトル・説明・URLも検査対象にする
        for embed in message.embeds:
            if embed.title:
                parts.append(embed.title)
            if embed.description:
                parts.append(embed.description)
            if embed.url:
                parts.append(embed.url)
            for field in embed.fields:
                parts.append(f"{field.name} {field.value}")
        # 小文字化して結合する
        return "\n".join(parts).lower()

    def _mask_safe_hosts(self, text: str) -> str:
        """許可ホストをマスクし、誤検知を減らす。"""
        # 作業用コピーを作る
        masked = text
        # 各許可ホストをダミーに置換する
        for host in SAFE_LINK_HOSTS:
            # 大文字小文字を無視して置換する
            masked = re.sub(re.escape(host), "safehost", masked, flags=re.IGNORECASE)
        # マスク後テキストを返す
        return masked

    def _match_patterns(self, text: str, patterns: list[str]) -> str | None:
        """正規表現リストに一致したらパターン文字列を返す。"""
        # 各パターンを順に試す
        for pattern in patterns:
            try:
                # 部分一致で検知する
                if re.search(pattern, text, flags=re.IGNORECASE):
                    return pattern
            except re.error:
                # 不正な正規表現はスキップする
                continue
        # 一致なし
        return None

    def _count_emojis(self, content: str) -> int:
        """カスタム絵文字とUnicode絵文字のおおよその数を数える。"""
        # Discordカスタム絵文字 <:name:id>
        custom = len(re.findall(r"<a?:\w+:\d+>", content))
        # ざっくりしたUnicode絵文字範囲
        unicode_emoji = len(
            re.findall(
                r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]",
                content,
            )
        )
        # 合計を返す
        return custom + unicode_emoji

    def _caps_ratio(self, content: str) -> float:
        """英字に占める大文字比率を返す。"""
        # 英字だけ抽出する
        letters = [c for c in content if c.isalpha() and c.isascii()]
        # 英字がなければ0
        if not letters:
            return 0.0
        # 大文字数 / 英字数
        upper = sum(1 for c in letters if c.isupper())
        return upper / len(letters)

    def check_spam_content(self, message: discord.Message) -> str | None:
        """スパム内容なら理由文字列、でなければNoneを返す。"""
        # フィルター全体が無効なら何もしない
        if not self.config.get("enable_spam_filter", True):
            return None
        # 検査テキストを用意する
        text = self._collect_text(message)
        content = message.content or ""

        # Discord招待リンク
        if self.config.get("block_discord_invites", True):
            patterns = self.config.get("discord_invite_patterns") or []
            hit = self._match_patterns(text, patterns)
            if hit:
                return "Discord招待リンク"

        # 疑わしいリンク（公式ホストはマスク）
        if self.config.get("block_suspicious_links", True):
            masked = self._mask_safe_hosts(text)
            patterns = self.config.get("blocked_link_patterns") or []
            hit = self._match_patterns(masked, patterns)
            if hit:
                return "疑わしいリンク"

        # NGワード
        words = self.config.get("custom_blocked_words") or []
        for word in words:
            # 空文字は無視する
            if not word:
                continue
            # 小文字同士で部分一致する
            if str(word).lower() in text:
                return f"NGワード: {word}"

        # 過度な絵文字
        if self.config.get("block_excessive_emojis", True):
            max_emoji = int(self.config.get("max_emoji_count", 10))
            if self._count_emojis(content) > max_emoji:
                return "過度な絵文字"

        # 過度な大文字
        if self.config.get("block_excessive_caps", True):
            threshold = float(self.config.get("caps_ratio_threshold", 0.7))
            # 短い文は誤検知しやすいのでスキップする
            if len(content) >= 8 and self._caps_ratio(content) >= threshold:
                return "過度な大文字"

        # マスメンション
        if self.config.get("block_mass_mentions", True):
            if message.mention_everyone:
                return "マスメンション"

        # 過度なメンション
        if self.config.get("block_excessive_mentions", True):
            max_mentions = int(self.config.get("max_mention_count", 5))
            mention_count = len(message.mentions) + len(message.role_mentions)
            if mention_count > max_mentions:
                return "過度なメンション"

        # 埋め込みブロックはBOT側分岐で扱う
        return None

    async def _delete_with_warning(
        self,
        message: discord.Message,
        reason: str,
    ) -> None:
        """メッセージを削除し、必要なら警告を出す。"""
        try:
            # 対象メッセージを削除する
            await message.delete()
        except discord.HTTPException as e:
            # 権限不足等はログに残す
            print(f"削除失敗: {e}")
            return
        # コンソールとモデレーションログへ記録する
        log_text = (
            f"🗑️ 削除: {reason} — {message.author} "
            f"(`{message.author.id}`) ch=<#{message.channel.id}>"
        )
        print(log_text)
        if message.guild:
            await send_mod_log(message.guild, self.config, log_text)
        # 警告が無効ならここで終了する
        if not self.config.get("send_warning", False):
            return
        try:
            # 短い警告を送り、数秒後に消す
            warning = await message.channel.send(
                f"⚠️ {reason}: `{message.author}` の投稿を削除しました"
            )
            await warning.delete(delay=5)
        except discord.HTTPException:
            # 警告送信失敗は無視する
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """投稿を監視し、スパムなら削除する。"""
        # 自BOTは無視する
        if message.author == self.bot.user:
            return
        # 監視範囲外は無視する
        if not self._is_monitored(message):
            return
        # 除外ユーザーは無視する
        if self._is_exempt_user(message.author):
            return

        # BOT投稿の扱い
        if message.author.bot:
            # 許可BOTはスキップする
            if self._is_allowed_bot(message.author):
                return
            # リスト空＝全許可なのでBOT制限しない
            if self._allowed_bots_empty():
                # 人間向けスパム検知のみ適用する可能性はあるが、
                # BOTは許可扱いのためここで終了する
                return
            # 未許可BOTを全削除する設定
            if self.config.get("block_all_unauthorized_bots", True):
                await self._delete_with_warning(message, "許可されていないBOT")
                return
            # 埋め込みのみブロック
            if message.embeds and self.config.get("block_embeds", False):
                await self._delete_with_warning(message, "未許可BOTの埋め込み")
                return
            # 内容ベースの検知
            reason = self.check_spam_content(message)
            if reason:
                await self._delete_with_warning(message, reason)
            return

        # 一般ユーザーのスパム検知
        reason = self.check_spam_content(message)
        if reason:
            await self._delete_with_warning(message, reason)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """未許可BOTの参加をKICKする。"""
        # BOT以外は対象外
        if not member.bot:
            return
        # 機能が無効なら何もしない
        if not self.config.get("kick_unauthorized_bots_on_join", True):
            return
        # 全BOT許可モードならKICKしない
        if self._allowed_bots_empty():
            return
        # 許可リスト内なら何もしない
        if self._is_allowed_bot(member):
            return
        # 自BOTはKICKしない
        if member.id == self.bot.user.id:
            return
        try:
            # 未許可BOTをKICKする
            await member.kick(reason="未許可BOTの参加を拒否")
            log_text = f"👢 未許可BOTをKICK: {member} (`{member.id}`)"
            print(log_text)
            await send_mod_log(member.guild, self.config, log_text)
        except discord.HTTPException as e:
            # ロール階層不足等をログする
            fail = f"未許可BOT KICK失敗: {e}"
            print(fail)
            await send_mod_log(member.guild, self.config, fail)

    def _persist(self) -> None:
        """現在の設定を保存する。"""
        # YAMLへ書き戻す
        save_config(self.config)

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def reload_config(self, ctx: commands.Context) -> None:
        """設定ファイルを再読み込みする。"""
        try:
            # 全Cogへも反映したいのでイベント的に再読込する
            self.reload()
            # 他Cogがあればreloadメソッドを呼ぶ
            for cog in self.bot.cogs.values():
                if cog is self:
                    continue
                if hasattr(cog, "reload"):
                    cog.reload()
            await ctx.send("✅ 設定ファイルを再読み込みしました")
        except Exception as e:
            await ctx.send(f"❌ エラー: {e}")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def add_guild(self, ctx: commands.Context, guild_id: str) -> None:
        """監視対象サーバーを追加する。"""
        # リストを取得する
        monitored = list(self.config.get("monitored_guilds") or [])
        # 未登録なら追加する
        if guild_id not in monitored:
            monitored.append(guild_id)
            self.config["monitored_guilds"] = monitored
            self._persist()
            await ctx.send(f"✅ サーバー ID `{guild_id}` を監視対象に追加しました")
        else:
            await ctx.send(f"⚠️ サーバー ID `{guild_id}` は既に監視対象です")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def list_bots(self, ctx: commands.Context) -> None:
        """許可BOT一覧を表示する。"""
        # 空なら全許可である旨を出す
        bots = self.config.get("allowed_bots") or []
        if not bots:
            desc = "（空＝すべてのBOTを許可）"
        else:
            desc = "\n".join(f"- <@{bot_id}>" for bot_id in bots)
        embed = discord.Embed(
            title="許可されたBOT一覧",
            description=desc,
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def add_bot(self, ctx: commands.Context, bot_id: str) -> None:
        """許可BOTを追加する。"""
        # リストを用意する
        bots = list(self.config.get("allowed_bots") or [])
        if bot_id not in bots:
            bots.append(bot_id)
            self.config["allowed_bots"] = bots
            self._persist()
            await ctx.send(f"✅ BOT ID `{bot_id}` を許可リストに追加しました")
        else:
            await ctx.send(f"⚠️ BOT ID `{bot_id}` は既に許可リストに含まれています")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def remove_bot(self, ctx: commands.Context, bot_id: str) -> None:
        """許可BOTを削除する。"""
        bots = list(self.config.get("allowed_bots") or [])
        if bot_id in bots:
            bots.remove(bot_id)
            self.config["allowed_bots"] = bots
            self._persist()
            await ctx.send(f"✅ BOT ID `{bot_id}` を許可リストから削除しました")
        else:
            await ctx.send(f"⚠️ BOT ID `{bot_id}` は許可リストに含まれていません")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def add_channel(self, ctx: commands.Context, channel_id: str) -> None:
        """監視チャンネルを追加する。"""
        channels = list(self.config.get("monitored_channels") or [])
        if channel_id not in channels:
            channels.append(channel_id)
            self.config["monitored_channels"] = channels
            self._persist()
            await ctx.send(f"✅ チャンネル ID `{channel_id}` を監視対象に追加しました")
        else:
            await ctx.send(f"⚠️ チャンネル ID `{channel_id}` は既に監視対象です")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def remove_channel(self, ctx: commands.Context, channel_id: str) -> None:
        """監視チャンネルを削除する。"""
        channels = list(self.config.get("monitored_channels") or [])
        if channel_id in channels:
            channels.remove(channel_id)
            self.config["monitored_channels"] = channels
            self._persist()
            await ctx.send(f"✅ チャンネル ID `{channel_id}` を監視対象から削除しました")
        else:
            await ctx.send(f"⚠️ チャンネル ID `{channel_id}` は監視対象にありません")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def list_channels(self, ctx: commands.Context) -> None:
        """監視チャンネル一覧を表示する。"""
        channels = self.config.get("monitored_channels") or []
        if not channels:
            desc = "（空＝全チャンネル監視）"
        else:
            desc = "\n".join(f"- <#{cid}> (`{cid}`)" for cid in channels)
        embed = discord.Embed(
            title="監視チャンネル一覧",
            description=desc,
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def add_keyword(self, ctx: commands.Context, *, keyword: str) -> None:
        """NGワードを追加する。"""
        words = list(self.config.get("custom_blocked_words") or [])
        if keyword not in words:
            words.append(keyword)
            self.config["custom_blocked_words"] = words
            self._persist()
            await ctx.send(f"✅ キーワード `{keyword}` をNGリストに追加しました")
        else:
            await ctx.send(f"⚠️ キーワード `{keyword}` は既に登録されています")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def remove_keyword(self, ctx: commands.Context, *, keyword: str) -> None:
        """NGワードを削除する。"""
        words = list(self.config.get("custom_blocked_words") or [])
        if keyword in words:
            words.remove(keyword)
            self.config["custom_blocked_words"] = words
            self._persist()
            await ctx.send(f"✅ キーワード `{keyword}` をNGリストから削除しました")
        else:
            await ctx.send(f"⚠️ キーワード `{keyword}` は登録されていません")

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def list_keywords(self, ctx: commands.Context) -> None:
        """NGワード一覧を表示する。"""
        words = self.config.get("custom_blocked_words") or []
        if words:
            desc = "\n".join(f"- {w}" for w in words)
        else:
            desc = "登録されているキーワードはありません"
        embed = discord.Embed(
            title="NGワード一覧",
            description=desc,
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

@commands.hybrid_command()
@commands.has_permissions(administrator=True)
@discord.app_commands.default_permissions(administrator=True)
async def status(self, ctx: commands.Context) -> None:
        """主要設定の状態を表示する。"""
        # トップレベルスイッチを表示する
        lines = [
            f"spam_filter: `{self.config.get('enable_spam_filter', True)}`",
            f"block_unauthorized_bots: `{self.config.get('block_all_unauthorized_bots', True)}`",
            f"allowed_bots: `{len(self.config.get('allowed_bots') or [])}` 件"
            f"（空＝全許可）",
            f"honeypot: `{self.config.get('honeypot_enabled', False)}`",
            f"nuke: `{self.config.get('nuke_enabled', True)}`",
            f"lockdown_enabled: `{self.config.get('nuke_lockdown_enabled', False)}`",
        ]
        embed = discord.Embed(
            title="Spam Blocker 状態",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """CogをBotへ登録する。"""
    # スパムCogを追加する
    await bot.add_cog(SpamBlockerCog(bot))
