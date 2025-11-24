import discord
from discord.ext import commands
from discord import app_commands
import logging
import traceback
import sys
import os

# 親ディレクトリのdatabase.pyをインポート
# 親ディレクトリのdatabase.pyをインポート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import Database

# ロガー設定
logger = logging.getLogger('embeddisplay')
logger.setLevel(logging.INFO)

class EmbedDisplay(commands.Cog):
    """埋め込み表示機能"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database()
        # チャンネルごとの最新埋め込みメッセージIDを保存
        # {channel_id: message_id}
        self.active_embeds = {}
        # 処理中フラグ（無限ループ防止）
        self.processing_channels = set()
        # 起動時にデータベースから復元
        bot.loop.create_task(self.restore_from_database())
    
    @app_commands.command(name="embeddisplay", description="埋め込みメッセージを表示・更新")
    async def embeddisplay(self, interaction: discord.Interaction):
        """埋め込みメッセージを表示・更新するコマンド"""
        # 既に埋め込みメッセージがある場合は削除
        if interaction.channel.id in self.active_embeds:
            try:
                old_message_id = self.active_embeds[interaction.channel.id]
                old_message = await interaction.channel.fetch_message(old_message_id)
                await old_message.delete()
                # 辞書からも削除
                del self.active_embeds[interaction.channel.id]
                # データベースからも削除
                self.db.delete_embed_display(interaction.channel.id)
                await interaction.response.send_message("埋め込みメッセージを削除しました", ephemeral=True)
                logger.info(f"埋め込みメッセージを削除しました (Channel: {interaction.channel.id})")
                return
            except discord.NotFound:
                # メッセージが既に削除されている場合は辞書から削除
                if interaction.channel.id in self.active_embeds:
                    del self.active_embeds[interaction.channel.id]
            except Exception as e:
                logger.error(f"埋め込みメッセージ削除エラー: {e}")
                logger.error(traceback.format_exc())
        
        # 埋め込みメッセージがない場合はモーダルを表示
        modal = EmbedDisplayModal(self)
        await interaction.response.send_modal(modal)
    
    async def update_embed(self, channel: discord.TextChannel, content: str):
        """埋め込みメッセージを更新"""
        # 処理中フラグを設定
        if channel.id in self.processing_channels:
            logger.debug(f"既に処理中のためスキップ (Channel: {channel.id})")
            return
        
        self.processing_channels.add(channel.id)
        try:
            # 以前の埋め込みメッセージを削除
            if channel.id in self.active_embeds:
                old_message_id = self.active_embeds[channel.id]
                try:
                    old_message = await channel.fetch_message(old_message_id)
                    await old_message.delete()
                    logger.info(f"以前の埋め込みメッセージを削除しました (Channel: {channel.id}, Message: {old_message_id})")
                except discord.NotFound:
                    logger.debug(f"以前の埋め込みメッセージが見つかりませんでした (Message: {old_message_id})")
                except Exception as e:
                    logger.error(f"以前の埋め込みメッセージ削除エラー: {e}")
            
            # 新しい埋め込みメッセージを送信
            embed = discord.Embed(
                description=content,
                color=0x5865F2
            )
            message = await channel.send(embed=embed)
            self.active_embeds[channel.id] = message.id
            
            # データベースに保存
            self.db.save_embed_display(channel.id, message.id, content)
            
            logger.info(f"埋め込みメッセージを送信しました (Channel: {channel.id}, Message: {message.id})")
        except Exception as e:
            logger.error(f"埋め込みメッセージ更新エラー: {e}")
            logger.error(traceback.format_exc())
        finally:
            # 処理中フラグを解除
            self.processing_channels.discard(channel.id)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージが送信されたときに埋め込みを更新"""
        # ボット自身のメッセージは無視
        if message.author.bot:
            return
        
        # 処理中のチャンネルは無視（無限ループ防止）
        if message.channel.id in self.processing_channels:
            return
        
        # 埋め込みメッセージ自体も無視
        if message.channel.id in self.active_embeds and message.id == self.active_embeds[message.channel.id]:
            return
        
        # このチャンネルにアクティブな埋め込みがある場合
        if message.channel.id in self.active_embeds:
            # 以前の埋め込みメッセージを削除
            old_message_id = self.active_embeds[message.channel.id]
            try:
                old_message = await message.channel.fetch_message(old_message_id)
                # 埋め込みの内容を取得
                if old_message.embeds and len(old_message.embeds) > 0:
                    old_content = old_message.embeds[0].description or ""
                    # 削除して再送信（update_embed内でデータベースも更新される）
                    await old_message.delete()
                    await self.update_embed(message.channel, old_content)
                    logger.info(f"埋め込みメッセージを更新しました (Channel: {message.channel.id})")
            except discord.NotFound:
                # メッセージが既に削除されている場合は辞書から削除
                if message.channel.id in self.active_embeds:
                    del self.active_embeds[message.channel.id]
            except Exception as e:
                logger.error(f"埋め込みメッセージ更新エラー: {e}")
                logger.error(traceback.format_exc())
    
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """メッセージが削除されたときの処理"""
        if payload.channel_id in self.active_embeds and payload.message_id == self.active_embeds[payload.channel_id]:
            # 埋め込みメッセージが削除された場合
            del self.active_embeds[payload.channel_id]
            self.db.delete_embed_display(payload.channel_id)
            logger.info(f"埋め込みメッセージが削除されました (Channel: {payload.channel_id})")
    
    async def restore_from_database(self):
        """データベースから埋め込み表示を復元"""
        await self.bot.wait_until_ready()
        
        try:
            displays = self.db.get_embed_displays()
            restored_count = 0
            
            for channel_id, data in displays.items():
                try:
                    channel = self.bot.get_channel(channel_id)
                    if not channel or not isinstance(channel, discord.TextChannel):
                        # チャンネルが見つからない場合はデータベースから削除
                        self.db.delete_embed_display(channel_id)
                        continue
                    
                    message_id = data['message_id']
                    content = data['content']
                    
                    # メッセージが存在するか確認
                    try:
                        message = await channel.fetch_message(message_id)
                        # メッセージが存在する場合はactive_embedsに追加
                        self.active_embeds[channel_id] = message_id
                        restored_count += 1
                        logger.info(f"埋め込み表示を復元しました (Channel: {channel_id}, Message: {message_id})")
                    except discord.NotFound:
                        # メッセージが削除されている場合は再作成
                        embed = discord.Embed(
                            description=content,
                            color=0x5865F2
                        )
                        new_message = await channel.send(embed=embed)
                        self.active_embeds[channel_id] = new_message.id
                        self.db.save_embed_display(channel_id, new_message.id, content)
                        restored_count += 1
                        logger.info(f"埋め込み表示を再作成しました (Channel: {channel_id}, Message: {new_message.id})")
                except Exception as e:
                    logger.error(f"埋め込み表示復元エラー (Channel: {channel_id}): {e}")
                    logger.error(traceback.format_exc())
            
            logger.info(f"✅ 埋め込み表示の復元完了: {restored_count}件")
        except Exception as e:
            logger.error(f"埋め込み表示復元エラー: {e}")
            logger.error(traceback.format_exc())


class EmbedDisplayModal(discord.ui.Modal):
    """埋め込み表示用モーダル"""
    
    def __init__(self, cog: EmbedDisplay):
        super().__init__(title="埋め込みメッセージ入力")
        self.cog = cog
    
    content_input = discord.ui.TextInput(
        label="表示する内容",
        placeholder="ここに入力した内容が埋め込み形式で表示されます",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理"""
        try:
            content = self.content_input.value
            
            # 埋め込みメッセージを更新
            await self.cog.update_embed(interaction.channel, content)
            
            await interaction.response.send_message("埋め込みメッセージを更新しました", ephemeral=True)
        except Exception as e:
            logger.error(f"埋め込み表示モーダルエラー: {e}")
            logger.error(traceback.format_exc())
            if not interaction.response.is_done():
                await interaction.response.send_message("エラーが発生しました", ephemeral=True)
            else:
                await interaction.followup.send("エラーが発生しました", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedDisplay(bot))

