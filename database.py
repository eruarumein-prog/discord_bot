import sqlite3
import os
from typing import List, Optional
import threading
import logging

# ロガー設定
logger = logging.getLogger('database')
logger.setLevel(logging.INFO)

class Database:
    """SQLiteデータベース管理クラス"""
    
    def __init__(self, db_path: str = None):
        # 環境変数からデータベースパスを取得（優先）
        if db_path is None:
            db_path = os.getenv('DATABASE_PATH') or os.getenv('DB_PATH')
        
        # デフォルトパス（環境変数がない場合）
        if db_path is None:
            # データディレクトリを作成
            data_dir = os.getenv('DATA_DIR', 'data')
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, 'vc_data.db')
        
        # 既存データベースの移行処理
        # 古い場所（プロジェクトルート）にデータベースがある場合、新しい場所にコピー
        old_db_path = "vc_data.db"  # 古いデフォルトパス
        if os.path.exists(old_db_path) and not os.path.exists(db_path):
            try:
                import shutil
                db_dir = os.path.dirname(db_path)
                if db_dir and not os.path.exists(db_dir):
                    os.makedirs(db_dir, exist_ok=True)
                shutil.copy2(old_db_path, db_path)
                logger.info(f"既存データベースを移行しました: {old_db_path} -> {db_path}")
            except Exception as e:
                logger.warning(f"データベース移行に失敗しました（新規作成します）: {e}")
        
        # ディレクトリが存在しない場合は作成
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        
        self.db_path = db_path
        self.lock = threading.Lock()
        logger.info(f"データベースパス: {self.db_path}")
        self.init_database()
    
    def get_connection(self):
        """データベース接続を取得"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")  # 並行アクセス性能向上
        conn.execute("PRAGMA synchronous=NORMAL")  # 書き込み性能向上
        return conn
    
    def init_database(self):
        """データベースとテーブルを初期化"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # ユーザーごとのVC設定テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_vc_settings (
                user_id INTEGER PRIMARY KEY,
                banned_users TEXT DEFAULT ''
            )
        ''')
        
        # VCシステム設定テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vc_systems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                category_id INTEGER,
                hub_vc_id INTEGER NOT NULL UNIQUE,
                vc_type TEXT NOT NULL,
                user_limit INTEGER DEFAULT 0,
                allowed_roles TEXT DEFAULT '',
                vc_roles TEXT DEFAULT '',
                hidden_roles TEXT DEFAULT '',
                location_mode TEXT NOT NULL,
                target_category_id INTEGER,
                options TEXT DEFAULT '',
                locked_name TEXT DEFAULT NULL,
                notify_enabled INTEGER DEFAULT 0,
                notify_channel_id INTEGER,
                notify_role_id INTEGER,
                control_category_id INTEGER,
                delete_delay_minutes INTEGER
            )
        ''')
        
        # 既存のテーブルに新しいカラムを追加（マイグレーション）
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN vc_roles TEXT DEFAULT ''")
        except:
            pass  # カラムが既に存在する場合
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN options TEXT DEFAULT ''")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN locked_name TEXT DEFAULT NULL")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN hidden_roles TEXT DEFAULT ''")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN notify_enabled INTEGER DEFAULT 0")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN notify_channel_id INTEGER")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN notify_role_id INTEGER")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN control_category_id INTEGER")
        except:
            pass
        try:
            cursor.execute("ALTER TABLE vc_systems ADD COLUMN delete_delay_minutes INTEGER")
        except:
            pass
        
        try:
            cursor.execute("ALTER TABLE active_vcs ADD COLUMN view_allowed_users TEXT DEFAULT ''")
        except:
            pass
        
        # 既存テーブルにhub_vc_idのUNIQUE制約を追加（マイグレーション）
        # SQLiteでは直接ADD CONSTRAINTができないため、UNIQUEインデックスで代用
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_vc_systems_hub_vc_id ON vc_systems(hub_vc_id)")
        except:
            pass  # 既に存在する場合
        
        # アクティブなVCテーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_vcs (
                vc_id INTEGER PRIMARY KEY,
                original_limit INTEGER DEFAULT 0,
                original_name TEXT NOT NULL,
                bot_count INTEGER DEFAULT 0,
                text_channel_id INTEGER,
                control_channel_id INTEGER,
                vc_type TEXT NOT NULL,
                category_id INTEGER,
                owner_id INTEGER NOT NULL,
                banned_users TEXT DEFAULT '',
                is_locked INTEGER DEFAULT 0,
                allowed_users TEXT DEFAULT '',
                view_allowed_users TEXT DEFAULT '',
                options TEXT DEFAULT '',
                delete_ready_at TEXT,
                delete_delay_minutes INTEGER
            )
        ''')
        
        # 既存のテーブルにoptionsカラムを追加（マイグレーション）
        try:
            cursor.execute("ALTER TABLE active_vcs ADD COLUMN options TEXT DEFAULT ''")
        except:
            pass  # カラムが既に存在する場合
        try:
            cursor.execute("ALTER TABLE active_vcs ADD COLUMN delete_ready_at TEXT")
        except:
            pass
        try:
            cursor.execute("ALTER TABLE active_vcs ADD COLUMN delete_delay_minutes INTEGER")
        except:
            pass
        
        # 埋め込み表示テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS embed_displays (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                content TEXT NOT NULL
            )
        ''')
        
        # ロール管理操作盤テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS role_panels (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                role_ids TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL
            )
        ''')
        
        # DMチャンネルテーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_dms (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                delete_at TEXT NOT NULL
            )
        ''')
        
        # DMカテゴリーテーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dm_categories (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invite_watchers (
                guild_id INTEGER NOT NULL,
                inviter_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, inviter_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invite_counts (
                guild_id INTEGER NOT NULL,
                inviter_id INTEGER NOT NULL,
                total_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, inviter_id)
            )
        ''')
        
        # インデックスを作成（パフォーマンス向上）
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_vc_systems_guild ON vc_systems(guild_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_vc_systems_hub ON vc_systems(hub_vc_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_active_vcs_owner ON active_vcs(owner_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_settings_user ON user_vc_settings(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_role_panels_guild ON role_panels(guild_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_active_dms_guild ON active_dms(guild_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_active_dms_user1 ON active_dms(user1_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_active_dms_user2 ON active_dms(user2_id)')
        
        conn.commit()
        conn.close()
    
    def get_banned_users(self, user_id: int) -> List[int]:
        """ユーザーのブロックリストを取得"""
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = conn.cursor()
            
                cursor.execute('SELECT banned_users FROM user_vc_settings WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                conn.close()
                
                if result and result[0]:
                    # カンマ区切りの文字列を整数リストに変換
                    return [int(uid) for uid in result[0].split(',') if uid]
                return []
        except Exception as e:
            print(f"❌ DB Error (get_banned_users): {e}")
            return []
    
    def add_banned_user(self, owner_id: int, banned_user_id: int):
        """ブロックユーザーを追加"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # 既存のブロックリストを取得
            cursor.execute('SELECT banned_users FROM user_vc_settings WHERE user_id = ?', (owner_id,))
            result = cursor.fetchone()
            banned_users = [int(uid) for uid in result[0].split(',') if uid] if result and result[0] else []
            
            # 重複チェック
            if banned_user_id not in banned_users:
                banned_users.append(banned_user_id)
            
            # カンマ区切りの文字列に変換
            banned_str = ','.join(map(str, banned_users))
            
            # データベースに保存（UPSERT）
            cursor.execute('''
                INSERT INTO user_vc_settings (user_id, banned_users)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET banned_users = ?
            ''', (owner_id, banned_str, banned_str))
            
            conn.commit()
            conn.close()
    
    def remove_banned_user(self, owner_id: int, banned_user_id: int):
        """ブロックユーザーを解除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # 既存のブロックリストを取得
            cursor.execute('SELECT banned_users FROM user_vc_settings WHERE user_id = ?', (owner_id,))
            result = cursor.fetchone()
            banned_users = [int(uid) for uid in result[0].split(',') if uid] if result and result[0] else []
            
            # リストから削除
            if banned_user_id in banned_users:
                banned_users.remove(banned_user_id)
            
            # カンマ区切りの文字列に変換
            banned_str = ','.join(map(str, banned_users))
            
            # データベースに保存
            cursor.execute('''
                UPDATE user_vc_settings SET banned_users = ? WHERE user_id = ?
            ''', (banned_str, owner_id))
            
            conn.commit()
            conn.close()
    
    def save_vc_system(self, guild_id: int, category_id: int, hub_vc_id: int, vc_type: str, 
                       user_limit: int, allowed_roles: List[int], vc_roles: List[int], hidden_roles: List[int],
                       location_mode: str, target_category_id: int, options: List[str], locked_name: str = None,
                       notify_enabled: bool = False, notify_channel_id: Optional[int] = None, notify_role_id: Optional[int] = None,
                       control_category_id: Optional[int] = None, delete_delay_minutes: Optional[int] = None):
        """VCシステムを保存"""
        with self.lock:
            conn = None
            try:
                conn = self.get_connection()
                cursor = conn.cursor()
                
                allowed_roles_str = ','.join(map(str, allowed_roles)) if allowed_roles else ''
                vc_roles_str = ','.join(map(str, vc_roles)) if vc_roles else ''
                hidden_roles_str = ','.join(map(str, hidden_roles)) if hidden_roles else ''
                options_str = ','.join(options) if options else ''
                
                cursor.execute('''
                    INSERT OR REPLACE INTO vc_systems (guild_id, category_id, hub_vc_id, vc_type, user_limit, 
                                           allowed_roles, vc_roles, hidden_roles, location_mode, target_category_id, 
                                           options, locked_name, notify_enabled, notify_channel_id, notify_role_id, control_category_id, delete_delay_minutes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (guild_id, category_id, hub_vc_id, vc_type, user_limit, 
                      allowed_roles_str, vc_roles_str, hidden_roles_str, location_mode, target_category_id, 
                      options_str, locked_name, 1 if notify_enabled else 0, notify_channel_id, notify_role_id, control_category_id, delete_delay_minutes))
                
                conn.commit()
                logger.info(f"✅ VCシステム保存完了 (Guild ID: {guild_id}, Hub VC ID: {hub_vc_id})")
            except Exception as e:
                logger.error(f"❌ VCシステム保存エラー (Guild ID: {guild_id}): {e}")
                if conn:
                    conn.rollback()
                raise
            finally:
                if conn:
                    conn.close()
    
    def get_vc_systems(self):
        """全VCシステムを取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # カラム名を取得
            cursor.execute('PRAGMA table_info(vc_systems)')
            columns = [col[1] for col in cursor.fetchall()]
            
            cursor.execute('SELECT * FROM vc_systems')
            results = cursor.fetchall()
            conn.close()
            
            systems = []
            for row in results:
                # カラム名からインデックスを取得
                row_dict = dict(zip(columns, row))
                
                allowed_roles = [int(r) for r in row_dict.get('allowed_roles', '').split(',') if r]
                vc_roles = [int(r) for r in row_dict.get('vc_roles', '').split(',') if r]
                hidden_roles = [int(r) for r in row_dict.get('hidden_roles', '').split(',') if r]
                options = row_dict.get('options', '').split(',') if row_dict.get('options') else []
                locked_name = row_dict.get('locked_name')
                
                systems.append({
                    'id': row_dict.get('id'),
                    'guild_id': row_dict.get('guild_id'),
                    'category_id': row_dict.get('category_id'),
                    'hub_vc_id': row_dict.get('hub_vc_id'),
                    'vc_type': row_dict.get('vc_type'),
                    'user_limit': row_dict.get('user_limit', 0),
                    'allowed_roles': allowed_roles,
                    'vc_roles': vc_roles,
                    'hidden_roles': hidden_roles,
                    'location_mode': row_dict.get('location_mode'),
                    'target_category_id': row_dict.get('target_category_id'),
                    'options': options,
                    'locked_name': locked_name,
                    'notify_enabled': bool(row_dict.get('notify_enabled', 0)),
                    'notify_channel_id': row_dict.get('notify_channel_id'),
                    'notify_role_id': row_dict.get('notify_role_id'),
                    'control_category_id': row_dict.get('control_category_id'),
                    'delete_delay_minutes': row_dict.get('delete_delay_minutes')
                })
            return systems
    
    def delete_vc_system_by_hub(self, hub_vc_id: int):
        """ハブVCを削除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM vc_systems WHERE hub_vc_id = ?', (hub_vc_id,))
            conn.commit()
            conn.close()
    
    def save_active_vc(self, vc_id: int, data: dict):
        """アクティブなVCを保存"""
        with self.lock:
            conn = None
            try:
                conn = self.get_connection()
                cursor = conn.cursor()
                
                banned_str = ','.join(map(str, data.get('banned_users', []))) if data.get('banned_users') else ''
                allowed_str = ','.join(map(str, data.get('allowed_users', []))) if data.get('allowed_users') else ''
                view_allowed_str = ','.join(map(str, data.get('view_allowed_users', []))) if data.get('view_allowed_users') else ''
                options_str = ','.join(data.get('options', [])) if data.get('options') else ''
                
                delete_ready_at = data.get('delete_ready_at')
                delete_delay_minutes = data.get('delete_delay_minutes')
                cursor.execute('''
                    INSERT OR REPLACE INTO active_vcs 
                    (vc_id, original_limit, original_name, bot_count, text_channel_id, control_channel_id,
                     vc_type, category_id, owner_id, banned_users, is_locked, allowed_users, view_allowed_users, options,
                     delete_ready_at, delete_delay_minutes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (vc_id, data.get('original_limit', 0), data.get('original_name', ''),
                      data.get('bot_count', 0), data.get('text_channel_id'), data.get('control_channel_id'),
                      data.get('vc_type', ''), data.get('category_id'), data.get('owner_id', 0),
                      banned_str, 1 if data.get('is_locked', False) else 0, allowed_str, view_allowed_str, options_str,
                      delete_ready_at, delete_delay_minutes))
                
                conn.commit()
                logger.debug(f"✅ アクティブVC保存完了 (VC ID: {vc_id})")
            except Exception as e:
                logger.error(f"❌ アクティブVC保存エラー (VC ID: {vc_id}): {e}")
                if conn:
                    conn.rollback()
                raise
            finally:
                if conn:
                    conn.close()
    
    def get_active_vcs(self):
        """全アクティブVCを取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # カラム名を取得
            cursor.execute('PRAGMA table_info(active_vcs)')
            columns = [col[1] for col in cursor.fetchall()]
            
            cursor.execute('SELECT * FROM active_vcs')
            results = cursor.fetchall()
            conn.close()
            
            vcs = {}
            for row in results:
                row_dict = dict(zip(columns, row))
                
                banned_users = [int(u) for u in row_dict.get('banned_users', '').split(',') if u]
                allowed_users = [int(u) for u in row_dict.get('allowed_users', '').split(',') if u]
                view_allowed_users = [int(u) for u in row_dict.get('view_allowed_users', '').split(',') if u]
                options = row_dict.get('options', '').split(',') if row_dict.get('options') else []
                
                # delete_delay_minutesが文字列の場合は整数に変換
                delete_delay_minutes = row_dict.get('delete_delay_minutes')
                if delete_delay_minutes is not None:
                    try:
                        delete_delay_minutes = int(delete_delay_minutes)
                    except (ValueError, TypeError):
                        delete_delay_minutes = None
                
                vcs[row_dict['vc_id']] = {
                    'original_limit': row_dict.get('original_limit', 0),
                    'original_name': row_dict.get('original_name', ''),
                    'bot_count': row_dict.get('bot_count', 0),
                    'text_channel_id': row_dict.get('text_channel_id'),
                    'control_channel_id': row_dict.get('control_channel_id'),
                    'vc_type': row_dict.get('vc_type', ''),
                    'category_id': row_dict.get('category_id'),
                    'owner_id': row_dict.get('owner_id', 0),
                    'banned_users': banned_users,
                    'is_locked': bool(row_dict.get('is_locked', 0)),
                    'allowed_users': allowed_users,
                    'view_allowed_users': view_allowed_users,
                    'options': options,
                    'delete_ready_at': row_dict.get('delete_ready_at'),
                    'delete_delay_minutes': delete_delay_minutes
                }
            return vcs
    
    def delete_active_vc(self, vc_id: int):
        """アクティブなVCを削除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM active_vcs WHERE vc_id = ?', (vc_id,))
            conn.commit()
            conn.close()
    
    def save_embed_display(self, channel_id: int, message_id: int, content: str):
        """埋め込み表示を保存"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO embed_displays (channel_id, message_id, content)
                VALUES (?, ?, ?)
            ''', (channel_id, message_id, content))
            conn.commit()
            conn.close()
    
    def get_embed_displays(self):
        """全埋め込み表示を取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM embed_displays')
            results = cursor.fetchall()
            conn.close()
            
            displays = {}
            for row in results:
                displays[row[0]] = {  # channel_id
                    'message_id': row[1],
                    'content': row[2]
                }
            return displays
    
    def delete_embed_display(self, channel_id: int):
        """埋め込み表示を削除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM embed_displays WHERE channel_id = ?', (channel_id,))
            conn.commit()
            conn.close()
    
    def save_role_panel(self, message_id: int, guild_id: int, channel_id: int, role_ids: List[int], title: str, description: str):
        """ロール管理操作盤を保存"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            role_ids_str = ','.join(map(str, role_ids))
            cursor.execute('''
                INSERT OR REPLACE INTO role_panels (message_id, guild_id, channel_id, role_ids, title, description)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (message_id, guild_id, channel_id, role_ids_str, title, description))
            conn.commit()
            conn.close()
    
    def get_role_panels(self):
        """全ロール管理操作盤を取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM role_panels')
            results = cursor.fetchall()
            conn.close()
            
            panels = {}
            for row in results:
                role_ids = [int(r) for r in row[3].split(',') if r]  # role_ids
                panels[row[0]] = {  # message_id
                    'guild_id': row[1],
                    'channel_id': row[2],
                    'role_ids': role_ids,
                    'title': row[4],
                    'description': row[5]
                }
            return panels
    
    def delete_role_panel(self, message_id: int):
        """ロール管理操作盤を削除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM role_panels WHERE message_id = ?', (message_id,))
            conn.commit()
            conn.close()
    
    def save_active_dm(self, channel_id: int, guild_id: int, user1_id: int, user2_id: int, delete_at: str):
        """アクティブなDMチャンネルを保存"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO active_dms (channel_id, guild_id, user1_id, user2_id, delete_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (channel_id, guild_id, user1_id, user2_id, delete_at))
            conn.commit()
            conn.close()
    
    def get_active_dms(self):
        """全アクティブDMチャンネルを取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM active_dms')
            results = cursor.fetchall()
            conn.close()
            
            dms = {}
            for row in results:
                dms[row[0]] = {  # channel_id
                    'guild_id': row[1],
                    'user1_id': row[2],
                    'user2_id': row[3],
                    'delete_at': row[4]
                }
            return dms
    
    def delete_active_dm(self, channel_id: int):
        """アクティブなDMチャンネルを削除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM active_dms WHERE channel_id = ?', (channel_id,))
            conn.commit()
            conn.close()
    
    def save_dm_category(self, guild_id: int, category_id: int):
        """DMカテゴリーを保存"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO dm_categories (guild_id, category_id)
                VALUES (?, ?)
            ''', (guild_id, category_id))
            conn.commit()
            conn.close()
    
    def get_dm_categories(self):
        """全DMカテゴリーを取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dm_categories')
            results = cursor.fetchall()
            conn.close()
            
            categories = {}
            for row in results:
                categories[row[0]] = row[1]  # guild_id: category_id
            return categories
    
    def delete_dm_category(self, guild_id: int):
        """DMカテゴリーを削除"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dm_categories WHERE guild_id = ?', (guild_id,))
            conn.commit()
            conn.close()

    # ===== 招待監視関連 =====

    def upsert_invite_watcher(self, guild_id: int, inviter_id: int, channel_id: int):
        """招待監視設定を保存（同じユーザーは最新のチャンネルを保持）"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO invite_watchers (guild_id, inviter_id, channel_id, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, inviter_id)
                DO UPDATE SET channel_id = excluded.channel_id,
                              created_at = CURRENT_TIMESTAMP
            ''', (guild_id, inviter_id, channel_id))
            conn.commit()
            conn.close()

    def get_all_invite_watchers(self):
        """全ギルドの招待監視設定を取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT guild_id, inviter_id, channel_id FROM invite_watchers')
            rows = cursor.fetchall()
            conn.close()
            return [{'guild_id': row[0], 'inviter_id': row[1], 'channel_id': row[2]} for row in rows]

    def get_invite_watcher_channel(self, guild_id: int, inviter_id: int) -> Optional[int]:
        """指定ユーザーの監視チャンネルを取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT channel_id FROM invite_watchers WHERE guild_id = ? AND inviter_id = ?', (guild_id, inviter_id))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None

    def increment_invite_count(self, guild_id: int, inviter_id: int) -> int:
        """招待数をインクリメントして最新値を返す"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO invite_counts (guild_id, inviter_id, total_count)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, inviter_id)
                DO UPDATE SET total_count = total_count + 1
            ''', (guild_id, inviter_id))
            cursor.execute('SELECT total_count FROM invite_counts WHERE guild_id = ? AND inviter_id = ?', (guild_id, inviter_id))
            total = cursor.fetchone()[0]
            conn.commit()
            conn.close()
            return total

    def get_invite_count(self, guild_id: int, inviter_id: int) -> int:
        """招待数を取得"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT total_count FROM invite_counts WHERE guild_id = ? AND inviter_id = ?', (guild_id, inviter_id))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 0

