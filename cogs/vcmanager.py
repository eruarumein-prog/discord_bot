import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, List, Tuple
from dataclasses import dataclass
import asyncio
import sys
import os
import logging
import traceback
import math
from datetime import datetime, timedelta
from discord.errors import HTTPException, RateLimited, NotFound

# ロガー設定
logger = logging.getLogger('vcmanager')
logger.setLevel(logging.INFO)

# 親ディレクトリのdatabase.pyをインポート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import Database


async def send_interaction_error(interaction: discord.Interaction, message: str = "エラーが発生しました。もう一度お試しください。"):
    """安全にエラーを利用者へ通知"""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as send_err:
        logger.error(f"エラー通知に失敗しました: {send_err}")

class VCType:
    """VCのタイプ定数"""
    NO_LIMIT = "人数指定なし"
    WITH_LIMIT = "人数指定"

class VCOption:
    """VCオプション"""
    TEXT_CHANNEL = "参加者専用チャット"
    NO_CONTROL = "操作パネルなし"
    HIDE_FULL = "満員時に非表示"
    LOCK_NAME = "名前変更制限"
    NO_STATE_CONTROL = "状態操作なし"  # ロック、非表示、人数制限の操作を消す
    NO_JOIN_LEAVE_LOG = "入退室ログなし"  # 入退室ログを表示しない
    NO_OWNERSHIP_TRANSFER = "管理者譲渡なし"  # 管理者譲渡機能を無効化
    DELAY_DELETE = "時間指定で削除"

DELETE_DELAY_CHOICES: List[Tuple[int, str]] = [
    (15, "15分"),
    (30, "30分"),
    (60, "1時間"),
    (180, "3時間"),
    (720, "12時間"),
    (1440, "24時間"),
]

class VCLocationMode:
    """VC作成場所モード"""
    AUTO_CATEGORY = "カテゴリー自動作成"
    SAME_CATEGORY = "指定カテゴリー内"
    UNDER_HUB = "ハブVCの下"

async def retry_on_rate_limit(coro, max_retries=5):
    """レート制限時に自動リトライする"""
    for attempt in range(max_retries):
        try:
            return await coro
        except RateLimited as e:
            if attempt < max_retries - 1:
                wait_time = e.retry_after
                print(f"レート制限: {wait_time}秒待機中... (試行 {attempt + 1}/{max_retries})")
                await asyncio.sleep(wait_time)
            else:
                print("レート制限: 最大リトライ回数に達しました")
                raise
        except HTTPException as e:
            if e.status == 429:  # Too Many Requests
                if attempt < max_retries - 1:
                    wait_time = 5
                    print(f"レート制限検出: {wait_time}秒待機中...")
                    await asyncio.sleep(wait_time)
                else:
                    raise
            else:
                raise

class VCManager(commands.Cog):
    """VC自動管理システム"""
    
    def __init__(self, bot):
        self.bot = bot
        # {guild_id: {category_id: {'hub_vc_id': id, 'vc_type': type, 'user_limit': int, 'allowed_roles': [], 'location_mode': str, 'target_category_id': id}}}
        self.vc_systems = {}
        # {vc_id: {'original_limit': int, 'bot_count': int, 'text_channel_id': id}}
        self.active_vcs = {}
        # データベース
        self.db = Database()
        # 排他制御用ロック
        self.vc_creation_locks = {}  # {user_id: asyncio.Lock}
        self.db_lock = asyncio.Lock()  # データベース書き込み用
        self.delayed_delete_tasks: dict[int, asyncio.Task] = {}
        # Bot起動時にデータを復元
        self.bot.loop.create_task(self.restore_from_database())
    
    async def restore_from_database(self):
        """データベースからVCシステムとアクティブVCを復元"""
        await self.bot.wait_until_ready()
        
        # VCシステムを復元
        systems = self.db.get_vc_systems()
        for system in systems:
            guild = self.bot.get_guild(system['guild_id'])
            if not guild:
                continue
            
            # ハブVCがまだ存在するか確認
            hub_vc = guild.get_channel(system['hub_vc_id'])
            if not hub_vc:
                # 存在しない場合はDBから削除
                self.db.delete_vc_system_by_hub(system['hub_vc_id'])
                continue
            
            # メモリに復元
            guild_id = system['guild_id']
            category_id = system['category_id']
            
            if guild_id not in self.vc_systems:
                self.vc_systems[guild_id] = {}
            
            storage_key = category_id if category_id else system['hub_vc_id']
            self.vc_systems[guild_id][storage_key] = {
                'hub_vc_id': system['hub_vc_id'],
                'vc_type': system['vc_type'],
                'user_limit': system['user_limit'],
                'hub_roles': system.get('allowed_roles', []),
                'vc_roles': system.get('vc_roles', []),
                'hidden_roles': system.get('hidden_roles', []),
                'location_mode': system['location_mode'],
                'target_category_id': system['target_category_id'],
                'options': system.get('options', []),
                'locked_name': system.get('locked_name'),
                'notify_enabled': system.get('notify_enabled', False),
                'notify_channel_id': system.get('notify_channel_id'),
                'notify_role_id': system.get('notify_role_id'),
                'control_category_id': system.get('control_category_id'),
                'delete_delay_minutes': system.get('delete_delay_minutes'),
                'name_counter': {}
            }
        
        # アクティブVCを復元
        active_vcs = self.db.get_active_vcs()
        for vc_id, data in active_vcs.items():
            # VCがまだ存在するか確認
            found = False
            for guild in self.bot.guilds:
                vc = guild.get_channel(vc_id)
                if vc:
                    self.active_vcs[vc_id] = data
                    self._restore_delayed_delete_task(vc_id)
                    found = True
                    break
            
            if not found:
                # 存在しない場合はDBから削除
                self.db.delete_active_vc(vc_id)
        
        print(f"VCシステムを復元しました: {len(self.vc_systems)} ギルド")
        print(f"アクティブVCを復元しました: {len(self.active_vcs)} チャンネル")

    
    @app_commands.command(name="vc", description="VC管理システムを作成します")
    @app_commands.default_permissions(administrator=True)
    async def vc_create(self, interaction: discord.Interaction):
        """VC作成コマンド（管理者のみ）"""
        try:
            embed = discord.Embed(
                title="🎭 VC管理システム セットアップ",
                description="**ステップ 1/9: 人数制限の設定**\n\n作成されるVCに人数制限を付けるか選択してください。",
                color=0x5865F2)
            view = VCStep1_Type(self, interaction)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"VCコマンドエラー: {e}")
            await interaction.response.send_message("❌ エラー", ephemeral=True)
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """VC参加・退出時の処理"""
        # VC移動の検出（before.channelとafter.channelが両方存在し、異なる場合）
        is_move = before.channel and after.channel and before.channel != after.channel
        
        # VC参加時の処理
        if after.channel and after.channel != before.channel:
            await self.handle_vc_join(member, after.channel)
        
        # VC退出時の処理
        if before.channel and before.channel != after.channel:
            await self.handle_vc_leave(member, before.channel)
        
        # 移動時に元のVCが空になったかチェック（移動先がハブVCの場合も含む）
        # handle_vc_leave内の削除チェックと重複しないように、移動時のみ追加チェック
        if is_move and before.channel.id in self.active_vcs:
            # 短い遅延を入れて、他のイベント処理が完了してからチェック
            await asyncio.sleep(0.1)
            # 元のVCがまだ存在し、BOT以外のメンバーが0人になったら削除
            if before.channel.id in self.active_vcs:
                non_bot_members = [m for m in before.channel.members if not m.bot]
                if len(non_bot_members) == 0:
                    if self._can_delete_channel_now(before.channel):
                        logger.info(f"移動によりVCが空になったため削除します: {before.channel.name} (ID: {before.channel.id})")
                        await self.delete_user_vc(before.channel)
    
    async def handle_vc_join(self, member: discord.Member, channel: discord.VoiceChannel):
        """VC参加時の処理"""
        guild_id = member.guild.id
        
        # ハブVCへの参加をチェック
        if guild_id in self.vc_systems:
            for category_id, system_data in self.vc_systems[guild_id].items():
                if channel.id == system_data['hub_vc_id']:
                    # 新しいVCを作成してユーザーを移動
                    hub_vc = member.guild.get_channel(system_data['hub_vc_id'])
                    await self.create_and_move_user(member, hub_vc, system_data)
                    return
        
        # 既存のVCへのBOT参加をチェック
        if channel.id in self.active_vcs and member.bot:
            await self.handle_bot_join(channel)
        
        # 既存のVCへのユーザー参加をログに記録（初回作成時は除く）
        if channel.id in self.active_vcs and not member.bot:
            # 初回参加ログをスキップするフラグをチェック
            if self.active_vcs[channel.id].get('skip_first_join_log'):
                # フラグをクリア
                self.active_vcs[channel.id]['skip_first_join_log'] = False
            else:
                # ログを出力
                await self.log_vc_join(channel, member)
            
            # テキストチャンネルの権限を更新
            await self.update_text_channel_permissions(channel, member, joined=True)
            
            # 満員で非表示タイプの場合、満員チェック
            await self.check_and_hide_if_full(channel)
    
    async def handle_vc_leave(self, member: discord.Member, channel: discord.VoiceChannel):
        """VC退出時の処理"""
        # BOT退出時の人数制限調整
        if channel.id in self.active_vcs and member.bot:
            await self.handle_bot_leave(channel)
        
        # ユーザー退出をログに記録
        if channel.id in self.active_vcs and not member.bot:
            await self.log_vc_leave(channel, member)
            # テキストチャンネルの権限を更新
            await self.update_text_channel_permissions(channel, member, joined=False)
            
            # 作成者が退出した場合、権限を引き継ぐ
            if member.id == self.active_vcs[channel.id]['owner_id']:
                await self.transfer_ownership_on_leave(channel, member)
            
            # 満員で非表示タイプの場合、再表示チェック
            await self.check_and_show_if_not_full(channel)
        
        # 全員退出チェック（BOT以外が0人）
        if channel.id in self.active_vcs:
            non_bot_members = [m for m in channel.members if not m.bot]
            if len(non_bot_members) == 0:
                if self._can_delete_channel_now(channel):
                    await self.delete_user_vc(channel)
    
    async def create_and_move_user(self, member: discord.Member, hub_vc: discord.VoiceChannel, system_data: dict):
        """新しいVCを作成してユーザーを移動"""
        # ユーザーごとのロックを取得または作成
        if member.id not in self.vc_creation_locks:
            self.vc_creation_locks[member.id] = asyncio.Lock()
        
        # 排他制御: 同じユーザーが同時にVC作成できないようにする
        async with self.vc_creation_locks[member.id]:
            try:
                await self._create_and_move_user_impl(member, hub_vc, system_data)
            except Exception as e:
                logger.error(f"VC作成エラー (ユーザー: {member.name}, ID: {member.id}): {e}")
                logger.error(traceback.format_exc())
                # エラーが発生してもクラッシュしない
            finally:
                # ロックのクリーンアップ（メモリリーク防止）
                # 処理完了後、一定時間経過したらロックを削除
                asyncio.create_task(self._cleanup_lock(member.id))
    
    async def _cleanup_lock(self, user_id: int):
        """ロックをクリーンアップ（60秒後に削除）"""
        await asyncio.sleep(60)
        if user_id in self.vc_creation_locks:
            # ロックが使用中でなければ削除
            if not self.vc_creation_locks[user_id].locked():
                del self.vc_creation_locks[user_id]
                logger.debug(f"ロッククリーンアップ (ユーザーID: {user_id})")

    def _parse_delete_ready_at(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _can_delete_channel_now(self, channel: discord.VoiceChannel) -> bool:
        vc_data = self.active_vcs.get(channel.id)
        if not vc_data:
            return True
        delay_minutes = vc_data.get('delete_delay_minutes')
        if not delay_minutes:
            return True
        ready_at = self._parse_delete_ready_at(vc_data.get('delete_ready_at'))
        if not ready_at:
            return True
        return datetime.utcnow() >= ready_at

    def _schedule_delayed_delete_task(self, vc_id: int):
        if vc_id in self.delayed_delete_tasks:
            task = self.delayed_delete_tasks.pop(vc_id)
            if task and not task.done():
                task.cancel()
        task = self.bot.loop.create_task(self._delayed_delete_worker(vc_id))
        self.delayed_delete_tasks[vc_id] = task

    def _restore_delayed_delete_task(self, vc_id: int):
        vc_data = self.active_vcs.get(vc_id)
        if not vc_data:
            return
        if not vc_data.get('delete_ready_at'):
            return
        self._schedule_delayed_delete_task(vc_id)

    def _cancel_delayed_delete_task(self, vc_id: int):
        task = self.delayed_delete_tasks.pop(vc_id, None)
        if task and not task.done():
            # 自分自身（実行中のタスク）をキャンセルすると削除処理が中断するので避ける
            current = asyncio.current_task()
            if task is not current:
                task.cancel()

    async def _delayed_delete_worker(self, vc_id: int):
        try:
            vc_data = self.active_vcs.get(vc_id)
            if not vc_data:
                return
            ready_at = self._parse_delete_ready_at(vc_data.get('delete_ready_at'))
            if not ready_at:
                return
            delay = (ready_at - datetime.utcnow()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            vc = self.bot.get_channel(vc_id)
            if not isinstance(vc, discord.VoiceChannel):
                return
            non_bot_members = [m for m in vc.members if not m.bot]
            if non_bot_members:
                # 削除猶予は経過しているので以降は通常の空チェックで削除される
                return
            await self.delete_user_vc(vc)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"遅延削除タスクエラー (VC ID: {vc_id}): {e}")
        finally:
            if vc_id in self.delayed_delete_tasks:
                self.delayed_delete_tasks.pop(vc_id, None)
    
    def _channel_exists(self, channel: discord.abc.Connectable) -> bool:
        """チャンネルがまだ存在するかを確認"""
        guild = getattr(channel, "guild", None)
        return guild is not None and guild.get_channel(channel.id) is not None
    
    async def _safe_channel_send(self, channel: discord.abc.Messageable, *args, **kwargs):
        """チャンネルの存在を確認しつつメッセージを送信"""
        if not self._channel_exists(channel):
            logger.warning(f"メッセージ送信先が見つからないため送信をスキップしました (Channel ID: {getattr(channel, 'id', 'unknown')})")
            return None
        try:
            return await channel.send(*args, **kwargs)
        except NotFound:
            logger.warning(f"メッセージ送信先が削除されているため送信できませんでした (Channel ID: {channel.id})")
        except HTTPException as e:
            logger.warning(f"メッセージ送信に失敗しました (Channel ID: {channel.id}): {e}")
        return None
    
    async def _create_and_move_user_impl(self, member: discord.Member, hub_vc: discord.VoiceChannel, system_data: dict):
        """新しいVCを作成してユーザーを移動"""
        vc_type = system_data['vc_type']
        user_limit = system_data.get('user_limit', 0)
        location_mode = system_data.get('location_mode', VCLocationMode.AUTO_CATEGORY)
        options = system_data.get('options', [])
        locked_name = system_data.get('locked_name')
        
        # チャンネル名を決定
        if VCOption.LOCK_NAME in options and locked_name is not None:
            # 名前変更制限オプション：固定名を使用
            if locked_name == "":
                # 空白の場合は初期名（スクリーンネーム・VC）を固定
                base_name = f"{member.name}・VC"
            else:
                # 入力された名前を使用
                base_name = locked_name
            
            # カテゴリーIDを取得（作成先のカテゴリー）
            target_category_id = None
            location_mode = system_data.get('location_mode')
            if location_mode == VCLocationMode.AUTO_CATEGORY or location_mode == VCLocationMode.SAME_CATEGORY:
                target_category_id = system_data.get('target_category_id')
            elif hub_vc.category:
                target_category_id = hub_vc.category.id
            
            # 重複チェックと番号付け（カテゴリーごとに最小の空き番号を使用）
            # 既存のVCから使用中の番号を取得（同じカテゴリー内のみ）
            existing_numbers = set()
            for vc_id, vc_data in self.active_vcs.items():
                if (vc_data.get('base_name') == base_name and 
                    vc_data.get('category_id') == target_category_id):
                    existing_numbers.add(vc_data.get('name_number', 1))
            
            # 最小の空き番号を探す
            number = 1
            while number in existing_numbers:
                number += 1
            
            # 常に番号付き
            channel_name = f"{base_name}-{number}"
            
            # VCデータに保存するための情報
            name_number = number
            name_base = base_name
        else:
            # 通常：スクリーンネーム・VC
            channel_name = f"{member.name}・VC"
            name_number = None
            name_base = None
        
        # 権限設定
        guild = member.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        }
        
        # 閲覧可能ロールの設定（指定したロールのみ閲覧可能）
        # 注意: この設定を先に行い、VC参加権限で上書きする
        if system_data.get('hidden_roles', []):
            # 全員の閲覧を拒否（Botは除く）
            overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False, connect=False)
            # Botは必ず見える
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            # 指定ロールのみ閲覧を許可
            for role_id in system_data['hidden_roles']:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        
        # VC用ロール指定がある場合（閲覧可能ロールの後に設定）
        if system_data.get('vc_roles', []):
            # 閲覧可能ロールが設定されている場合は、view_channelは維持してconnectのみ制御
            if system_data.get('hidden_roles', []):
                # 閲覧可能ロールを持つ人の中で、VC参加権限を持つ人だけが入れる
                for role_id in system_data['vc_roles']:
                    role = guild.get_role(role_id)
                    if role:
                        # 既存の権限を取得して、connectのみ変更
                        existing = overwrites.get(role, discord.PermissionOverwrite())
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=existing.view_channel if existing.view_channel is not None else True,
                            connect=True
                        )
            else:
                # 閲覧可能ロールがない場合は通常通り
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=True, connect=False)
                for role_id in system_data['vc_roles']:
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        
        # 人数制限を設定（人数指定タイプの場合のみ）
        vc_user_limit = system_data.get('user_limit', 0) if system_data.get('vc_type') == VCType.WITH_LIMIT else 0
        
        # 作成場所モードに応じてVCを作成（レート制限対策付き）
        category = None
        if location_mode == VCLocationMode.AUTO_CATEGORY:
            # カテゴリー自動作成モード
            target_category_id = system_data.get('target_category_id')
            category = guild.get_channel(target_category_id)
            new_vc = await retry_on_rate_limit(
                category.create_voice_channel(
                    name=channel_name,
                    user_limit=vc_user_limit,
                    overwrites=overwrites
                )
            )
        elif location_mode == VCLocationMode.SAME_CATEGORY:
            # 指定カテゴリー内モード
            target_category_id = system_data.get('target_category_id')
            category = guild.get_channel(target_category_id)
            new_vc = await retry_on_rate_limit(
                category.create_voice_channel(
                    name=channel_name,
                    user_limit=vc_user_limit,
                    overwrites=overwrites
                )
            )
        else:  # UNDER_HUB
            # ハブVCの下モード
            if hub_vc.category:
                category = hub_vc.category
                new_vc = await retry_on_rate_limit(
                    hub_vc.category.create_voice_channel(
                        name=channel_name,
                        user_limit=vc_user_limit,
                        overwrites=overwrites,
                        position=hub_vc.position + 1
                    )
                )
            else:
                # カテゴリーがない場合はハブVCの下に作成
                new_vc = await retry_on_rate_limit(
                    guild.create_voice_channel(
                        name=channel_name,
                        user_limit=vc_user_limit,
                        overwrites=overwrites,
                        position=hub_vc.position + 1
                    )
                )
        
        # オプションの取得
        options = system_data.get('options', [])
        has_control = VCOption.NO_CONTROL not in options
        has_text = VCOption.TEXT_CHANNEL in options
        has_hide_full = VCOption.HIDE_FULL in options
        
        # 操作パネルありの場合のみブロックリストを適用
        banned_users = []
        if has_control:
            # データベースからブロックリストを読み込み
            banned_users = self.db.get_banned_users(member.id)
            
            # ブロックユーザーに対して接続権限を拒否
            if banned_users:
                for banned_user_id in banned_users:
                    banned_user = guild.get_member(banned_user_id)
                    if banned_user:
                        overwrites[banned_user] = discord.PermissionOverwrite(connect=False)
                
                # VCを再編集して権限を適用
                await new_vc.edit(overwrites=overwrites)
        
        # VCデータを保存（初回参加ログをスキップするフラグ付き）
        self.active_vcs[new_vc.id] = {
            'original_limit': 0,
            'original_name': channel_name,
            'bot_count': 0,
            'text_channel_id': None,
            'control_channel_id': None,
            'control_message_id': None,
            'vc_type': vc_type,
            'category_id': category.id if category else None,
            'owner_id': member.id,
            'banned_users': banned_users,
            'is_locked': False,
            'allowed_users': [],
            'view_allowed_users': [],
            'skip_first_join_log': True,
            'options': options,
            'name_locked': VCOption.LOCK_NAME in options,
            'base_name': name_base,
            'name_number': name_number,
            'system_data': system_data  # システムデータへの参照を保存
        }

        delete_delay_minutes = system_data.get('delete_delay_minutes')
        if delete_delay_minutes:
            ready_at = datetime.utcnow() + timedelta(minutes=delete_delay_minutes)
            self.active_vcs[new_vc.id]['delete_delay_minutes'] = delete_delay_minutes
            self.active_vcs[new_vc.id]['delete_ready_at'] = ready_at.isoformat()
            self._schedule_delayed_delete_task(new_vc.id)
        
        if not self._channel_exists(new_vc):
            logger.warning(f"作成したVCが既に存在しません (VC ID: {new_vc.id})。セットアップを中断します。")
            self.active_vcs.pop(new_vc.id, None)
            return
        
        # テキストチャンネル作成（参加者専用チャットオプションの場合）
        if has_text:
            text_channel = await retry_on_rate_limit(
                self.create_text_channel_for_vc(new_vc, member, guild)
            )
            self.active_vcs[new_vc.id]['text_channel_id'] = text_channel.id
        
        # ユーザーを移動
        try:
            await member.move_to(new_vc)
        except NotFound:
            logger.warning(f"ユーザーを移動する前にVCが削除されたため処理を中断しました (VC ID: {new_vc.id})")
            self.active_vcs.pop(new_vc.id, None)
            return
        except discord.HTTPException as e:
            logger.warning(f"ユーザー移動エラー (User: {member.name}, VC: {new_vc.name}): {e}")
        
        # 操作パネルありの場合は、操作チャンネルと操作パネルを作成
        if has_control:
            # 作成者専用の操作チャンネルを作成
            control_category_id = system_data.get('control_category_id')
            target_category = None
            if control_category_id:
                target_category = guild.get_channel(control_category_id)
                if not isinstance(target_category, discord.CategoryChannel):
                    target_category = None
            
            control_channel = await retry_on_rate_limit(
                self.create_control_channel_for_vc(new_vc, member, guild, target_category)
            )
            self.active_vcs[new_vc.id]['control_channel_id'] = control_channel.id
            
            # 操作パネルを作成して送信
            await self.send_control_panel(new_vc, control_channel, member)
        
        # VC作成通知を送信
        await self.send_creation_notification(new_vc, member, system_data)
        
        # 名前変更制限がない場合のみ、VC名変更案内を送信（操作パネルの有無に関わらず）
        if VCOption.LOCK_NAME not in options:
            if not self._channel_exists(new_vc):
                logger.warning(f"VCが削除されたため名前変更案内の送信をスキップしました (VC ID: {new_vc.id})")
                self.active_vcs.pop(new_vc.id, None)
                return
            embed = discord.Embed(
                title="VC名を変更して何をしているか伝えよう",
                description="下のボタンから入力してください",
                color=discord.Color.blue()
            )
            view = VCNameQuickEditView(new_vc, member, self)
            msg = await self._safe_channel_send(new_vc, embed=embed, view=view)
            
            # メッセージIDを保存（後で削除するため）
            if msg:
                self.active_vcs[new_vc.id]['name_edit_message_id'] = msg.id
        
        # データベースに保存（排他制御）
        async with self.db_lock:
            try:
                self.db.save_active_vc(new_vc.id, self.active_vcs[new_vc.id])
            except Exception as e:
                logger.error(f"❌ データベース保存エラー (VC ID: {new_vc.id}): {e}")
    
    async def create_text_channel_for_vc(self, vc: discord.VoiceChannel, owner: discord.Member, guild: discord.Guild):
        """VCに紐づくテキストチャンネルを作成"""
        # VC参加者全員が閲覧可能な権限設定
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        # 現在のVC参加者に権限を付与
        for member in vc.members:
            if not member.bot:
                overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        
        # カテゴリー内に作成（カテゴリーがない場合は直下）
        if vc.category:
            text_channel = await vc.category.create_text_channel(
                name=f"{vc.name}",
                overwrites=overwrites
            )
        else:
            text_channel = await guild.create_text_channel(
                name=f"{vc.name}",
                overwrites=overwrites,
                position=vc.position + 1
            )
        
        return text_channel
    
    async def create_control_channel_for_vc(self, vc: discord.VoiceChannel, owner: discord.Member, guild: discord.Guild, target_category: Optional[discord.CategoryChannel] = None):
        """VC操作用のチャンネルを作成"""
        # 作成者のみが閲覧可能な権限設定
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            owner: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        # カテゴリー内に作成（指定カテゴリー、VCのカテゴリー、または直下）
        if target_category:
            control_channel = await target_category.create_text_channel(
                name=f"control-{vc.name}",
                overwrites=overwrites
            )
        elif vc.category:
            control_channel = await vc.category.create_text_channel(
                name=f"control-{vc.name}",
                overwrites=overwrites
            )
        else:
            control_channel = await guild.create_text_channel(
                name=f"control-{vc.name}",
                overwrites=overwrites,
                position=vc.position + 1
            )
        
        return control_channel
    
    async def send_creation_notification(self, new_vc: discord.VoiceChannel, owner: discord.Member, system_data: dict):
        """VC作成開始通知を送信"""
        if not system_data.get('notify_enabled'):
            logger.debug(f"通知が無効のため通知をスキップしました (VC ID: {new_vc.id})")
            return

        notify_channel_id = system_data.get('notify_channel_id')
        if not notify_channel_id:
            logger.warning(f"通知が有効ですが通知チャンネルが設定されていません (VC ID: {new_vc.id})")
            return

        notify_channel = new_vc.guild.get_channel(notify_channel_id)
        if not notify_channel or not isinstance(notify_channel, discord.TextChannel):
            logger.warning(f"通知チャンネルが見つからないかテキストチャンネルではありません: {notify_channel_id}")
            return

        mention_role = None
        mention_role_id = system_data.get('notify_role_id')
        if mention_role_id:
            mention_role = new_vc.guild.get_role(mention_role_id)
            if not mention_role:
                logger.warning(f"メンションロールが見つかりません: {mention_role_id}")

        # シンプルな通知Embed（アイコン + "{ユーザー名}がvcを開始しました"を横並び）
        embed = discord.Embed(color=0x5865F2)
        embed.set_author(
            name=f"{owner.display_name}がvcを開始しました",
            icon_url=owner.display_avatar.url
        )
        embed.description = new_vc.mention

        # VC参加用のリンクボタン
        view = discord.ui.View()
        url = f"https://discord.com/channels/{new_vc.guild.id}/{new_vc.id}"
        view.add_item(discord.ui.Button(label="vcに参加", style=discord.ButtonStyle.link, url=url))

        content = mention_role.mention if mention_role else None
        result = await self._safe_channel_send(notify_channel, content=content, embed=embed, view=view)
        if result:
            logger.info(f"✅ VC作成通知を送信しました (VC: {new_vc.name}, チャンネル: {notify_channel.name})")
        else:
            logger.warning(f"⚠️ VC作成通知の送信に失敗しました (VC: {new_vc.name}, チャンネル: {notify_channel.name})")
    
    async def send_control_panel(self, vc: discord.VoiceChannel, control_channel: discord.TextChannel, owner: discord.Member):
        """操作パネルを送信"""
        if vc.id not in self.active_vcs:
            return
        if not self._channel_exists(vc):
            logger.warning(f"VCが存在しないため操作パネル送信をスキップしました (VC ID: {vc.id})")
            return
        if not self._channel_exists(control_channel):
            logger.warning(f"操作チャンネルが存在しないため操作パネル送信をスキップしました (Channel ID: {control_channel.id})")
            return
        
        # メンション
        await self._safe_channel_send(control_channel, content=f"{owner.mention} VC操作パネルが作成されました")
        
        # オプションを取得
        vc_options = self.active_vcs[vc.id].get('options', [])
        no_state_control = VCOption.NO_STATE_CONTROL in vc_options
        
        # 状態操作（状態操作なしオプションが無効の場合のみ表示）
        msg1 = None
        if not no_state_control:
            embed1 = discord.Embed(
                title="状態操作",
                description="```\n通話の公開設定やセキュリティを管理\n```",
                color=0x5865F2
            )
            view1 = VCStateControlView(vc, owner, self)
            msg1 = await self._safe_channel_send(control_channel, embed=embed1, view=view1)
        
        # 参加制限
        embed2 = discord.Embed(
            title="参加制限",
            description="```\n特定のユーザーをブロック\nブロックリストは次回VC作成時も引き継がれます\n```",
            color=0xED4245
        )
        view2 = VCBanControlView(vc, owner, self)
        msg2 = await self._safe_channel_send(control_channel, embed=embed2, view=view2)
        
        # 人数制限（人数指定タイプでない場合、かつ状態操作なしオプションが無効の場合のみ表示）
        msg3 = None
        vc_type = self.active_vcs[vc.id].get('vc_type', VCType.NO_LIMIT)
        if vc_type != VCType.WITH_LIMIT and not no_state_control:
            embed3 = discord.Embed(
                title="人数制限",
                description="```\n参加可能な人数を設定\n```",
                color=0x57F287
            )
            view3 = VCLimitControlView(vc, owner, self)
            msg3 = await self._safe_channel_send(control_channel, embed=embed3, view=view3)
        
        # 名前変更（名前ロックされていない場合のみ表示）
        msg4 = None
        if vc.id in self.active_vcs and not self.active_vcs[vc.id].get('name_locked', False):
            embed4 = discord.Embed(
                title="チャンネル名",
                description="```\nVCチャンネルの名前を編集\n```",
                color=0xEB459E
            )
            view4 = VCNameControlView(vc, owner, self)
            msg4 = await self._safe_channel_send(control_channel, embed=embed4, view=view4)
        
        # 権限譲渡（管理者譲渡なしオプションが無効の場合のみ表示）
        msg5 = None
        no_ownership_transfer = VCOption.NO_OWNERSHIP_TRANSFER in vc_options
        if not no_ownership_transfer:
            embed5 = discord.Embed(
                title="管理権限の譲渡",
                description="```\n他のユーザーに管理者を変更\n```",
                color=0xFEE75C
            )
            view5 = VCOwnershipTransferView(vc, owner, self)
            msg5 = await self._safe_channel_send(control_channel, embed=embed5, view=view5)
        
        # 操作パネルメッセージIDを保存
        if vc.id in self.active_vcs:
            message_ids = []
            if msg1:
                message_ids.append(msg1.id)
            if msg2:
                message_ids.append(msg2.id)
            if msg3:
                message_ids.append(msg3.id)
            if msg4:
                message_ids.append(msg4.id)
            if msg5:
                message_ids.append(msg5.id)
            self.active_vcs[vc.id]['control_message_id'] = message_ids
    
    async def transfer_ownership_on_leave(self, vc: discord.VoiceChannel, old_owner: discord.Member):
        """作成者退出時の権限引継ぎ"""
        if vc.id not in self.active_vcs:
            return
        
        # 管理者譲渡なしオプションが有効な場合は何もしない
        options = self.active_vcs[vc.id].get('options', [])
        if VCOption.NO_OWNERSHIP_TRANSFER in options:
            logger.info(f"管理者譲渡なしオプションが有効なため、権限引継ぎをスキップします (VC: {vc.name})")
            return
        
        # VC内のBOT以外のメンバーを取得
        non_bot_members = [m for m in vc.members if not m.bot]
        
        if len(non_bot_members) == 0:
            # 誰もいない場合は何もしない（削除処理が実行される）
            return
        
        # 次の管理者（最初に参加した人）
        new_owner = non_bot_members[0]
        
        # オーナーIDを更新
        self.active_vcs[vc.id]['owner_id'] = new_owner.id
        
        # 新しい管理者のブロックリストを読み込み、VCの権限に適用
        new_owner_banned_users = self.db.get_banned_users(new_owner.id)
        self.active_vcs[vc.id]['banned_users'] = new_owner_banned_users
        
        # 現在のVCメンバーを精査し、ブロックユーザーを切断
        for member_in_vc in vc.members:
            if not member_in_vc.bot and member_in_vc.id in new_owner_banned_users:
                try:
                    await member_in_vc.move_to(None)  # VCから切断
                    logger.info(f"✅ ブロックユーザー {member_in_vc.display_name} をVC {vc.name} から切断しました。")
                except discord.HTTPException as e:
                    logger.warning(f"⚠️ ブロックユーザー {member_in_vc.display_name} の切断に失敗しました: {e}")
        
        # VCの権限を更新してブロックリストを反映
        current_overwrites = vc.overwrites
        for banned_user_id in new_owner_banned_users:
            banned_member = vc.guild.get_member(banned_user_id)
            if banned_member:
                current_overwrites[banned_member] = discord.PermissionOverwrite(connect=False)
        
        try:
            await vc.edit(overwrites=current_overwrites)
            logger.info(f"✅ VC {vc.name} の権限を更新し、新しい管理者のブロックリストを適用しました。")
        except discord.HTTPException as e:
            logger.error(f"❌ VC {vc.name} の権限更新に失敗しました: {e}")
        
        # 操作パネルありの場合のみ、操作チャンネルを作り直す
        options = self.active_vcs[vc.id].get('options', [])
        has_control = VCOption.NO_CONTROL not in options
        
        if has_control:
            # 操作チャンネルを削除
            control_channel_id = self.active_vcs[vc.id].get('control_channel_id')
            if control_channel_id:
                control_channel = vc.guild.get_channel(control_channel_id)
                if control_channel:
                    try:
                        await control_channel.delete()
                    except discord.HTTPException as e:
                        logger.warning(f"⚠️ 操作チャンネル削除エラー (ID: {control_channel.id}): {e}")
            
            # 新しい操作チャンネルを作成
            system_data = self.active_vcs[vc.id].get('system_data', {})
            control_category_id = system_data.get('control_category_id')
            target_category = None
            if control_category_id:
                target_category = vc.guild.get_channel(control_category_id)
                if not isinstance(target_category, discord.CategoryChannel):
                    target_category = None
            
            new_control_channel = await self.create_control_channel_for_vc(vc, new_owner, vc.guild, target_category)
            self.active_vcs[vc.id]['control_channel_id'] = new_control_channel.id
            
            # 新しい操作パネルを送信
            await self.send_control_panel(vc, new_control_channel, new_owner)
    
    async def check_and_hide_if_full(self, vc: discord.VoiceChannel):
        """満員の場合、チャンネルを非表示にする"""
        if vc.id not in self.active_vcs:
            return
        
        vc_data = self.active_vcs[vc.id]
        options = vc_data.get('options', [])
        
        # 満員時に非表示オプションがある場合のみ処理
        if VCOption.HIDE_FULL not in options:
            return
        
        # 満員チェック
        if vc.user_limit > 0 and len(vc.members) >= vc.user_limit:
            # 非表示にする
            overwrites = vc.overwrites
            overwrites[vc.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            try:
                await vc.edit(overwrites=overwrites)
            except Exception as e:
                logger.warning(f"⚠️ VC設定エラー (VC ID: {vc.id}): {e}")
    
    async def check_and_show_if_not_full(self, vc: discord.VoiceChannel):
        """満員でなくなった場合、チャンネルを再表示する"""
        if vc.id not in self.active_vcs:
            return
        
        vc_data = self.active_vcs[vc.id]
        options = vc_data.get('options', [])
        
        # 満員時に非表示オプションがある場合のみ処理
        if VCOption.HIDE_FULL not in options:
            return
        
        # 満員でないかチェック
        if vc.user_limit > 0 and len(vc.members) < vc.user_limit:
            # 再表示する
            overwrites = vc.overwrites
            overwrites[vc.guild.default_role] = discord.PermissionOverwrite(view_channel=True)
            try:
                await vc.edit(overwrites=overwrites)
            except Exception as e:
                logger.warning(f"⚠️ VC設定エラー (VC ID: {vc.id}): {e}")
    
    async def update_text_channel_permissions(self, vc: discord.VoiceChannel, member: discord.Member, joined: bool):
        """テキストチャンネルの権限を更新"""
        if vc.id not in self.active_vcs:
            return
        
        text_channel_id = self.active_vcs[vc.id].get('text_channel_id')
        if not text_channel_id:
            return
        
        text_channel = vc.guild.get_channel(text_channel_id)
        if not text_channel:
            return
        
        # 参加時は権限を付与、退出時は権限を削除
        if joined:
            await text_channel.set_permissions(member, read_messages=True, send_messages=True)
        else:
            await text_channel.set_permissions(member, overwrite=None)
    
    async def handle_bot_join(self, channel: discord.VoiceChannel):
        """BOT参加時の人数制限調整"""
        if channel.id not in self.active_vcs:
            return
        
        vc_data = self.active_vcs[channel.id]
        
        # 人数指定タイプのみ処理
        if vc_data.get('vc_type') == VCType.LIMIT:
            vc_data['bot_count'] += 1
            original_limit = vc_data['original_limit']
            new_limit = original_limit + vc_data['bot_count']
            
            if new_limit <= 99:  # Discord の最大制限
                await channel.edit(user_limit=new_limit)
    
    async def handle_bot_leave(self, channel: discord.VoiceChannel):
        """BOT退出時の人数制限調整"""
        if channel.id not in self.active_vcs:
            return
        
        vc_data = self.active_vcs[channel.id]
        
        # 人数指定タイプのみ処理
        if vc_data.get('vc_type') == VCType.LIMIT and vc_data['bot_count'] > 0:
            vc_data['bot_count'] -= 1
            original_limit = vc_data['original_limit']
            new_limit = original_limit + vc_data['bot_count']
            
            await channel.edit(user_limit=new_limit)
    
    async def log_vc_join(self, channel: discord.VoiceChannel, member: discord.Member):
        """VC参加をログに記録"""
        if channel.id not in self.active_vcs:
            return
        
        # 入退室ログなしオプションがある場合はスキップ
        options = self.active_vcs[channel.id].get('options', [])
        if VCOption.NO_JOIN_LEAVE_LOG in options:
            return
        
        embed = discord.Embed(
            title="ユーザーが参加しました",
            color=discord.Color.green()
        )
        embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        
        await channel.send(embed=embed)
    
    async def log_vc_leave(self, channel: discord.VoiceChannel, member: discord.Member):
        """VC退出をログに記録"""
        if channel.id not in self.active_vcs:
            return
        
        # 入退室ログなしオプションがある場合はスキップ
        options = self.active_vcs[channel.id].get('options', [])
        if VCOption.NO_JOIN_LEAVE_LOG in options:
            return
        
        embed = discord.Embed(
            title="ユーザーが退出しました",
            color=discord.Color.red()
        )
        embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        
        await channel.send(embed=embed)
    
    async def delete_user_vc(self, channel: discord.VoiceChannel):
        """ユーザーVCを削除"""
        try:
            if channel.id not in self.active_vcs:
                return
            
            vc_data = self.active_vcs[channel.id]
            
            # テキストチャンネルも削除（存在する場合）
            if vc_data.get('text_channel_id'):
                text_channel = channel.guild.get_channel(vc_data['text_channel_id'])
                if text_channel:
                    try:
                        await text_channel.delete()
                    except discord.HTTPException as e:
                        logger.warning(f"⚠️ テキストチャンネル削除エラー (ID: {text_channel.id}): {e}")
            
            # 操作チャンネルも削除（存在する場合）
            if vc_data.get('control_channel_id'):
                control_channel = channel.guild.get_channel(vc_data['control_channel_id'])
                if control_channel:
                    try:
                        await control_channel.delete()
                    except discord.HTTPException:
                        pass
            
            # VCを削除
            try:
                await channel.delete()
            except discord.HTTPException as e:
                logger.warning(f"⚠️ VCチャンネル削除エラー (ID: {channel.id}): {e}")
            
            # データベースから削除（排他制御）
            async with self.db_lock:
                try:
                    self.db.delete_active_vc(channel.id)
                except Exception as e:
                    logger.error(f"❌ データベース削除エラー (VC ID: {channel.id}): {e}")
            
            # メモリから削除
            del self.active_vcs[channel.id]
            self._cancel_delayed_delete_task(channel.id)
            logger.info(f"✅ VC削除完了 (ID: {channel.id})")
            
        except Exception as e:
            logger.error(f"❌ VC削除処理エラー (ID: {channel.id}): {e}")
            logger.error(traceback.format_exc())
            print(f"❌ VC削除エラー: {e}")
            # エラーでもクラッシュしない
    
    async def create_vc_system(self, guild: discord.Guild, vc_type: str, user_limit: int, hub_role_ids: List[int], vc_role_ids: List[int], hidden_role_ids: List[int], location_mode: str, target_category_id: Optional[int], source_channel, options: List[str], locked_name: Optional[str] = None, control_category_id: Optional[int] = None, notify_enabled: bool = False, notify_channel_id: Optional[int] = None, notify_category_id: Optional[int] = None, notify_role_id: Optional[int] = None, notify_category_new: bool = False, control_category_new: bool = False, delete_delay_minutes: Optional[int] = None):
        """VC管理システムを作成"""
        try:
            logger.info(f"🚀 VC管理システム作成開始 (Guild: {guild.name}, Type: {vc_type})")
            await self._create_vc_system_impl(guild, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids, location_mode, target_category_id, source_channel, options, locked_name, control_category_id, notify_enabled, notify_channel_id, notify_category_id, notify_role_id, notify_category_new, control_category_new, delete_delay_minutes)
            logger.info(f"✅ VC管理システム作成完了 (Guild: {guild.name})")
        except Exception as e:
            logger.error(f"❌ VC管理システム作成エラー (Guild: {guild.name}): {e}")
            logger.error(traceback.format_exc())
            raise
    
    async def _create_vc_system_impl(self, guild: discord.Guild, vc_type: str, user_limit: int, hub_role_ids: List[int], vc_role_ids: List[int], hidden_role_ids: List[int], location_mode: str, target_category_id: Optional[int], source_channel, options: List[str], locked_name: Optional[str] = None, control_category_id: Optional[int] = None, notify_enabled: bool = False, notify_channel_id: Optional[int] = None, notify_category_id: Optional[int] = None, notify_role_id: Optional[int] = None, notify_category_new: bool = False, control_category_new: bool = False):
        """VC管理システムを作成（内部実装）"""
        # source_channelがリストの場合は最初の要素を取得（エラー回避）
        if isinstance(source_channel, list):
            source_channel = source_channel[0] if source_channel else None
        
        # コマンドが実行されたチャンネルのカテゴリーを取得
        target_category = None
        position = None
        
        if source_channel and hasattr(source_channel, 'category') and source_channel.category:
            # チャンネルがカテゴリー内にある場合
            target_category = source_channel.category
        elif source_channel and hasattr(source_channel, 'position'):
            # カテゴリーがない場合、チャンネルの位置を取得
            position = source_channel.position + 1
        
        # VC作成用のカテゴリーを準備
        if location_mode == VCLocationMode.AUTO_CATEGORY:
            # カテゴリー自動作成モード
            user_vc_category = await retry_on_rate_limit(
                guild.create_category(name="VC管理システム")
            )
            vc_target_category_id = user_vc_category.id
        elif location_mode == VCLocationMode.SAME_CATEGORY and target_category_id:
            # 指定カテゴリー内モード
            user_vc_category = guild.get_channel(target_category_id)
            vc_target_category_id = target_category_id
        else:
            # ハブVCの下モード（カテゴリーIDは不要）
            user_vc_category = None
            vc_target_category_id = None
        
        # ハブVCの権限設定
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        }
        
        # 閲覧可能ロールの設定（最初に設定）
        if hidden_role_ids:
            # 全員の閲覧を拒否（Botは除く）
            overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False, connect=False)
            # Botは必ず見える
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            # 指定ロールのみ閲覧を許可
            for role_id in hidden_role_ids:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        
        # ハブ参加権限の設定（閲覧可能ロールの後に設定）
        if hub_role_ids:
            # 閲覧可能ロールが設定されている場合
            if hidden_role_ids:
                # 閲覧可能ロールを持つ人の中で、ハブ参加権限を持つ人だけが入れる
                # 閲覧可能ロールを持たない人は全員接続不可
                for role_id in hub_role_ids:
                    role = guild.get_role(role_id)
                    if role:
                        existing = overwrites.get(role, discord.PermissionOverwrite())
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=existing.view_channel if existing.view_channel is not None else True,
                            connect=True
                        )
            else:
                # 閲覧可能ロールがない場合は通常通り
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=True, connect=False)
                for role_id in hub_role_ids:
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        
        # ハブVCを作成（コマンド実行元のカテゴリーまたはその下）
        if target_category:
            hub_vc = await retry_on_rate_limit(
                target_category.create_voice_channel(
                    name="VCを作成",
                    overwrites=overwrites
                )
            )
        else:
            hub_vc = await retry_on_rate_limit(
                guild.create_voice_channel(
                    name="VCを作成",
                    overwrites=overwrites,
                    position=position
                )
            )
        
        # システムデータを保存（ハブVCのIDをキーとして使用）
        if guild.id not in self.vc_systems:
            self.vc_systems[guild.id] = {}
        
        # カテゴリーIDがない場合はハブVCのIDを使用
        storage_key = vc_target_category_id if vc_target_category_id else hub_vc.id
        final_notify_category_id = notify_category_id

        if notify_enabled and notify_category_new:
            try:
                category = await guild.create_category("VC作成通知")
                final_notify_category_id = category.id
                logger.info(f"🆕 通知カテゴリーを作成: {category.name} (ID: {category.id})")
            except Exception as e:
                logger.error(f"通知カテゴリー作成エラー: {e}")
                final_notify_category_id = None
        
        if control_category_new:
            try:
                category = await guild.create_category("VC操作パネル")
                control_category_id = category.id
                logger.info(f"🆕 操作パネル用カテゴリーを作成: {category.name} (ID: {category.id})")
            except Exception as e:
                logger.error(f"操作パネルカテゴリー作成エラー: {e}")
                control_category_id = None
        self.vc_systems[guild.id][storage_key] = {
            'hub_vc_id': hub_vc.id,
            'vc_type': vc_type,
            'user_limit': user_limit,
            'hub_roles': hub_role_ids,
            'vc_roles': vc_role_ids,
            'hidden_roles': hidden_role_ids if hidden_role_ids else [],
            'location_mode': location_mode,
            'target_category_id': vc_target_category_id,
            'options': options,
            'locked_name': locked_name,
            'control_category_id': control_category_id,
            'delete_delay_minutes': delete_delay_minutes,
            'name_counter': {}
        }
        
        # notify_category_idが設定されている場合は、そのカテゴリー内に通知チャンネルを作成
        final_notify_channel_id = notify_channel_id
        if notify_enabled and final_notify_category_id and not notify_channel_id:
            try:
                category = guild.get_channel(final_notify_category_id)
                if isinstance(category, discord.CategoryChannel):
                    notify_channel = await category.create_text_channel("vc作成通知")
                    final_notify_channel_id = notify_channel.id
                    logger.info(f"📢 通知チャンネル作成: {notify_channel.name} (ID: {notify_channel.id})")
            except Exception as e:
                logger.error(f"❌ 通知チャンネル作成エラー: {e}")
        
        # 通知設定をself.vc_systemsに保存
        self.vc_systems[guild.id][storage_key]['notify_enabled'] = notify_enabled
        self.vc_systems[guild.id][storage_key]['notify_channel_id'] = final_notify_channel_id
        self.vc_systems[guild.id][storage_key]['notify_role_id'] = notify_role_id
        
        # データベースに保存（排他制御）
        async with self.db_lock:
            try:
                self.db.save_vc_system(
                    guild.id,
                    vc_target_category_id,
                    hub_vc.id,
                    vc_type,
                    user_limit,
                    hub_role_ids,
                    vc_role_ids,
                    hidden_role_ids,
                    location_mode,
                    vc_target_category_id,
                    options,
                    locked_name,
                    notify_enabled=notify_enabled,
                    notify_channel_id=final_notify_channel_id,
                    notify_role_id=notify_role_id,
                    control_category_id=control_category_id,
                    delete_delay_minutes=delete_delay_minutes
                )
            except Exception as e:
                logger.error(f"❌ VCシステムDB保存エラー (Guild: {guild.name}): {e}")
        
        return user_vc_category, hub_vc


class VCSetupView(discord.ui.View):
    """VC設定用のビュー"""
    
    def __init__(self, cog: VCManager, user: discord.User, source_channel, guild: discord.Guild):
        super().__init__(timeout=300)  # 5分でタイムアウト
        self.cog = cog
        self.user = user
        self.source_channel = source_channel
        self.guild = guild
        self.hub_role_ids = []  # ハブVCに入れるロール
        self.vc_role_ids = []   # 作成されたVCに入れるロール
        self.hidden_role_ids = []  # VCを見えなくするロール
        self.hub_role_mode = "none"  # ハブVCロール制限モード
        self.vc_role_mode = "none"   # 作成VCロール制限モード
        self.hidden_role_mode = "none"  # 閲覧可能ロールモード
        self.vc_type = VCType.NO_LIMIT  # デフォルト: 人数指定なし
        self.user_limit = 0
        self.location_mode = VCLocationMode.AUTO_CATEGORY
        self.target_category_id = None
        self.selected_options = []
        self.locked_name = None
        
        # ハブVCロール制限選択
        self.add_item(HubRoleModeDropdown(self))
        # 作成VCロール制限選択
        self.add_item(VCRoleModeDropdown(self))
        # 閲覧可能ロール選択
        self.add_item(HiddenRoleModeDropdown(self))
        # VCタイプ選択ドロップダウンを追加
        self.add_item(VCTypeSelectDropdown(self))
        # 次へボタンとキャンセルボタンを追加（最下部）
        self.add_item(CreateButton(self))
        self.add_item(CancelButton(self))
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """操作者チェック"""
        if interaction.user.id != self.user.id:
            return False
        return True
    
    async def on_timeout(self):
        """タイムアウト時の処理"""
        logger.info(f"⏱️ VCSetupView タイムアウト (ユーザー: {self.user.name})")
        # メモリクリーンアップなどが必要な場合はここに追加
    
    def get_current_settings_text(self):
        """現在の設定を文字列で取得（選択したものだけ）"""
        settings = []
        
        # ハブVC入室制限（デフォルトから変更された場合のみ）
        if hasattr(self, '_hub_selected'):
            if self.hub_role_mode == "none":
                settings.append("ハブ参加権限: 全員入室可能")
            else:
                settings.append("ハブ参加権限: ロール限定")
        
        # 作成VC入室制限（デフォルトから変更された場合のみ）
        if hasattr(self, '_vc_selected'):
            if self.vc_role_mode == "none":
                settings.append("VC参加権限: 全員入室可能")
            else:
                settings.append("VC参加権限: ロール限定")
        
        # 閲覧可能ロール（デフォルトから変更された場合のみ）
        if hasattr(self, '_hidden_selected'):
            if self.hidden_role_mode == "none":
                settings.append("閲覧可能: 全員")
            else:
                settings.append("閲覧可能: ロール限定")
        
        # VCタイプ（デフォルトから変更された場合のみ）
        if hasattr(self, '_type_selected'):
            type_text = "あり" if self.vc_type == VCType.WITH_LIMIT else "なし"
            settings.append(f"人数指定の有無: {type_text}")
        
        if not settings:
            return "未選択（デフォルト設定で進みます）"
        
        return "\n".join([f"✓ {s}" for s in settings])
    
    async def create_vc_system(self, interaction: discord.Interaction):
        """VC管理システムを作成"""
        # 既に応答済みの場合はfollowupを使う
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        
        # VC管理システムを作成
        await self.cog.create_vc_system(
            interaction.guild,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.location_mode,
            self.target_category_id,
            self.source_channel,
            self.selected_options,
            self.locked_name,
            control_category_new=False
        )
        
        await interaction.followup.send("✅ VC管理システムを作成しました", ephemeral=True)
        self.stop()
    
    async def finish_creation(self, interaction: discord.Interaction):
        """作成完了処理（モーダルから呼ばれる）"""
        await interaction.response.defer(ephemeral=True)
        
        # VC管理システムを作成
        await self.cog.create_vc_system(
            interaction.guild,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.location_mode,
            self.target_category_id,
            self.source_channel,
            self.selected_options,
            self.locked_name,
            control_category_new=False
        )
        
        await interaction.followup.send("✅ VC管理システムを作成しました", ephemeral=True)
        self.stop()


class HubRoleModeDropdown(discord.ui.Select):
    """ハブVCロール制限モード選択ドロップダウン"""
    
    def __init__(self, parent_view):
        self.parent_view = parent_view
        
        options = [
            discord.SelectOption(label="全員入室可能", value="none", description="@everyoneが入れる"),
            discord.SelectOption(label="ロール限定", value="specify", description="指定したロールのみ入室可能")
        ]
        
        super().__init__(
            placeholder="ハブ参加権限ロール",
            min_values=0,
            max_values=1,
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 値を保存
        if len(self.values) > 0:
            self.parent_view._hub_selected = True  # 選択フラグを立てる
            if self.values[0] == "none":
                self.parent_view.hub_role_ids = []
                self.parent_view.hub_role_mode = "none"
            else:
                self.parent_view.hub_role_mode = "specify"
        
        # 埋め込みを更新して選択内容を表示
        settings_text = self.parent_view.get_current_settings_text()
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=f"```\n【現在の設定】\n{settings_text}\n```",
            color=0x5865F2
        )
        
        # メッセージを更新
        await interaction.response.edit_message(embed=embed, view=self.view)


class HubRoleSelectView(discord.ui.View):
    """ハブVCロール選択用のページネーションビュー"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        
        # 全ロールを取得
        self.all_roles = [r for r in guild.roles if r.name != "@everyone" and not r.managed]
        self.total_pages = (len(self.all_roles) + 23) // 24  # 24個ずつ（1つは完了ボタン用）
        
        # 現在のページのロールを表示
        self.update_components()
    
    def update_components(self):
        """コンポーネントを更新"""
        self.clear_items()
        
        # 現在のページのロールを取得
        start_idx = self.page * 24
        end_idx = min(start_idx + 24, len(self.all_roles))
        page_roles = self.all_roles[start_idx:end_idx]
        
        # ロール選択ドロップダウンを追加
        if len(page_roles) > 0:
            self.add_item(HubRoleSelectDropdown(self, page_roles, start_idx))
        
        # ページネーションボタン
        if self.total_pages > 1:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀ 前のページ", style=discord.ButtonStyle.gray, row=1)
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            
            page_info_btn = discord.ui.Button(
                label=f"{self.page + 1} / {self.total_pages}ページ",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1
            )
            self.add_item(page_info_btn)
            
            if self.page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="次のページ ▶", style=discord.ButtonStyle.gray, row=1)
                next_btn.callback = self.next_page
                self.add_item(next_btn)
        
        # 完了ボタン
        done_btn = discord.ui.Button(
            label=f"✅ 選択完了 ({len(self.parent_view.hub_role_ids)}個)",
            style=discord.ButtonStyle.green,
            row=2
        )
        done_btn.callback = self.done
        self.add_item(done_btn)
        
        # クリアボタン
        if len(self.parent_view.hub_role_ids) > 0:
            clear_btn = discord.ui.Button(label="🗑️ 全解除", style=discord.ButtonStyle.danger, row=2)
            clear_btn.callback = self.clear_all
            self.add_item(clear_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def done(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ ハブVCロールを{len(self.parent_view.hub_role_ids)}個選択しました",
            ephemeral=True
        )
        self.stop()
    
    async def clear_all(self, interaction: discord.Interaction):
        self.parent_view.hub_role_ids = []
        self.update_components()
        await interaction.response.edit_message(view=self)


class HubRoleSelectDropdown(discord.ui.Select):
    """ハブVCロール選択ドロップダウン"""
    
    def __init__(self, role_view: HubRoleSelectView, roles: list, start_idx: int):
        self.role_view = role_view
        
        options = []
        for role in roles:
            is_selected = role.id in role_view.parent_view.hub_role_ids
            # ロール名を短く制限（20文字まで）
            role_name = role.name[:20] if len(role.name) > 20 else role.name
            label = f"{'✓ ' if is_selected else ''}{role_name}"
            options.append(discord.SelectOption(
                label=label,
                value=str(role.id)
            ))
        
        super().__init__(
            placeholder=f"ロールを選択 ({start_idx + 1}～{start_idx + len(roles)})",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 選択されたロールIDを取得
        selected_ids = [int(role_id) for role_id in self.values]
        
        # 現在のドロップダウンのロールIDリストを取得
        current_dropdown_role_ids = [int(opt.value) for opt in self.options]
        
        # 現在のドロップダウンのロールを一旦削除
        self.role_view.parent_view.hub_role_ids = [
            rid for rid in self.role_view.parent_view.hub_role_ids 
            if rid not in current_dropdown_role_ids
        ]
        
        # 新しく選択されたロールを追加
        self.role_view.parent_view.hub_role_ids.extend(selected_ids)
        
        # 選択フラグを立てる
        self.role_view.has_selected = True
        
        # 次へボタンを有効化
        self.role_view.next_btn.disabled = False
        self.role_view.next_btn.style = discord.ButtonStyle.green
        
        # 選択されたロール名を取得
        selected_role_names = []
        for role_id in self.role_view.parent_view.hub_role_ids:
            role = self.role_view.guild.get_role(role_id)
            if role:
                selected_role_names.append(role.name)
        
        # 埋め込みに選択内容を表示
        if selected_role_names:
            roles_text = "\n".join([f"✓ {name[:30]}" for name in selected_role_names[:5]])  # 最大5個、30文字まで
            if len(selected_role_names) > 5:
                roles_text += f"\n\n... その他 {len(selected_role_names) - 5}個のロール"
            
            embed = discord.Embed(
                title="🎭 ハブ参加権限ロール",
                description=f"```\nハブVCに入室できるロールを指定します\n\n【選択中のロール】\n{roles_text}\n```",
                color=0x5865F2
            )
        else:
            embed = discord.Embed(
                title="🎭 ハブ参加権限ロール",
                description="```\nハブVCに入室できるロールを指定します\n\nドロップダウンからロールを選択してください\n```",
                color=0x5865F2
            )
        
        # ビューを更新（edit_messageを使う）
        await interaction.response.edit_message(embed=embed, view=self.role_view)


class VCRoleModeDropdown(discord.ui.Select):
    """作成されたVCロール制限モード選択ドロップダウン"""
    
    def __init__(self, parent_view):
        self.parent_view = parent_view
        
        options = [
            discord.SelectOption(label="全員入室可能", value="none", description="@everyoneが入れる"),
            discord.SelectOption(label="ロール限定", value="specify", description="指定したロールのみ入室可能")
        ]
        
        super().__init__(
            placeholder="VC参加権限ロール",
            min_values=0,
            max_values=1,
            options=options,
            row=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 値を保存
        if len(self.values) > 0:
            self.parent_view._vc_selected = True  # 選択フラグを立てる
            if self.values[0] == "none":
                self.parent_view.vc_role_ids = []
                self.parent_view.vc_role_mode = "none"
            else:
                self.parent_view.vc_role_mode = "specify"
        
        # 埋め込みを更新して選択内容を表示
        settings_text = self.parent_view.get_current_settings_text()
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=f"```\n【現在の設定】\n{settings_text}\n```",
            color=0x5865F2
        )
        
        # メッセージを更新
        await interaction.response.edit_message(embed=embed, view=self.view)


class HiddenRoleModeDropdown(discord.ui.Select):
    """閲覧可能ロール選択ドロップダウン"""
    
    def __init__(self, parent_view):
        self.parent_view = parent_view
        
        options = [
            discord.SelectOption(label="全員閲覧可能", value="none", description="@everyoneが見える"),
            discord.SelectOption(label="ロール限定", value="specify", description="指定したロールのみ閲覧可能")
        ]
        
        super().__init__(
            placeholder="閲覧可能ロール",
            min_values=0,
            max_values=1,
            options=options,
            row=2
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 値を保存
        if len(self.values) > 0:
            self.parent_view._hidden_selected = True  # 選択フラグを立てる
            if self.values[0] == "none":
                self.parent_view.hidden_role_ids = []
                self.parent_view.hidden_role_mode = "none"
            else:
                self.parent_view.hidden_role_mode = "specify"
        
        # 埋め込みを更新して選択内容を表示
        settings_text = self.parent_view.get_current_settings_text()
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=f"```\n【現在の設定】\n{settings_text}\n```",
            color=0x5865F2
        )
        
        # メッセージを更新
        await interaction.response.edit_message(embed=embed, view=self.view)


class VCRoleSelectView(discord.ui.View):
    """作成されたVCロール選択用のページネーションビュー"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        
        # 全ロールを取得
        self.all_roles = [r for r in guild.roles if r.name != "@everyone" and not r.managed]
        self.total_pages = (len(self.all_roles) + 23) // 24
        
        # 現在のページのロールを表示
        self.update_components()
    
    def update_components(self):
        """コンポーネントを更新"""
        self.clear_items()
        
        # 現在のページのロールを取得
        start_idx = self.page * 24
        end_idx = min(start_idx + 24, len(self.all_roles))
        page_roles = self.all_roles[start_idx:end_idx]
        
        # ロール選択ドロップダウンを追加
        if len(page_roles) > 0:
            self.add_item(VCRoleSelectDropdown(self, page_roles, start_idx))
        
        # ページネーションボタン
        if self.total_pages > 1:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀ 前のページ", style=discord.ButtonStyle.gray, row=1)
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            
            page_info_btn = discord.ui.Button(
                label=f"{self.page + 1} / {self.total_pages}ページ",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1
            )
            self.add_item(page_info_btn)
            
            if self.page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="次のページ ▶", style=discord.ButtonStyle.gray, row=1)
                next_btn.callback = self.next_page
                self.add_item(next_btn)
        
        # 完了ボタン
        done_btn = discord.ui.Button(
            label=f"✅ 選択完了 ({len(self.parent_view.vc_role_ids)}個)",
            style=discord.ButtonStyle.green,
            row=2
        )
        done_btn.callback = self.done
        self.add_item(done_btn)
        
        # クリアボタン
        if len(self.parent_view.vc_role_ids) > 0:
            clear_btn = discord.ui.Button(label="🗑️ 全解除", style=discord.ButtonStyle.danger, row=2)
            clear_btn.callback = self.clear_all
            self.add_item(clear_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def done(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ 作成されたVCロールを{len(self.parent_view.vc_role_ids)}個選択しました",
            ephemeral=True
        )
        self.stop()
    
    async def clear_all(self, interaction: discord.Interaction):
        self.parent_view.vc_role_ids = []
        self.update_components()
        await interaction.response.edit_message(view=self)


class VCRoleSelectDropdown(discord.ui.Select):
    """作成されたVCロール選択ドロップダウン"""
    
    def __init__(self, role_view: VCRoleSelectView, roles: list, start_idx: int):
        self.role_view = role_view
        
        options = []
        for role in roles:
            is_selected = role.id in role_view.parent_view.vc_role_ids
            label = f"{'✓ ' if is_selected else ''}{role.name}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(role.id),
                description=f"ID: {role.id}"[:100]
            ))
        
        super().__init__(
            placeholder=f"ロールを選択 ({start_idx + 1}～{start_idx + len(roles)})",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        
        # トグル処理
        if role_id in self.role_view.parent_view.vc_role_ids:
            self.role_view.parent_view.vc_role_ids.remove(role_id)
        else:
            self.role_view.parent_view.vc_role_ids.append(role_id)
        
        # ビューを更新
        self.role_view.update_components()
        await interaction.response.edit_message(view=self.role_view)


class VCTypeSelectDropdown(discord.ui.Select):
    """VCタイプ選択ドロップダウン"""
    
    def __init__(self, parent_view: VCSetupView):
        self.parent_view = parent_view
        
        options = [
            discord.SelectOption(label=VCType.NO_LIMIT, value=VCType.NO_LIMIT, description="基本のVC"),
            discord.SelectOption(label=VCType.WITH_LIMIT, value=VCType.WITH_LIMIT, description="人数制限付きVC（1～25人）")
        ]
        
        super().__init__(
            placeholder="人数指定の有無",
            min_values=0,
            max_values=1,
            options=options,
            row=3
        )
    
    async def callback(self, interaction: discord.Interaction):
        if len(self.values) > 0:
            self.parent_view._type_selected = True  # 選択フラグを立てる
            self.parent_view.vc_type = self.values[0]
        
        # 埋め込みを更新して選択内容を表示
        settings_text = self.parent_view.get_current_settings_text()
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=f"```\n【現在の設定】\n{settings_text}\n```",
            color=0x5865F2
        )
        
        # メッセージを更新
        await interaction.response.edit_message(embed=embed, view=self.view)


class VCOptionSelectDropdown(discord.ui.Select):
    """VCオプション選択ドロップダウン"""
    
    def __init__(self, parent_view: VCSetupView):
        self.parent_view = parent_view
        
        options = [
            discord.SelectOption(
                label=VCOption.TEXT_CHANNEL, 
                value=VCOption.TEXT_CHANNEL,
                description="VC参加者のみが見えるテキストチャンネルを作成"
            ),
            discord.SelectOption(
                label=VCOption.NO_CONTROL, 
                value=VCOption.NO_CONTROL,
                description="VC作成時に操作パネルを表示しない"
            ),
            discord.SelectOption(
                label=VCOption.HIDE_FULL, 
                value=VCOption.HIDE_FULL,
                description="VCが満員になると自動で非表示になる"
            ),
            discord.SelectOption(
                label=VCOption.LOCK_NAME, 
                value=VCOption.LOCK_NAME,
                description="VC名を固定（番号で管理）"
            ),
            discord.SelectOption(
                label=VCOption.NO_STATE_CONTROL, 
                value=VCOption.NO_STATE_CONTROL,
                description="ロック・非表示・人数制限の操作を消す"
            ),
            discord.SelectOption(
                label=VCOption.NO_JOIN_LEAVE_LOG, 
                value=VCOption.NO_JOIN_LEAVE_LOG,
                description="入退室ログを表示しない"
            ),
            discord.SelectOption(
                label=VCOption.NO_OWNERSHIP_TRANSFER, 
                value=VCOption.NO_OWNERSHIP_TRANSFER,
                description="管理者譲渡機能を無効化"
            )
        ]
        
        # デバッグ: オプション数を確認
        logger.info(f"🔍 VCOptionSelectDropdown初期化: {len(options)}個のオプション")
        for i, opt in enumerate(options):
            logger.info(f"  オプション{i+1}: {opt.label} = {opt.value}")
        
        super().__init__(
            placeholder="オプションを選択（複数可）",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        # インタラクションを即座に処理
        await interaction.response.defer()
        
        self.parent_view.selected_options = self.values
        
        # 次へボタンを有効化
        for item in self.view.children:
            if hasattr(item, 'label') and item.label == "次へ":
                item.disabled = False
                item.style = discord.ButtonStyle.green
        
        # 選択内容を埋め込みに表示
        if self.values:
            selected_text = "\n".join([f"✓ {opt}" for opt in self.values])
        else:
            selected_text = "なし"
        
        embed = discord.Embed(
            title="⚙️ オプション機能を選択",
            description=f"```\n複数指定可能、不要な方はスキップ\n\n【選択中のオプション】\n{selected_text}\n```",
            color=0x5865F2
        )
        
        # メッセージを更新
        await interaction.edit_original_response(embed=embed, view=self.view)


class CombinedInputModal(discord.ui.Modal, title="VC設定を入力"):
    """固定名と人数を同時に入力するモーダル"""
    
    def __init__(self, parent_view: VCSetupView):
        super().__init__()
        self.parent_view = parent_view
    
    name_input = discord.ui.TextInput(
        label="固定するVC名（空白で初期名のまま固定）",
        placeholder="例: ゲーム部屋（空白可）",
        min_length=0,
        max_length=100,
        required=False
    )
    
    limit_input = discord.ui.TextInput(
        label="人数制限",
        placeholder="1から25までの数字を入力してください",
        min_length=1,
        max_length=2,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # 固定名を保存
            name = self.name_input.value.strip()
            self.parent_view.locked_name = name if name else ""
            
            # 人数制限を保存
            limit = int(self.limit_input.value)
            if limit < 1 or limit > 25:
                await interaction.response.send_message("人数は1から25の範囲で入力してください", ephemeral=True)
                return
            
            self.parent_view.user_limit = limit
            
            # VC作成
            await interaction.response.defer(ephemeral=True)
            
            await self.parent_view.cog.create_vc_system(
                interaction.guild,
                self.parent_view.vc_type,
                self.parent_view.user_limit,
                self.parent_view.hub_role_ids,
                self.parent_view.vc_role_ids,
                self.parent_view.hidden_role_ids,
                self.parent_view.location_mode,
                self.parent_view.target_category_id,
                self.parent_view.source_channel,
                self.parent_view.selected_options,
                self.parent_view.locked_name
            )
            
            await interaction.followup.send("✅ VC管理システムを作成しました", ephemeral=True)
            self.parent_view.stop()
            
        except ValueError as e:
            logger.warning(f"⚠️ 人数入力エラー (ユーザー入力): {self.limit_input.value}")
            await interaction.response.send_message("人数は数字で入力してください", ephemeral=True)
        except Exception as e:
            logger.error(f"❌ CombinedInputModal エラー: {e}")
            logger.error(traceback.format_exc())
            await interaction.response.send_message("エラーが発生しました", ephemeral=True)


class LockedNameInputModal(discord.ui.Modal, title="固定名を入力"):
    """固定名入力モーダル"""
    
    def __init__(self, parent_view: VCSetupView):
        super().__init__()
        self.parent_view = parent_view
    
    name_input = discord.ui.TextInput(
        label="固定するVC名（空白で初期名のまま固定）",
        placeholder="例: ゲーム部屋（空白可）",
        min_length=0,
        max_length=100,
        required=False
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        self.parent_view.locked_name = name if name else ""
        
        # VC作成（このモーダルは名前変更制限のみの場合にしか呼ばれない）
        await interaction.response.defer(ephemeral=True)
        
        await self.parent_view.cog.create_vc_system(
            interaction.guild,
            self.parent_view.vc_type,
            self.parent_view.user_limit,
            self.parent_view.hub_role_ids,
            self.parent_view.vc_role_ids,
            self.parent_view.hidden_role_ids,
            self.parent_view.location_mode,
            self.parent_view.target_category_id,
            self.parent_view.source_channel,
            self.parent_view.selected_options,
            self.parent_view.locked_name
        )
        
        await interaction.followup.send("✅ VC管理システムを作成しました", ephemeral=True)
        self.parent_view.stop()


class VCLocationSelectDropdown(discord.ui.Select):
    """VC作成場所選択ドロップダウン"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild):
        self.parent_view = parent_view
        self.guild = guild
        
        options = [
            discord.SelectOption(label="【必須】VC作成場所を選択", value="placeholder", description="このオプションを選択してください", default=True),
            discord.SelectOption(label=VCLocationMode.AUTO_CATEGORY, value=VCLocationMode.AUTO_CATEGORY, description="自動でカテゴリーを作成"),
            discord.SelectOption(label=VCLocationMode.SAME_CATEGORY, value=VCLocationMode.SAME_CATEGORY, description="指定カテゴリー内に作成"),
            discord.SelectOption(label=VCLocationMode.UNDER_HUB, value=VCLocationMode.UNDER_HUB, description="ハブVCの下に作成")
        ]
        
        super().__init__(
            placeholder="【必須】VC作成場所を選択してください",
            min_values=1,
            max_values=1,
            options=options,
            row=4
        )
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "placeholder":
            await interaction.response.defer()
            return
        
        self.parent_view.location_mode = self.values[0]
        
        # 指定カテゴリー内の場合、カテゴリー選択ビューを表示
        if self.values[0] == VCLocationMode.SAME_CATEGORY:
            await interaction.response.send_message(
                "カテゴリーを選択してください",
                view=CategorySelectView(self.parent_view, self.guild),
                ephemeral=True
            )
        else:
            self.parent_view.target_category_id = None
            await interaction.response.defer()


class CategorySelectView(discord.ui.View):
    """カテゴリー選択ビュー"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        self.all_categories = [ch for ch in guild.channels if isinstance(ch, discord.CategoryChannel)]
        
        self.add_item(CategorySelectDropdown(self, guild, page))
        
        # ページネーションボタンを追加
        if len(self.all_categories) > 25:
            if page > 0:
                self.add_item(PrevPageButton(self))
            if (page + 1) * 25 < len(self.all_categories):
                self.add_item(NextPageButton(self))


class CategorySelectDropdown(discord.ui.Select):
    """カテゴリー選択ドロップダウン"""
    
    def __init__(self, category_view: CategorySelectView, guild: discord.Guild, page: int):
        self.category_view = category_view
        
        # サーバー内のカテゴリーを取得（ページングあり）
        all_categories = [ch for ch in guild.channels if isinstance(ch, discord.CategoryChannel)]
        start_idx = page * 25
        end_idx = start_idx + 25
        categories = all_categories[start_idx:end_idx]
        
        options = []
        for category in categories:
            options.append(discord.SelectOption(label=category.name, value=str(category.id)))
        
        # カテゴリーがない場合
        if len(options) == 0:
            options.append(discord.SelectOption(label="カテゴリーなし", value="none"))
        
        super().__init__(
            placeholder=f"カテゴリーを選択してください（{start_idx + 1}～{start_idx + len(options)}）",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] != "none":
            self.category_view.parent_view.target_category_id = int(self.values[0])
            category = interaction.guild.get_channel(int(self.values[0]))
            await interaction.response.send_message(f"カテゴリー「{category.name}」を選択しました", ephemeral=True)
        else:
            await interaction.response.send_message("有効なカテゴリーを選択してください", ephemeral=True)


class PrevPageButton(discord.ui.Button):
    """前のページボタン"""
    
    def __init__(self, category_view: CategorySelectView):
        super().__init__(label="前のページ", style=discord.ButtonStyle.secondary, row=1)
        self.category_view = category_view
    
    async def callback(self, interaction: discord.Interaction):
        new_page = self.category_view.page - 1
        new_view = CategorySelectView(self.category_view.parent_view, self.category_view.guild, new_page)
        await interaction.response.edit_message(view=new_view)


class NextPageButton(discord.ui.Button):
    """次のページボタン"""
    
    def __init__(self, category_view: CategorySelectView):
        super().__init__(label="次のページ", style=discord.ButtonStyle.secondary, row=1)
        self.category_view = category_view
    
    async def callback(self, interaction: discord.Interaction):
        new_page = self.category_view.page + 1
        new_view = CategorySelectView(self.category_view.parent_view, self.category_view.guild, new_page)
        await interaction.response.edit_message(view=new_view)


class CancelButton(discord.ui.Button):
    """キャンセルボタン"""
    
    def __init__(self, parent_view: VCSetupView):
        super().__init__(label="キャンセル", style=discord.ButtonStyle.red, row=4)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("❌ セットアップをキャンセルしました", ephemeral=True)
        self.parent_view.stop()


class CreateButton(discord.ui.Button):
    """次へボタン"""
    
    def __init__(self, parent_view: VCSetupView):
        super().__init__(label="次へ", style=discord.ButtonStyle.green, row=4, custom_id="create_button_next")
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        # ロール指定が選択されているかチェック
        if self.parent_view.hub_role_mode == "specify":
            # ハブVCロール選択画面へ
            embed = discord.Embed(
                title="🎭 ハブ参加権限ロール",
                description="```\nハブVCに入室できるロールを指定します\n```",
                color=0x5865F2
            )
            await interaction.response.send_message(
                embed=embed,
                view=HubRoleSelectionView(self.parent_view, interaction.guild),
                ephemeral=True
            )
        elif self.parent_view.vc_role_mode == "specify":
            # 作成VCロール選択画面へ
            embed = discord.Embed(
                title="🎭 VC参加権限ロール",
                description="```\n作成されたVCに参加できるロールを指定します\n```",
                color=0x5865F2
            )
            await interaction.response.send_message(
                embed=embed,
                view=VCRoleSelectionView(self.parent_view, interaction.guild),
                ephemeral=True
            )
        elif self.parent_view.hidden_role_mode == "specify":
            # 閲覧可能ロール選択画面へ
            embed = discord.Embed(
                title="👁️ 閲覧可能ロール",
                description="```\nVCを閲覧できるロールを指定します\n```",
                color=0x5865F2
            )
            await interaction.response.send_message(
                embed=embed,
                view=HiddenRoleSelectionView(self.parent_view, interaction.guild),
                ephemeral=True
            )
        else:
            # オプション選択画面へ
            embed = discord.Embed(
                title="⚙️ オプション機能を選択",
                description="```\n複数指定可能、不要な方はスキップ\n```",
                color=0x5865F2
            )
            await interaction.response.send_message(
                embed=embed,
                view=VCOptionSelectionView(self.parent_view),
                ephemeral=True
            )


class HubRoleSelectionView(discord.ui.View):
    """ハブVCロール選択画面"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        self.has_selected = False
        
        # 全ロールを取得（@everyone以外）
        self.all_roles = [r for r in guild.roles if r.name != "@everyone"]
        self.total_pages = (len(self.all_roles) + 23) // 24  # 24個ずつ（1つのドロップダウン）
        
        # 次へボタンを作成（再利用するため先に作成）
        self.next_btn = discord.ui.Button(label="次へ", style=discord.ButtonStyle.gray, row=4, disabled=True)
        self.next_btn.callback = self.next_step
        
        # キャンセルボタンを作成
        self.cancel_btn = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.red, row=4)
        self.cancel_btn.callback = self.cancel
        
        # 現在のページのロールを表示
        self.update_components()
    
    def update_components(self):
        """コンポーネントを更新"""
        self.clear_items()
        
        # 現在のページのロールを取得（24個）
        start_idx = self.page * 24
        end_idx = min(start_idx + 24, len(self.all_roles))
        page_roles = self.all_roles[start_idx:end_idx]
        
        # 1つのドロップダウンで24個表示（複数選択可能）
        if len(page_roles) > 0:
            self.add_item(HubRoleSelectDropdown(self, page_roles, start_idx))
        
        # ページネーションボタン
        if self.total_pages > 1:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀ 前のページ", style=discord.ButtonStyle.gray, row=4)
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            
            if self.page < self.total_pages - 1:
                next_page_btn = discord.ui.Button(label="次のページ ▶", style=discord.ButtonStyle.gray, row=4)
                next_page_btn.callback = self.next_page
                self.add_item(next_page_btn)
        
        # 次へボタンとキャンセルボタンを追加
        self.add_item(self.next_btn)
        self.add_item(self.cancel_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def cancel(self, interaction: discord.Interaction):
        """キャンセル"""
        await interaction.response.send_message("❌ セットアップをキャンセルしました", ephemeral=True)
        self.stop()
    
    async def next_step(self, interaction: discord.Interaction):
        """次のステップへ"""
        # 作成VCロール選択が必要か確認
        if self.parent_view.vc_role_mode == "specify":
            embed = discord.Embed(
                title="🎭 作成VC参加制限ロール",
                description="```\n作成されたVCに参加できるロールを指定します\n未選択の場合は全員が参加可能です\n```",
                color=0x5865F2
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed,
                    view=VCRoleSelectionView(self.parent_view, interaction.guild),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    view=VCRoleSelectionView(self.parent_view, interaction.guild),
                    ephemeral=True
                )
        else:
            # オプション選択へ
            embed = discord.Embed(
                title="⚙️ オプション機能を選択",
                description="```\n複数指定可能、不要な方はスキップ\n```",
                color=0x5865F2
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed,
                    view=VCOptionSelectionView(self.parent_view, interaction.guild),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    view=VCOptionSelectionView(self.parent_view, interaction.guild),
                    ephemeral=True
                )


class HubRoleMultiDropdown(discord.ui.Select):
    """ハブVCロール選択ドロップダウン（複数配置用）"""
    
    def __init__(self, role_view: HubRoleSelectionView, roles: list, start_idx: int, row: int):
        self.role_view = role_view
        
        options = []
        for role in roles:
            is_selected = role.id in role_view.parent_view.hub_role_ids
            label = f"{'✓ ' if is_selected else ''}{role.name}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(role.id),
                description=f"ID: {role.id}"[:100]
            ))
        
        super().__init__(
            placeholder=f"ロールを選択 ({start_idx + 1}～{start_idx + len(roles)})",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 選択されたロールIDを取得
        selected_ids = [int(role_id) for role_id in self.values]
        
        # 現在のドロップダウンのロールIDリストを取得
        current_dropdown_role_ids = [int(opt.value) for opt in self.options]
        
        # 現在のドロップダウンのロールを一旦削除
        self.role_view.parent_view.hub_role_ids = [
            rid for rid in self.role_view.parent_view.hub_role_ids 
            if rid not in current_dropdown_role_ids
        ]
        
        # 新しく選択されたロールを追加
        self.role_view.parent_view.hub_role_ids.extend(selected_ids)
        
        # 選択フラグを立てる
        self.role_view.has_selected = True
        
        # 次へボタンを有効化
        self.role_view.next_btn.disabled = False
        self.role_view.next_btn.style = discord.ButtonStyle.green
        
        # 選択されたロール名を取得
        selected_role_names = []
        for role_id in self.role_view.parent_view.hub_role_ids:
            role = self.role_view.guild.get_role(role_id)
            if role:
                selected_role_names.append(role.name)
        
        # 埋め込みに選択内容を表示
        if selected_role_names:
            roles_text = "\n".join([f"✓ {name[:30]}" for name in selected_role_names[:5]])  # 最大5個、30文字まで
            if len(selected_role_names) > 5:
                roles_text += f"\n\n... その他 {len(selected_role_names) - 5}個のロール"
            
            embed = discord.Embed(
                title="🎭 ハブ参加権限ロール",
                description=f"```\nハブVCに入室できるロールを指定します\n\n【選択中のロール】\n{roles_text}\n```",
                color=0x5865F2
            )
        else:
            embed = discord.Embed(
                title="🎭 ハブ参加権限ロール",
                description="```\nハブVCに入室できるロールを指定します\n\nドロップダウンからロールを選択してください\n```",
                color=0x5865F2
            )
        
        # メッセージを更新
        await interaction.response.edit_message(embed=embed, view=self.role_view)


class VCRoleSelectionView(discord.ui.View):
    """作成VCロール選択画面"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        self.has_selected = False
        
        # 全ロールを取得（@everyone以外）
        self.all_roles = [r for r in guild.roles if r.name != "@everyone"]
        self.total_pages = (len(self.all_roles) + 23) // 24  # 24個ずつ（1つのドロップダウン）
        
        # 次へボタンを先に作成（再利用）
        self.next_btn = discord.ui.Button(label="次へ", style=discord.ButtonStyle.gray, row=4, disabled=True)
        self.next_btn.callback = self.next_step
        
        # キャンセルボタンを先に作成
        self.cancel_btn = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.red, row=4)
        self.cancel_btn.callback = self.cancel
        
        # 現在のページのロールを表示
        self.update_components()
    
    def update_components(self):
        """コンポーネントを更新"""
        self.clear_items()
        
        # 現在のページのロールを取得（24個）
        start_idx = self.page * 24
        end_idx = min(start_idx + 24, len(self.all_roles))
        page_roles = self.all_roles[start_idx:end_idx]
        
        # 1つのドロップダウンで24個表示（複数選択可能）
        if len(page_roles) > 0:
            self.add_item(VCRoleMultiDropdown(self, page_roles, start_idx, 0))
        
        # ページネーションボタン
        if self.total_pages > 1:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀ 前のページ", style=discord.ButtonStyle.gray, row=4)
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            
            if self.page < self.total_pages - 1:
                next_page_btn = discord.ui.Button(label="次のページ ▶", style=discord.ButtonStyle.gray, row=4)
                next_page_btn.callback = self.next_page
                self.add_item(next_page_btn)
        
        # 次へボタンとキャンセルボタンを追加
        self.add_item(self.next_btn)
        self.add_item(self.cancel_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def cancel(self, interaction: discord.Interaction):
        """キャンセル"""
        await interaction.response.send_message("❌ セットアップをキャンセルしました", ephemeral=True)
        self.stop()
    
    async def next_step(self, interaction: discord.Interaction):
        """閲覧可能ロール選択またはオプション選択へ"""
        if self.parent_view.hidden_role_mode == "specify":
            # 閲覧可能ロール選択画面へ
            embed = discord.Embed(
                title="👁️ 閲覧可能ロール",
                description="```\nVCを閲覧できるロールを指定します\n```",
                color=0x5865F2
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed,
                    view=HiddenRoleSelectionView(self.parent_view, interaction.guild),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    view=HiddenRoleSelectionView(self.parent_view, interaction.guild),
                    ephemeral=True
                )
        else:
            # オプション選択画面へ
            embed = discord.Embed(
                title="⚙️ オプション機能を選択",
                description="```\n複数指定可能、不要な方はスキップ\n```",
                color=0x5865F2
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed,
                    view=VCOptionSelectionView(self.parent_view),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    view=VCOptionSelectionView(self.parent_view),
                    ephemeral=True
                )


class HiddenRoleSelectionView(discord.ui.View):
    """閲覧可能ロール選択画面"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        self.has_selected = False
        
        # 全ロールを取得（@everyone以外）
        self.all_roles = [r for r in guild.roles if r.name != "@everyone"]
        self.total_pages = (len(self.all_roles) + 23) // 24  # 24個ずつ（1つのドロップダウン）
        
        # 次へボタンを先に作成（再利用）
        self.next_btn = discord.ui.Button(label="次へ", style=discord.ButtonStyle.gray, row=4, disabled=True)
        self.next_btn.callback = self.next_step
        
        # キャンセルボタンを先に作成
        self.cancel_btn = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.red, row=4)
        self.cancel_btn.callback = self.cancel
        
        # 現在のページのロールを表示
        self.update_components()
    
    def update_components(self):
        """コンポーネントを更新"""
        self.clear_items()
        
        # 現在のページのロールを取得（24個）
        start_idx = self.page * 24
        end_idx = min(start_idx + 24, len(self.all_roles))
        page_roles = self.all_roles[start_idx:end_idx]
        
        # 1つのドロップダウンで24個表示（複数選択可能）
        if len(page_roles) > 0:
            self.add_item(HiddenRoleMultiDropdown(self, page_roles, start_idx, 0))
        
        # ページネーションボタン
        if self.total_pages > 1:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀ 前のページ", style=discord.ButtonStyle.gray, row=4)
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            
            if self.page < self.total_pages - 1:
                next_page_btn = discord.ui.Button(label="次のページ ▶", style=discord.ButtonStyle.gray, row=4)
                next_page_btn.callback = self.next_page
                self.add_item(next_page_btn)
        
        # 次へボタンとキャンセルボタンを追加
        self.add_item(self.next_btn)
        self.add_item(self.cancel_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def cancel(self, interaction: discord.Interaction):
        """キャンセル"""
        await interaction.response.send_message("❌ セットアップをキャンセルしました", ephemeral=True)
        self.stop()
    
    async def next_step(self, interaction: discord.Interaction):
        """オプション選択へ"""
        embed = discord.Embed(
            title="⚙️ オプション機能を選択",
            description="```\n複数指定可能、不要な方はスキップ\n```",
            color=0x5865F2
        )
        if interaction.response.is_done():
            await interaction.followup.send(
                embed=embed,
                view=VCOptionSelectionView(self.parent_view),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                view=VCOptionSelectionView(self.parent_view),
                ephemeral=True
            )


class VCOptionSelectionView(discord.ui.View):
    """オプション選択画面"""
    
    def __init__(self, parent_view: VCSetupView):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        
        logger.info("🔍 VCOptionSelectionView初期化開始")
        # オプション選択ドロップダウン
        dropdown = VCOptionSelectDropdown(parent_view)
        logger.info(f"🔍 ドロップダウン作成完了: {len(dropdown.options)}個のオプション")
        self.add_item(dropdown)
        logger.info("🔍 VCOptionSelectionView初期化完了")
        
        # 次へボタン、スキップボタン、キャンセルボタン
        next_btn = discord.ui.Button(label="次へ", style=discord.ButtonStyle.gray, row=4, disabled=True)
        next_btn.callback = self.next_to_category
        self.add_item(next_btn)
        
        skip_btn = discord.ui.Button(label="スキップ", style=discord.ButtonStyle.primary, row=4)
        skip_btn.callback = self.skip_to_category
        self.add_item(skip_btn)
        
        cancel_btn = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.red, row=4)
        cancel_btn.callback = self.cancel
        self.add_item(cancel_btn)
    
    async def skip_to_category(self, interaction: discord.Interaction):
        """スキップしてカテゴリー選択へ"""
        self.parent_view.selected_options = []
        await interaction.response.send_message(
            "VC作成先のカテゴリーを選択してください",
            view=VCCategorySelectView(self.parent_view, interaction.guild),
            ephemeral=True
        )
    
    async def cancel(self, interaction: discord.Interaction):
        """キャンセル"""
        await interaction.response.send_message("❌ セットアップをキャンセルしました", ephemeral=True)
        self.stop()
    
    async def next_to_category(self, interaction: discord.Interaction):
        """カテゴリー選択へ"""
        await interaction.response.send_message(
            "VC作成先のカテゴリーを選択してください",
            view=VCCategorySelectView(self.parent_view, interaction.guild),
            ephemeral=True
        )


class VCRoleMultiDropdown(discord.ui.Select):
    """作成VCロール選択ドロップダウン（複数配置用）"""
    
    def __init__(self, role_view: VCRoleSelectionView, roles: list, start_idx: int, row: int):
        self.role_view = role_view
        
        options = []
        for role in roles:
            is_selected = role.id in role_view.parent_view.vc_role_ids
            # ロール名を短く制限（20文字まで）
            role_name = role.name[:20] if len(role.name) > 20 else role.name
            label = f"{'✓ ' if is_selected else ''}{role_name}"
            options.append(discord.SelectOption(
                label=label,
                value=str(role.id)
            ))
        
        super().__init__(
            placeholder=f"ロールを選択 ({start_idx + 1}～{start_idx + len(roles)})",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 選択されたロールIDを取得
        selected_ids = [int(role_id) for role_id in self.values]
        
        # 現在のドロップダウンのロールIDリストを取得
        current_dropdown_role_ids = [int(opt.value) for opt in self.options]
        
        # 現在のドロップダウンのロールを一旦削除
        self.role_view.parent_view.vc_role_ids = [
            rid for rid in self.role_view.parent_view.vc_role_ids 
            if rid not in current_dropdown_role_ids
        ]
        
        # 新しく選択されたロールを追加
        self.role_view.parent_view.vc_role_ids.extend(selected_ids)
        
        # 選択フラグを立てる
        self.role_view.has_selected = True
        
        # 次へボタンを有効化
        self.role_view.next_btn.disabled = False
        self.role_view.next_btn.style = discord.ButtonStyle.green
        
        # 選択されたロール名を取得
        selected_role_names = []
        for role_id in self.role_view.parent_view.vc_role_ids:
            role = self.role_view.guild.get_role(role_id)
            if role:
                selected_role_names.append(role.name)
        
        # 埋め込みに選択内容を表示
        if selected_role_names:
            roles_text = "\n".join([f"✓ {name[:30]}" for name in selected_role_names[:5]])  # 最大5個、30文字まで
            if len(selected_role_names) > 5:
                roles_text += f"\n\n... その他 {len(selected_role_names) - 5}個のロール"
            
            embed = discord.Embed(
                title="🎭 作成VC参加制限ロール",
                description=f"```\n作成されたVCに参加できるロールを指定します\n\n【選択中のロール】\n{roles_text}\n```",
                color=0x5865F2
            )
        else:
            embed = discord.Embed(
                title="🎭 作成VC参加制限ロール",
                description="```\n作成されたVCに参加できるロールを指定します\n\nドロップダウンからロールを選択してください\n```",
                color=0x5865F2
            )
        
        # ビューを更新（edit_messageを使う）
        await interaction.response.edit_message(embed=embed, view=self.role_view)


class HiddenRoleMultiDropdown(discord.ui.Select):
    """閲覧可能ロール選択ドロップダウン（複数配置用）"""
    
    def __init__(self, role_view: HiddenRoleSelectionView, roles: list, start_idx: int, row: int):
        self.role_view = role_view
        
        options = []
        for role in roles:
            is_selected = role.id in role_view.parent_view.hidden_role_ids
            # ロール名を短く制限（20文字まで）
            role_name = role.name[:20] if len(role.name) > 20 else role.name
            label = f"{'✓ ' if is_selected else ''}{role_name}"
            options.append(discord.SelectOption(
                label=label,
                value=str(role.id)
            ))
        
        super().__init__(
            placeholder=f"ロールを選択 ({start_idx + 1}～{start_idx + len(roles)})",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 選択されたロールIDを取得
        selected_ids = [int(role_id) for role_id in self.values]
        
        # 現在のドロップダウンのロールIDリストを取得
        current_dropdown_role_ids = [int(opt.value) for opt in self.options]
        
        # 現在のドロップダウンのロールを一旦削除
        self.role_view.parent_view.hidden_role_ids = [
            rid for rid in self.role_view.parent_view.hidden_role_ids 
            if rid not in current_dropdown_role_ids
        ]
        
        # 新しく選択されたロールを追加
        self.role_view.parent_view.hidden_role_ids.extend(selected_ids)
        
        # 選択フラグを立てる
        self.role_view.has_selected = True
        
        # 次へボタンを有効化
        self.role_view.next_btn.disabled = False
        self.role_view.next_btn.style = discord.ButtonStyle.green
        
        # 選択されたロール名を取得
        selected_role_names = []
        for role_id in self.role_view.parent_view.hidden_role_ids:
            role = self.role_view.guild.get_role(role_id)
            if role:
                selected_role_names.append(role.name)
        
        # 埋め込みに選択内容を表示
        if selected_role_names:
            roles_text = "\n".join([f"✓ {name[:30]}" for name in selected_role_names[:5]])  # 最大5個、30文字まで
            if len(selected_role_names) > 5:
                roles_text += f"\n\n... その他 {len(selected_role_names) - 5}個のロール"
            
            embed = discord.Embed(
                title="👁️ 閲覧可能ロール",
                description=f"```\nVCを閲覧できるロールを指定します\n\n【選択中のロール】\n{roles_text}\n```",
                color=0x5865F2
            )
        else:
            embed = discord.Embed(
                title="👁️ 閲覧可能ロール",
                description="```\nVCを閲覧できるロールを指定します\n\nドロップダウンからロールを選択してください\n```",
                color=0x5865F2
            )
        
        # ビューを更新（edit_messageを使う）
        await interaction.response.edit_message(embed=embed, view=self.role_view)


class VCCategorySelectView(discord.ui.View):
    """VC作成用カテゴリー選択ビュー"""
    
    def __init__(self, parent_view: VCSetupView, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.guild = guild
        self.page = page
        self.all_categories = [ch for ch in guild.channels if isinstance(ch, discord.CategoryChannel)]
        self.selected_category = None
        
        # カテゴリー選択ドロップダウンを追加
        self.add_item(VCCategorySelectDropdown(self, guild, page))
        
        # ページネーションボタンを追加
        if len(self.all_categories) > 25:
            if page > 0:
                self.add_item(VCCategoryPrevButton(self))
            if (page + 1) * 25 < len(self.all_categories):
                self.add_item(VCCategoryNextButton(self))
        
        # 次へボタンを追加（初期は無効）
        self.next_btn = discord.ui.Button(label="次へ", style=discord.ButtonStyle.secondary, row=4, disabled=True)
        self.next_btn.callback = self.next_step
        self.add_item(self.next_btn)
        
        # キャンセルボタンを追加
        cancel_btn = discord.ui.Button(label="キャンセル", style=discord.ButtonStyle.red, row=4)
        cancel_btn.callback = self.cancel
        self.add_item(cancel_btn)
    
    async def next_step(self, interaction: discord.Interaction):
        """次のステップへ"""
        parent_view = self.parent_view
        
        # カテゴリー選択のドロップダウンでedit_messageを使っているため、
        # 次へボタンでは新しいメッセージとして処理する
        
        # 名前変更制限と人数指定の両方がある場合、統合モーダルを表示
        if VCOption.LOCK_NAME in parent_view.selected_options and parent_view.vc_type == VCType.WITH_LIMIT:
            # モーダルはresponseでしか表示できないので、followupで案内
            if interaction.response.is_done():
                await interaction.followup.send(
                    "📝 次のメッセージで名前と人数を入力してください",
                    ephemeral=True
                )
                # 新しいメッセージでモーダルを表示するボタンを送る
                await interaction.followup.send(
                    "下のボタンをクリックして入力してください",
                    view=ModalTriggerView(parent_view, "combined"),
                    ephemeral=True
                )
            else:
                await interaction.response.send_modal(CombinedInputModal(parent_view))
        # 名前変更制限のみの場合
        elif VCOption.LOCK_NAME in parent_view.selected_options:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "📝 次のメッセージで名前を入力してください",
                    ephemeral=True
                )
                await interaction.followup.send(
                    "下のボタンをクリックして入力してください",
                    view=ModalTriggerView(parent_view, "name"),
                    ephemeral=True
                )
            else:
                await interaction.response.send_modal(LockedNameInputModal(parent_view))
        # 人数指定のみの場合
        elif parent_view.vc_type == VCType.WITH_LIMIT:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "📝 次のメッセージで人数を入力してください",
                    ephemeral=True
                )
                await interaction.followup.send(
                    "下のボタンをクリックして入力してください",
                    view=ModalTriggerView(parent_view, "limit"),
                    ephemeral=True
                )
            else:
                await interaction.response.send_modal(VCLimitInputModal(parent_view))
        # それ以外はVC作成
        else:
            # deferする（まだ応答していない場合のみ）
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            else:
                # 既に応答済みの場合は何もしない（create_vc_systemでfollowupを使う）
                pass
            
            await parent_view.create_vc_system(interaction)
    
    async def cancel(self, interaction: discord.Interaction):
        """キャンセル"""
        await interaction.response.send_message("❌ セットアップをキャンセルしました", ephemeral=True)
        self.stop()


class VCCategorySelectDropdown(discord.ui.Select):
    """VC作成用カテゴリー選択ドロップダウン"""
    
    def __init__(self, category_view: VCCategorySelectView, guild: discord.Guild, page: int):
        self.category_view = category_view
        
        # カテゴリーリストを取得（ページネーション対応）
        all_categories = [ch for ch in guild.channels if isinstance(ch, discord.CategoryChannel)]
        start_idx = page * 25
        end_idx = min(start_idx + 25, len(all_categories))
        page_categories = all_categories[start_idx:end_idx]
        
        options = [
            discord.SelectOption(label="新しいカテゴリーを作成", value="new", description="「VC管理システム」という名前で作成")
        ]
        
        for category in page_categories:
            options.append(discord.SelectOption(label=category.name, value=str(category.id)))
        
        super().__init__(
            placeholder="カテゴリーを選択してください",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "new":
            # 新しいカテゴリーを作成
            self.category_view.parent_view.location_mode = VCLocationMode.AUTO_CATEGORY
            self.category_view.parent_view.target_category_id = None
            self.category_view.selected_category = "new"
        else:
            # 既存のカテゴリーを選択
            self.category_view.parent_view.location_mode = VCLocationMode.SAME_CATEGORY
            self.category_view.parent_view.target_category_id = int(self.values[0])
            self.category_view.selected_category = int(self.values[0])
        
        # 次へボタンを有効化
        self.category_view.next_btn.disabled = False
        self.category_view.next_btn.style = discord.ButtonStyle.green
        
        # ビューを更新
        await interaction.response.edit_message(view=self.category_view)


class VCCategoryPrevButton(discord.ui.Button):
    """前のページボタン"""
    
    def __init__(self, category_view: VCCategorySelectView):
        super().__init__(label="前のページ", style=discord.ButtonStyle.gray, row=1)
        self.category_view = category_view
    
    async def callback(self, interaction: discord.Interaction):
        new_page = self.category_view.page - 1
        new_view = VCCategorySelectView(self.category_view.parent_view, self.category_view.guild, new_page)
        await interaction.response.edit_message(view=new_view)


class VCCategoryNextButton(discord.ui.Button):
    """次のページボタン"""
    
    def __init__(self, category_view: VCCategorySelectView):
        super().__init__(label="次のページ", style=discord.ButtonStyle.gray, row=1)
        self.category_view = category_view
    
    async def callback(self, interaction: discord.Interaction):
        new_page = self.category_view.page + 1
        new_view = VCCategorySelectView(self.category_view.parent_view, self.category_view.guild, new_page)
        await interaction.response.edit_message(view=new_view)



class VCLimitInputModal(discord.ui.Modal, title="人数制限を入力"):
    """VC作成時の人数入力モーダル"""
    
    def __init__(self, parent_view: VCSetupView):
        super().__init__()
        self.parent_view = parent_view
    
    user_limit_input = discord.ui.TextInput(
        label="人数制限",
        placeholder="1から25までの数字を入力してください",
        min_length=1,
        max_length=2,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.user_limit_input.value)
            if limit < 1 or limit > 25:
                await interaction.response.send_message("1から25の数字を入力してください", ephemeral=True)
                return
            
            self.parent_view.user_limit = limit
            
            # VC作成
            await interaction.response.defer(ephemeral=True)
            
            await self.parent_view.cog.create_vc_system(
                interaction.guild,
                self.parent_view.vc_type,
                self.parent_view.user_limit,
                self.parent_view.hub_role_ids,
                self.parent_view.vc_role_ids,
                self.parent_view.hidden_role_ids,
                self.parent_view.location_mode,
                self.parent_view.target_category_id,
                self.parent_view.source_channel,
                self.parent_view.selected_options,
                self.parent_view.locked_name
            )
            
            await interaction.followup.send("✅ VC管理システムを作成しました", ephemeral=True)
            self.parent_view.stop()
            
        except ValueError:
            await interaction.response.send_message("数字を入力してください", ephemeral=True)


class VCNameQuickEditView(discord.ui.View):
    """VC名クイック編集ビュー"""
    
    def __init__(self, vc: discord.VoiceChannel, owner: discord.Member, cog: VCManager):
        super().__init__(timeout=None)
        self.vc = vc
        self.owner = owner
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """作成者のみ操作可能"""
        if self.vc.id not in self.cog.active_vcs:
            return False
        if interaction.user.id != self.cog.active_vcs[self.vc.id]['owner_id']:
            return False
        return True
    
    @discord.ui.button(label="VC名変更", style=discord.ButtonStyle.primary)
    async def open_input(self, interaction: discord.Interaction, button: discord.ui.Button):
        """入力モーダルを開く"""
        await interaction.response.send_modal(VCNameQuickEditModal(self.vc, self.cog))


class VCNameQuickEditModal(discord.ui.Modal, title="VC名を入力"):
    """VC名クイック編集モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    name_input = discord.ui.TextInput(
        label="入力欄",
        placeholder="VC名を変更して何をしているか伝えよう",
        min_length=1,
        max_length=100,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_name = self.name_input.value
            
            # VCチャンネル名を変更
            await self.vc.edit(name=new_name)
            await interaction.response.send_message(f"VC名を「{new_name}」に変更しました", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました: {str(e)}", ephemeral=True)


# ============================================
# VC操作パネル用のViewクラス
# ============================================

class VCStateControlView(discord.ui.View):
    """状態操作ビュー"""
    
    def __init__(self, vc: discord.VoiceChannel, owner: discord.Member, cog: VCManager):
        super().__init__(timeout=None)
        self.vc = vc
        self.owner = owner
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.vc.id not in self.cog.active_vcs:
            return False
        if interaction.user.id != self.cog.active_vcs[self.vc.id]['owner_id']:
            return False
        return True
    
    @discord.ui.button(label="🔒 鍵をかける", style=discord.ButtonStyle.danger, row=0)
    async def lock_vc(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 全員の接続権限を拒否（許可リストを除く）
        overwrites = self.vc.overwrites
        overwrites[self.vc.guild.default_role] = discord.PermissionOverwrite(connect=False)
        
        # 許可リストのユーザーは接続可能に
        for user_id in self.cog.active_vcs[self.vc.id]['allowed_users']:
            user = self.vc.guild.get_member(user_id)
            if user:
                overwrites[user] = discord.PermissionOverwrite(connect=True)
        
        await self.vc.edit(overwrites=overwrites)
        self.cog.active_vcs[self.vc.id]['is_locked'] = True
        await interaction.response.send_message("鍵をかけました", ephemeral=True)
    
    @discord.ui.button(label="🔓 鍵を解除", style=discord.ButtonStyle.success, row=0)
    async def unlock_vc(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 接続権限を復元（BAN中のユーザーを除く）
        overwrites = self.vc.overwrites
        overwrites[self.vc.guild.default_role] = discord.PermissionOverwrite(connect=True)
        
        # BANユーザーは引き続き接続不可
        for user_id in self.cog.active_vcs[self.vc.id]['banned_users']:
            user = self.vc.guild.get_member(user_id)
            if user:
                overwrites[user] = discord.PermissionOverwrite(connect=False)
        
        await self.vc.edit(overwrites=overwrites)
        self.cog.active_vcs[self.vc.id]['is_locked'] = False
        await interaction.response.send_message("鍵を解除しました", ephemeral=True)
    
    @discord.ui.button(label="🔑 鍵許可を追加", style=discord.ButtonStyle.primary, row=1)
    async def allow_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCAllowUserModal(self.vc, self.cog))
    
    @discord.ui.button(label="🗑️ 鍵許可を削除", style=discord.ButtonStyle.secondary, row=1)
    async def remove_allow_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCRemoveAllowUserModal(self.vc, self.cog))
    
    @discord.ui.button(label="📋 鍵許可リスト表示", style=discord.ButtonStyle.secondary, row=2)
    async def show_allow_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_users = self.cog.active_vcs[self.vc.id].get('allowed_users', [])
        if not allowed_users:
            await interaction.response.send_message("鍵許可リストは空です", ephemeral=True)
            return
        
        user_info = []
        for user_id in allowed_users:
            user = interaction.guild.get_member(user_id)
            if user:
                user_info.append(f"スクリーンネーム: {user.display_name}\nスクリーンID: {user.name}")
            else:
                user_info.append(f"不明なユーザー\nID: {user_id}")
        
        if user_info:
            await interaction.response.send_message(
                f"**鍵許可リスト:**\n" + "\n\n".join(user_info),
                ephemeral=True
            )
        else:
            await interaction.response.send_message("鍵許可リストは空です", ephemeral=True)
    
    @discord.ui.button(label="👁️ 表示", style=discord.ButtonStyle.success, row=3)
    async def show_vc(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 現在の権限を保持したまま、view_channelのみ変更
        overwrites = self.vc.overwrites.copy()
        
        # システムデータから閲覧可能ロールを取得
        system_data = self.cog.active_vcs[self.vc.id].get('system_data', {})
        hidden_roles = system_data.get('hidden_roles', [])
        vc_roles = system_data.get('vc_roles', [])
        
        # 鍵の状態を取得
        is_locked = self.cog.active_vcs[self.vc.id].get('is_locked', False)
        allowed_users = self.cog.active_vcs[self.vc.id].get('allowed_users', [])
        banned_users = self.cog.active_vcs[self.vc.id].get('banned_users', [])
        
        if hidden_roles:
            # 閲覧可能ロールが設定されている場合
            # デフォルトは非表示
            existing_default = overwrites.get(self.vc.guild.default_role, discord.PermissionOverwrite())
            overwrites[self.vc.guild.default_role] = discord.PermissionOverwrite(
                view_channel=False,
                connect=existing_default.connect  # connectは維持
            )
            
            # 閲覧可能ロールを持つ人は表示
            for role_id in hidden_roles:
                role = self.vc.guild.get_role(role_id)
                if role:
                    existing = overwrites.get(role, discord.PermissionOverwrite())
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        connect=existing.connect if existing.connect is not None else True
                    )
        else:
            # 閲覧可能ロールがない場合は全員に表示
            existing_default = overwrites.get(self.vc.guild.default_role, discord.PermissionOverwrite())
            overwrites[self.vc.guild.default_role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=existing_default.connect  # connectは維持
            )
        
        # 表示許可リストのユーザーも見えるようにする（connectは維持）
        for user_id in self.cog.active_vcs[self.vc.id].get('view_allowed_users', []):
            user = self.vc.guild.get_member(user_id)
            if user:
                existing = overwrites.get(user, discord.PermissionOverwrite())
                overwrites[user] = discord.PermissionOverwrite(
                    view_channel=True,
                    connect=existing.connect if existing.connect is not None else True
                )
        
        # BANユーザーと鍵の状態を再適用
        for user_id in banned_users:
            user = self.vc.guild.get_member(user_id)
            if user:
                existing = overwrites.get(user, discord.PermissionOverwrite())
                overwrites[user] = discord.PermissionOverwrite(
                    view_channel=existing.view_channel,
                    connect=False
                )
        
        if is_locked:
            # 鍵がかかっている場合、許可リスト以外は接続不可
            for member in self.vc.guild.members:
                if member.id not in allowed_users and member.id not in banned_users and not member.bot:
                    existing = overwrites.get(member, discord.PermissionOverwrite())
                    if existing.view_channel is not False:  # 見える人だけ処理
                        overwrites[member] = discord.PermissionOverwrite(
                            view_channel=existing.view_channel,
                            connect=False
                        )
        
        await self.vc.edit(overwrites=overwrites)
        await interaction.response.send_message("チャンネルを表示しました", ephemeral=True)
    
    @discord.ui.button(label="👁️ 非表示", style=discord.ButtonStyle.danger, row=3)
    async def hide_vc(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 現在の権限を保持したまま、view_channelのみ変更
        overwrites = self.vc.overwrites.copy()
        
        # 全員を非表示にする（connectは維持）
        existing_default = overwrites.get(self.vc.guild.default_role, discord.PermissionOverwrite())
        overwrites[self.vc.guild.default_role] = discord.PermissionOverwrite(
            view_channel=False,
            connect=existing_default.connect  # connectは維持
        )
        
        # 全てのロールとユーザーも非表示にする（connectは維持）
        for target, perm in list(overwrites.items()):
            if target != self.vc.guild.me:  # Bot以外
                overwrites[target] = discord.PermissionOverwrite(
                    view_channel=False,
                    connect=perm.connect  # connectは維持
                )
        
        # Botは必ず見える
        overwrites[self.vc.guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        
        await self.vc.edit(overwrites=overwrites)
        await interaction.response.send_message("チャンネルを非表示にしました", ephemeral=True)
    
    @discord.ui.button(label="👁️ 表示許可を追加", style=discord.ButtonStyle.primary, row=4)
    async def add_view_allow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCViewAllowUserModal(self.vc, self.cog))
    
    @discord.ui.button(label="🗑️ 表示許可を削除", style=discord.ButtonStyle.secondary, row=4)
    async def remove_view_allow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCRemoveViewAllowUserModal(self.vc, self.cog))
    
    @discord.ui.button(label="📋 表示許可リスト表示", style=discord.ButtonStyle.secondary, row=4)
    async def show_view_allow_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        view_allowed_users = self.cog.active_vcs[self.vc.id].get('view_allowed_users', [])
        if not view_allowed_users:
            await interaction.response.send_message("表示許可リストは空です", ephemeral=True)
            return
        
        user_info = []
        for user_id in view_allowed_users:
            user = interaction.guild.get_member(user_id)
            if user:
                user_info.append(f"スクリーンネーム: {user.display_name}\nスクリーンID: {user.name}")
            else:
                user_info.append(f"不明なユーザー\nID: {user_id}")
        
        if user_info:
            await interaction.response.send_message(
                f"**表示許可リスト:**\n" + "\n\n".join(user_info),
                ephemeral=True
            )
        else:
            await interaction.response.send_message("表示許可リストは空です", ephemeral=True)


class VCBanControlView(discord.ui.View):
    """参加制限ビュー"""
    
    def __init__(self, vc: discord.VoiceChannel, owner: discord.Member, cog: VCManager):
        super().__init__(timeout=None)
        self.vc = vc
        self.owner = owner
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.vc.id not in self.cog.active_vcs:
            return False
        if interaction.user.id != self.cog.active_vcs[self.vc.id]['owner_id']:
            return False
        return True
    
    @discord.ui.button(label="🚫 ユーザーをブロック", style=discord.ButtonStyle.danger, row=0)
    async def add_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCBanUserModal(self.vc, self.cog, ban=True))
    
    @discord.ui.button(label="✅ ブロック解除", style=discord.ButtonStyle.success, row=0)
    async def remove_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCBanUserModal(self.vc, self.cog, ban=False))
    
    @discord.ui.button(label="📋 ブロックリスト表示", style=discord.ButtonStyle.secondary, row=1)
    async def show_ban_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        banned_users = self.cog.active_vcs[self.vc.id].get('banned_users', [])
        if not banned_users:
            await interaction.response.send_message("ブロックリストは空です", ephemeral=True)
            return
        
        user_info = []
        for user_id in banned_users:
            user = interaction.guild.get_member(user_id)
            if user:
                user_info.append(f"スクリーンネーム: {user.display_name}\nスクリーンID: {user.name}")
            else:
                user_info.append(f"不明なユーザー\nID: {user_id}")
        
        await interaction.response.send_message(
            f"**ブロックリスト:**\n" + "\n\n".join(user_info),
            ephemeral=True
        )


class VCBanUserModal(discord.ui.Modal, title="スクリーンID入力"):
    """BANユーザー入力モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager, ban: bool):
        super().__init__()
        self.vc = vc
        self.cog = cog
        self.ban = ban
    
    user_id_input = discord.ui.TextInput(
        label="スクリーンID",
        placeholder="例: taro123",
        min_length=1,
        max_length=32,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            screen_id = self.user_id_input.value.strip()
            
            # スクリーンID（name）でユーザーを検索
            user = None
            for member in interaction.guild.members:
                if member.name == screen_id:
                    user = member
                    break
            
            if not user:
                await interaction.response.send_message(f"スクリーンID「{screen_id}」のユーザーが見つかりません", ephemeral=True)
                return
            
            user_id = user.id
            
            owner_id = self.cog.active_vcs[self.vc.id]['owner_id']
            
            if self.ban:
                # BAN追加
                if user_id not in self.cog.active_vcs[self.vc.id]['banned_users']:
                    self.cog.active_vcs[self.vc.id]['banned_users'].append(user_id)
                
                # データベースに保存
                self.cog.db.add_banned_user(owner_id, user_id)
                
                # 許可リストからも削除
                if user_id in self.cog.active_vcs[self.vc.id]['allowed_users']:
                    self.cog.active_vcs[self.vc.id]['allowed_users'].remove(user_id)
                
                overwrites = self.vc.overwrites
                overwrites[user] = discord.PermissionOverwrite(connect=False)
                await self.vc.edit(overwrites=overwrites)
                
                # VCから強制切断
                if user in self.vc.members:
                    try:
                        await user.move_to(None)
                    except Exception as e:
                        logger.warning(f"⚠️ ユーザー切断エラー (User: {user.name}): {e}")
                
                await interaction.response.send_message(f"{user.name}をブロックして切断しました", ephemeral=True)
            else:
                # BAN解除
                if user_id in self.cog.active_vcs[self.vc.id]['banned_users']:
                    self.cog.active_vcs[self.vc.id]['banned_users'].remove(user_id)
                
                # データベースから削除
                self.cog.db.remove_banned_user(owner_id, user_id)
                
                overwrites = self.vc.overwrites
                is_locked = self.cog.active_vcs[self.vc.id].get('is_locked', False)
                
                if is_locked:
                    # 鍵がかかっている場合は接続不可のまま
                    if user in overwrites:
                        del overwrites[user]
                else:
                    # 鍵がかかっていない場合は接続可能に
                    if user in overwrites:
                        del overwrites[user]
                
                await self.vc.edit(overwrites=overwrites)
                await interaction.response.send_message(f"{user.name}のブロックを解除しました", ephemeral=True)
                
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました", ephemeral=True)


class VCAllowUserModal(discord.ui.Modal, title="許可リストにユーザーを追加"):
    """許可リスト追加モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    user_id_input = discord.ui.TextInput(
        label="スクリーンID",
        placeholder="例: taro123",
        min_length=1,
        max_length=32,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            screen_id = self.user_id_input.value.strip()
            
            # スクリーンID（name）でユーザーを検索
            user = None
            for member in interaction.guild.members:
                if member.name == screen_id:
                    user = member
                    break
            
            if not user:
                await interaction.response.send_message(f"スクリーンID「{screen_id}」のユーザーが見つかりません", ephemeral=True)
                return
            
            user_id = user.id
            
            # BANリストに含まれている場合は追加不可
            if user_id in self.cog.active_vcs[self.vc.id]['banned_users']:
                await interaction.response.send_message(f"{user.name}はブロック中のため許可できません", ephemeral=True)
                return
            
            # 許可リストに追加
            if user_id not in self.cog.active_vcs[self.vc.id]['allowed_users']:
                self.cog.active_vcs[self.vc.id]['allowed_users'].append(user_id)
            
            # 接続権限を付与
            overwrites = self.vc.overwrites
            overwrites[user] = discord.PermissionOverwrite(connect=True)
            await self.vc.edit(overwrites=overwrites)
            
            await interaction.response.send_message(f"{user.name}を許可リストに追加しました", ephemeral=True)
            
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました", ephemeral=True)


class VCRemoveAllowUserModal(discord.ui.Modal, title="許可リストからユーザーを削除"):
    """許可リスト削除モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    user_id_input = discord.ui.TextInput(
        label="スクリーンID",
        placeholder="例: taro123",
        min_length=1,
        max_length=32,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            screen_id = self.user_id_input.value.strip()
            
            # スクリーンID（name）でユーザーを検索
            user = None
            for member in interaction.guild.members:
                if member.name == screen_id:
                    user = member
                    break
            
            if not user:
                await interaction.response.send_message(f"スクリーンID「{screen_id}」のユーザーが見つかりません", ephemeral=True)
                return
            
            user_id = user.id
            
            # 許可リストから削除
            if user_id in self.cog.active_vcs[self.vc.id]['allowed_users']:
                self.cog.active_vcs[self.vc.id]['allowed_users'].remove(user_id)
            
            # 接続権限を削除（鍵がかかっている場合は接続不可に）
            overwrites = self.vc.overwrites
            is_locked = self.cog.active_vcs[self.vc.id].get('is_locked', False)
            
            if is_locked:
                # 鍵がかかっている場合は削除（デフォルトの接続不可に戻る）
                if user in overwrites:
                    del overwrites[user]
            else:
                # 鍵がかかっていない場合も削除（デフォルトの接続可能に戻る）
                if user in overwrites:
                    del overwrites[user]
            
            await self.vc.edit(overwrites=overwrites)
            
            await interaction.response.send_message(f"{user.name}を許可リストから削除しました", ephemeral=True)
            
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました", ephemeral=True)


class VCViewAllowUserModal(discord.ui.Modal, title="表示許可リストにユーザーを追加"):
    """表示許可リスト追加モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    user_id_input = discord.ui.TextInput(
        label="スクリーンID",
        placeholder="例: taro123",
        min_length=1,
        max_length=32,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            screen_id = self.user_id_input.value.strip()
            
            # スクリーンID（name）でユーザーを検索
            user = None
            for member in interaction.guild.members:
                if member.name == screen_id:
                    user = member
                    break
            
            if not user:
                await interaction.response.send_message(f"スクリーンID「{screen_id}」のユーザーが見つかりません", ephemeral=True)
                return
            
            user_id = user.id
            
            # システムデータから閲覧可能ロールを取得
            system_data = self.cog.active_vcs[self.vc.id].get('system_data', {})
            hidden_roles = system_data.get('hidden_roles', [])
            
            # 閲覧可能ロールが設定されている場合、そのロールを持っているかチェック
            if hidden_roles:
                user_has_role = any(role_id in [r.id for r in user.roles] for role_id in hidden_roles)
                if user_has_role:
                    await interaction.response.send_message(
                        f"{user.name}は既に閲覧可能ロールを持っているため、表示許可リストに追加できません",
                        ephemeral=True
                    )
                    return
            
            # 表示許可リストに追加
            if user_id not in self.cog.active_vcs[self.vc.id]['view_allowed_users']:
                self.cog.active_vcs[self.vc.id]['view_allowed_users'].append(user_id)
            
            # 閲覧権限を付与
            overwrites = self.vc.overwrites
            overwrites[user] = discord.PermissionOverwrite(view_channel=True, connect=True)
            await self.vc.edit(overwrites=overwrites)
            
            await interaction.response.send_message(f"{user.name}を表示許可リストに追加しました", ephemeral=True)
            
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました", ephemeral=True)


class VCRemoveViewAllowUserModal(discord.ui.Modal, title="表示許可リストからユーザーを削除"):
    """表示許可リスト削除モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    user_id_input = discord.ui.TextInput(
        label="スクリーンID",
        placeholder="例: taro123",
        min_length=1,
        max_length=32,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            screen_id = self.user_id_input.value.strip()
            
            # スクリーンID（name）でユーザーを検索
            user = None
            for member in interaction.guild.members:
                if member.name == screen_id:
                    user = member
                    break
            
            if not user:
                await interaction.response.send_message(f"スクリーンID「{screen_id}」のユーザーが見つかりません", ephemeral=True)
                return
            
            user_id = user.id
            
            # 表示許可リストから削除
            if user_id in self.cog.active_vcs[self.vc.id]['view_allowed_users']:
                self.cog.active_vcs[self.vc.id]['view_allowed_users'].remove(user_id)
            
            # 閲覧権限を削除
            overwrites = self.vc.overwrites
            
            # システムデータから閲覧可能ロールを取得
            system_data = self.cog.active_vcs[self.vc.id].get('system_data', {})
            hidden_roles = system_data.get('hidden_roles', [])
            
            if hidden_roles:
                # 閲覧可能ロールが設定されている場合、そのロールを持っていなければ見えなくする
                user_has_role = any(role_id in [r.id for r in user.roles] for role_id in hidden_roles)
                if not user_has_role:
                    # ロールを持っていないので非表示
                    overwrites[user] = discord.PermissionOverwrite(view_channel=False, connect=False)
                else:
                    # ロールを持っているので削除（デフォルトに戻る）
                    if user in overwrites:
                        del overwrites[user]
            else:
                # 閲覧可能ロールがない場合は削除（デフォルトに戻る）
                if user in overwrites:
                    del overwrites[user]
            
            await self.vc.edit(overwrites=overwrites)
            
            await interaction.response.send_message(f"{user.name}を表示許可リストから削除しました", ephemeral=True)
            
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました", ephemeral=True)


class VCLimitControlView(discord.ui.View):
    """人数制限ビュー"""
    
    def __init__(self, vc: discord.VoiceChannel, owner: discord.Member, cog: VCManager):
        super().__init__(timeout=None)
        self.vc = vc
        self.owner = owner
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.vc.id not in self.cog.active_vcs:
            return False
        if interaction.user.id != self.cog.active_vcs[self.vc.id]['owner_id']:
            return False
        return True
    
    @discord.ui.button(label="🔢 人数を設定", style=discord.ButtonStyle.primary, row=0)
    async def set_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCUserLimitModal(self.vc, self.cog))
    
    @discord.ui.button(label="♾️ 制限解除", style=discord.ButtonStyle.secondary, row=0)
    async def remove_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vc.edit(user_limit=0)
        await interaction.response.send_message("人数制限を解除しました", ephemeral=True)


class VCUserLimitModal(discord.ui.Modal, title="人数制限設定"):
    """人数制限入力モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    limit_input = discord.ui.TextInput(
        label="人数制限",
        placeholder="1から25までの数字を入力してください",
        min_length=1,
        max_length=2,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.limit_input.value)
            if limit < 1 or limit > 25:
                await interaction.response.send_message("1から25の範囲で入力してください", ephemeral=True)
                return
            
            # BOT数をカウント
            bot_count = sum(1 for m in self.vc.members if m.bot)
            
            # BOT数を加算した人数制限を設定
            adjusted_limit = limit + bot_count
            
            # VCデータを更新
            self.cog.active_vcs[self.vc.id]['original_limit'] = limit
            self.cog.active_vcs[self.vc.id]['bot_count'] = bot_count
            
            await self.vc.edit(user_limit=adjusted_limit)
            
            if bot_count > 0:
                await interaction.response.send_message(f"人数制限を{limit}人に設定しました（BOT {bot_count}体分を加算: 実質{adjusted_limit}人）", ephemeral=True)
            else:
                await interaction.response.send_message(f"人数制限を{limit}人に設定しました", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("数字で入力してください", ephemeral=True)


class VCNameControlView(discord.ui.View):
    """名前変更ビュー"""
    
    def __init__(self, vc: discord.VoiceChannel, owner: discord.Member, cog: VCManager):
        super().__init__(timeout=None)
        self.vc = vc
        self.owner = owner
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.vc.id not in self.cog.active_vcs:
            return False
        if interaction.user.id != self.cog.active_vcs[self.vc.id]['owner_id']:
            return False
        return True
    
    @discord.ui.button(label="✏️ 名前を変更", style=discord.ButtonStyle.primary, row=0)
    async def change_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCNameChangeModal(self.vc))
    
    @discord.ui.button(label="🔄 初期状態に戻す", style=discord.ButtonStyle.secondary, row=0)
    async def reset_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        original_name = self.cog.active_vcs[self.vc.id]['original_name']
        await self.vc.edit(name=original_name)
        await interaction.response.send_message(f"名前を「{original_name}」に戻しました", ephemeral=True)


class VCNameChangeModal(discord.ui.Modal, title="VC名を変更"):
    """VC名変更モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel):
        super().__init__()
        self.vc = vc
    
    name_input = discord.ui.TextInput(
        label="新しいVC名",
        placeholder="変更後のVC名を入力してください",
        min_length=1,
        max_length=100,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.name_input.value.strip()
        await self.vc.edit(name=new_name)
        await interaction.response.send_message(f"VC名を「{new_name}」に変更しました", ephemeral=True)


class ModalTriggerView(discord.ui.View):
    """モーダルを表示するためのトリガービュー"""
    
    def __init__(self, parent_view: VCSetupView, modal_type: str):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.modal_type = modal_type
    
    @discord.ui.button(label="📝 入力する", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.modal_type == "combined":
            await interaction.response.send_modal(CombinedInputModal(self.parent_view))
        elif self.modal_type == "name":
            await interaction.response.send_modal(LockedNameInputModal(self.parent_view))
        elif self.modal_type == "limit":
            await interaction.response.send_modal(VCLimitInputModal(self.parent_view))


class VCOwnershipTransferView(discord.ui.View):
    """権限譲渡ビュー"""
    
    def __init__(self, vc: discord.VoiceChannel, owner: discord.Member, cog: VCManager):
        super().__init__(timeout=None)
        self.vc = vc
        self.owner = owner
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.vc.id not in self.cog.active_vcs:
            return False
        if interaction.user.id != self.cog.active_vcs[self.vc.id]['owner_id']:
            return False
        return True
    
    @discord.ui.button(label="👑 管理者を譲渡", style=discord.ButtonStyle.danger, row=0)
    async def transfer_ownership(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCOwnershipTransferModal(self.vc, self.cog))


class VCOwnershipTransferModal(discord.ui.Modal, title="権限譲渡"):
    """権限譲渡入力モーダル"""
    
    def __init__(self, vc: discord.VoiceChannel, cog: VCManager):
        super().__init__()
        self.vc = vc
        self.cog = cog
    
    user_name_input = discord.ui.TextInput(
        label="新しい管理者のスクリーンネーム",
        placeholder="スクリーンネームを入力してください",
        min_length=1,
        max_length=32,
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            screen_name = self.user_name_input.value.strip()
            
            # スクリーンネームでユーザーを検索
            user = None
            for member in interaction.guild.members:
                if member.name == screen_name:
                    user = member
                    break
            
            if not user:
                await interaction.response.send_message(f"スクリーンネーム「{screen_name}」のユーザーが見つかりません", ephemeral=True)
                return
            
            if user.bot:
                await interaction.response.send_message("❌ BOTには権限を譲渡できません", ephemeral=True)
                return
            
            # VCに参加しているかチェック
            if user not in self.vc.members:
                await interaction.response.send_message(f"❌ {user.mention} はVCに参加していません。\n権限を譲渡するには、対象ユーザーがVCに参加している必要があります。", ephemeral=True)
                return
            
            user_id = user.id
            
            # 権限譲渡
            old_owner_id = self.cog.active_vcs[self.vc.id]['owner_id']
            self.cog.active_vcs[self.vc.id]['owner_id'] = user_id
            
            # 操作チャンネルの権限を更新
            control_channel_id = self.cog.active_vcs[self.vc.id].get('control_channel_id')
            if control_channel_id:
                control_channel = interaction.guild.get_channel(control_channel_id)
                if control_channel:
                    old_owner = interaction.guild.get_member(old_owner_id)
                    if old_owner:
                        await control_channel.set_permissions(old_owner, overwrite=None)
                    await control_channel.set_permissions(user, read_messages=True, send_messages=True)
                    await control_channel.send(f"{user.mention} 管理権限が譲渡されました")
            
            await interaction.response.send_message(f"{user.name}に管理権限を譲渡しました", ephemeral=True)
            
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました", ephemeral=True)


# ============================================================
# ステップ式セットアップView（一つずつ方式）
# ============================================================

class VCStep1_Type(discord.ui.View):
    """ステップ1: VCタイプ選択"""
    def __init__(self, cog, original_interaction):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        options = [
            discord.SelectOption(label="人数制限なし", value="no_limit", description="作成されるVCごとの人数制限を設けない"),
            discord.SelectOption(label="人数制限を付ける", value="with_limit", description="上限人数を決めてVCを作成")
        ]
        self.select = discord.ui.Select(placeholder="人数制限の有無を選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        try:
            vc_type = VCType.WITH_LIMIT if self.select.values[0] == "with_limit" else VCType.NO_LIMIT
            type_text = "人数指定" if vc_type == VCType.WITH_LIMIT else "人数指定なし"

            if vc_type == VCType.WITH_LIMIT:
                modal = VCUserLimitModal(self.cog, self.original_interaction, vc_type)
                await interaction.response.send_modal(modal)
            else:
                embed = discord.Embed(
                    title="🎭 VC管理システム セットアップ",
                    description=f"**ステップ 3/9: VC作成権限**\n\n✅ VCタイプ: **{type_text}**\nVCを作成できるユーザーをロールで制限するか選択してください。",
                    color=0x5865F2)
                view = VCStep3_HubRole(self.cog, self.original_interaction, vc_type, user_limit=0)
                await interaction.response.edit_message(embed=embed, view=view)
        except Exception as e:
            logger.error(f"VCタイプ選択エラー: {e}")


class VCUserLimitModal(discord.ui.Modal, title="人数制限を入力"):
    """人数制限値入力モーダル"""
    limit_input = discord.ui.TextInput(
        label="人数を指定してください（2〜25）",
        style=discord.TextStyle.short,
        placeholder="例: 4",
        required=True,
        max_length=2,
        min_length=1
    )

    def __init__(self, cog, original_interaction, vc_type):
        super().__init__()
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user_limit = int(self.limit_input.value)
        except ValueError:
            await interaction.response.send_message("❌ 数値を入力してください", ephemeral=True)
            return

        if user_limit < 2 or user_limit > 25:
            await interaction.response.send_message("❌ 人数は2-25の範囲で入力してください", ephemeral=True)
            return

        await interaction.response.defer(thinking=False)
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=f"**ステップ 3/9: VC作成権限**\n\n✅ VCタイプ: **人数指定**\n✅ 人数制限: **{user_limit}人**\nVCを作成できるユーザーをロールで制限するか選択してください。",
            color=0x5865F2)
        view = VCStep3_HubRole(self.cog, self.original_interaction, self.vc_type, user_limit)
        await self.original_interaction.edit_original_response(embed=embed, view=view)



class PaginatedRoleSelectView(discord.ui.View):
    """ロールを25件ずつ表示して選択する共通ビュー"""

    chunk_size = 25

    def __init__(
        self,
        *,
        guild: discord.Guild,
        title: str,
        description: str,
        placeholder: str,
        roles: List[discord.Role],
        on_complete,
        on_skip,
        allow_empty_confirm: bool = False,
        color: int = 0x5865F2,
    ):
        super().__init__(timeout=300)
        self.guild = guild
        self.title = title
        self.description = description
        self.placeholder = placeholder
        self.available_roles = [role for role in roles if role and role != guild.default_role]
        self.on_complete = on_complete
        self.on_skip = on_skip
        self.allow_empty_confirm = allow_empty_confirm
        self.color = color
        self.selected_role_ids: List[int] = []
        self.current_page = 0
        self.role_select: Optional[discord.ui.Select] = None
        self.total_pages = max(1, math.ceil(len(self.available_roles) / self.chunk_size)) if self.available_roles else 1

        self._build_role_dropdown()
        self._build_controls()

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

        self.skip_button = discord.ui.Button(label="スキップ（指定なし）", style=discord.ButtonStyle.secondary, row=2, disabled=self.on_skip is None)
        self.skip_button.callback = self._skip_selection
        self.add_item(self.skip_button)

        self.clear_button = discord.ui.Button(label="選択をクリア", style=discord.ButtonStyle.danger, row=2)
        self.clear_button.callback = self._clear_selection
        self.add_item(self.clear_button)

        if not self.available_roles:
            self.confirm_button.disabled = not self.allow_empty_confirm
            self.prev_button.disabled = True
            self.next_button.disabled = True

    def _build_role_dropdown(self):
        if self.role_select:
            self.remove_item(self.role_select)
            self.role_select = None

        chunk = self._get_current_chunk()
        if not chunk:
            return

        options = [
            discord.SelectOption(label=role.name[:100], value=str(role.id))
            for role in chunk
        ]
        placeholder = f"{self.placeholder} ({self.current_page + 1}/{self.total_pages})"
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

    def _get_current_chunk(self) -> List[discord.Role]:
        if not self.available_roles:
            return []
        start = self.current_page * self.chunk_size
        end = start + self.chunk_size
        return self.available_roles[start:end]

    def build_embed(self) -> discord.Embed:
        summary = format_role_list(self.guild, self.selected_role_ids)
        desc = f"{self.description}\n\n**現在の選択:** {summary}"
        embed = discord.Embed(title=self.title, description=desc, color=self.color)
        if self.available_roles:
            embed.set_footer(text=f"ページ {self.current_page + 1}/{self.total_pages}")
        else:
            embed.set_footer(text="選択できるロールがありません")
        return embed

    async def _go_prev(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page - 1) % self.total_pages
        self._build_role_dropdown()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _go_next(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page + 1) % self.total_pages
        self._build_role_dropdown()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _clear_selection(self, interaction: discord.Interaction):
        self.selected_role_ids.clear()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _skip_selection(self, interaction: discord.Interaction):
        if not self.on_skip:
            await interaction.response.send_message("スキップできません", ephemeral=True)
            return
        await self.on_skip(interaction)

    async def _confirm_selection(self, interaction: discord.Interaction):
        if not self.selected_role_ids and not self.allow_empty_confirm:
            await interaction.response.send_message("少なくとも1つのロールを選択してください。", ephemeral=True)
            return
        if not self.on_complete:
            await interaction.response.send_message("次のステップに進めませんでした。", ephemeral=True)
            return
        await self.on_complete(interaction, list(self.selected_role_ids))

    async def _on_select(self, interaction: discord.Interaction):
        updated = False
        for value in getattr(self.role_select, 'values', []):
            role_id = int(value)
            if role_id not in self.selected_role_ids:
                self.selected_role_ids.append(role_id)
                updated = True
        if updated:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.defer()

class VCStep3_HubRole(discord.ui.View):
    """ステップ3: VC作成権限"""
    def __init__(self, cog, original_interaction, vc_type, user_limit):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        options = [
            discord.SelectOption(label="制限なし", value="none", description="誰でもハブVCからVCを作成できる"),
            discord.SelectOption(label="ロール指定", value="specify", description="指定したロールだけがVCを作成できる")]
        self.select = discord.ui.Select(placeholder="VC作成権限を選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    def _build_next_embed(self, guild: discord.Guild, hub_role_ids: List[int]) -> discord.Embed:
        role_text, count = summarize_role_names(guild, hub_role_ids)
        if count == 0:
            summary = "✅ VC作成: **制限なし**"
        else:
            summary = f"✅ VC作成: **{role_text}** ({count}件)"
        description = (
            "**ステップ 4/9: 入室ロール設定**\n\n"
            f"{summary}\n"
            "作成されたVCに入場できるロールを設定します。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def _proceed(self, interaction: discord.Interaction, hub_role_ids: List[int]):
        embed = self._build_next_embed(interaction.guild, hub_role_ids)
        view = VCStep4_VCRole(self.cog, self.original_interaction, self.vc_type, self.user_limit, hub_role_ids)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_select(self, interaction: discord.Interaction):
        try:
            mode = self.select.values[0]
            if mode == "none":
                await self._proceed(interaction, [])
                return

            guild = interaction.guild
            roles = [r for r in guild.roles if r != guild.default_role]
            if not roles:
                await self._proceed(interaction, [])
                return

            async def handle_complete(select_interaction: discord.Interaction, selected_ids: List[int]):
                valid_ids = [rid for rid in selected_ids if select_interaction.guild.get_role(rid)]
                if not valid_ids:
                    await select_interaction.response.send_message("選択したロールが見つかりませんでした。", ephemeral=True)
                    return
                await self._proceed(select_interaction, valid_ids)

            async def handle_skip(skip_interaction: discord.Interaction):
                await self._proceed(skip_interaction, [])

            selector_view = PaginatedRoleSelectView(
                guild=guild,
                title="🎭 VC管理システム セットアップ",
                description=(
                    "**ステップ 3-2/9: VC作成ロール選択**\n\n"
                    "VCを作成できるロールを選択してください。必要なロールがなければスキップを押してください。"
                ),
                placeholder="VCを作成できるロールを選択",
                roles=roles,
                on_complete=handle_complete,
                on_skip=handle_skip
            )
            await interaction.response.edit_message(embed=selector_view.build_embed(), view=selector_view)
        except Exception as e:
            logger.error(f"ハブVCロール選択エラー: {e}")


class VCStep4_VCRole(discord.ui.View):
    """ステップ4: 入室ロール設定"""
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        options = [
            discord.SelectOption(label="制限なし", value="none", description="作成されたVCに誰でも入室できる"),
            discord.SelectOption(label="ロール指定", value="specify", description="指定したロールだけが入室できる")]
        self.select = discord.ui.Select(placeholder="入室ロールの制限を選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    def _build_step5_embed(self, guild: discord.Guild, vc_role_ids: List[int]) -> discord.Embed:
        role_text, count = summarize_role_names(guild, vc_role_ids)
        if count == 0:
            summary = "✅ 入場ロール: **制限なし**"
        else:
            summary = f"✅ 入場ロール: **{role_text}** ({count}件)"
        description = (
            "**ステップ 5/9: 表示対象ロール**\n\n"
            f"{summary}\n"
            "VCを表示する相手を設定します。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def _proceed(self, interaction: discord.Interaction, vc_role_ids: List[int]):
        embed = self._build_step5_embed(interaction.guild, vc_role_ids)
        view = VCStep5_HiddenRole(self.cog, self.original_interaction, self.vc_type, self.user_limit, self.hub_role_ids, vc_role_ids)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_select(self, interaction: discord.Interaction):
        try:
            mode = self.select.values[0]
            if mode == "none":
                await self._proceed(interaction, [])
                return

            guild = interaction.guild
            roles = [r for r in guild.roles if r != guild.default_role]
            if not roles:
                await self._proceed(interaction, [])
                return

            async def handle_complete(select_interaction: discord.Interaction, selected_ids: List[int]):
                valid_ids = [rid for rid in selected_ids if select_interaction.guild.get_role(rid)]
                if not valid_ids:
                    await select_interaction.response.send_message("選択したロールが見つかりませんでした。", ephemeral=True)
                    return
                await self._proceed(select_interaction, valid_ids)

            async def handle_skip(skip_interaction: discord.Interaction):
                await self._proceed(skip_interaction, [])

            selector_view = PaginatedRoleSelectView(
                guild=guild,
                title="🎭 VC管理システム セットアップ",
                description=(
                    "**ステップ 4-2/9: 入室ロール選択**\n\n"
                    "作成されたVCに入場できるロールを選択してください。必要なロールが無ければスキップできます。"
                ),
                placeholder="作成されたVCに入場できるロールを選択",
                roles=roles,
                on_complete=handle_complete,
                on_skip=handle_skip
            )
            await interaction.response.edit_message(embed=selector_view.build_embed(), view=selector_view)
        except Exception as e:
            logger.error(f"入室ロール設定エラー: {e}")


class VCStep5_HiddenRole(discord.ui.View):
    """ステップ5: 表示対象ロール設定"""
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        options = [
            discord.SelectOption(label="全員に表示", value="none", description="VCを全員に表示"),
            discord.SelectOption(label="ロール指定", value="specify", description="指定したロールだけに表示")]
        self.select = discord.ui.Select(placeholder="VCを表示する相手を選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    def _build_step6_embed(self, guild: discord.Guild, hidden_role_ids: List[int]) -> discord.Embed:
        role_text, count = summarize_role_names(guild, hidden_role_ids)
        if count == 0:
            summary = "✅ 表示対象: **全員**"
        else:
            summary = f"✅ 表示対象: **{role_text}** ({count}件)"
        description = (
            "**ステップ 6/9: VCオプション**\n\n"
            f"{summary}\n"
            "作成されるVCに適用するオプションを選択してください。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def _proceed(self, interaction: discord.Interaction, hidden_role_ids: List[int]):
        embed = self._build_step6_embed(interaction.guild, hidden_role_ids)
        view = VCStep6_Options(self.cog, self.original_interaction, self.vc_type, self.user_limit, self.hub_role_ids, self.vc_role_ids, hidden_role_ids)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_select(self, interaction: discord.Interaction):
        try:
            mode = self.select.values[0]
            if mode == "none":
                await self._proceed(interaction, [])
                return

            guild = interaction.guild
            roles = [r for r in guild.roles if r != guild.default_role]
            if not roles:
                await self._proceed(interaction, [])
                return

            async def handle_complete(select_interaction: discord.Interaction, selected_ids: List[int]):
                valid_ids = [rid for rid in selected_ids if select_interaction.guild.get_role(rid)]
                if not valid_ids:
                    await select_interaction.response.send_message("選択したロールが見つかりませんでした。", ephemeral=True)
                    return
                await self._proceed(select_interaction, valid_ids)

            async def handle_skip(skip_interaction: discord.Interaction):
                await self._proceed(skip_interaction, [])

            selector_view = PaginatedRoleSelectView(
                guild=guild,
                title="🎭 VC管理システム セットアップ",
                description=(
                    "**ステップ 5-2/9: 表示ロール選択**\n\n"
                    "VCを表示するロールを選択してください。必要なロールが無ければスキップできます。"
                ),
                placeholder="VCを表示するロールを選択",
                roles=roles,
                on_complete=handle_complete,
                on_skip=handle_skip
            )
            await interaction.response.edit_message(embed=selector_view.build_embed(), view=selector_view)
        except Exception as e:
            logger.error(f"表示対象ロール設定エラー: {e}")


class VCStep6_Options(discord.ui.View):
    """ステップ6: VCオプション"""
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        
        options = [
            discord.SelectOption(label="参加者専用チャット", value=VCOption.TEXT_CHANNEL, description="VC参加者専用のテキストチャンネル"),
            discord.SelectOption(label="操作パネルなし", value=VCOption.NO_CONTROL, description="操作パネルを表示しない"),
            discord.SelectOption(label="満員時に非表示", value=VCOption.HIDE_FULL, description="満員時にVCを非表示"),
            discord.SelectOption(label="名前変更制限", value=VCOption.LOCK_NAME, description="VC名を固定"),
            discord.SelectOption(label="状態操作なし", value=VCOption.NO_STATE_CONTROL, description="ロック等の操作を消す"),
            discord.SelectOption(label="入退室ログなし", value=VCOption.NO_JOIN_LEAVE_LOG, description="入退室ログを表示しない"),
            discord.SelectOption(label="管理者譲渡なし", value=VCOption.NO_OWNERSHIP_TRANSFER, description="管理者譲渡機能を無効化"),
            discord.SelectOption(label="時間指定で削除", value=VCOption.DELAY_DELETE, description="一定時間経過後のみVCを削除")
        ]
        self.select = discord.ui.Select(
            placeholder="作成されるVCに適用するオプションを選択（複数可・スキップ可）", 
            min_values=0, max_values=len(options), options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        
        # スキップボタン
        skip_btn = discord.ui.Button(label="スキップ（オプションなし）", style=discord.ButtonStyle.secondary)
        skip_btn.callback = self.on_skip
        self.add_item(skip_btn)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            selected_options = self.select.values if self.select.values else []
            await self.proceed(interaction, selected_options)
        except Exception as e:
            logger.error(f"オプション選択処理エラー(on_select): {e}", exc_info=True)
            await send_interaction_error(interaction)
    
    async def on_skip(self, interaction: discord.Interaction):
        try:
            await self.proceed(interaction, [])
        except Exception as e:
            logger.error(f"オプション選択処理エラー(on_skip): {e}", exc_info=True)
            await send_interaction_error(interaction)
    
    async def proceed(self, interaction, selected_options):
        """次へ進む"""
        try:
            need_delay_option = VCOption.DELAY_DELETE in selected_options
            # 名前変更制限がある場合は固定名入力へ
            if VCOption.LOCK_NAME in selected_options:
                option_text = f"{len(selected_options)}個選択"
                embed = discord.Embed(
                    title="🎭 VC管理システム セットアップ",
                    description=f"**ステップ 6-2/9: 固定名入力**\n\n✅ オプション: **{option_text}**",
                    color=0x5865F2)
                view = VCStep6_LockedName(self.cog, self.original_interaction, self.vc_type, self.user_limit, 
                    self.hub_role_ids, self.vc_role_ids, self.hidden_role_ids, selected_options)
                await interaction.response.edit_message(embed=embed, view=view)
            elif need_delay_option:
                delay_view = VCStep6_DeleteDelay(
                    self.cog,
                    self.original_interaction,
                    self.vc_type,
                    self.user_limit,
                    self.hub_role_ids,
                    self.vc_role_ids,
                    self.hidden_role_ids,
                    selected_options,
                    locked_name=None
                )
                await interaction.response.edit_message(embed=delay_view.build_embed(), view=delay_view)
            else:
                # 通知設定画面へ
                notify_ctx = VCNotifyContext(
                    cog=self.cog,
                    original_interaction=self.original_interaction,
                    vc_type=self.vc_type,
                    user_limit=self.user_limit,
                    hub_role_ids=self.hub_role_ids,
                    vc_role_ids=self.vc_role_ids,
                    hidden_role_ids=self.hidden_role_ids,
                    selected_options=selected_options,
                    locked_name=None
                )
                notify_view = VCNotifyEnableView(notify_ctx, VCNotifyConfig())
                await interaction.response.edit_message(embed=notify_view.build_embed(), view=notify_view)
        except Exception as e:
            logger.error(f"オプション選択エラー: {e}")


@dataclass
class VCNotifyContext:
    cog: "VCManager"
    original_interaction: discord.Interaction
    vc_type: "VCType"
    user_limit: int
    hub_role_ids: List[int]
    vc_role_ids: List[int]
    hidden_role_ids: List[int]
    selected_options: List[str]
    locked_name: Optional[str]
    delete_delay_minutes: Optional[int] = None


@dataclass
class VCNotifyConfig:
    enabled: bool = False
    channel_id: Optional[int] = None
    category_id: Optional[int] = None
    role_id: Optional[int] = None
    category_new: bool = False
    new_category_name: str = "VC作成通知"


def describe_notify_destination(guild: discord.Guild, config: VCNotifyConfig) -> str:
    if config.category_new:
        return f"{config.new_category_name}（新規作成）"
    if config.channel_id:
        channel = guild.get_channel(config.channel_id)
        if hasattr(channel, "mention"):
            return channel.mention  # type: ignore[attr-defined]
        if channel:
            return channel.name
        return "選択したチャンネル"
    if config.category_id:
        category = guild.get_channel(config.category_id)
        if category:
            return f"{category.name}（カテゴリー）"
        return "新しく作成されるカテゴリー"
    return "未設定"


class VCNotifyBaseView(discord.ui.View):
    def __init__(self, ctx: VCNotifyContext, notify_config: VCNotifyConfig):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.notify_config = notify_config

    def _summary_texts(self) -> Tuple[str, str, str, str]:
        option_text = f"{len(self.ctx.selected_options)}個選択" if self.ctx.selected_options else "なし"
        locked_text = f"\n✅ 固定名: **{self.ctx.locked_name}**" if self.ctx.locked_name else ""
        delay_text = ""
        if self.ctx.delete_delay_minutes:
            delay_label = format_delete_delay(self.ctx.delete_delay_minutes)
            delay_text = f"\n⏱ 削除タイマー: **{delay_label}**"
        notify_text = ""
        if self.notify_config.enabled:
            destination = describe_notify_destination(self.ctx.original_interaction.guild, self.notify_config)
            notify_text = f"\n🔔 通知先: **{destination}**"
            if self.notify_config.role_id:
                role = self.ctx.original_interaction.guild.get_role(self.notify_config.role_id)
                if role:
                    notify_text += f"（{role.mention} をメンション）"
        return option_text, locked_text, notify_text, delay_text

    async def go_to_location_step(self, interaction: discord.Interaction):
        option_text, locked_text, notify_text, delay_text = self._summary_texts()
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=(
                "**ステップ 7/9: VC作成場所**\n\n"
                "作成するVCを配置するカテゴリーを選択してください。"
                f"\n✅ オプション: **{option_text}**{locked_text}{delay_text}{notify_text}"
            ),
            color=0x5865F2
        )
        view = VCStep7_Location(
            self.ctx.cog,
            self.ctx.original_interaction,
            self.ctx.vc_type,
            self.ctx.user_limit,
            self.ctx.hub_role_ids,
            self.ctx.vc_role_ids,
            self.ctx.hidden_role_ids,
            self.ctx.selected_options,
            self.ctx.locked_name,
            self.ctx.delete_delay_minutes,
            self.notify_config.enabled,
            self.notify_config.channel_id,
            self.notify_config.category_id,
            self.notify_config.role_id,
            notify_category_new=self.notify_config.category_new
        )
        await interaction.response.edit_message(embed=embed, view=view)


class VCNotifyEnableView(VCNotifyBaseView):
    def __init__(self, ctx: VCNotifyContext, notify_config: VCNotifyConfig):
        super().__init__(ctx, notify_config)
        yes_btn = discord.ui.Button(label="通知を送信する", style=discord.ButtonStyle.primary)
        yes_btn.callback = self.enable_notify
        self.add_item(yes_btn)
        no_btn = discord.ui.Button(label="通知は送信しない", style=discord.ButtonStyle.secondary)
        no_btn.callback = self.disable_notify
        self.add_item(no_btn)

    def build_embed(self) -> discord.Embed:
        description = (
            "**ステップ 6-3/9: 通知の有無**\n\n"
            "VCが作成された際に案内メッセージを送信するか選択してください。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def enable_notify(self, interaction: discord.Interaction):
        self.notify_config.enabled = True
        view = VCNotifyChannelView(self.ctx, self.notify_config)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def disable_notify(self, interaction: discord.Interaction):
        self.notify_config.enabled = False
        await self.go_to_location_step(interaction)


class VCNotifyChannelView(VCNotifyBaseView):
    def __init__(self, ctx: VCNotifyContext, notify_config: VCNotifyConfig):
        super().__init__(ctx, notify_config)
        self.add_item(VCNotifyChannelSelect(self))
        self.add_item(VCNotifyCategoryCreateSelect(self))

    def build_embed(self) -> discord.Embed:
        description = (
            "**ステップ 6-3/9: 通知チャンネル**\n\n"
            "通知を送信するテキストチャンネルを選択するか、専用カテゴリーを作成してください。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def proceed_to_mentions(self, interaction: discord.Interaction):
        view = VCNotifyMentionView(self.ctx, self.notify_config)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def handle_new_category(self, interaction: discord.Interaction):
        self.notify_config.category_new = True
        self.notify_config.channel_id = None
        self.notify_config.category_id = None
        self.notify_config.new_category_name = "VC作成通知"
        await self.proceed_to_mentions(interaction)


class VCNotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent_view: VCNotifyChannelView):
        super().__init__(channel_types=[discord.ChannelType.text], placeholder="通知先のテキストチャンネルを選択", min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        channel_id = getattr(selected, "id", None)
        if channel_id is None:
            channel_id = int(selected)
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            self.parent_view.notify_config.channel_id = channel.id
            self.parent_view.notify_config.category_id = None
            self.parent_view.notify_config.category_new = False
            await self.parent_view.proceed_to_mentions(interaction)
        else:
            await interaction.response.send_message("チャンネルの取得に失敗しました", ephemeral=True)


class VCNotifyCategoryCreateSelect(discord.ui.Select):
    def __init__(self, parent_view: VCNotifyChannelView):
        options = [
            discord.SelectOption(label="🆕 通知専用カテゴリーを作成", value="create", description="専用カテゴリーにチャンネルをまとめて作成")
        ]
        super().__init__(placeholder="新しいカテゴリーを作成する場合はこちら", options=options, min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.handle_new_category(interaction)


class VCNotifyMentionView(VCNotifyBaseView):
    def __init__(self, ctx: VCNotifyContext, notify_config: VCNotifyConfig):
        super().__init__(ctx, notify_config)
        role_btn = discord.ui.Button(label="ロールを指定する", style=discord.ButtonStyle.primary)
        role_btn.callback = self.choose_role
        self.add_item(role_btn)
        none_btn = discord.ui.Button(label="メンションしない", style=discord.ButtonStyle.secondary)
        none_btn.callback = self.choose_none
        self.add_item(none_btn)

    def build_embed(self) -> discord.Embed:
        destination = describe_notify_destination(self.ctx.original_interaction.guild, self.notify_config)
        description = (
            "**ステップ 6-3/9: メンション設定**\n\n"
            f"通知先: {destination}\n"
            "通知を送信するときにロールをメンションするか選択してください。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def choose_none(self, interaction: discord.Interaction):
        self.notify_config.role_id = None
        await self.go_to_location_step(interaction)

    async def choose_role(self, interaction: discord.Interaction):
        view = VCNotifyRoleView(self.ctx, self.notify_config)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class VCNotifyRoleView(VCNotifyBaseView):
    def __init__(self, ctx: VCNotifyContext, notify_config: VCNotifyConfig):
        super().__init__(ctx, notify_config)
        self.add_item(VCNotifyRolePicker(self))

    def build_embed(self) -> discord.Embed:
        description = (
            "**ステップ 6-3/9: メンションするロール**\n\n"
            "メンションに使用するロールを1つ選択してください。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def finish(self, interaction: discord.Interaction):
        await self.go_to_location_step(interaction)


class VCNotifyRolePicker(discord.ui.RoleSelect):
    def __init__(self, parent_view: VCNotifyRoleView):
        super().__init__(placeholder="メンションするロールを選択", min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        self.parent_view.notify_config.role_id = role.id
        await self.parent_view.finish(interaction)


class VCStep6_LockedName(discord.ui.View):
    """ステップ6-2: 固定名入力"""
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids, selected_options):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
        
        btn = discord.ui.Button(label="固定名を入力", style=discord.ButtonStyle.primary)
        btn.callback = self.open_modal
        self.add_item(btn)
    
    async def open_modal(self, interaction: discord.Interaction):
        try:
            modal = VCLockedNameModal(self.cog, self.original_interaction, self.vc_type, self.user_limit,
                self.hub_role_ids, self.vc_role_ids, self.hidden_role_ids, self.selected_options)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"固定名入力モーダル表示エラー: {e}", exc_info=True)
            await send_interaction_error(interaction)


class VCLockedNameModal(discord.ui.Modal, title="固定名入力"):
    """固定名入力モーダル"""
    name_input = discord.ui.TextInput(label="VC名", style=discord.TextStyle.short,
        placeholder="例: 作業部屋", required=True, max_length=100)
    
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids, selected_options):
        super().__init__()
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            locked_name = self.name_input.value.strip()
            if VCOption.DELAY_DELETE in self.selected_options:
                delay_view = VCStep6_DeleteDelay(
                    self.cog,
                    self.original_interaction,
                    self.vc_type,
                    self.user_limit,
                    self.hub_role_ids,
                    self.vc_role_ids,
                    self.hidden_role_ids,
                    self.selected_options,
                    locked_name
                )
                await self.original_interaction.edit_original_response(embed=delay_view.build_embed(), view=delay_view)
            else:
                notify_ctx = VCNotifyContext(
                    cog=self.cog,
                    original_interaction=self.original_interaction,
                    vc_type=self.vc_type,
                    user_limit=self.user_limit,
                    hub_role_ids=self.hub_role_ids,
                    vc_role_ids=self.vc_role_ids,
                    hidden_role_ids=self.hidden_role_ids,
                    selected_options=self.selected_options,
                    locked_name=locked_name
                )
                notify_view = VCNotifyEnableView(notify_ctx, VCNotifyConfig())
                await self.original_interaction.edit_original_response(embed=notify_view.build_embed(), view=notify_view)
            await interaction.response.send_message("✅ 固定名を保存しました。", ephemeral=True, delete_after=5)
        except Exception as e:
            logger.error(f"固定名入力処理エラー: {e}", exc_info=True)
            await send_interaction_error(interaction)


class VCStep6_DeleteDelay(discord.ui.View):
    """ステップ6: 削除タイマー設定"""
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids, selected_options, locked_name):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
        self.locked_name = locked_name

        options = [
            discord.SelectOption(label=label, value=str(value))
            for value, label in DELETE_DELAY_CHOICES
        ]
        self.select = discord.ui.Select(
            placeholder="VCを保持する時間を選択",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    def build_embed(self) -> discord.Embed:
        description = (
            "**ステップ 6-2/9: 削除タイマー**\n\n"
            "VCを作成してからどれくらいの時間が経過したら削除できるかを選択してください。\n"
            "指定時間を過ぎるまではユーザーが0人でもVCは残り、時間経過後に空になった時点で削除されます。"
        )
        return discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)

    async def on_select(self, interaction: discord.Interaction):
        try:
            if not self.select.values:
                await interaction.response.defer()
                return
            minutes = int(self.select.values[0])
            await self.proceed(interaction, minutes)
        except Exception as e:
            logger.error(f"削除タイマー選択エラー: {e}", exc_info=True)
            await send_interaction_error(interaction)

    async def proceed(self, interaction: discord.Interaction, minutes: int):
        try:
            notify_ctx = VCNotifyContext(
                cog=self.cog,
                original_interaction=self.original_interaction,
                vc_type=self.vc_type,
                user_limit=self.user_limit,
                hub_role_ids=self.hub_role_ids,
                vc_role_ids=self.vc_role_ids,
                hidden_role_ids=self.hidden_role_ids,
                selected_options=self.selected_options,
                locked_name=self.locked_name,
                delete_delay_minutes=minutes
            )
            notify_view = VCNotifyEnableView(notify_ctx, VCNotifyConfig())
            await interaction.response.edit_message(embed=notify_view.build_embed(), view=notify_view)
        except Exception as e:
            logger.error(f"削除タイマー適用エラー: {e}", exc_info=True)
            await send_interaction_error(interaction)


def format_role_list(guild: discord.Guild, role_ids: List[int]) -> str:
    names = []
    for role_id in role_ids or []:
        role = guild.get_role(role_id)
        if role:
            names.append(role.name)
    if not names:
        return "なし"
    if len(names) > 5:
        return ", ".join(names[:5]) + f" など{len(names) - 5}件"
    return ", ".join(names)



def summarize_role_names(guild: discord.Guild, role_ids: List[int]) -> Tuple[str, int]:
    names = []
    for role_id in role_ids or []:
        role = guild.get_role(role_id)
        if role:
            names.append(role.name)
    count = len(names)
    if not names:
        return "なし", 0
    if count > 3:
        return ", ".join(names[:3]) + f" 他{count - 3}件", count
    return ", ".join(names), count

def format_options_text(options: List[str]) -> str:
    labels = {
        VCOption.TEXT_CHANNEL: "参加者専用チャット",
        VCOption.NO_CONTROL: "操作パネルなし",
        VCOption.HIDE_FULL: "満員時に非表示",
        VCOption.LOCK_NAME: "名前変更制限",
        VCOption.NO_STATE_CONTROL: "状態操作なし",
        VCOption.NO_JOIN_LEAVE_LOG: "入退室ログなし",
        VCOption.DELAY_DELETE: "時間指定で削除",
    }
    selected = [labels[opt] for opt in options or [] if opt in labels]
    if not selected:
        return "なし"
    if len(selected) > 5:
        return ", ".join(selected[:5]) + f" など{len(selected) - 5}件"
    return ", ".join(selected)


def format_delete_delay(minutes: Optional[int]) -> str:
    if not minutes:
        return "なし"
    for value, label in DELETE_DELAY_CHOICES:
        if value == minutes:
            return label
    if minutes % 60 == 0:
        return f"{minutes // 60}時間"
    return f"{minutes}分"


def describe_location(guild: discord.Guild, location_mode: str, target_category_id: Optional[int]) -> str:
    if location_mode == VCLocationMode.AUTO_CATEGORY:
        return "カテゴリー自動作成"
    if location_mode == VCLocationMode.UNDER_HUB:
        return "ハブVCの直下"
    if location_mode == VCLocationMode.SAME_CATEGORY and target_category_id:
        category = guild.get_channel(target_category_id)
        if isinstance(category, discord.CategoryChannel):
            return f"指定カテゴリー ({category.name})"
        return "指定カテゴリー"
    return "未設定"


def describe_control_category(guild: discord.Guild, category_id: Optional[int]) -> str:
    """制御チャンネルカテゴリーの説明を取得"""
    if category_id is None:
        return "VCと同じカテゴリー"
    category = guild.get_channel(category_id)
    if isinstance(category, discord.CategoryChannel):
        return f"指定カテゴリー ({category.name})"
    return "指定カテゴリー"


def build_vc_summary_embed(
    guild: discord.Guild,
    vc_type: str,
    user_limit: int,
    hub_role_ids: List[int],
    vc_role_ids: List[int],
    hidden_role_ids: List[int],
    selected_options: List[str],
    locked_name: Optional[str],
    delete_delay_minutes: Optional[int],
    location_mode: str,
    target_category_id: Optional[int],
    control_category_id: Optional[int],
    control_category_new: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title="設定内容の確認",
        description="以下の内容でVC管理システムを作成します。内容を確認してください。",
        color=0x5865F2
    )
    if vc_type == VCType.WITH_LIMIT:
        embed.add_field(name="VCタイプ", value=f"人数指定（最大{user_limit}人）", inline=False)
    else:
        embed.add_field(name="VCタイプ", value="人数指定なし", inline=False)
    embed.add_field(name="ハブVCロール", value=format_role_list(guild, hub_role_ids), inline=False)
    embed.add_field(name="入場ロール", value=format_role_list(guild, vc_role_ids), inline=False)
    embed.add_field(name="表示対象ロール", value=format_role_list(guild, hidden_role_ids), inline=False)
    embed.add_field(name="オプション", value=format_options_text(selected_options), inline=False)
    embed.add_field(name="固定名", value=locked_name or "なし", inline=False)
    embed.add_field(name="削除タイマー", value=format_delete_delay(delete_delay_minutes), inline=False)
    embed.add_field(
        name="VC作成場所",
        value=describe_location(guild, location_mode, target_category_id),
        inline=False
    )
    has_control = VCOption.NO_CONTROL not in selected_options
    if has_control:
        if control_category_new:
            control_text = "新しいカテゴリーを自動作成"
        else:
            control_text = describe_control_category(guild, control_category_id)
        embed.add_field(
            name="操作チャンネル作成先",
            value=control_text,
            inline=False
        )
    return embed


class VCFinalConfirm(discord.ui.View):
    def __init__(
        self,
        cog,
        original_interaction,
        vc_type,
        user_limit,
        hub_role_ids,
        vc_role_ids,
        hidden_role_ids,
        selected_options,
        locked_name,
        delete_delay_minutes,
        location_mode,
        target_category_id,
        control_category_id,
        notify_enabled=False,
        notify_channel_id=None,
        notify_category_id=None,
        notify_role_id=None,
        control_category_new: bool = False,
        notify_category_new: bool = False,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
        self.locked_name = locked_name
        self.delete_delay_minutes = delete_delay_minutes
        self.location_mode = location_mode
        self.target_category_id = target_category_id
        self.control_category_id = control_category_id
        self.notify_enabled = notify_enabled
        self.notify_channel_id = notify_channel_id
        self.notify_category_id = notify_category_id
        self.notify_role_id = notify_role_id
        self.control_category_new = control_category_new
        self.notify_category_new = notify_category_new
    
    async def _create_system(self, interaction: discord.Interaction):
        await self.cog.create_vc_system(
            interaction.guild,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.location_mode,
            self.target_category_id,
            self.original_interaction.channel,
            self.selected_options,
            self.locked_name,
            delete_delay_minutes=self.delete_delay_minutes,
            control_category_id=self.control_category_id,
            notify_enabled=self.notify_enabled,
            notify_channel_id=self.notify_channel_id,
            notify_category_id=self.notify_category_id,
            notify_role_id=self.notify_role_id,
            notify_category_new=self.notify_category_new,
            control_category_new=self.control_category_new
        )
    
    @discord.ui.button(label="作成", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            await self._create_system(interaction)
            success_embed = discord.Embed(
                title="VC管理システムを作成しました",
                description="設定は保存されました。",
                color=0x57F287
            )
            await self.original_interaction.edit_original_response(embed=success_embed, view=None)
            await interaction.followup.send("VC管理システムを作成しました", ephemeral=True)
        except Exception as e:
            logger.error(f"完了エラー: {e}")
            await interaction.followup.send("エラーが発生しました", ephemeral=True)
    
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_embed = discord.Embed(
            title="セットアップをキャンセルしました",
            color=0xED4245
        )
        await interaction.response.edit_message(embed=cancel_embed, view=None)


class VCStep7_Location(discord.ui.View):
    """ステップ7: VC作成場所"""
    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids, selected_options, locked_name, delete_delay_minutes=None, notify_enabled=False, notify_channel_id=None, notify_category_id=None, notify_role_id=None, notify_category_new: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
        self.locked_name = locked_name
        self.delete_delay_minutes = delete_delay_minutes
        self.notify_enabled = notify_enabled
        self.notify_channel_id = notify_channel_id
        self.notify_category_id = notify_category_id
        self.notify_role_id = notify_role_id
        self.notify_category_new = notify_category_new
        
        options = [
            discord.SelectOption(label="カテゴリー自動作成", value="auto", description="新しいカテゴリーを自動作成"),
            discord.SelectOption(label="指定カテゴリー内", value="same", description="指定したカテゴリー内に作成"),
            discord.SelectOption(label="ハブVCの下", value="under", description="ハブVCの直下に作成")]
        self.select = discord.ui.Select(placeholder="VC作成場所を選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
    
    async def on_select(self, interaction: discord.Interaction):
        try:
            location = self.select.values[0]
            
            if location == "auto":
                location_mode = VCLocationMode.AUTO_CATEGORY
                await self.finalize(interaction, location_mode, None)
            elif location == "under":
                location_mode = VCLocationMode.UNDER_HUB
                await self.finalize(interaction, location_mode, None)
            else:
                # カテゴリー選択へ
                embed = discord.Embed(
                    title="VC管理システム セットアップ",
                    description="ステップ 8/9: VC作成先のカテゴリーを選択してください",
                    color=0x5865F2)
                view = VCStep8_Category(self.cog, self.original_interaction, self.vc_type, self.user_limit,
                    self.hub_role_ids, self.vc_role_ids, self.hidden_role_ids, self.selected_options, self.locked_name,
                    self.delete_delay_minutes, self.notify_enabled, self.notify_channel_id, self.notify_category_id, self.notify_role_id,
                    notify_category_new=self.notify_category_new)
                await interaction.response.edit_message(embed=embed, view=view)
        except:
            pass
    
    async def finalize(self, interaction, location_mode, target_category_id):
        """制御チャンネルカテゴリー選択または最終確認を表示"""
        # 操作パネルありの場合のみ制御チャンネルカテゴリー選択へ
        has_control = VCOption.NO_CONTROL not in self.selected_options
        if has_control:
            embed = discord.Embed(
                title="🎭 VC管理システム セットアップ",
                description=(
                    "**ステップ 9/9: 操作パネルの配置**\n\n"
                    "作成したVCを管理する操作パネルを配置するカテゴリーを選択してください。"
                ),
                color=0x5865F2
            )
            view = VCStep9_ControlCategory(
                self.cog,
                self.original_interaction,
                self.vc_type,
                self.user_limit,
                self.hub_role_ids,
                self.vc_role_ids,
                self.hidden_role_ids,
                self.selected_options,
                self.locked_name,
                self.delete_delay_minutes,
                location_mode,
                target_category_id,
                self.notify_enabled,
                self.notify_channel_id,
                self.notify_category_id,
                self.notify_role_id,
                notify_category_new=self.notify_category_new
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
        else:
            # 操作パネルなしの場合は最終確認へ
            guild = self.original_interaction.guild
            embed = build_vc_summary_embed(
                guild,
                self.vc_type,
                self.user_limit,
                self.hub_role_ids,
                self.vc_role_ids,
                self.hidden_role_ids,
                self.selected_options,
                self.locked_name,
                self.delete_delay_minutes,
                location_mode,
                target_category_id,
                None,  # control_category_id
                control_category_new=False
            )
            view = VCFinalConfirm(
                self.cog,
                self.original_interaction,
                self.vc_type,
                self.user_limit,
                self.hub_role_ids,
                self.vc_role_ids,
                self.hidden_role_ids,
                self.selected_options,
                self.locked_name,
                location_mode,
                target_category_id,
                None,  # control_category_id
                self.notify_enabled,
                self.notify_channel_id,
                self.notify_category_id,
                self.notify_role_id,
                control_category_new=False,
                notify_category_new=self.notify_category_new
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.response.edit_message(embed=embed, view=view)


class VCStep8_Category(discord.ui.View):
    """ステップ8: カテゴリー選択"""
    chunk_size = 25

    def __init__(self, cog, original_interaction, vc_type, user_limit, hub_role_ids, vc_role_ids, hidden_role_ids, selected_options, locked_name, delete_delay_minutes=None, notify_enabled=False, notify_channel_id=None, notify_category_id=None, notify_role_id=None, notify_category_new: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
        self.locked_name = locked_name
        self.delete_delay_minutes = delete_delay_minutes
        self.notify_enabled = notify_enabled
        self.notify_channel_id = notify_channel_id
        self.notify_category_id = notify_category_id
        self.notify_role_id = notify_role_id
        self.categories = list(original_interaction.guild.categories)
        self.current_page = 0
        self.category_select: Optional[discord.ui.Select] = None
        self.total_pages = max(1, math.ceil(len(self.categories) / self.chunk_size)) if self.categories else 1

        self._build_dropdown()
        self._build_controls()

    def _build_controls(self):
        self.prev_button = discord.ui.Button(label="前の25件", style=discord.ButtonStyle.secondary, disabled=self.total_pages <= 1, row=1)
        self.prev_button.callback = self._go_prev
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(label="次の25件", style=discord.ButtonStyle.secondary, disabled=self.total_pages <= 1, row=1)
        self.next_button.callback = self._go_next
        self.add_item(self.next_button)

        skip_button = discord.ui.Button(label="戻る（作成場所を選び直す）", style=discord.ButtonStyle.secondary, row=2)
        skip_button.callback = self._return_to_location_step
        self.add_item(skip_button)

    def _build_dropdown(self):
        if self.category_select:
            self.remove_item(self.category_select)
            self.category_select = None

        chunk = self._get_current_chunk()
        if not chunk:
            return

        options = [
            discord.SelectOption(label=category.name[:100], value=str(category.id))
            for category in chunk
        ]
        placeholder = f"VCを作成するカテゴリーを選択 ({self.current_page + 1}/{self.total_pages})"
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=1,
            row=0
        )
        select.callback = self.on_select
        self.category_select = select
        self.add_item(select)

    def _get_current_chunk(self) -> List[discord.CategoryChannel]:
        if not self.categories:
            return []
        start = self.current_page * self.chunk_size
        end = start + self.chunk_size
        return self.categories[start:end]

    def build_embed(self) -> discord.Embed:
        description = (
            "**ステップ 8/9: VC作成先のカテゴリー**\n\n"
            "VCを作成するカテゴリーを選択してください。カテゴリーが多い場合は前後のボタンでページを切り替えられます。"
        )
        embed = discord.Embed(title="🎭 VC管理システム セットアップ", description=description, color=0x5865F2)
        if self.categories:
            embed.set_footer(text=f"ページ {self.current_page + 1}/{self.total_pages}")
        else:
            embed.set_footer(text="選択できるカテゴリーがありません。戻るボタンで作成方法を変更できます。")
        return embed

    async def _go_prev(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page - 1) % self.total_pages
        self._build_dropdown()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _go_next(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page + 1) % self.total_pages
        self._build_dropdown()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _return_to_location_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🎭 VC管理システム セットアップ",
            description=(
                "**ステップ 7/9: VC作成場所**\n\n"
                "作成するVCをどのカテゴリーに配置するか選択してください。"
            ),
            color=0x5865F2
        )
        view = VCStep7_Location(
            self.cog,
            self.original_interaction,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.selected_options,
            self.locked_name,
            self.delete_delay_minutes,
            self.notify_enabled,
            self.notify_channel_id,
            self.notify_category_id,
            self.notify_role_id,
            notify_category_new=self.notify_category_new
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_select(self, interaction: discord.Interaction):
        if not self.category_select or not self.category_select.values:
            await interaction.response.defer()
            return
        category_id = int(self.category_select.values[0])
        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("カテゴリーを選択してください", ephemeral=True)
            return

        target_category_id = category.id
        location_mode = VCLocationMode.SAME_CATEGORY

        has_control = VCOption.NO_CONTROL not in self.selected_options
        if has_control:
            embed = discord.Embed(
                title="🎭 VC管理システム セットアップ",
                description=(
                    "**ステップ 9/9: 操作パネルの配置**\n\n"
                    "作成したVCを管理する操作パネルを配置するカテゴリーを選択してください。"
                ),
                color=0x5865F2
            )
            view = VCStep9_ControlCategory(
                self.cog,
                self.original_interaction,
                self.vc_type,
                self.user_limit,
                self.hub_role_ids,
                self.vc_role_ids,
                self.hidden_role_ids,
                self.selected_options,
                self.locked_name,
                self.delete_delay_minutes,
                location_mode,
                target_category_id,
                self.notify_enabled,
                self.notify_channel_id,
                self.notify_category_id,
                self.notify_role_id,
                notify_category_new=self.notify_category_new
            )
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            guild = self.original_interaction.guild
            embed = build_vc_summary_embed(
                guild,
                self.vc_type,
                self.user_limit,
                self.hub_role_ids,
                self.vc_role_ids,
                self.hidden_role_ids,
                self.selected_options,
                self.locked_name,
                location_mode,
                target_category_id,
                None,
                control_category_new=False
            )
            view = VCFinalConfirm(
                self.cog,
                self.original_interaction,
                self.vc_type,
                self.user_limit,
                self.hub_role_ids,
                self.vc_role_ids,
                self.hidden_role_ids,
                self.selected_options,
                self.locked_name,
                self.delete_delay_minutes,
                location_mode,
                target_category_id,
                None,
                self.notify_enabled,
                self.notify_channel_id,
                self.notify_category_id,
                self.notify_role_id,
                control_category_new=False,
                notify_category_new=self.notify_category_new
            )
            await interaction.response.edit_message(embed=embed, view=view)

class VCStep9_ControlCategory(discord.ui.View):
    """ステップ9: 操作チャンネル作成先カテゴリー選択（1つのドロップダウンに統合）"""

    chunk_size = 24  # 24カテゴリ + 1つは「新しいカテゴリーを作成」用

    def __init__(
        self,
        cog,
        original_interaction,
        vc_type,
        user_limit,
        hub_role_ids,
        vc_role_ids,
        hidden_role_ids,
        selected_options,
        locked_name,
        delete_delay_minutes,
        location_mode,
        target_category_id,
        notify_enabled: bool = False,
        notify_channel_id=None,
        notify_category_id=None,
        notify_role_id=None,
        notify_category_new: bool = False,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_interaction = original_interaction
        self.vc_type = vc_type
        self.user_limit = user_limit
        self.hub_role_ids = hub_role_ids
        self.vc_role_ids = vc_role_ids
        self.hidden_role_ids = hidden_role_ids
        self.selected_options = selected_options
        self.locked_name = locked_name
        self.delete_delay_minutes = delete_delay_minutes
        self.location_mode = location_mode
        self.target_category_id = target_category_id
        self.notify_enabled = notify_enabled
        self.notify_channel_id = notify_channel_id
        self.notify_category_id = notify_category_id
        self.notify_role_id = notify_role_id
        self.notify_category_new = notify_category_new

        self.categories: List[discord.CategoryChannel] = list(original_interaction.guild.categories)
        self.current_page: int = 0
        self.total_pages: int = max(
            1, math.ceil(len(self.categories) / self.chunk_size)
        ) if self.categories else 1
        self.category_select: Optional[discord.ui.Select] = None

        self._build_dropdown()
        self._build_controls()

    def _build_controls(self) -> None:
        """前後ページ移動などのボタン"""
        self.prev_button = discord.ui.Button(
            label="前の25件",
            style=discord.ButtonStyle.secondary,
            disabled=self.total_pages <= 1,
            row=1,
        )
        self.prev_button.callback = self._go_prev  # type: ignore[assignment]
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(
            label="次の25件",
            style=discord.ButtonStyle.secondary,
            disabled=self.total_pages <= 1,
            row=1,
        )
        self.next_button.callback = self._go_next  # type: ignore[assignment]
        self.add_item(self.next_button)

        back_button = discord.ui.Button(
            label="戻る（VC作成先カテゴリーに戻る）",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        back_button.callback = self._go_back_to_step8  # type: ignore[assignment]
        self.add_item(back_button)

    def _get_current_chunk(self) -> List[discord.CategoryChannel]:
        if not self.categories:
            return []
        start = self.current_page * self.chunk_size
        end = start + self.chunk_size
        return self.categories[start:end]

    def _build_dropdown(self) -> None:
        """1つのSelectに「既存カテゴリ + 新規作成」をまとめる"""
        if self.category_select:
            self.remove_item(self.category_select)
            self.category_select = None

        chunk = self._get_current_chunk()
        options: List[discord.SelectOption] = []

        for category in chunk:
            options.append(
                discord.SelectOption(
                    label=category.name[:100],
                    value=str(category.id),
                )
            )

        # 最後に「新しいカテゴリーを作成」を追加
        options.append(
            discord.SelectOption(
                label="🆕 新しいカテゴリーを作成",
                value="create",
                description="操作パネル用のカテゴリーを新しく作成",
            )
        )

        placeholder = (
            f"操作パネルを配置するカテゴリーを選択 ({self.current_page + 1}/{self.total_pages})"
        )
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        select.callback = self.on_select  # type: ignore[assignment]
        self.category_select = select
        self.add_item(select)

    async def _go_prev(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page - 1) % self.total_pages
        self._build_dropdown()
        await interaction.response.edit_message(view=self)

    async def _go_next(self, interaction: discord.Interaction):
        if self.total_pages <= 1:
            await interaction.response.defer()
            return
        self.current_page = (self.current_page + 1) % self.total_pages
        self._build_dropdown()
        await interaction.response.edit_message(view=self)

    async def _go_back_to_step8(self, interaction: discord.Interaction):
        """ステップ8のVC作成先カテゴリー選択に戻る"""
        embed = VCStep8_Category(
            self.cog,
            self.original_interaction,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.selected_options,
            self.locked_name,
            self.delete_delay_minutes,
            self.notify_enabled,
            self.notify_channel_id,
            self.notify_category_id,
            self.notify_role_id,
            notify_category_new=self.notify_category_new,
        ).build_embed()
        view = VCStep8_Category(
            self.cog,
            self.original_interaction,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.selected_options,
            self.locked_name,
            self.delete_delay_minutes,
            self.notify_enabled,
            self.notify_channel_id,
            self.notify_category_id,
            self.notify_role_id,
            notify_category_new=self.notify_category_new,
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_select(self, interaction: discord.Interaction):
        if not self.category_select or not self.category_select.values:
            await interaction.response.defer()
            return

        value = self.category_select.values[0]
        if value == "create":
            # 新しいカテゴリーを作成するパターン
            await self.show_summary(interaction, None, control_category_new=True)
            return

        try:
            category_id = int(value)
        except ValueError:
            await interaction.response.send_message(
                "カテゴリーを正しく選択してください。", ephemeral=True
            )
            return

        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "カテゴリーを選択してください", ephemeral=True
            )
            return

        await self.show_summary(interaction, category.id, control_category_new=False)

    async def show_summary(
        self,
        interaction: discord.Interaction,
        control_category_id: Optional[int],
        control_category_new: bool,
    ):
        guild = self.original_interaction.guild
        embed = build_vc_summary_embed(
            guild,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.selected_options,
            self.locked_name,
            self.delete_delay_minutes,
            self.location_mode,
            self.target_category_id,
            control_category_id,
            control_category_new=control_category_new,
        )
        view = VCFinalConfirm(
            self.cog,
            self.original_interaction,
            self.vc_type,
            self.user_limit,
            self.hub_role_ids,
            self.vc_role_ids,
            self.hidden_role_ids,
            self.selected_options,
            self.locked_name,
            self.delete_delay_minutes,
            self.location_mode,
            self.target_category_id,
            control_category_id,
            self.notify_enabled,
            self.notify_channel_id,
            self.notify_category_id,
            self.notify_role_id,
            control_category_new=control_category_new,
            notify_category_new=self.notify_category_new,
        )
        await interaction.response.edit_message(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(VCManager(bot))
