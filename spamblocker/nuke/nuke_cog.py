"""アンチニューク＋緊急ロックダウンCog。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Any

import discord
from discord.ext import commands

from spamblocker.common.config import ensure_data_dir, id_set, load_config

# ロックダウン状態の永続ファイル
LOCKDOWN_FILE = os.path.join("data", "lockdown_state.json")


class NukeCog(commands.Cog):
    """短時間の破壊行為を検知し、隔離・ロックダウンする。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot参照を保持する
        self.bot = bot
        # 設定を読む
        self.config = load_config()
        # データディレクトリを用意する
        ensure_data_dir()
        # guild_id -> action -> deque[timestamps]
        self.events: dict[int, dict[str, deque[float]]] = defaultdict(
            lambda: defaultdict(deque)
        )
        # 実行中ロック（同一ギルドの多重ロックダウン防止）
        self._lockdown_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # 永続ロックダウン状態
        self.lockdown_state: dict[str, Any] = self._load_lockdown_state()

    def reload(self) -> None:
        """設定を再読込する。"""
        # YAMLを再読込する
        self.config = load_config()

    def _nuke(self) -> dict[str, Any]:
        """nuke設定節を返す。"""
        # 未定義なら空
        return self.config.get("nuke") or {}

    def _flag(self, flat_key: str, nested_key: str, default: bool) -> bool:
        """トップレベルスイッチを優先してboolを返す。"""
        # token直下にあればそれを使う
        if flat_key in self.config:
            return bool(self.config.get(flat_key))
        # なければネストから取る
        return bool(self._nuke().get(nested_key, default))

    def _is_admin(self, user_id: int) -> bool:
        """configのadmin_idsに含まれるか判定する。"""
        # 文字列比較で判定する
        return str(user_id) in id_set(self.config.get("admin_ids"))

    def _load_lockdown_state(self) -> dict[str, Any]:
        """ロックダウン状態JSONを読む。"""
        # 無ければ空
        if not os.path.exists(LOCKDOWN_FILE):
            return {}
        try:
            with open(LOCKDOWN_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_lockdown_state(self) -> None:
        """ロックダウン状態を保存する。"""
        # ディレクトリ確保
        ensure_data_dir()
        # 書き出す
        with open(LOCKDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(self.lockdown_state, f, ensure_ascii=False, indent=2)

    def _window(self) -> float:
        """検知ウィンドウ秒数を返す。"""
        # 設定値、既定10秒
        return float(self._nuke().get("window_seconds", 10))

    def _record(self, guild_id: int, action: str) -> int:
        """イベントを記録し、ウィンドウ内件数を返す。"""
        # 現在時刻
        now = time.time()
        # 対象deque
        q = self.events[guild_id][action]
        # 追加する
        q.append(now)
        # 古いものを捨てる
        cutoff = now - self._window()
        while q and q[0] < cutoff:
            q.popleft()
        # 件数を返す
        return len(q)

    def _threshold(self, key: str, default: int) -> int:
        """閾値を取得する。"""
        # int化して返す
        return int(self._nuke().get(key, default))

    async def _log(self, guild: discord.Guild, text: str) -> None:
        """ログチャンネルまたはコンソールへ出す。"""
        # チャンネルIDを取る
        channel_id = self._nuke().get("log_channel_id")
        if not channel_id:
            print(text)
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            print(text)
            return
        try:
            await channel.send(text)
        except discord.HTTPException as e:
            print(f"ニュークログ失敗: {e}")
            print(text)

    async def _dm_admins(self, guild: discord.Guild, content: str) -> None:
        """admin_idsへDMする。失敗はログへフォールバック。"""
        # DM通知が無効ならやめる
        if not self._flag("nuke_notify_admins_dm", "notify_admins_dm", True):
            return
        # 各管理者へ送る
        for admin_id in id_set(self.config.get("admin_ids")):
            try:
                # ユーザーを取得する
                user = self.bot.get_user(int(admin_id)) or await self.bot.fetch_user(
                    int(admin_id)
                )
                # DMを送る
                await user.send(content)
            except (discord.HTTPException, ValueError) as e:
                # 失敗をログする
                await self._log(
                    guild,
                    f"管理者DM失敗 (`{admin_id}`): {e}\n内容: {content}",
                )

    async def _isolate(self, member: discord.Member, reason: str) -> None:
        """実行者をタイムアウトまたはロール剥奪する。"""
        # 自BOT・adminは除外
        if member.id == self.bot.user.id or self._is_admin(member.id):
            return
        # オーナーは触れない
        if member.guild.owner_id == member.id:
            return
        action = str(self._nuke().get("action", "timeout")).lower()
        try:
            if action == "strip_roles":
                # 管理系ロールを外す（everyone以外）
                removable = [
                    r
                    for r in member.roles
                    if r != member.guild.default_role and r < member.guild.me.top_role
                ]
                if removable:
                    await member.remove_roles(*removable, reason=reason)
            else:
                # タイムアウトする
                minutes = int(self._nuke().get("timeout_minutes", 60))
                until = discord.utils.utcnow() + timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
        except discord.HTTPException as e:
            await self._log(member.guild, f"隔離失敗: {member} — {e}")

    def _is_lockdown(self, guild_id: int) -> bool:
        """ギルドがロックダウン中か。"""
        # 状態辞書を見る
        return str(guild_id) in self.lockdown_state

    async def apply_lockdown(self, guild: discord.Guild, reason: str) -> bool:
        """緊急ロックダウンを適用する。成功ならTrue。"""
        # 機能スイッチ
        if not self._flag("nuke_lockdown_enabled", "lockdown_enabled", False):
            await self._log(guild, "ロックダウンは無効です (nuke_lockdown_enabled: false)")
            return False
        # 二重適用を防ぐ
        async with self._lockdown_locks[guild.id]:
            if self._is_lockdown(guild.id):
                await self._log(guild, "既にロックダウン中です")
                return False
            # @everyone の送信・リアクションを拒否する
            everyone = guild.default_role
            # 変更前を保存する
            before = {
                "send_messages": everyone.permissions.send_messages,
                "add_reactions": everyone.permissions.add_reactions,
                "create_instant_invite": everyone.permissions.create_instant_invite,
            }
            try:
                # 権限を上書きする
                perms = everyone.permissions
                perms.update(
                    send_messages=False,
                    add_reactions=False,
                    create_instant_invite=False,
                )
                await everyone.edit(permissions=perms, reason=reason)
            except discord.HTTPException as e:
                await self._log(guild, f"ロックダウン失敗: {e}")
                return False
            # 状態を永続化する
            self.lockdown_state[str(guild.id)] = {
                "reason": reason,
                "everyone_perms": before,
                "started_at": time.time(),
            }
            self._save_lockdown_state()
            # ログとDM
            msg = (
                f"🔒 緊急ロックダウンを開始しました\n"
                f"サーバー: **{guild.name}** (`{guild.id}`)\n"
                f"理由: {reason}\n"
                f"解除: `!!!unlock_lockdown`"
            )
            await self._log(guild, msg)
            await self._dm_admins(guild, msg)
            return True

    async def release_lockdown(self, guild: discord.Guild) -> bool:
        """ロックダウンを解除し、権限を復元する。"""
        # 機能スイッチ（解除は有効時のみ、ただし状態が残っていれば復元は許可）
        state = self.lockdown_state.get(str(guild.id))
        if not state:
            return False
        everyone = guild.default_role
        saved = state.get("everyone_perms") or {}
        try:
            # 保存済み権限を戻す
            perms = everyone.permissions
            perms.update(
                send_messages=bool(saved.get("send_messages", True)),
                add_reactions=bool(saved.get("add_reactions", True)),
                create_instant_invite=bool(saved.get("create_instant_invite", True)),
            )
            await everyone.edit(permissions=perms, reason="ロックダウン解除")
        except discord.HTTPException as e:
            await self._log(guild, f"ロックダウン解除失敗: {e}")
            return False
        # 状態を消す
        self.lockdown_state.pop(str(guild.id), None)
        self._save_lockdown_state()
        await self._log(guild, "🔓 ロックダウンを解除しました")
        return True

    async def _handle_threshold(
        self,
        guild: discord.Guild,
        actor: discord.Member | None,
        action: str,
        count: int,
        limit: int,
    ) -> None:
        """閾値超過時の隔離と自動ロックダウン。"""
        # 機能無効なら何もしない
        if not self._flag("nuke_enabled", "enabled", True):
            return
        # 閾値以下なら何もしない
        if count < limit:
            return
        # ログする
        await self._log(
            guild,
            f"🚨 ニュー検知: `{action}` が {count}/{limit} "
            f"(window={self._window()}s) actor={actor}",
        )
        # 実行者がいれば隔離する
        if actor is not None:
            await self._isolate(actor, f"アンチニューク: {action}")
        # 自動ロックダウン
        if self._flag("nuke_lockdown_enabled", "lockdown_enabled", False) and self._flag(
            "nuke_auto_lockdown",
            "auto_lockdown",
            True,
        ):
            await self.apply_lockdown(guild, f"自動: {action} 閾値超過")

    async def _resolve_actor(
        self,
        guild: discord.Guild,
        target_user: discord.abc.User | None = None,
    ) -> discord.Member | None:
        """Audit Logから直近の実行者を推定する（簡易）。"""
        # 権限が無ければNone
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            # 直近の監査ログを少し読む
            async for entry in guild.audit_logs(limit=5):
                # 実行者がMemberなら返す
                if isinstance(entry.user, discord.Member):
                    # admin自身は処罰対象にしない呼び出し側で弾く
                    return entry.user
        except discord.HTTPException:
            return None
        return None

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """大量BANを検知する。"""
        # 件数を記録する
        count = self._record(guild.id, "ban")
        # 実行者を推定する
        actor = await self._resolve_actor(guild, user)
        # 閾値処理
        await self._handle_threshold(
            guild,
            actor,
            "ban",
            count,
            self._threshold("max_bans", 3),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """キック疑いを検知する（退出全般なので緩めに扱う）。"""
        # 自BOT退出は無視
        if member.id == self.bot.user.id:
            return
        # 監査ログで直近キックがあるときだけカウントする
        me = member.guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return
        try:
            async for entry in member.guild.audit_logs(
                limit=1,
                action=discord.AuditLogAction.kick,
            ):
                # 対象が一致し、数秒以内ならキック扱い
                if entry.target and entry.target.id == member.id:
                    if (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                        count = self._record(member.guild.id, "kick")
                        actor = entry.user if isinstance(entry.user, discord.Member) else None
                        await self._handle_threshold(
                            member.guild,
                            actor,
                            "kick",
                            count,
                            self._threshold("max_kicks", 3),
                        )
                    break
        except discord.HTTPException:
            return

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """チャンネル大量削除を検知する。"""
        guild = channel.guild
        count = self._record(guild.id, "channel_delete")
        actor = await self._resolve_actor(guild)
        await self._handle_threshold(
            guild,
            actor,
            "channel_delete",
            count,
            self._threshold("max_channel_deletes", 3),
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """チャンネル大量作成を検知する。"""
        guild = channel.guild
        count = self._record(guild.id, "channel_create")
        actor = await self._resolve_actor(guild)
        await self._handle_threshold(
            guild,
            actor,
            "channel_create",
            count,
            self._threshold("max_channel_creates", 5),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """ロール大量削除を検知する。"""
        guild = role.guild
        count = self._record(guild.id, "role_delete")
        actor = await self._resolve_actor(guild)
        await self._handle_threshold(
            guild,
            actor,
            "role_delete",
            count,
            self._threshold("max_role_deletes", 3),
        )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        """Webhook更新（乱造の近似）を検知する。"""
        guild = channel.guild
        count = self._record(guild.id, "webhook")
        actor = await self._resolve_actor(guild)
        await self._handle_threshold(
            guild,
            actor,
            "webhook",
            count,
            self._threshold("max_webhooks", 3),
        )

    @commands.command(name="lockdown")
    async def lockdown_cmd(self, ctx: commands.Context) -> None:
        """手動で緊急ロックダウンする（admin_idsのみ）。"""
        # ギルド必須
        if not ctx.guild:
            return
        # admin_idsのみ
        if not self._is_admin(ctx.author.id):
            await ctx.send("❌ このコマンドは config の admin_ids のみ実行できます")
            return
        # 機能無効
        if not self._flag("nuke_lockdown_enabled", "lockdown_enabled", False):
            await ctx.send("❌ nuke_lockdown_enabled が false のため使用できません")
            return
        # 適用する
        ok = await self.apply_lockdown(
            ctx.guild,
            f"手動 ({ctx.author})",
        )
        if ok:
            await ctx.send("🔒 ロックダウンを開始しました")

    @commands.command(name="unlock_lockdown")
    async def unlock_lockdown_cmd(self, ctx: commands.Context) -> None:
        """ロックダウンを解除する（admin_idsのみ）。"""
        # ギルド必須
        if not ctx.guild:
            return
        # admin_idsのみ
        if not self._is_admin(ctx.author.id):
            await ctx.send("❌ このコマンドは config の admin_ids のみ実行できます")
            return
        # 解除する
        ok = await self.release_lockdown(ctx.guild)
        if ok:
            await ctx.send("🔓 ロックダウンを解除しました")
        else:
            await ctx.send("⚠️ ロックダウン中ではありません")

    @commands.command(name="nuke_status")
    async def nuke_status_cmd(self, ctx: commands.Context) -> None:
        """ニューク／ロックダウン状態を表示する。"""
        # ギルド必須
        if not ctx.guild:
            return
        # admin_idsのみ
        if not self._is_admin(ctx.author.id):
            await ctx.send("❌ このコマンドは config の admin_ids のみ実行できます")
            return
        # 状態を組み立てる
        locked = self._is_lockdown(ctx.guild.id)
        recent = {
            k: len(v) for k, v in self.events.get(ctx.guild.id, {}).items()
        }
        embed = discord.Embed(
            title="Nuke Status",
            color=discord.Color.red() if locked else discord.Color.green(),
        )
        embed.add_field(
            name="lockdown",
            value=str(locked),
            inline=True,
        )
        embed.add_field(
            name="lockdown_enabled",
            value=str(self._flag("nuke_lockdown_enabled", "lockdown_enabled", False)),
            inline=True,
        )
        embed.add_field(
            name="recent_counts",
            value=str(recent) if recent else "なし",
            inline=False,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Cogを登録する。"""
    # ニュークCogを追加する
    await bot.add_cog(NukeCog(bot))
