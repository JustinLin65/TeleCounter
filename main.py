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
        # 群組報數進度表
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                current_number INTEGER DEFAULT 0,
                last_user_id INTEGER DEFAULT NULL
            )
        ''')
        # 使用者個人紀錄表
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
    logger.info(f"資料庫初始化完成，已載入 {len(cache)} 筆群組進度資料")

async def sync_state_to_db(chat_id):
    """同步群組進度至資料庫"""
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
        logger.error(f"DB Sync State Error: {e}")

async def update_user_record(user_id, username, current_score):
    """更新使用者個人最高紀錄"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            # 先檢查舊紀錄
            async with db.execute('SELECT high_score FROM user_stats WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
                old_high = row[0] if row else 0
            
            # 如果目前報數大於舊紀錄，則更新
            if current_score > old_high:
                await db.execute('''
                    INSERT OR REPLACE INTO user_stats (user_id, username, high_score)
                    VALUES (?, ?, ?)
                ''', (user_id, username, current_score))
                await db.commit()
            elif row is None:
                # 即使沒破紀錄，第一次參加也建立資料
                await db.execute('''
                    INSERT INTO user_stats (user_id, username, high_score)
                    VALUES (?, ?, ?)
                ''', (user_id, username, current_score))
                await db.commit()
    except Exception as e:
        logger.error(f"DB Update Record Error: {e}")

# --- 數學解析邏輯 ---
def safe_math_eval(text):
    """使用 SymPy 安全解析數學算式"""
    expr_str = text.replace('^', '**')
    expr_str = re.sub(r'√\s*(\((?:[^()]|\([^()]*\))*\)|[\d\.]+)', r'sqrt(\1)', expr_str)
    
    if '√' in expr_str:
        return None

    try:
        transformations = standard_transformations + (implicit_multiplication_application,)
        expr = parse_expr(expr_str, transformations=transformations, evaluate=True)
        result = expr.evalf()
        return result
    except:
        return None

# --- 指令處理器 ---
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示排行榜"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT username, high_score FROM user_stats ORDER BY high_score DESC LIMIT 10') as cursor:
                rows = await cursor.fetchall()
        
        if not rows:
            await update.message.reply_text("目前還沒有人報數喔！")
            return

        text = "🏆 **報數排行榜 (最高紀錄)**\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for i, (name, score) in enumerate(rows):
            rank_icon = medals[i] if i < 3 else f"{i+1}."
            text += f"{rank_icon} {name or '神秘人'}: `{score}`\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Leaderboard Error: {e}")

# --- 核心報數邏輯 ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    msg = update.message
    chat_id = msg.chat_id
    topic_id = msg.message_thread_id 
    
    if ALLOWED_CHAT_ID is not None and chat_id != ALLOWED_CHAT_ID: return
    if ALLOWED_TOPIC_ID is not None and topic_id != ALLOWED_TOPIC_ID: return
    
    user_id = msg.from_user.id
    user_name = msg.from_user.full_name # 使用全名
    text = msg.text.strip()

    # 1. 提取數學區塊
    match = re.search(r'([\d√\(][\d\+\-\*\/\(\)\.\^\√\s]*[\d\)])|(\b\d+\b)', text)
    if not match:
        return

    potential_expr = match.group(0).strip()
    evaluated_val = safe_math_eval(potential_expr)

    if evaluated_val is None:
        return

    # 2. 狀態管理
    if chat_id not in cache:
        cache[chat_id] = {"current_number": 0, "last_user_id": None}
    
    state = cache[chat_id]
    current_num = state["current_number"]
    last_user = state["last_user_id"]
    target_num = current_num + 1

    # 3. 判斷邏輯
    try:
        val_after_floor = math.floor(float(evaluated_val))

        if user_id == last_user:
            await msg.reply_text(f"⚠️ {user_name}，你不能連續報數！")
            return

        if val_after_floor == target_num:
            # --- 答對了 ---
            cache[chat_id] = {"current_number": target_num, "last_user_id": user_id}
            # 更新群組進度與使用者個人高分
            asyncio.create_task(sync_state_to_db(chat_id))
            asyncio.create_task(update_user_record(user_id, user_name, target_num))
            
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except: pass
        else:
            # --- 答錯了 ---
            cache[chat_id] = {"current_number": 0, "last_user_id": None}
            asyncio.create_task(sync_state_to_db(chat_id))
            
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👎")])
            except: pass

            raw_val_str = f"{float(evaluated_val):g}"
            error_reason = f"應該是 {target_num} (你的結果捨去後為 {val_after_floor}，原始值 {raw_val_str})"

            until_date = int(time.time() + MUTE_DURATION)
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await msg.reply_text(
                    f"💀 數錯了！\n{error_reason}\n"
                    f"{user_name} 已被禁言 1 小時，數字歸 1。"
                )
            except (Forbidden, BadRequest):
                await msg.reply_text(f"⚠️ 數錯了！\n{error_reason}\n數字已重置為 1。")

    except TelegramError as te:
        logger.error(f"Telegram API Error: {te}")
    except Exception as ge:
        logger.error(f"General Error: {ge}")

async def post_init(application: Application):
    """初始化資料庫並設定指令清單"""
    await init_db_and_cache()
    
    # 向 Telegram 註冊指令清單
    commands = [
        BotCommand("leaderboard", "查看報數最高紀錄排行榜"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("機器人指令清單已更新")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    # 註冊指令處理器
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    # 註冊訊息處理器
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.GROUPS, handle_message))
    
    logger.info("機器人已啟動 (支援排行榜與指令選單)...")
    app.run_polling()