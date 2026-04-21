import logging
import aiosqlite
import time
import asyncio
import re
import math
from telegram import Update, ChatPermissions, ReactionTypeEmoji
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, Application
from telegram.error import TelegramError, BadRequest, Forbidden
from simpleeval import simple_eval, InvalidExpression

# --- 配置區 ---
TOKEN = "0123456789:ABCD1234EFGH5678IJKLMNOPQRSTUVWXYZ"  # 請替換成你的 Telegram Bot Token
DB_NAME = "counting_bot.db"
MUTE_DURATION = 60  # 禁言秒數

# 白名單設定 (設為 None 則不限制)
ALLOWED_CHAT_ID = -100123456789  # 群組 ID
ALLOWED_TOPIC_ID = 1234           # Topic ID

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
    """初始化資料庫並將所有資料載入記憶體快取"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                current_number INTEGER DEFAULT 0,
                last_user_id INTEGER DEFAULT NULL
            )
        ''')
        await db.commit()
        
        async with db.execute('SELECT chat_id, current_number, last_user_id FROM chat_state') as cursor:
            async for row in cursor:
                cache[row[0]] = {
                    "current_number": row[1],
                    "last_user_id": row[2]
                }
    logger.info(f"資料庫初始化完成，已載入 {len(cache)} 筆群組資料")

async def sync_to_db(chat_id):
    """將快取同步至資料庫 (非同步執行)"""
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

# --- 核心業務邏輯 ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    msg = update.message
    chat_id = msg.chat_id
    topic_id = msg.message_thread_id 
    
    # 權限與白名單過濾
    if ALLOWED_CHAT_ID is not None and chat_id != ALLOWED_CHAT_ID: return
    if ALLOWED_TOPIC_ID is not None and topic_id != ALLOWED_TOPIC_ID: return
    
    user_id = msg.from_user.id
    user_name = msg.from_user.first_name
    text = msg.text.strip()

    # 1. 解析訊息
    is_numeric_input = False
    evaluated_val = None

    match = re.search(r'([sS][qQ][rR][tT]\s*\(.*?\)|[\d\(\)√][\d\+\-\*\/\(\)\.\^\√\s\w]*)', text)
    
    if match:
        potential_expr = match.group(1).strip()
        potential_expr = potential_expr.replace('^', '**')
        potential_expr = re.sub(r'√\s*([\d\.]+)', r'sqrt(\1)', potential_expr)

        try:
            if len(potential_expr) <= 60:
                res = simple_eval(potential_expr, functions={"sqrt": math.sqrt})
                if isinstance(res, (int, float)):
                    is_numeric_input = True
                    evaluated_val = res
        except:
            pass

    if not is_numeric_input:
        return

    # 2. 獲取當前進度
    if chat_id not in cache:
        cache[chat_id] = {"current_number": 0, "last_user_id": None}
    
    state = cache[chat_id]
    current_num = state["current_number"]
    last_user = state["last_user_id"]
    target_num = current_num + 1

    # 3. 判斷邏輯
    try:
        is_integer = False
        final_int_val = 0
        
        if isinstance(evaluated_val, int):
            is_integer = True
            final_int_val = evaluated_val
        elif isinstance(evaluated_val, float) and evaluated_val.is_integer():
            is_integer = True
            final_int_val = int(evaluated_val)

        if user_id == last_user:
            await msg.reply_text(f"⚠️ {user_name}，你不能連續報數！")
            return

        if is_integer and final_int_val == target_num:
            # --- 答對了 ---
            cache[chat_id] = {"current_number": target_num, "last_user_id": user_id}
            asyncio.create_task(sync_to_db(chat_id))
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except:
                pass
        else:
            # --- 答錯了 ---
            cache[chat_id] = {"current_number": 0, "last_user_id": None}
            asyncio.create_task(sync_to_db(chat_id))
            
            # 1. 給予 👎 反應
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👎")])
            except:
                pass

            # 2. 準備錯誤訊息與禁言
            error_reason = f"應該是 {target_num}"
            if not is_integer:
                error_reason = f"報數必須是整數，不接受「{evaluated_val}」"

            until_date = int(time.time() + MUTE_DURATION)
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await msg.reply_text(
                    f"💀 數錯了！({error_reason})\n"
                    f"{user_name} 已被禁言 1 小時，報數重新從 1 開始！"
                )
            except (Forbidden, BadRequest):
                await msg.reply_text(f"⚠️ 數錯了！({error_reason})。由於權限限制，無法禁言你，但數字已重置為 1。")

    except TelegramError as te:
        logger.error(f"Telegram API Error: {te}")
    except Exception as ge:
        logger.error(f"General Error: {ge}")

async def post_init(application: Application):
    """在機器人啟動前執行的非同步初始化"""
    await init_db_and_cache()

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.GROUPS, handle_message))
    
    logger.info("機器人已啟動 (數錯會給 👎)...")
    app.run_polling()