import logging
import aiosqlite
import time
import asyncio
import re
import math
import sympy
from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application
from telegram import Update, ChatPermissions, ReactionTypeEmoji, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, Application
from telegram.error import TelegramError, BadRequest, Forbidden

# --- 配置區 ---
TOKEN = "0123456789:ABCD1234EFGH5678IJKLMNOPQRSTUVWXYZ"  # 請替換成你的 Telegram Bot Token
DB_NAME = "counting_bot.db"
MUTE_DURATION = 60  # 禁言秒數

# 白名單設定 (設為 None 則不限制)
ALLOWED_CHAT_ID = -100123456789  # 群組 ID
ALLOWED_TOPIC_ID = 1234           # 限制的 Topic ID (即討論串 ID)，若無 Topic 請設為 None

# 記憶體快取：儲存各群組狀態 { chat_id: {"current_number": int, "last_user_id": int} }
cache = {}

# 設定日誌
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 資料庫與快取邏輯 ---
async def init_db_and_cache():
    """初始化資料庫並載入快取"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                current_number INTEGER DEFAULT 0,
                last_user_id INTEGER DEFAULT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                high_score INTEGER DEFAULT 0
            )
        ''')
        await db.commit()
        
        async with db.execute('SELECT chat_id, current_number, last_user_id FROM chat_state') as cursor:
            async for row in cursor:
                cache[row[0]] = {"current_number": row[1], "last_user_id": row[2]}
    logger.info(f"資料庫初始化完成，載入 {len(cache)} 筆資料")

async def sync_state_to_db(chat_id):
    """同步進度至 DB"""
    state = cache.get(chat_id)
    if not state: return
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute('''
                INSERT OR REPLACE INTO chat_state (chat_id, current_number, last_user_id)
                VALUES (?, ?, ?)
            ''', (chat_id, state["current_number"], state["last_user_id"]))
            await db.commit()
    except Exception as e:
        logger.error(f"DB Sync Error: {e}")

async def update_user_record(user_id, username, current_score):
    """更新使用者最高紀錄"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT high_score FROM user_stats WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
                old_high = row[0] if row else 0
            
            if current_score > old_high:
                await db.execute('''
                    INSERT OR REPLACE INTO user_stats (user_id, username, high_score)
                    VALUES (?, ?, ?)
                ''', (user_id, username, current_score))
                await db.commit()
            elif row is None:
                await db.execute('INSERT INTO user_stats (user_id, username, high_score) VALUES (?, ?, ?)', 
                                 (user_id, username, current_score))
                await db.commit()
    except Exception as e:
        logger.error(f"User Record Update Error: {e}")

# --- 數學解析邏輯 ---
def safe_math_eval(text):
    """使用 SymPy 安全解析數學算式 (支援 π, ×, ÷, [], {})"""
    # 1. 符號預處理
    expr_str = text.replace('^', '**')
    expr_str = expr_str.replace('×', '*')
    expr_str = expr_str.replace('÷', '/')
    expr_str = expr_str.replace('π', 'pi')
    
    # 處理括號變體
    expr_str = expr_str.replace('[', '(').replace(']', ')')
    expr_str = expr_str.replace('{', '(').replace('}', ')')

    # 2. 根號轉換
    expr_str = re.sub(r'√\s*(\((?:[^()]|\([^()]*\))*\)|[\d\.]+)', r'sqrt(\1)', expr_str)
    
    if '√' in expr_str:
        return None

    try:
        # 啟動隱含乘法支援 (如 2pi -> 2 * pi)
        transformations = standard_transformations + (implicit_multiplication_application,)
        expr = parse_expr(expr_str, transformations=transformations, evaluate=True)
        return expr.evalf()
    except:
        return None

# --- 指令與事件處理 ---
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """排行榜指令"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT username, high_score FROM user_stats ORDER BY high_score DESC LIMIT 10') as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        await update.message.reply_text("還沒有紀錄喔！")
        return

    text = "🏆 **報數最高紀錄榜**\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, score) in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        text += f"{rank} {name or '神秘人'}: `{score}`\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    msg = update.message
    chat_id, topic_id = msg.chat_id, msg.message_thread_id 
    
    if ALLOWED_CHAT_ID is not None and chat_id != ALLOWED_CHAT_ID: return
    if ALLOWED_TOPIC_ID is not None and topic_id != ALLOWED_TOPIC_ID: return
    
    user_id, user_name = msg.from_user.id, msg.from_user.full_name
    text = msg.text.strip()

    # 擴大 Regex 以匹配新支援的符號
    # 匹配範圍：數字、根號、π、括號、運算符號 (含 × ÷)
    match = re.search(r'([\d√\(π\[\{][\d\+\-\*\/\(\)\.\^\√\s×÷π\[\]\{\}]*[\d\)\]\}π])|(\b\d+\b)', text)
    if not match: return

    potential_expr = match.group(0).strip()
    evaluated_val = safe_math_eval(potential_expr)
    if evaluated_val is None: return

    # 狀態獲取
    if chat_id not in cache: cache[chat_id] = {"current_number": 0, "last_user_id": None}
    state = cache[chat_id]
    target_num = state["current_number"] + 1

    try:
        # 無條件捨去判斷
        val_after_floor = math.floor(float(evaluated_val))

        # 連續報數檢查
        if user_id == state["last_user_id"]:
            await msg.reply_text(f"⚠️ {user_name}，不可連續報數！")
            return

        if val_after_floor == target_num:
            # 答對了
            cache[chat_id] = {"current_number": target_num, "last_user_id": user_id}
            asyncio.create_task(sync_state_to_db(chat_id))
            asyncio.create_task(update_user_record(user_id, user_name, target_num))
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except: pass
        else:
            # 數錯了
            cache[chat_id] = {"current_number": 0, "last_user_id": None}
            asyncio.create_task(sync_state_to_db(chat_id))
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👎")])
            except: pass

            raw_val_str = f"{float(evaluated_val):g}"
            error_reason = f"正確應為 {target_num} (你的結果捨去後為 {val_after_floor}，計算值 {raw_val_str})"

            until_date = int(time.time() + MUTE_DURATION)
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await msg.reply_text(f"💀 數錯了！\n{error_reason}\n{user_name} 已被禁言 1 小時，重頭開始。")
            except (Forbidden, BadRequest):
                await msg.reply_text(f"⚠️ 數錯了！\n{error_reason}\n進度已重置。")

    except Exception as e:
        logger.error(f"Process Error: {e}")

async def post_init(application: Application):
    await init_db_and_cache()
    await application.bot.set_my_commands([BotCommand("leaderboard", "查看最高紀錄排行榜")])
    logger.info("指令清單已維護")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.GROUPS, handle_message))
    logger.info("機器人已啟動...")
    app.run_polling()