import logging
import aiosqlite
import time
import asyncio
import re
import math
import sympy
from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application
from telegram import Update, ChatPermissions, ReactionTypeEmoji
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, Application
from telegram.error import TelegramError, BadRequest, Forbidden

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
    """初始化資料庫並載入快取"""
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
                cache[row[0]] = {"current_number": row[1], "last_user_id": row[2]}
    logger.info(f"資料庫初始化完成，已載入 {len(cache)} 筆資料")

async def sync_to_db(chat_id):
    """同步快取至資料庫"""
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

# --- 數學解析邏輯 ---
def safe_math_eval(text):
    """使用 SymPy 安全解析數學算式"""
    # 1. 預處理符號
    # 將 ^ 換成 **
    expr_str = text.replace('^', '**')
    # 將 √ 轉換為 sqrt()
    # 支援 √9 -> sqrt(9) 以及 √(2+2) -> sqrt((2+2))
    expr_str = re.sub(r'√\s*(\((?:[^()]|\([^()]*\))*\)|[\d\.]+)', r'sqrt(\1)', expr_str)
    
    # 如果還剩下單獨的 √ 符號，說明格式錯誤
    if '√' in expr_str:
        return None

    try:
        # 使用 SymPy 的解析器，並加入「隱含乘法」支援 (例如 2(3+1) -> 8)
        transformations = standard_transformations + (implicit_multiplication_application,)
        # 限制解析環境，只允許數值運算
        expr = parse_expr(expr_str, transformations=transformations, evaluate=True)
        
        # 取得數值結果 (evalf)
        result = expr.evalf()
        return result
    except:
        return None

# --- 核心業務邏輯 ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    msg = update.message
    chat_id = msg.chat_id
    topic_id = msg.message_thread_id 
    
    # 白名單過濾
    if ALLOWED_CHAT_ID is not None and chat_id != ALLOWED_CHAT_ID: return
    if ALLOWED_TOPIC_ID is not None and topic_id != ALLOWED_TOPIC_ID: return
    
    user_id = msg.from_user.id
    user_name = msg.from_user.first_name
    text = msg.text.strip()

    # 1. 從文字中提取第一個可能的數學區塊
    # Regex 包含數字、運算符、√、^ 與括號
    match = re.search(r'([\d√\(][\d\+\-\*\/\(\)\.\^\√\s]*[\d\)])|(\b\d+\b)', text)
    if not match:
        return

    potential_expr = match.group(0).strip()
    evaluated_val = safe_math_eval(potential_expr)

    # 如果解析不出有效數字，視為一般聊天
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
        # 使用 SymPy 判斷是否為整數
        is_integer = False
        val_as_int = 0
        
        # 容許微小的浮點誤差 (例如 3.0000000000001)
        if abs(evaluated_val - round(evaluated_val)) < 1e-10:
            is_integer = True
            val_as_int = int(round(evaluated_val))

        # 檢查連續報數
        if user_id == last_user:
            await msg.reply_text(f"⚠️ {user_name}，你不能連續報數！")
            return

        # 判斷對錯
        if is_integer and val_as_int == target_num:
            # --- 答對了 ---
            cache[chat_id] = {"current_number": target_num, "last_user_id": user_id}
            asyncio.create_task(sync_to_db(chat_id))
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except: pass
        else:
            # --- 答錯了 ---
            cache[chat_id] = {"current_number": 0, "last_user_id": None}
            asyncio.create_task(sync_to_db(chat_id))
            
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👎")])
            except: pass

            error_reason = f"應該是 {target_num}"
            if not is_integer:
                # 使用 :g 格式化去掉多餘的 0，並確保轉換為 float 以進行標準格式化
                formatted_val = f"{float(evaluated_val):g}"
                error_reason = f"報數必須是整數，不接受「{formatted_val}」"

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
                await msg.reply_text(f"⚠️ 數錯了！({error_reason})。數字已重置為 1。")

    except TelegramError as te:
        logger.error(f"Telegram API Error: {te}")
    except Exception as ge:
        logger.error(f"General Error: {ge}")

async def post_init(application: Application):
    await init_db_and_cache()

if __name__ == '__main__':
    # 注意：需要安裝 sympy, aiosqlite, python-telegram-bot
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.GROUPS, handle_message))
    
    logger.info("機器人已啟動...")
    app.run_polling()