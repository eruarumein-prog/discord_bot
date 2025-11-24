import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, List
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
            view = RoleSelectView(self, target_channel, interaction.guild)
            embed = discord.Embed(
                title="🎭 ロール管理操作盤 セットアップ",
                description="**ステップ 1/2: 管理するロールを選択**\n\n操作盤に表示するロールを選択してください。",
                color=0x5865F2
            )
            
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
    """ロール選択View"""
    
    def __init__(self, cog: RoleManager, target_channel: discord.TextChannel, guild: discord.Guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.target_channel = target_channel
        self.guild = guild
        
        # RoleSelectを追加
        self.role_select = discord.ui.RoleSelect(
            placeholder="管理するロールを選択",
            min_values=1,
            max_values=25
        )
        self.role_select.callback = self.on_role_select
        self.add_item(self.role_select)
    
    async def on_role_select(self, interaction: discord.Interaction):
        """ロール選択時の処理"""
        try:
            selected_roles = self.role_select.values
            
            # タイトルと内容を入力するモーダルを表示
            modal = RolePanelTextModal(self.cog, self.target_channel, [role.id for role in selected_roles], self.guild)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"ロール選択エラー: {e}")
            logger.error(traceback.format_exc())
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    content="エラーが発生しました",
                    embed=None,
                    view=None
                )


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

