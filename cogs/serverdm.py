import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from database import Database

# ロガー設定
logger = logging.getLogger('serverdm')
logger.setLevel(logging.INFO)


async def send_dm_error(interaction: discord.Interaction, message: str = "エラーが発生しました。もう一度お試しください。"):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as notify_err:
        logger.error(f"DMエラー通知に失敗: {notify_err}")

class ServerDM(commands.Cog):
    """サーバー内DM作成機能"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        self.active_dms = {}  # {channel_id: {'user1_id': int, 'user2_id': int, 'delete_at': datetime}}
        self.dm_categories = {}  # {guild_id: category_id} ギルドごとのDMカテゴリーID
        self.bot.loop.create_task(self._cleanup_expired_dms())
        self.bot.loop.create_task(self._cleanup_nonexistent_dms())
        self.bot.loop.create_task(self.restore_from_database())
    
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """再起動後もViewが動作するようにViewを再構築"""
        try:
            if interaction.type != discord.InteractionType.component:
                return
            
            if not interaction.data or 'custom_id' not in interaction.data:
                return
            
            custom_id = interaction.data['custom_id']
            
            # DM作成ボタンの場合
            if custom_id.startswith('serverdm_create'):
                view = ServerDMView(self)
                for item in view.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id == custom_id:
                        await item.callback(interaction)
                        return
            
            # DM削除ボタンの場合
            elif custom_id.startswith('serverdm_delete_'):
                channel_id_str = custom_id.replace('serverdm_delete_', '')
                channel_id = int(channel_id_str)
                view = DMDeleteView(self, channel_id)
                for item in view.children:
                    if isinstance(item, discord.ui.Button):
                        await item.callback(interaction)
                        return
        except ValueError:
            logger.warning(f"無効なserverdm_delete ID: {interaction.data.get('custom_id')}")
            await send_dm_error(interaction)
        except Exception as e:
            logger.error(f"serverdm on_interaction エラー: {e}", exc_info=True)
            await send_dm_error(interaction)
    
    @app_commands.command(name="serverdm", description="サーバー内DM作成操作盤を表示")
    @app_commands.describe(channel="操作盤を表示するチャンネル（省略可）")
    async def serverdm(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """サーバー内DM作成操作盤を表示（設定フロー付き）"""
        try:
            guild_id = interaction.guild.id
            
            # 既にカテゴリーが設定されているか確認
            if guild_id in self.dm_categories:
                category = interaction.guild.get_channel(self.dm_categories[guild_id])
                if category and isinstance(category, discord.CategoryChannel):
                    # 既に設定済みの場合は操作盤を表示
                    target_channel = channel or interaction.channel
                    embed = discord.Embed(
                        title="💬 サーバー内DM作成",
                        description="ボタンを押して、相手のスクリーンID（表示名）を入力してください。\n二人だけが話せるテキストチャンネルが作成されます。",
                        color=0x5865F2
                    )
                    
                    view = ServerDMView(self)
                    await target_channel.send(embed=embed, view=view)
                    
                    if channel:
                        await interaction.response.send_message(f"操作盤を {target_channel.mention} に表示しました", ephemeral=True)
                    else:
                        await interaction.response.send_message("操作盤を表示しました", ephemeral=True)
                    return
            
            # カテゴリーが設定されていない場合は設定フローを開始
            embed = discord.Embed(
                title="💬 サーバー内DM作成 セットアップ",
                description="**ステップ 1/2: DM作成先カテゴリー選択**\n\nDMチャンネルを作成するカテゴリーを選択してください。",
                color=0x5865F2
            )
            view = DMCategorySelectView(self, channel, interaction.guild)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"操作盤表示エラー: {e}")
            logger.error(traceback.format_exc())
            await send_dm_error(interaction)
    
    async def create_dm_channel(self, creator: discord.Member, target_screen_id: str, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """DMチャンネルを作成"""
        try:
            # スクリーンID（表示名）からユーザーを検索
            target_user = None
            screen_id_lower = target_screen_id.lower().strip()
            
            for member in guild.members:
                # 表示名（display_name）またはユーザー名（name）で検索（大文字小文字を区別しない）
                if member.display_name.lower() == screen_id_lower or member.name.lower() == screen_id_lower:
                    target_user = member
                    break
            
            if not target_user:
                return None
            
            target_user_id = target_user.id
            
            # 既存のDMチャンネルをチェック
            for channel_id, dm_data in self.active_dms.items():
                user1_id = dm_data['user1_id']
                user2_id = dm_data['user2_id']
                if (creator.id == user1_id and target_user_id == user2_id) or \
                   (creator.id == user2_id and target_user_id == user1_id):
                    # 既存のチャンネルを返す
                    existing_channel = guild.get_channel(channel_id)
                    if existing_channel:
                        return existing_channel
            
            # サーバー全体のDMチャンネル数をチェック（最大200）
            total_dm_count = 0
            channels_to_remove = []  # 存在しないチャンネルを記録
            
            for channel_id, dm_data in list(self.active_dms.items()):
                # チャンネルがまだ存在するか確認
                channel = guild.get_channel(channel_id)
                if channel:
                    total_dm_count += 1
                else:
                    # チャンネルが存在しない場合は削除対象に追加
                    channels_to_remove.append(channel_id)
            
            # 存在しないチャンネルをactive_dmsから削除（データベースからも削除）
            for channel_id in channels_to_remove:
                self.active_dms.pop(channel_id, None)
                self.db.delete_active_dm(channel_id)  # データベースからも削除
                logger.debug(f"存在しないDMチャンネルをactive_dmsから削除: {channel_id}")
            
            if total_dm_count >= 200:
                return "max_total_dms"
            
            # 作成者が既に1つ以上のDMチャンネルを持っているかチェック
            creator_dm_count = 0
            channels_to_remove_creator = []  # 存在しないチャンネルを記録
            for channel_id, dm_data in list(self.active_dms.items()):
                user1_id = dm_data['user1_id']
                user2_id = dm_data['user2_id']
                if creator.id == user1_id or creator.id == user2_id:
                    # チャンネルがまだ存在するか確認
                    channel = guild.get_channel(channel_id)
                    if channel:
                        creator_dm_count += 1
                    else:
                        # チャンネルが存在しない場合は削除対象に追加
                        channels_to_remove_creator.append(channel_id)
            
            # 存在しないチャンネルをactive_dmsから削除（データベースからも削除）
            for channel_id in channels_to_remove_creator:
                self.active_dms.pop(channel_id, None)
                self.db.delete_active_dm(channel_id)  # データベースからも削除
                logger.debug(f"存在しないDMチャンネルをactive_dmsから削除（作成者チェック時）: {channel_id}")
            
            if creator_dm_count >= 1:
                return "max_user_dms"
            
            # カテゴリーを取得または作成
            category = await self._get_or_create_category(guild)
            if not category:
                # カテゴリー作成失敗（上限到達の可能性）
                return "max_channels"
            
            # チャンネル名を生成（作成者のIDのみ）
            channel_name = f"dm-{creator.id}"
            
            # チャンネルを作成
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                target_user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
            }
            
            try:
                dm_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites
                )
            except discord.HTTPException as e:
                # チャンネル数上限エラーをチェック
                if "maximum number" in str(e).lower() or "channel limit" in str(e).lower() or e.status == 400:
                    # エラーメッセージを詳しく確認
                    error_msg = str(e).lower()
                    if "limit" in error_msg or "maximum" in error_msg:
                        return "max_channels"
                raise
            
            # 24時間後の削除時刻を設定
            delete_at = datetime.utcnow() + timedelta(hours=24)
            
            # アクティブDMリストに追加（作成者IDを保存）
            self.active_dms[dm_channel.id] = {
                'user1_id': creator.id,
                'user2_id': target_user_id,
                'creator_id': creator.id,  # 作成者IDを保存（削除ボタン用）
                'delete_at': delete_at
            }
            
            # データベースに保存
            self.db.save_active_dm(
                dm_channel.id,
                guild.id,
                creator.id,
                target_user_id,
                delete_at.isoformat()
            )
            
            # 作成通知を送信（埋め込みと通常メッセージの両方でメンション）
            content = f"{creator.mention} {target_user.mention}"
            embed = discord.Embed(
                description=f"{creator.mention} と {target_user.mention} のDMチャンネルが作成されました。\n24時間後に自動削除されます。",
                color=0x5865F2
            )
            view = DMDeleteView(self, dm_channel.id)
            # ボタンにカスタムIDを設定（再起動後も動作するように）
            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    item.custom_id = f"serverdm_delete_{dm_channel.id}"
            await dm_channel.send(content=content, embed=embed, view=view)
            
            return dm_channel
            
        except Exception as e:
            logger.error(f"DMチャンネル作成エラー: {e}")
            logger.error(traceback.format_exc())
            return None
    
    async def _get_or_create_category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        """DMカテゴリーを取得（設定されたカテゴリーを使用）"""
        guild_id = guild.id
        
        # 設定されたカテゴリーを取得
        if guild_id in self.dm_categories:
            category = guild.get_channel(self.dm_categories[guild_id])
            if category and isinstance(category, discord.CategoryChannel):
                return category
            else:
                # カテゴリーが削除されている場合は設定から削除
                self.dm_categories.pop(guild_id, None)
        
        # 設定されていない場合はNoneを返す（設定フローが必要）
        return None
    
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.TextChannel):
        """チャンネル削除時にactive_dmsから削除（制限から除外）"""
        if channel.id in self.active_dms:
            self.active_dms.pop(channel.id)
            self.db.delete_active_dm(channel.id)  # データベースからも削除
            logger.info(f"✅ DMチャンネルをactive_dmsから削除しました（制限から除外）: {channel.id}")
    
    async def delete_dm_channel(self, channel_id: int) -> bool:
        """DMチャンネルを削除"""
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                # チャンネルが既に削除されている場合
                self.active_dms.pop(channel_id, None)
                return False
            
            # チャンネルを削除
            await channel.delete()
            # active_dmsから削除（on_guild_channel_deleteでも削除されるが、念のため）
            self.active_dms.pop(channel_id, None)
            self.db.delete_active_dm(channel_id)  # データベースからも削除
            logger.info(f"✅ DMチャンネルを削除しました: {channel_id}")
            return True
        except Exception as e:
            logger.error(f"DMチャンネル削除エラー (ID: {channel_id}): {e}")
            return False
    
    async def restore_from_database(self):
        """データベースからactive_dmsとdm_categoriesを復元"""
        await self.bot.wait_until_ready()
        
        try:
            # active_dmsを復元
            dms = self.db.get_active_dms()
            for channel_id_str, dm_data in dms.items():
                channel_id = int(channel_id_str)
                # チャンネルが存在するか確認
                channel = self.bot.get_channel(channel_id)
                if channel:
                    # delete_atをdatetimeオブジェクトに変換
                    delete_at_str = dm_data['delete_at']
                    delete_at = datetime.fromisoformat(delete_at_str) if isinstance(delete_at_str, str) else delete_at_str
                    # creator_idを復元（user1_idが作成者）
                    self.active_dms[channel_id] = {
                        'user1_id': dm_data['user1_id'],
                        'user2_id': dm_data['user2_id'],
                        'creator_id': dm_data['user1_id'],  # user1_idが作成者
                        'delete_at': delete_at
                    }
                    logger.debug(f"✅ DMチャンネルを復元: {channel_id}")
                else:
                    # 存在しないチャンネルはデータベースから削除
                    self.db.delete_active_dm(channel_id)
                    logger.debug(f"存在しないDMチャンネルをデータベースから削除: {channel_id}")
            
            # dm_categoriesを復元
            categories = self.db.get_dm_categories()
            for guild_id_str, category_id in categories.items():
                guild_id = int(guild_id_str)
                category = None
                guild = self.bot.get_guild(guild_id)
                if guild:
                    category = guild.get_channel(category_id)
                if category and isinstance(category, discord.CategoryChannel):
                    self.dm_categories[guild_id] = category_id
                    logger.debug(f"✅ DMカテゴリーを復元: Guild {guild_id}, Category {category_id}")
                else:
                    # 存在しないカテゴリーはデータベースから削除
                    self.db.delete_dm_category(guild_id)
                    logger.debug(f"存在しないDMカテゴリーをデータベースから削除: Guild {guild_id}")
            
            logger.info(f"✅ DM機能の復元完了: {len(self.active_dms)}件のDMチャンネル, {len(self.dm_categories)}件のカテゴリー")
        except Exception as e:
            logger.error(f"DM機能の復元エラー: {e}")
            logger.error(traceback.format_exc())
    
    async def _cleanup_nonexistent_dms(self):
        """存在しないDMチャンネルをactive_dmsから削除"""
        await self.bot.wait_until_ready()
        
        try:
            channels_to_remove = []
            for channel_id in list(self.active_dms.keys()):
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    channels_to_remove.append(channel_id)
            
            for channel_id in channels_to_remove:
                self.active_dms.pop(channel_id, None)
                self.db.delete_active_dm(channel_id)  # データベースからも削除
                logger.info(f"✅ 存在しないDMチャンネルをactive_dmsから削除: {channel_id}")
            
            if channels_to_remove:
                logger.info(f"✅ {len(channels_to_remove)}件の存在しないDMチャンネルをクリーンアップしました")
        except Exception as e:
            logger.error(f"存在しないDMチャンネルクリーンアップエラー: {e}")
    
    async def _cleanup_expired_dms(self):
        """期限切れのDMチャンネルを削除"""
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            try:
                now = datetime.utcnow()
                channels_to_delete = []
                
                for channel_id, dm_data in list(self.active_dms.items()):
                    if dm_data['delete_at'] <= now:
                        channels_to_delete.append(channel_id)
                
                for channel_id in channels_to_delete:
                    try:
                        channel = self.bot.get_channel(channel_id)
                        if channel:
                            # 削除通知を送信
                            embed = discord.Embed(
                                description="このチャンネルは24時間経過したため削除されました。",
                                color=0xED4245
                            )
                            try:
                                await channel.send(embed=embed)
                                await asyncio.sleep(1)  # メッセージが送信されるのを待つ
                            except:
                                pass
                            
                            # チャンネルを削除
                            await channel.delete()
                            logger.info(f"✅ DMチャンネルを削除しました: {channel_id}")
                        else:
                            # チャンネルが既に存在しない場合もactive_dmsから削除
                            logger.info(f"DMチャンネルが既に存在しないためactive_dmsから削除: {channel_id}")
                    except Exception as e:
                        logger.error(f"DMチャンネル削除エラー (ID: {channel_id}): {e}")
                    finally:
                        # 確実にリストから削除（制限から除外）
                        if channel_id in self.active_dms:
                            self.active_dms.pop(channel_id, None)
                            self.db.delete_active_dm(channel_id)  # データベースからも削除
                            logger.debug(f"active_dmsから削除しました（制限から除外）: {channel_id}")
                
                # 1分ごとにチェック
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"DMクリーンアップエラー: {e}")
                await asyncio.sleep(60)


class DMCategorySelectView(discord.ui.View):
    """DMカテゴリー選択View"""
    
    def __init__(self, cog: ServerDM, target_channel: Optional[discord.TextChannel], guild: discord.Guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.target_channel = target_channel
        self.guild = guild
        
        categories = [c for c in guild.categories][:24]
        options = [discord.SelectOption(label=c.name[:100], value=str(c.id)) for c in categories]
        # 新カテゴリー作成オプションを追加
        options.append(discord.SelectOption(
            label="新カテゴリーを作成",
            value="new_category",
            description="新しいカテゴリーを作成"
        ))
        
        if options:
            self.select = discord.ui.Select(
                placeholder="DM作成先カテゴリーを選択",
                options=options
            )
            self.select.callback = self.on_select
            self.add_item(self.select)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            value = self.select.values[0]
            guild = self.guild
            
            if value == "new_category":
                # 新カテゴリーを自動作成
                try:
                    category = await guild.create_category("サーバー内DM")
                    await self.finalize(interaction, category)
                except Exception as e:
                    logger.error(f"カテゴリー作成エラー: {e}")
                    await interaction.response.send_message("カテゴリーの作成に失敗しました", ephemeral=True)
            else:
                category_id = int(value)
                category = guild.get_channel(category_id)
                if category and isinstance(category, discord.CategoryChannel):
                    await self.finalize(interaction, category)
                else:
                    await interaction.response.send_message("カテゴリーが見つかりません", ephemeral=True)
        except Exception as e:
            logger.error(f"カテゴリー選択エラー: {e}")
            await interaction.response.send_message("エラーが発生しました", ephemeral=True)
    
    async def finalize(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        """設定を完了して操作盤を表示"""
        try:
            # カテゴリーを保存
            self.cog.dm_categories[interaction.guild.id] = category.id
            # データベースに保存
            self.cog.db.save_dm_category(interaction.guild.id, category.id)
            
            # 操作盤を表示
            target_channel = self.target_channel or interaction.channel
            embed = discord.Embed(
                title="💬 サーバー内DM作成",
                description="ボタンを押して、相手のスクリーンID（表示名）を入力してください。\n二人だけが話せるテキストチャンネルが作成されます。",
                color=0x5865F2
            )
            
            view = ServerDMView(self.cog)
            await target_channel.send(embed=embed, view=view)
            
            # 設定完了メッセージ
            embed = discord.Embed(
                title="✅ セットアップ完了",
                description=f"DM作成先カテゴリーを設定しました: {category.mention}\n操作盤を {target_channel.mention} に表示しました。",
                color=0x57F287
            )
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception as e:
            logger.error(f"設定完了エラー: {e}")
            await interaction.response.send_message("エラーが発生しました", ephemeral=True)


class ServerDMView(discord.ui.View):
    """サーバー内DM作成操作盤"""
    
    def __init__(self, cog: ServerDM):
        super().__init__(timeout=None)
        self.cog = cog
    
    @discord.ui.button(label="DMを作成", style=discord.ButtonStyle.primary, emoji="💬", custom_id="serverdm_create")
    async def create_dm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """DM作成ボタン"""
        try:
            # 既にacknowledgeされている場合はスキップ
            if interaction.response.is_done():
                return
            
            modal = ServerDMModal(self.cog, interaction.user)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"DM作成ボタンエラー: {e}", exc_info=True)
            await send_dm_error(interaction)


class DMDeleteView(discord.ui.View):
    """DMチャンネル削除ボタン"""
    
    def __init__(self, cog: ServerDM, channel_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        # ボタンにカスタムIDを設定（再起動後も動作するように）
        # 注意: ボタンはデコレータで追加されるため、ここでは設定できない
        # 代わりに、on_interactionで処理する
    
    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """削除ボタン"""
        try:
            # 既にacknowledgeされている場合はスキップ
            if interaction.response.is_done():
                return
            
            # チャンネルが存在するか確認
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel:
                await interaction.response.send_message("このチャンネルは既に削除されています", ephemeral=True)
                return
            
            # 作成者かどうかを確認
            dm_data = self.cog.active_dms.get(self.channel_id)
            if not dm_data:
                await interaction.response.send_message("このチャンネルの情報が見つかりません", ephemeral=True)
                return
            
            creator_id = dm_data.get('creator_id', dm_data.get('user1_id'))  # 後方互換性のためuser1_idも確認
            if interaction.user.id != creator_id:
                await interaction.response.send_message("❌ このチャンネルは作成者だけが削除できます", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True)
            
            # チャンネルを削除
            success = await self.cog.delete_dm_channel(self.channel_id)
            
            # チャンネルが削除された後はfollowup.sendが失敗する可能性があるため、try-exceptで囲む
            try:
                if success:
                    await interaction.followup.send("✅ DMチャンネルを削除しました", ephemeral=True)
                else:
                    await interaction.followup.send("❌ チャンネルの削除に失敗しました", ephemeral=True)
            except discord.errors.HTTPException as e:
                # チャンネルが既に削除されている場合はエラーを無視
                if e.code == 10003:  # Unknown Channel
                    logger.debug(f"チャンネル削除後のfollowup送信をスキップ（チャンネルは既に削除済み）: {self.channel_id}")
                else:
                    raise
                
        except Exception as e:
            logger.error(f"削除ボタンエラー: {e}", exc_info=True)
            await send_dm_error(interaction)


class ServerDMModal(discord.ui.Modal, title="サーバー内DM作成"):
    """スクリーンID入力モーダル"""
    
    screen_id_input = discord.ui.TextInput(
        label="相手のスクリーンID（表示名）",
        style=discord.TextStyle.short,
        placeholder="例: ユーザー名",
        required=True,
        max_length=32
    )
    
    def __init__(self, cog: ServerDM, creator: discord.Member):
        super().__init__()
        self.cog = cog
        self.creator = creator
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # スクリーンIDを取得
            target_screen_id = self.screen_id_input.value.strip()
            
            if not target_screen_id:
                await interaction.response.send_message("❌ スクリーンID（表示名）を入力してください", ephemeral=True)
                return
            
            # 自分自身を指定していないかチェック
            if target_screen_id.lower() == self.creator.display_name.lower() or target_screen_id.lower() == self.creator.name.lower():
                await interaction.response.send_message("❌ 自分自身を指定することはできません", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True)
            
            # DMチャンネルを作成
            result = await self.cog.create_dm_channel(
                self.creator,
                target_screen_id,
                interaction.guild
            )
            
            if result == "max_channels":
                await interaction.followup.send(
                    "❌ サーバーのテキストチャンネル数が上限に達しています。\n管理者に連絡してください。",
                    ephemeral=True
                )
            elif result == "max_total_dms":
                await interaction.followup.send(
                    "❌ サーバー全体で200個までしかDMチャンネルを作成できません。\n既存のDMチャンネルを削除してから再度お試しください。",
                    ephemeral=True
                )
            elif result == "max_user_dms":
                await interaction.followup.send(
                    "❌ 一人当たり1つまでしかDMチャンネルを作成できません。\n既存のDMチャンネルを削除してから再度お試しください。",
                    ephemeral=True
                )
            elif result is None:
                await interaction.followup.send(
                    "❌ DMチャンネルの作成に失敗しました。\nスクリーンID（表示名）が正しいか、ユーザーがサーバーに存在するか確認してください。",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"✅ DMチャンネルを作成しました: {result.mention}",
                    ephemeral=True
                )
                
        except Exception as e:
            logger.error(f"DM作成モーダルエラー: {e}", exc_info=True)
            await send_dm_error(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerDM(bot))

