import logging
from datetime import timedelta
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import utcnow

from database import Database


logger = logging.getLogger("invite_tracker")
logger.setLevel(logging.INFO)


async def send_invite_error(
    interaction: discord.Interaction,
    message: str = "招待監視処理中にエラーが発生しました。時間をおいて再度お試しください。"
) -> None:
    """招待関連のエラーを安全に返信するヘルパー"""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as notify_err:
        logger.error(f"招待エラー通知に失敗: {notify_err}", exc_info=True)


class InviteWatchModal(discord.ui.Modal):
    """指定ユーザーIDを入力するモーダル"""

    def __init__(self, cog: "InviteTracker", channel_id: int):
        super().__init__(title="招待監視ユーザーを入力")
        self.cog = cog
        self.channel_id = channel_id
        self.user_id_input = discord.ui.TextInput(
            label="ユーザーID",
            placeholder="例: 123456789012345678",
            min_length=5,
            max_length=20
        )
        self.add_item(self.user_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            try:
                target_id = int(self.user_id_input.value.strip())
            except ValueError:
                await interaction.response.send_message("IDは数字のみで入力してください。", ephemeral=True)
                return

            if not interaction.guild:
                await interaction.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)
                return

            member = interaction.guild.get_member(target_id)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(target_id)
                except discord.NotFound:
                    await interaction.response.send_message("指定したユーザーが見つかりません。", ephemeral=True)
                    return
                except discord.HTTPException as e:
                    logger.error(f"ユーザー取得エラー: {e}", exc_info=True)
                    await interaction.response.send_message("ユーザー情報の取得に失敗しました。", ephemeral=True)
                    return

            await self.cog.register_invite_watch(interaction.guild, member, self.channel_id)
            await interaction.response.send_message(
                f"{member.mention} の招待をこのチャンネルで監視します。",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"InviteWatchModal.on_submit エラー: {e}", exc_info=True)
            await send_invite_error(interaction)


class InviteTracker(commands.Cog):
    """招待リンクを監視してカウントする機能"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        self.invite_cache: Dict[int, Dict[str, int]] = {}
        self.watch_targets: Dict[int, Dict[int, int]] = {}
        self.bot.loop.create_task(self._initialize_state())

    @staticmethod
    def _screen_name(user: discord.abc.User) -> str:
        if hasattr(user, "global_name") and user.global_name:
            return user.global_name  # type: ignore[attr-defined]
        if hasattr(user, "display_name") and user.display_name:
            return user.display_name  # type: ignore[attr-defined]
        return user.name

    async def _initialize_state(self):
        await self.bot.wait_until_ready()
        try:
            for watcher in self.db.get_all_invite_watchers():
                guild_map = self.watch_targets.setdefault(watcher['guild_id'], {})
                guild_map[watcher['inviter_id']] = watcher['channel_id']

            for guild in self.bot.guilds:
                await self._sync_guild_invites(guild)
            logger.info("招待監視情報を初期化しました")
        except Exception as e:
            logger.error(f"招待監視初期化エラー: {e}", exc_info=True)

    async def _sync_guild_invites(self, guild: discord.Guild):
        """現在の招待状況をキャッシュ"""
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            logger.warning(f"[{guild.name}] 招待の取得権限がありません")
            return
        except discord.HTTPException as e:
            logger.error(f"[{guild.name}] 招待の取得に失敗: {e}")
            return

        self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

    async def register_invite_watch(self, guild: discord.Guild, inviter: discord.Member, channel_id: int):
        """DBとメモリに監視設定を保存"""
        try:
            self.db.upsert_invite_watcher(guild.id, inviter.id, channel_id)
            guild_map = self.watch_targets.setdefault(guild.id, {})
            guild_map[inviter.id] = channel_id

            channel = guild.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                embed = discord.Embed(
                    title="招待監視を開始しました",
                    description=(
                        f"{inviter.mention} が発行した招待でメンバーが参加すると\n"
                        f"このチャンネルに詳細を通知します。"
                    ),
                    color=0x5865F2
                )
                embed.add_field(
                    name="招待者情報",
                    value=(
                        f"ユーザー名: **{inviter.name}**\n"
                        f"スクリーンID: **{inviter.global_name or inviter.display_name}**\n"
                        f"ID: `{inviter.id}`"
                    ),
                    inline=False
                )
                embed.set_footer(text="アカウント作成から30日未満の参加者には警告を表示します")
                await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"register_invite_watch エラー: {e}", exc_info=True)

    @app_commands.command(name="invitewatch", description="指定ユーザーの招待を監視し、累計カウントを記録します")
    @app_commands.default_permissions(manage_guild=True)
    async def invitewatch(self, interaction: discord.Interaction):
        try:
            if not interaction.guild or not interaction.channel:
                await interaction.response.send_message("サーバーのテキストチャンネルで実行してください。", ephemeral=True)
                return
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
                return
            await interaction.response.send_modal(InviteWatchModal(self, interaction.channel.id))
        except Exception as e:
            logger.error(f"invitewatch コマンドエラー: {e}", exc_info=True)
            await send_invite_error(interaction)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._sync_guild_invites(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        if guild is None:
            return
        guild_cache = self.invite_cache.setdefault(guild.id, {})
        guild_cache[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild = invite.guild
        if guild is None:
            return
        if guild.id in self.invite_cache:
            self.invite_cache[guild.id].pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        try:
            if guild.id not in self.invite_cache:
                await self._sync_guild_invites(guild)

            before = self.invite_cache.get(guild.id, {}).copy()

            try:
                invites = await guild.invites()
            except discord.Forbidden:
                logger.debug(f"[{guild.name}] 招待取得権限がないため監視をスキップ")
                return
            except discord.HTTPException as e:
                logger.error(f"[{guild.name}] 招待取得エラー: {e}", exc_info=True)
                return

            self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

            inviter: Optional[discord.Member] = None
            used_invite: Optional[discord.Invite] = None

            for invite in invites:
                previous_uses = before.get(invite.code, 0)
                current_uses = invite.uses or 0
                if current_uses > previous_uses:
                    inviter = invite.inviter
                    used_invite = invite
                    break

            if inviter is None:
                return

            await self._handle_tracked_invite(member, inviter, used_invite)
        except Exception as e:
            logger.error(f"[{guild.name}] on_member_join 招待監視エラー: {e}", exc_info=True)

    async def _handle_tracked_invite(self, joined_member: discord.Member, inviter: discord.Member, invite: Optional[discord.Invite]):
        try:
            guild = joined_member.guild
            inviter_member = guild.get_member(inviter.id) or inviter
            target_id = inviter_member.id
            channel_id = self.watch_targets.get(guild.id, {}).get(target_id)
            if not channel_id:
                return

            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                return

            total_count = self.db.increment_invite_count(guild.id, target_id)
            account_age = utcnow() - joined_member.created_at

            inviter_url = f"https://discord.com/users/{target_id}"
            member_url = f"https://discord.com/users/{joined_member.id}"

            embed = discord.Embed(
                title="📥 新規参加を検知しました",
                color=0x2B2D31,
                timestamp=utcnow()
            )
            embed.set_thumbnail(url=joined_member.display_avatar.url)
            embed.add_field(
                name="参加ユーザー",
                value=(
                    f"[{joined_member.name}]({member_url})\n"
                    f"スクリーンID: **{self._screen_name(joined_member)}**\n"
                    f"ID: `{joined_member.id}`"
                ),
                inline=False
            )
            embed.add_field(
                name="招待者",
                value=(
                    f"[{inviter_member.name}]({inviter_url})\n"
                    f"スクリーンID: **{self._screen_name(inviter_member)}**\n"
                    f"ID: `{inviter_member.id}`"
                ),
                inline=False
            )

            if invite and invite.url:
                embed.add_field(name="使用された招待リンク", value=f"[{invite.code}]({invite.url})", inline=False)

            embed.add_field(name="累計招待数", value=f"{total_count} 人", inline=True)

            if account_age < timedelta(days=30):
                embed.add_field(
                    name="⚠ アカウント警告",
                    value="このユーザーはアカウント作成から30日未満です。",
                    inline=False
                )

            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[{joined_member.guild.name}] _handle_tracked_invite エラー: {e}", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracker(bot))

