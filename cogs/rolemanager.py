import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, List
import math
import logging
import traceback
import sys
import os

# 親ディレクトリのdatabase.pyをインポート
# 親ディレクトリのdatabase.pyをインポート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import Database

# ロガー設定
logger = logging.getLogger('rolemanager')
logger.setLevel(logging.INFO)

def summarize_role_mentions(guild: discord.Guild, role_ids: List[int], limit: int = 5) -> str:
    names = []
    for role_id in role_ids or []:
        role = guild.get_role(role_id)
        if role:
            names.append(role.mention)
    if not names:
        return "未選択"
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" 他{len(names) - limit}件"
    return ", ".join(names)

class RoleManager(commands.Cog):
    """ロール管理機能"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        # ギルドごとのロール設定を保存
        # {guild_id: {'channel_id': int, 'role_ids': [int, ...]}}
        self.role_panels = {}
        # 起動時にデータベースから復元
        bot.loop.create_task(self.restore_from_database())
    
    @app_commands.command(name="rolepanel", description="ロール管理操作盤を設定")
    @app_commands.describe(channel="操作盤を表示するチャンネル（省略可）")
    async def rolepanel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """ロール管理操作盤を設定するコマンド"""
        try:
            target_channel = channel or interaction.channel
            
            # ロール選択Viewを表示
            view = RoleSelectView(self, target_channel, interaction.guild, interaction.user)
            embed = view.build_embed()
            
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"ロール管理操作盤設定エラー: {e}")
            logger.error(traceback.format_exc())
            if not interaction.response.is_done():
                await interaction.response.send_message("エラーが発生しました", ephemeral=True)
            else:
                await interaction.followup.send("エラーが発生しました", ephemeral=True)
    
    async def create_role_panel(self, channel: discord.TextChannel, role_ids: List[int], guild: discord.Guild, title: str = "🎭 ロール管理", description: str = "ボタンを押してロールを取得/削除できます。"):
        """ロール管理操作盤を作成"""
        try:
            # 有効なロールのみをフィルタリング
            valid_roles = []
            for role_id in role_ids:
                role = guild.get_role(role_id)
                if role:
                    valid_roles.append(role)
            
            if not valid_roles:
                return False
            
            # 操作盤のViewを作成
            view = RolePanelView(self, valid_roles)
            
            # 操作盤のEmbedを作成
            embed = discord.Embed(
                title=title,
                description=description,
                color=0x5865F2
            )
            
            # ロール一覧を追加
            role_list = "\n".join([f"• {role.mention}" for role in valid_roles])
            embed.add_field(name="管理ロール一覧", value=role_list or "なし", inline=False)
            
            # 操作盤を送信
            message = await channel.send(embed=embed, view=view)
            
            # 設定を保存
            guild_id = guild.id
            if guild_id not in self.role_panels:
                self.role_panels[guild_id] = {}
            
            self.role_panels[guild_id][message.id] = {
                'channel_id': channel.id,
                'role_ids': [role.id for role in valid_roles]
            }
            
            # データベースに保存
            self.db.save_role_panel(
                message.id,
                guild_id,
                channel.id,
                [role.id for role in valid_roles],
                title,
                description
            )
            
            logger.info(f"ロール管理操作盤を作成しました (Guild: {guild.id}, Channel: {channel.id}, Message: {message.id})")
            return True
        except Exception as e:
            logger.error(f"ロール管理操作盤作成エラー: {e}")
            logger.error(traceback.format_exc())
            return False
    
    async def toggle_role(self, user: discord.Member, role: discord.Role, interaction: discord.Interaction):
        """ロールを付与/削除"""
        try:
            # 既にacknowledgeされている場合はスキップ
            if interaction.response.is_done():
                return
            
            # 先にdeferして、他のコールバックとの競合を防ぐ
            try:
                await interaction.response.defer(ephemeral=True)
            except (discord.errors.InteractionResponded, discord.errors.HTTPException) as e:
                # 既にacknowledgeされている場合はスキップ
                if "already been acknowledged" in str(e) or "40060" in str(e):
                    return
                raise
            
            if role in user.roles:
                # ロールを削除
                await user.remove_roles(role, reason="ロール管理操作盤から削除")
                await interaction.followup.send(
                    f"✅ {role.mention} を削除しました",
                    ephemeral=True
                )
                logger.info(f"ロールを削除しました (User: {user.id}, Role: {role.id})")
            else:
                # ロールを付与
                await user.add_roles(role, reason="ロール管理操作盤から付与")
                await interaction.followup.send(
                    f"✅ {role.mention} を取得しました",
                    ephemeral=True
                )
                logger.info(f"ロールを付与しました (User: {user.id}, Role: {role.id})")
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ ロールを操作する権限がありません",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ ロールを操作する権限がありません",
                    ephemeral=True
                )
        except Exception as e:
            logger.error(f"ロール操作エラー: {e}")
            logger.error(traceback.format_exc())
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("エラーが発生しました", ephemeral=True)
                except:
                    pass
            else:
                try:
                    await interaction.followup.send("エラーが発生しました", ephemeral=True)
                except:
                    pass
    
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """再起動後もViewが動作するようにViewを再構築"""
        if interaction.type != discord.InteractionType.component:
            return
        
        # カスタムIDからViewの種類を判定
        if not interaction.data or 'custom_id' not in interaction.data:
            return
        
        # 既にacknowledgeされている場合はスキップ
        if interaction.response.is_done():
            return
        
        custom_id = interaction.data['custom_id']
        
        # ロール管理ボタンの場合
        if custom_id.startswith('rolepanel_'):
            role_id_str = custom_id.replace('rolepanel_', '')
            try:
                role_id = int(role_id_str)
                role = interaction.guild.get_role(role_id)
                if not role:
                    await interaction.response.send_message("ロールが見つかりません", ephemeral=True)
                    return
                
                if not isinstance(interaction.user, discord.Member):
                    await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます", ephemeral=True)
                    return
                
                # ロールを操作
                await self.toggle_role(interaction.user, role, interaction)
            except ValueError:
                pass
            except Exception as e:
                logger.error(f"ロール管理ボタンエラー: {e}")
                logger.error(traceback.format_exc())
                if not interaction.response.is_done():
                    await interaction.response.send_message("エラーが発生しました", ephemeral=True)
    
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """メッセージが削除されたときの処理"""
        # ロール管理操作盤のメッセージが削除された場合
        for guild_id, panels in self.role_panels.items():
            if payload.message_id in panels:
                del panels[payload.message_id]
                self.db.delete_role_panel(payload.message_id)
                logger.info(f"ロール管理操作盤が削除されました (Message: {payload.message_id})")
                break
    
    async def restore_from_database(self):
        """データベースからロール管理操作盤を復元"""
        await self.bot.wait_until_ready()
        
        try:
            panels = self.db.get_role_panels()
            restored_count = 0
            
            for message_id, data in panels.items():
                try:
                    guild_id = data['guild_id']
                    channel_id = data['channel_id']
                    role_ids = data['role_ids']
                    title = data['title']
                    description = data['description']
                    
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        # ギルドが見つからない場合はデータベースから削除
                        self.db.delete_role_panel(message_id)
                        continue
                    
                    channel = guild.get_channel(channel_id)
                    if not channel or not isinstance(channel, discord.TextChannel):
                        # チャンネルが見つからない場合はデータベースから削除
                        self.db.delete_role_panel(message_id)
                        continue
                    
                    # 有効なロールのみをフィルタリング
                    valid_roles = []
                    for role_id in role_ids:
                        role = guild.get_role(role_id)
                        if role:
                            valid_roles.append(role)
                    
                    if not valid_roles:
                        # 有効なロールがない場合はデータベースから削除
                        self.db.delete_role_panel(message_id)
                        continue
                    
                    # メッセージが存在するか確認
                    try:
                        message = await channel.fetch_message(message_id)
                        # メッセージが存在する場合はViewを再構築
                        view = RolePanelView(self, valid_roles)
                        await message.edit(embed=message.embeds[0] if message.embeds else None, view=view)
                        
                        # role_panelsに追加
                        if guild_id not in self.role_panels:
                            self.role_panels[guild_id] = {}
                        self.role_panels[guild_id][message_id] = {
                            'channel_id': channel_id,
                            'role_ids': role_ids
                        }
                        
                        restored_count += 1
                        logger.info(f"ロール管理操作盤を復元しました (Guild: {guild_id}, Channel: {channel_id}, Message: {message_id})")
                    except discord.NotFound:
                        # メッセージが削除されている場合は再作成
                        view = RolePanelView(self, valid_roles)
                        embed = discord.Embed(
                            title=title,
                            description=description,
                            color=0x5865F2
                        )
                        role_list = "\n".join([f"• {role.mention}" for role in valid_roles])
                        embed.add_field(name="管理ロール一覧", value=role_list or "なし", inline=False)
                        
                        new_message = await channel.send(embed=embed, view=view)
                        
                        # データベースを更新
                        self.db.delete_role_panel(message_id)
                        self.db.save_role_panel(
                            new_message.id,
                            guild_id,
                            channel_id,
                            role_ids,
                            title,
                            description
                        )
                        
                        # role_panelsに追加
                        if guild_id not in self.role_panels:
                            self.role_panels[guild_id] = {}
                        self.role_panels[guild_id][new_message.id] = {
                            'channel_id': channel_id,
                            'role_ids': role_ids
                        }
                        
                        restored_count += 1
                        logger.info(f"ロール管理操作盤を再作成しました (Guild: {guild_id}, Channel: {channel_id}, Message: {new_message.id})")
                except Exception as e:
                    logger.error(f"ロール管理操作盤復元エラー (Message: {message_id}): {e}")
                    logger.error(traceback.format_exc())
            
            logger.info(f"✅ ロール管理操作盤の復元完了: {restored_count}件")
        except Exception as e:
            logger.error(f"ロール管理操作盤復元エラー: {e}")
            logger.error(traceback.format_exc())


class RoleSelectView(discord.ui.View):
    """管理するロールを段階的に選択するビュー"""
    chunk_size = 25

    def __init__(self, cog: RoleManager, target_channel: discord.TextChannel, guild: discord.Guild, requester: discord.abc.User):
        super().__init__(timeout=300)
        self.cog = cog
        self.target_channel = target_channel
        self.guild = guild
        self.requester_id = requester.id
        self.available_roles = self._filter_roles(guild)
        self.selected_role_ids: List[int] = []
        self.current_page = 0
        self.role_select: Optional[discord.ui.Select] = None
        self.total_pages = max(1, math.ceil(len(self.available_roles) / self.chunk_size)) if self.available_roles else 1

        self._build_role_dropdown()
        self._build_controls()

    def _filter_roles(self, guild: discord.Guild) -> List[discord.Role]:
        me = guild.me
        filtered = []
        for role in guild.roles:
            if role == guild.default_role:
                continue
            if me and role >= me.top_role:
                continue
            filtered.append(role)
        return filtered

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    def build_embed(self) -> discord.Embed:
        description = (
            "**ステップ 1/2: 管理するロールを選択**\n\n"
            "ロールは複数追加できます。ページを切り替えながら必要なロールをすべて選択してください。"
        )
        embed = discord.Embed(title="🎭 ロール管理操作盤 セットアップ", description=description, color=0x5865F2)
        summary = summarize_role_mentions(self.guild, self.selected_role_ids, limit=8)
        embed.add_field(name="現在の選択", value=summary, inline=False)
        if self.available_roles:
            embed.set_footer(text=f"ページ {self.current_page + 1}/{self.total_pages}")
        else:
            embed.set_footer(text="選択できるロールがありません。")
        return embed

    def _get_current_chunk(self) -> List[discord.Role]:
        if not self.available_roles:
            return []
        start = self.current_page * self.chunk_size
        end = start + self.chunk_size
        return self.available_roles[start:end]

    def _build_role_dropdown(self):
        if self.role_select:
            self.remove_item(self.role_select)
            self.role_select = None

        chunk = self._get_current_chunk()
        if not chunk:
            return

        options = [
            discord.SelectOption(label=role.name[:95], value=str(role.id))
            for role in chunk
        ]
        placeholder = f"管理するロールを選択 ({self.current_page + 1}/{self.total_pages})"
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            min_values=0,
            max_values=len(options),
            row=0
        )
        select.callback = self._on_select
        self.role_select = select
        self.add_item(select)

    def _build_controls(self):
        self.prev_button = discord.ui.Button(label="前の25件", style=discord.ButtonStyle.secondary, disabled=self.total_pages <= 1, row=1)
        self.prev_button.callback = self._go_prev
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(label="次の25件", style=discord.ButtonStyle.secondary, disabled=self.total_pages <= 1, row=1)
        self.next_button.callback = self._go_next
        self.add_item(self.next_button)

        self.confirm_button = discord.ui.Button(label="選択を確定", style=discord.ButtonStyle.success, row=2)
        self.confirm_button.callback = self._confirm_selection
        self.add_item(self.confirm_button)

        clear_button = discord.ui.Button(label="選択をクリア", style=discord.ButtonStyle.danger, row=2)
        clear_button.callback = self._clear_selection
        self.add_item(clear_button)

        cancel_button = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.secondary, row=3)
        cancel_button.callback = self._cancel
        self.add_item(cancel_button)

        if not self.available_roles:
            self.confirm_button.disabled = True
            self.prev_button.disabled = True
            self.next_button.disabled = True

    async def _update_message(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_select(self, interaction: discord.Interaction):
        updated = False
        for value in getattr(self.role_select, 'values', []):
            role_id = int(value)
            if role_id not in self.selected_role_ids:
                self.selected_role_ids.append(role_id)
                updated = True
        if updated:
            await self._update_message(interaction)
        else:
            await interaction.response.defer()

    async def _go_prev(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page - 1) % self.total_pages
        self._build_role_dropdown()
        await self._update_message(interaction)

    async def _go_next(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page + 1) % self.total_pages
        self._build_role_dropdown()
        await self._update_message(interaction)

    async def _clear_selection(self, interaction: discord.Interaction):
        self.selected_role_ids.clear()
        await self._update_message(interaction)

    async def _confirm_selection(self, interaction: discord.Interaction):
        if not self.selected_role_ids:
            await interaction.response.send_message("少なくとも1つのロールを選択してください。", ephemeral=True)
            return
        modal = RolePanelTextModal(self.cog, self.target_channel, list(self.selected_role_ids), self.guild)
        await interaction.response.send_modal(modal)

    async def _cancel(self, interaction: discord.Interaction):
        embed = discord.Embed(title="セットアップをキャンセルしました", color=0xED4245)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

class RolePanelTextModal(discord.ui.Modal):
    """ロール管理操作盤のタイトルと内容入力モーダル"""
    
    def __init__(self, cog: RoleManager, target_channel: discord.TextChannel, role_ids: List[int], guild: discord.Guild):
        super().__init__(title="操作盤の文言を設定")
        self.cog = cog
        self.target_channel = target_channel
        self.role_ids = role_ids
        self.guild = guild
    
    title_input = discord.ui.TextInput(
        label="タイトル",
        placeholder="操作盤のタイトルを入力してください",
        default="🎭 ロール管理",
        required=True,
        max_length=256
    )
    
    description_input = discord.ui.TextInput(
        label="内容",
        placeholder="操作盤の説明文を入力してください",
        default="ボタンを押してロールを取得/削除できます。",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理"""
        try:
            title = self.title_input.value
            description = self.description_input.value
            
            # 操作盤を作成
            success = await self.cog.create_role_panel(
                self.target_channel,
                self.role_ids,
                self.guild,
                title,
                description
            )
            
            if success:
                embed = discord.Embed(
                    title="✅ セットアップ完了",
                    description=f"ロール管理操作盤を {self.target_channel.mention} に表示しました。",
                    color=0x57F287
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(
                    "❌ 操作盤の作成に失敗しました",
                    ephemeral=True
                )
        except Exception as e:
            logger.error(f"操作盤作成エラー: {e}")
            logger.error(traceback.format_exc())
            if not interaction.response.is_done():
                await interaction.response.send_message("エラーが発生しました", ephemeral=True)
            else:
                await interaction.followup.send("エラーが発生しました", ephemeral=True)


class RolePanelView(discord.ui.View):
    """ロール管理操作盤View"""
    
    def __init__(self, cog: RoleManager, roles: List[discord.Role]):
        super().__init__(timeout=None)
        self.cog = cog
        self.roles = roles
        
        # 各ロールに対してボタンを作成
        for role in roles:
            button = discord.ui.Button(
                label=role.name,
                style=discord.ButtonStyle.primary,
                custom_id=f"rolepanel_{role.id}"
            )
            button.callback = self.create_role_callback(role)
            self.add_item(button)
    
    def create_role_callback(self, role: discord.Role):
        """ロール操作コールバックを作成"""
        async def callback(interaction: discord.Interaction):
            # 既にacknowledgeされている場合はスキップ（on_interactionで処理済みの可能性）
            if interaction.response.is_done():
                return
            
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます", ephemeral=True)
                return
            
            await self.cog.toggle_role(interaction.user, role, interaction)
        
        return callback


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleManager(bot))

