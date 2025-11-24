import discord
from discord import app_commands
from discord.ext import commands
import logging
import sqlite3
import json
import asyncio
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PANEL_TITLE = "サポートチャット"
DEFAULT_PANEL_DESCRIPTION = "ボタンを押してチャットを開始してください"
DEFAULT_PANEL_BUTTON_LABEL = "💬 チャット開始"
DEFAULT_START_TITLE = "チャット開始"
DEFAULT_START_DESCRIPTION = "こんにちは！\n\nサポートスタッフが対応します。"


def build_text_settings(
    welcome_message: Optional[str] = None,
    panel_title: Optional[str] = None,
    panel_description: Optional[str] = None,
    panel_button_label: Optional[str] = None,
    start_title: Optional[str] = None,
    start_description: Optional[str] = None,
):
    """埋め込みやボタン文言をまとめて保持"""
    start_desc = (start_description or DEFAULT_START_DESCRIPTION).strip()
    return {
        "panel_title": (panel_title or DEFAULT_PANEL_TITLE).strip(),
        "panel_description": (panel_description or DEFAULT_PANEL_DESCRIPTION).strip(),
        "panel_button_label": (panel_button_label or DEFAULT_PANEL_BUTTON_LABEL).strip(),
        "start_title": (start_title or DEFAULT_START_TITLE).strip(),
        "start_description": start_desc,
        "welcome_message": (welcome_message or start_desc),
    }


class TicketManager(commands.Cog):
    """チケットシステム"""
    
    def __init__(self, bot):
        self.bot = bot
        self.ticket_systems = {}
        self.active_tickets = {}
        self.db_path = "data/tickets.db"
        self.editing_channels = set()
        self.init_database()
        self.bot.loop.create_task(self.load_and_restore_async())
    
    def init_database(self):
        """DB初期化"""
        import os
        os.makedirs("data", exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""CREATE TABLE IF NOT EXISTS ticket_systems (
            guild_id INTEGER, message_id INTEGER, system_data TEXT, PRIMARY KEY (guild_id, message_id))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS active_tickets (
            channel_id INTEGER PRIMARY KEY, owner_id INTEGER, guild_id INTEGER,
            created_from INTEGER, system_data TEXT, is_closed INTEGER DEFAULT 0)""")
        conn.commit()
        conn.close()
    
    def load_data(self):
        """データ読み込み"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT guild_id, message_id, system_data FROM ticket_systems")
            for guild_id, message_id, data in cursor.fetchall():
                if guild_id not in self.ticket_systems:
                    self.ticket_systems[guild_id] = {}
                self.ticket_systems[guild_id][message_id] = json.loads(data)
            cursor.execute("SELECT channel_id, owner_id, guild_id, created_from, system_data, COALESCE(is_closed, 0) FROM active_tickets")
            for channel_id, owner_id, guild_id, created_from, data, is_closed in cursor.fetchall():
                self.active_tickets[channel_id] = {
                    'owner_id': owner_id, 'guild_id': guild_id, 'created_from': created_from,
                    'system_data': json.loads(data), 'is_closed': bool(int(is_closed))}
            cursor.execute("UPDATE active_tickets SET is_closed = 0 WHERE is_closed IS NULL")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"読み込みエラー: {e}")
    
    def save_ticket(self, channel_id: int):
        """チケット保存"""
        if channel_id not in self.active_tickets:
            return
        try:
            data = self.active_tickets[channel_id]
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""INSERT OR REPLACE INTO active_tickets 
                (channel_id, owner_id, guild_id, created_from, system_data, is_closed) VALUES (?, ?, ?, ?, ?, ?)""",
                (channel_id, data['owner_id'], data['guild_id'], data['created_from'],
                 json.dumps(data['system_data'], ensure_ascii=False), 1 if data.get('is_closed', False) else 0))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"保存エラー: {e}")
    
    def save_system(self, guild_id: int, message_id: int):
        """システム保存"""
        try:
            if guild_id not in self.ticket_systems or message_id not in self.ticket_systems[guild_id]:
                return
            system_data = self.ticket_systems[guild_id][message_id]
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""INSERT OR REPLACE INTO ticket_systems (guild_id, message_id, system_data) VALUES (?, ?, ?)""",
                (guild_id, message_id, json.dumps(system_data, ensure_ascii=False)))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"システム保存エラー: {e}")
    
    def delete_ticket(self, channel_id: int):
        """チケット削除"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM active_tickets WHERE channel_id = ?", (channel_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"削除エラー: {e}")
    
    async def load_and_restore_async(self):
        """非同期読み込み"""
        await asyncio.sleep(1)
        await asyncio.to_thread(self.load_data)
        await self.cleanup_ghost_tickets()
        for guild_id, systems in self.ticket_systems.items():
            for message_id, system_data in systems.items():
                try:
                    if self.bot.get_guild(guild_id):
                        self.bot.add_view(TicketButtonView(self, system_data), message_id=message_id)
                except:
                    pass
        for channel_id, data in list(self.active_tickets.items()):
            try:
                guild = self.bot.get_guild(data['guild_id'])
                if guild:
                    channel = guild.get_channel(channel_id)
                    owner = guild.get_member(data['owner_id'])
                    if channel and owner:
                        self.bot.add_view(TicketControlView(channel, owner, self))
            except:
                pass
        logger.info(f"✅ View復元完了")
    
    async def cleanup_ghost_tickets(self):
        """ゴースト削除"""
        to_delete = []
        for channel_id, data in list(self.active_tickets.items()):
            guild = self.bot.get_guild(data['guild_id'])
            if not guild or not guild.get_channel(channel_id):
                to_delete.append(channel_id)
        for channel_id in to_delete:
            del self.active_tickets[channel_id]
            self.delete_ticket(channel_id)
        if to_delete:
            logger.info(f"✅ {len(to_delete)}件削除")
    
    @app_commands.command(name="ticket", description="チケットシステムを作成")
    @app_commands.default_permissions(administrator=True)
    async def ticket_create(self, interaction: discord.Interaction):
        """チケット作成コマンド"""
        try:
            embed = discord.Embed(
                title="チケットシステム セットアップ",
                description="**ステップ 1/4: サポートロール設定**\n\nサポートロールを設定してください",
                color=0x5865F2)
            text_settings = build_text_settings()
            view = Step1_SupportRole(self, interaction, text_settings)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"エラー: {e}")
            await interaction.response.send_message("❌ エラー", ephemeral=True)
    
    async def create_ticket(self, member, button_channel, system_data):
        """チケット作成"""
        guild = member.guild
        category_id = system_data.get('category_id')
        category = guild.get_channel(category_id) if category_id else None
        support_roles = system_data.get('support_roles', [])
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)}
        for role_id in support_roles:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if category:
            channel = await category.create_text_channel(name=f"chat-{member.name}", overwrites=overwrites)
        else:
            channel = await guild.create_text_channel(name=f"chat-{member.name}", overwrites=overwrites)
        self.active_tickets[channel.id] = {
            'owner_id': member.id, 'guild_id': guild.id, 'created_from': button_channel.id,
            'system_data': system_data, 'is_closed': False}
        self.save_ticket(channel.id)
        start_title = system_data.get('start_title') or DEFAULT_START_TITLE
        start_description = system_data.get('start_description') or system_data.get('welcome_message') or DEFAULT_START_DESCRIPTION
        embed = discord.Embed(title=start_title, description=start_description, color=0x5865F2)
        view = TicketControlView(channel, member, self)
        await channel.send(f"{member.mention}", embed=embed, view=view)
    
    async def close_ticket(self, channel, closer, save_log=False):
        """チケット終了"""
        if channel.id not in self.active_tickets:
            return
        if save_log:
            self.active_tickets[channel.id]['is_closed'] = True
            self.save_ticket(channel.id)
            asyncio.create_task(channel.send(f"🔒 {closer.mention} が終了"))
            asyncio.create_task(self._edit_closed_channel(channel))
        else:
            asyncio.create_task(channel.send(f"🗑️ 5秒後に削除"))
            await asyncio.sleep(5)
            await channel.delete()
            if channel.id in self.active_tickets:
                del self.active_tickets[channel.id]
                self.delete_ticket(channel.id)
    
    async def _edit_closed_channel(self, channel):
        """終了処理"""
        if channel.id in self.editing_channels:
            return
        self.editing_channels.add(channel.id)
        try:
            data = self.active_tickets.get(channel.id, {})
            owner = channel.guild.get_member(data['owner_id'])
            system_data = data.get('system_data', {})
            archive_category_id = system_data.get('archive_category_id')
            new_name = f"closed-{channel.name}" if not channel.name.startswith("closed-") else channel.name
            overwrites = channel.overwrites
            if owner:
                overwrites[owner] = discord.PermissionOverwrite(read_messages=False, send_messages=False)
            if archive_category_id:
                log_category = channel.guild.get_channel(archive_category_id)
                if log_category:
                    await channel.edit(category=log_category, name=new_name, overwrites=overwrites)
                else:
                    await channel.edit(name=new_name, overwrites=overwrites)
            else:
                await channel.edit(name=new_name, overwrites=overwrites)
        except Exception as e:
            logger.error(f"編集エラー: {e}")
        finally:
            self.editing_channels.discard(channel.id)
    
    async def reopen_ticket(self, channel, reopener):
        """チケット再開"""
        if channel.id not in self.active_tickets:
            return
        self.active_tickets[channel.id]['is_closed'] = False
        self.save_ticket(channel.id)
        asyncio.create_task(channel.send(f"🔓 {reopener.mention} が再開"))
        asyncio.create_task(self._edit_reopened_channel(channel))
    
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
        
        # チケット作成ボタンの場合
        if custom_id == "create_ticket_button":
            try:
                # メッセージIDからシステムデータを取得
                message_id = interaction.message.id
                guild_id = interaction.guild.id
                
                if guild_id in self.ticket_systems and message_id in self.ticket_systems[guild_id]:
                    system_data = self.ticket_systems[guild_id][message_id]
                    view = TicketButtonView(self, system_data)
                    # コールバックを手動で呼び出す
                    for item in view.children:
                        if isinstance(item, discord.ui.Button) and item.custom_id == custom_id:
                            await item.callback(interaction)
                            return
            except Exception as e:
                logger.error(f"チケット作成ボタンエラー: {e}")
                if not interaction.response.is_done():
                    await interaction.response.send_message("エラーが発生しました", ephemeral=True)
        
        # チケット操作ボタンの場合
        elif custom_id in ["close_ticket_button", "reopen_ticket_button", "delete_ticket_button"]:
            try:
                channel_id = interaction.channel.id
                if channel_id not in self.active_tickets:
                    await interaction.response.send_message("このチャンネルはチケットではありません", ephemeral=True)
                    return
                
                data = self.active_tickets[channel_id]
                owner = interaction.guild.get_member(data['owner_id'])
                if not owner:
                    await interaction.response.send_message("チケットの所有者が見つかりません", ephemeral=True)
                    return
                
                view = TicketControlView(interaction.channel, owner, self)
                # コールバックを手動で呼び出す
                for item in view.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id == custom_id:
                        await item.callback(interaction)
                        return
            except Exception as e:
                logger.error(f"チケット操作ボタンエラー: {e}")
                logger.error(traceback.format_exc())
                if not interaction.response.is_done():
                    await interaction.response.send_message("エラーが発生しました", ephemeral=True)
    
    async def _edit_reopened_channel(self, channel):
        """再開処理"""
        if channel.id in self.editing_channels:
            return
        self.editing_channels.add(channel.id)
        try:
            data = self.active_tickets.get(channel.id, {})
            owner = channel.guild.get_member(data['owner_id'])
            system_data = data.get('system_data', {})
            category_id = system_data.get('category_id')
            new_name = channel.name.replace("closed-", "")
            overwrites = channel.overwrites
            if owner:
                overwrites[owner] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            if category_id:
                category = channel.guild.get_channel(category_id)
                if category:
                    await channel.edit(category=category, name=new_name, overwrites=overwrites)
                else:
                    await channel.edit(name=new_name, overwrites=overwrites)
            else:
                await channel.edit(name=new_name, overwrites=overwrites)
        except Exception as e:
            logger.error(f"編集エラー: {e}")
        finally:
            self.editing_channels.discard(channel.id)


# ============================================================
# ステップ1: サポートロール設定
# ============================================================
class Step1_SupportRole(discord.ui.View):
    """ステップ1: サポートロール"""
    def __init__(self, cog, original_interaction, text_settings=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.text_settings = text_settings or build_text_settings()
        options = [
            discord.SelectOption(label="サポートロールなし", value="none", description="管理者のみ閲覧可能"),
            discord.SelectOption(label="選択する", value="specify", description="複数のロールを指定")]
        self.select = discord.ui.Select(placeholder="サポートロール設定を選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        
        # ヘルプボタン
        help_btn = discord.ui.Button(label="ヘルプ", style=discord.ButtonStyle.secondary, row=1)
        help_btn.callback = self.show_help
        self.add_item(help_btn)
    
    async def show_help(self, interaction: discord.Interaction):
        """ヘルプ表示"""
        embed = discord.Embed(title="ヘルプ: サポートロール設定", color=0x00FF00)
        embed.add_field(name="サポートロールなし", value="管理者のみがチケットを閲覧できます", inline=False)
        embed.add_field(name="選択する", value="指定したロール（複数可）がチケットを閲覧できます\n例: @サポート, @モデレーター", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            mode = self.select.values[0]
            if mode == "none":
                view = Step2_Message(self.cog, self.original_interaction, [], self.text_settings, stage="panel")
                embed = view.build_embed()
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                embed = discord.Embed(
                    title="チケットシステム セットアップ",
                    description="**ステップ 1-2/4: サポートロール選択**\n\nサポートロールを選択してください（複数可）",
                    color=0x5865F2)
                view = Step1_RoleSelect(self.cog, self.original_interaction, self.text_settings)
                await interaction.response.edit_message(embed=embed, view=view)
        except:
            pass


class Step1_RoleSelect(discord.ui.View):
    """ステップ1-2: ロール選択"""
    def __init__(self, cog, original_interaction, text_settings):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.text_settings = text_settings
        roles = [r for r in original_interaction.guild.roles if r != original_interaction.guild.default_role][:25]
        if roles:
            self.select = discord.ui.Select(
                placeholder="サポートロールを選択（複数可）", min_values=1, max_values=min(len(roles), 25),
                options=[discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in roles])
            self.select.callback = self.on_select
            self.add_item(self.select)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            support_roles = [int(v) for v in self.select.values]
            role_names = [interaction.guild.get_role(r).name for r in support_roles if interaction.guild.get_role(r)]
            role_text = ", ".join(role_names[:3])
            if len(role_names) > 3:
                role_text += f" 他{len(role_names)-3}件"
            view = Step2_Message(self.cog, self.original_interaction, support_roles, self.text_settings, stage="panel")
            embed = view.build_embed()
            await interaction.response.edit_message(embed=embed, view=view)
        except:
            pass


# ============================================================
# ステップ2: 文言設定
# ============================================================
class Step2_Message(discord.ui.View):
    """ステップ2: 文言設定。受付パネル→チャット開始の順に選択させる。"""
    def __init__(self, cog, original_interaction, support_roles, text_settings, stage="panel"):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.support_roles = support_roles
        self.text_settings = text_settings or build_text_settings()
        self.stage = stage  # "panel" or "chat"
        
        if self.stage == "panel":
            placeholder = "受付パネルの文言設定方法を選択"
            help_desc = (
                "受付パネル（公開埋め込み・ボタン）の文言をどうするか選びます。\n"
                "・デフォルト: 既定の文言\n"
                "・カスタム: タイトル/説明/ボタン名をモーダルで入力"
            )
        else:
            placeholder = "チャット開始文言の設定方法を選択"
            help_desc = (
                "チケットチャンネルに送信される開始メッセージの文言を選びます。\n"
                "・デフォルト: 既定の文言\n"
                "・カスタム: タイトル/本文をモーダルで入力"
            )
        self.help_desc = help_desc
        
        self.select = discord.ui.Select(
            placeholder=placeholder,
            options=[
                discord.SelectOption(label="デフォルトを使う", value="default", description="標準の文言を使用"),
                discord.SelectOption(label="カスタム入力", value="custom", description="モーダルで入力")
            ]
        )
        self.select.callback = self.on_select
        self.add_item(self.select)
        
        help_btn = discord.ui.Button(label="ヘルプ", style=discord.ButtonStyle.secondary, row=1)
        help_btn.callback = self.show_help
        self.add_item(help_btn)
    
    def build_embed(self):
        if self.stage == "panel":
            desc = "**ステップ 2/4: 受付パネル文言**\nデフォルトかカスタム入力を選択してください。"
        else:
            desc = "**ステップ 2-2/4: チャット開始文言**\nデフォルトかカスタム入力を選択してください。"
        return discord.Embed(title="チケットシステム セットアップ", description=desc, color=0x5865F2)
    
    async def show_help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="ヘルプ: 文言設定", description=self.help_desc, color=0x00FF00)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    def _apply_panel_defaults(self):
        self.text_settings["panel_title"] = DEFAULT_PANEL_TITLE
        self.text_settings["panel_description"] = DEFAULT_PANEL_DESCRIPTION
        self.text_settings["panel_button_label"] = DEFAULT_PANEL_BUTTON_LABEL
    
    def _apply_chat_defaults(self):
        self.text_settings["start_title"] = DEFAULT_START_TITLE
        self.text_settings["start_description"] = DEFAULT_START_DESCRIPTION
        self.text_settings["welcome_message"] = DEFAULT_START_DESCRIPTION
    
    async def _show_chat_stage(self, interaction: discord.Interaction, from_modal: bool):
        new_view = Step2_Message(self.cog, self.original_interaction, self.support_roles, self.text_settings, stage="chat")
        embed = new_view.build_embed()
        if from_modal:
            await self.original_interaction.edit_original_response(embed=embed, view=new_view)
        else:
            await interaction.response.edit_message(embed=embed, view=new_view)
    
    async def _show_step3(self, interaction: discord.Interaction, from_modal: bool):
        embed = discord.Embed(
            title="チケットシステム セットアップ",
            description="**ステップ 3/4: チケット作成先カテゴリー**",
            color=0x5865F2)
        view = Step3_Category(self.cog, self.original_interaction, self.support_roles, self.text_settings)
        if from_modal:
            await self.original_interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
    
    async def on_select(self, interaction: discord.Interaction):
        choice = self.select.values[0]
        if self.stage == "panel":
            if choice == "default":
                self._apply_panel_defaults()
                await self._show_chat_stage(interaction, from_modal=False)
            else:
                modal = PanelTextModal(self)
                await interaction.response.send_modal(modal)
        else:
            if choice == "default":
                self._apply_chat_defaults()
                await self._show_step3(interaction, from_modal=False)
            else:
                modal = ChatStartTextModal(self)
                await interaction.response.send_modal(modal)


class PanelTextModal(discord.ui.Modal, title="パネル文言を設定"):
    panel_title = discord.ui.TextInput(
        label="埋め込みタイトル",
        max_length=100,
        required=False,
        placeholder=f"例: {DEFAULT_PANEL_TITLE}",
    )
    panel_description = discord.ui.TextInput(
        label="埋め込み説明",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
        placeholder=f"例: {DEFAULT_PANEL_DESCRIPTION}",
    )
    panel_button_label = discord.ui.TextInput(
        label="ボタンのラベル",
        max_length=50,
        required=False,
        placeholder=f"例: {DEFAULT_PANEL_BUTTON_LABEL}",
    )
    
    def __init__(self, parent_view: Step2_Message):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        settings = self.parent_view.text_settings
        settings["panel_title"] = (self.panel_title.value or DEFAULT_PANEL_TITLE).strip()
        settings["panel_description"] = (self.panel_description.value or DEFAULT_PANEL_DESCRIPTION).strip()
        settings["panel_button_label"] = (self.panel_button_label.value or DEFAULT_PANEL_BUTTON_LABEL).strip()
        await interaction.response.defer(ephemeral=True)
        await self.parent_view._show_chat_stage(interaction, from_modal=True)


class ChatStartTextModal(discord.ui.Modal, title="チャット開始文言を設定"):
    start_title = discord.ui.TextInput(
        label="チャット開始タイトル",
        max_length=100,
        required=False,
        placeholder=f"例: {DEFAULT_START_TITLE}",
    )
    start_description = discord.ui.TextInput(
        label="チャット開始メッセージ",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
        placeholder=f"例: {DEFAULT_START_DESCRIPTION}",
    )
    
    def __init__(self, parent_view: Step2_Message):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        settings = self.parent_view.text_settings
        start_title = (self.start_title.value or DEFAULT_START_TITLE).strip()
        start_desc = (self.start_description.value or DEFAULT_START_DESCRIPTION).strip()
        settings["start_title"] = start_title
        settings["start_description"] = start_desc
        settings["welcome_message"] = start_desc
        await interaction.response.defer(ephemeral=True)
        await self.parent_view._show_step3(interaction, from_modal=True)


# ============================================================
# ステップ3: チケット作成先カテゴリー
# ============================================================
class Step3_Category(discord.ui.View):
    """ステップ3: カテゴリー"""
    def __init__(self, cog, original_interaction, support_roles, text_settings):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.support_roles = support_roles
        self.text_settings = text_settings
        
        # 新規作成オプションを追加
        options = [discord.SelectOption(label="新規カテゴリー作成", value="new", description="チケット用の新しいカテゴリーを作成")]
        categories = [c for c in original_interaction.guild.categories][:24]
        options.extend([discord.SelectOption(label=c.name[:100], value=str(c.id)) for c in categories])
        
        self.select = discord.ui.Select(placeholder="チケット作成先カテゴリーを選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        
        # ヘルプボタン
        help_btn = discord.ui.Button(label="ヘルプ", style=discord.ButtonStyle.secondary, row=1)
        help_btn.callback = self.show_help
        self.add_item(help_btn)
    
    async def show_help(self, interaction: discord.Interaction):
        """ヘルプ表示"""
        embed = discord.Embed(title="ヘルプ: チケット作成先カテゴリー", color=0x00FF00)
        embed.add_field(name="新規カテゴリー作成", value="「サポートチケット」という新しいカテゴリーを自動作成します", inline=False)
        embed.add_field(name="既存カテゴリー選択", value="選択したカテゴリー内にチケットチャンネルを作成します", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            if self.select.values[0] == "new":
                # 新規カテゴリー作成
                await interaction.response.defer(ephemeral=True, thinking=False)
                category = await interaction.guild.create_category("サポートチケット")
                category_id = category.id
                embed = discord.Embed(
                    title="チケットシステム セットアップ",
                    description="**ステップ 4/4: ログ保存先カテゴリー**",
                    color=0x5865F2)
                view = Step4_ArchiveCategory(self.cog, self.original_interaction, self.support_roles, self.text_settings, category_id)
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            else:
                category_id = int(self.select.values[0])
                embed = discord.Embed(
                    title="チケットシステム セットアップ",
                    description="**ステップ 4/4: ログ保存先カテゴリー**",
                    color=0x5865F2)
                view = Step4_ArchiveCategory(self.cog, self.original_interaction, self.support_roles, self.text_settings, category_id)
                await interaction.response.edit_message(embed=embed, view=view)
        except:
            pass


# ============================================================
# ステップ4: ログ保存先カテゴリー
# ============================================================
class Step4_ArchiveCategory(discord.ui.View):
    """ステップ4: ログカテゴリー"""
    def __init__(self, cog, original_interaction, support_roles, text_settings, category_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.support_roles = support_roles
        self.text_settings = text_settings
        self.category_id = category_id
        
        # 新規作成オプションを追加
        options = [discord.SelectOption(label="新規カテゴリー作成", value="new", description="ログ用の新しいカテゴリーを作成")]
        categories = [c for c in original_interaction.guild.categories][:24]
        options.extend([discord.SelectOption(label=c.name[:100], value=str(c.id)) for c in categories])
        
        self.select = discord.ui.Select(placeholder="ログ保存先カテゴリーを選択（オプション）", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        
        # スキップボタン
        skip_btn = discord.ui.Button(label="スキップ（その場で終了）", style=discord.ButtonStyle.secondary, row=1)
        skip_btn.callback = self.on_skip
        self.add_item(skip_btn)
        
        # ヘルプボタン
        help_btn = discord.ui.Button(label="ヘルプ", style=discord.ButtonStyle.secondary, row=1)
        help_btn.callback = self.show_help
        self.add_item(help_btn)
    
    async def show_help(self, interaction: discord.Interaction):
        """ヘルプ表示"""
        embed = discord.Embed(title="ヘルプ: ログ保存先カテゴリー", color=0x00FF00)
        embed.add_field(name="新規カテゴリー作成", value="「チケットログ」という新しいカテゴリーを自動作成します", inline=False)
        embed.add_field(name="既存カテゴリー選択", value="選択したカテゴリーに終了したチケットを移動します", inline=False)
        embed.add_field(name="スキップ", value="カテゴリー移動せず、その場で終了状態にします", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            if self.select.values[0] == "new":
                # 新規カテゴリー作成
                await interaction.response.defer(ephemeral=True, thinking=False)
                category = await interaction.guild.create_category("チケットログ")
                archive_category_id = category.id
                await self.finalize(interaction, archive_category_id, use_followup=True)
            else:
                archive_category_id = int(self.select.values[0])
                await self.finalize(interaction, archive_category_id, use_followup=False)
        except:
            pass
    
    async def on_skip(self, interaction: discord.Interaction):
        try:
            await self.finalize(interaction, None, use_followup=False)
        except:
            pass
    
    async def finalize(self, interaction, archive_category_id, use_followup=False):
        """最終確認画面を表示"""
        if not use_followup:
            await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            # 設定内容をまとめる
            guild = self.original_interaction.guild
            
            # サポートロール
            if self.support_roles:
                role_names = [guild.get_role(r).name for r in self.support_roles if guild.get_role(r)]
                support_text = ", ".join(role_names)
            else:
                support_text = "なし（管理者のみ）"
            
            # メッセージ関連
            welcome_message = self.text_settings.get("welcome_message")
            panel_title = self.text_settings.get("panel_title") or DEFAULT_PANEL_TITLE
            panel_description = self.text_settings.get("panel_description") or DEFAULT_PANEL_DESCRIPTION
            panel_button_label = self.text_settings.get("panel_button_label") or DEFAULT_PANEL_BUTTON_LABEL
            start_title = self.text_settings.get("start_title") or DEFAULT_START_TITLE
            start_description = self.text_settings.get("start_description") or DEFAULT_START_DESCRIPTION
            
            if welcome_message:
                msg_preview = welcome_message[:50] + "..." if len(welcome_message) > 50 else welcome_message
                message_text = f"カスタム: {msg_preview}"
            else:
                message_text = "デフォルト"
            
            # チケット作成先
            ticket_cat = guild.get_channel(self.category_id)
            ticket_cat_text = ticket_cat.name if ticket_cat else "不明"
            
            # ログ保存先
            if archive_category_id:
                archive_cat = guild.get_channel(archive_category_id)
                archive_cat_text = archive_cat.name if archive_cat else "不明"
            else:
                archive_cat_text = "なし（その場で終了）"
            
            # 確認Embed
            embed = discord.Embed(
                title="設定内容の確認",
                description="以下の内容でチケットシステムを作成します",
                color=0x00FF00)
            embed.add_field(name="サポートロール", value=support_text, inline=False)
            embed.add_field(name="ウェルカムメッセージ", value=message_text, inline=False)
            embed.add_field(
                name="チケットパネル",
                value=f"タイトル: {panel_title}\n説明: {panel_description[:80]}",
                inline=False
            )
            embed.add_field(name="ボタンのラベル", value=panel_button_label, inline=False)
            embed.add_field(
                name="チャット開始メッセージ",
                value=f"タイトル: {start_title}\n説明: {start_description[:80]}",
                inline=False
            )
            embed.add_field(name="チケット作成先", value=ticket_cat_text, inline=False)
            embed.add_field(name="ログ保存先", value=archive_cat_text, inline=False)
            
            system_data = {
                'category_id': self.category_id,
                'archive_category_id': archive_category_id,
                'support_roles': self.support_roles,
                'welcome_message': welcome_message or "サポートスタッフがすぐに対応します。",
                'panel_title': panel_title,
                'panel_description': panel_description,
                'panel_button_label': panel_button_label,
                'start_title': start_title,
                'start_description': start_description
            }
            
            view = TicketFinalConfirm(self.cog, self.original_interaction, system_data)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"完了エラー: {e}")
            await interaction.followup.send("❌ エラー", ephemeral=True)


class TicketFinalConfirm(discord.ui.View):
    """最終確認"""
    def __init__(self, cog, original_interaction, system_data):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.system_data = system_data
        
        # 作成ボタン
        create_btn = discord.ui.Button(label="作成", style=discord.ButtonStyle.green)
        create_btn.callback = self.create_system
        self.add_item(create_btn)
        
        # キャンセルボタン
        cancel_btn = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.red)
        cancel_btn.callback = self.cancel
        self.add_item(cancel_btn)
    
    async def create_system(self, interaction: discord.Interaction):
        """システム作成"""
        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            panel_title = self.system_data.get('panel_title') or DEFAULT_PANEL_TITLE
            panel_description = self.system_data.get('panel_description') or DEFAULT_PANEL_DESCRIPTION
            embed = discord.Embed(title=panel_title, description=panel_description, color=0x5865F2)
            view = TicketButtonView(self.cog, self.system_data)
            message = await self.original_interaction.channel.send(embed=embed, view=view)
            
            guild_id = self.original_interaction.guild.id
            if guild_id not in self.cog.ticket_systems:
                self.cog.ticket_systems[guild_id] = {}
            self.cog.ticket_systems[guild_id][message.id] = self.system_data
            self.cog.save_system(guild_id, message.id)
            
            await interaction.followup.send("チケットシステムを作成しました", ephemeral=True)
        except Exception as e:
            logger.error(f"作成エラー: {e}")
            await interaction.followup.send("エラーが発生しました", ephemeral=True)
    
    async def cancel(self, interaction: discord.Interaction):
        """キャンセル"""
        await interaction.response.send_message("キャンセルしました", ephemeral=True)
        self.stop()


# ============================================================
# チケットボタン・操作View
# ============================================================
class TicketButtonView(discord.ui.View):
    """チャット開始ボタン"""
    def __init__(self, cog, system_data):
        super().__init__(timeout=None)
        self.cog = cog
        self.system_data = system_data
        label = system_data.get('panel_button_label') or DEFAULT_PANEL_BUTTON_LABEL
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id="create_ticket_button")
        button.callback = self.create_ticket
        self.add_item(button)
    
    async def create_ticket(self, interaction: discord.Interaction):
        try:
            for channel_id, data in self.cog.active_tickets.items():
                if data['owner_id'] == interaction.user.id and data['guild_id'] == interaction.guild.id:
                    if not data.get('is_closed', False):
                        channel = interaction.guild.get_channel(channel_id)
                        if channel:
                            await interaction.response.send_message(
                                f"既にアクティブなチケットがあります: {channel.mention}", ephemeral=True
                            )
                            return
            await interaction.response.send_message("チケットを作成しています...", ephemeral=True)
            asyncio.create_task(self.cog.create_ticket(interaction.user, interaction.channel, self.system_data))
        except Exception as e:
            logger.error(f"チケット作成開始エラー: {e}")


class TicketControlView(discord.ui.View):
    """チケット操作"""
    def __init__(self, ticket_channel, owner, cog):
        super().__init__(timeout=None)
        self.ticket_channel = ticket_channel
        self.owner = owner
        self.cog = cog
    
    def has_permission(self, interaction):
        if interaction.user.id == self.owner.id or interaction.user.guild_permissions.administrator:
            return True
        data = self.cog.active_tickets.get(self.ticket_channel.id, {})
        support_roles = data.get('system_data', {}).get('support_roles', [])
        return any(role.id in support_roles for role in interaction.user.roles)
    
    @discord.ui.button(label="🔒 終了", style=discord.ButtonStyle.secondary, custom_id="close_ticket_button")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not self.has_permission(interaction):
                await interaction.response.send_message("❌ 権限なし", ephemeral=True)
                return
            data = self.cog.active_tickets.get(self.ticket_channel.id, {})
            if data.get('is_closed', False):
                await interaction.response.send_message("❌ 既に終了", ephemeral=True)
                return
            await interaction.response.send_message("✅ 終了しました", ephemeral=True)
            asyncio.create_task(self.cog.close_ticket(self.ticket_channel, interaction.user, save_log=True))
        except:
            pass
    
    @discord.ui.button(label="🔓 再開", style=discord.ButtonStyle.success, custom_id="reopen_ticket_button")
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not self.has_permission(interaction):
                await interaction.response.send_message("❌ 権限なし", ephemeral=True)
                return
            data = self.cog.active_tickets.get(self.ticket_channel.id, {})
            if not data.get('is_closed', False):
                await interaction.response.send_message("❌ 既にアクティブ", ephemeral=True)
                return
            await interaction.response.send_message("✅ 再開しました", ephemeral=True)
            asyncio.create_task(self.cog.reopen_ticket(self.ticket_channel, interaction.user))
        except:
            pass
    
    @discord.ui.button(label="🗑️ 削除", style=discord.ButtonStyle.danger, custom_id="delete_ticket_button")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not self.has_permission(interaction):
                await interaction.response.send_message("❌ 権限なし", ephemeral=True)
                return
            await interaction.response.send_message("✅ 削除します", ephemeral=True)
            asyncio.create_task(self.cog.close_ticket(self.ticket_channel, interaction.user, save_log=False))
        except:
            pass


async def setup(bot):
    await bot.add_cog(TicketManager(bot))

