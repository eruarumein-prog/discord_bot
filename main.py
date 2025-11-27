import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio
import logging
from datetime import datetime

# ログ設定
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_filename = os.path.join(log_dir, f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# ログフォーマット
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
logger.info(f"ログファイル: {log_filename}")

# .envファイルから環境変数を読み込む
load_dotenv()

# Botのトークンを取得（複数の方法を試す）
TOKEN = os.getenv('DISCORD_TOKEN') or os.getenv('TOKEN') or os.getenv('BOT_TOKEN') or os.getenv('DISCORD_BOT_TOKEN')

if not TOKEN:
    # .envファイルを直接読み込んでみる
    try:
        with open('.env', 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key in ['DISCORD_TOKEN', 'TOKEN', 'BOT_TOKEN', 'DISCORD_BOT_TOKEN']:
                            TOKEN = value
                            break
    except:
        pass

if not TOKEN:
    print("エラー: DISCORD_TOKENが設定されていません")
    print(".envファイルにDISCORD_TOKEN=your_token_hereを追加してください")
    print("\n現在のディレクトリ:", os.getcwd())
    print(".envファイルの存在:", os.path.exists('.env'))
    exit(1)

# Intentsの設定
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

# Botの初期化
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'✅ Botがログインしました: {bot.user.name} (ID: {bot.user.id})')
    print(f'✅ {len(bot.guilds)}個のサーバーに接続中')
    
    # スラッシュコマンドを同期
    try:
        synced = await bot.tree.sync()
        print(f'✅ {len(synced)}個のスラッシュコマンドを同期しました')
    except Exception as e:
        print(f'❌ コマンド同期エラー: {e}')

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """スラッシュコマンドのエラーハンドラー"""
    if isinstance(error, discord.app_commands.CommandInvokeError):
        original_error = error.original
        print(f"❌ コマンドエラー: {original_error}")
        
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ エラーが発生しました: {str(original_error)[:100]}",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ エラーが発生しました: {str(original_error)[:100]}",
                ephemeral=True
            )
    else:
        print(f"❌ コマンドエラー: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ エラーが発生しました",
                ephemeral=True
            )

# Cogを読み込む
async def load_extensions():
    await bot.load_extension('cogs.vcmanager')
    print('VCManagerを読み込みました')
    await bot.load_extension('cogs.ticketmanager')
    print('TicketManagerを読み込みました')
    await bot.load_extension('cogs.serverdm')
    print('ServerDMを読み込みました')
    await bot.load_extension('cogs.embeddisplay')
    print('EmbedDisplayを読み込みました')
    await bot.load_extension('cogs.rolemanager')
    print('RoleManagerを読み込みました')
    await bot.load_extension('cogs.invite_tracker')
    print('InviteTrackerを読み込みました')

async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nBotを停止しました')
    except Exception as e:
        print(f'エラーが発生しました: {e}')
        import traceback
        traceback.print_exc()
















