import logging
import sqlite3
import time
import os
from telegram import Update, ChatPermissions, ReactionTypeEmoji
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, BadRequest, Forbidden
from simpleeval import simple_eval, InvalidExpression

# --- 配置區 ---
TOKEN = "0123456789:ABCD1234EFGH5678IJKLMNOPQRSTUVWX"  # 請替換成你的 Telegram Bot Token
DB_NAME = "counting_bot.db"
MUTE_DURATION = 10  # 禁言秒數 (1小時)

# 白名單設定 (設為 None 則不限制)
ALLOWED_CHAT_ID = -100123456789  # 限制的群組 ID
ALLOWED_TOPIC_ID = 1234           # 限制的 Topic ID，若無 Topic 請設為 None

# 設定日誌
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 資料庫邏輯 ---
def init_db():
    """初始化 SQLite 資料庫"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            current_number INTEGER DEFAULT 0,
            last_user_id INTEGER DEFAULT NULL
        )
    ''')
    conn.commit()
    conn.close()

def get_state(chat_id):
    """取得該群組目前的報數狀態"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT current_number, last_user_id FROM chat_state WHERE chat_id = ?', (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"current_number": row[0], "last_user_id": row[1]}
    return {"current_number": 0, "last_user_id": None}

def update_state(chat_id, current_number, last_user_id):
    """更新該群組的報數狀態"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO chat_state (chat_id, current_number, last_user_id)
        VALUES (?, ?, ?)
    ''', (chat_id, current_number, last_user_id))
    conn.commit()
    conn.close()

# --- 核心業務邏輯 ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    msg = update.message
    chat_id = msg.chat_id
    topic_id = msg.message_thread_id 
    
    if ALLOWED_CHAT_ID is not None and chat_id != ALLOWED_CHAT_ID:
        return

    if ALLOWED_TOPIC_ID is not None and topic_id != ALLOWED_TOPIC_ID:
        return
    
    user_id = msg.from_user.id
    user_name = msg.from_user.first_name
    text = msg.text.strip()

    # 1. 嘗試解析算式
    try:
        # 限制長度以防惡意計算
        if len(text) > 50:
            return
            
        # 執行計算
        result_raw = simple_eval(text)
        
        # 判斷是否為有效的數字結果
        if not isinstance(result_raw, (int, float)):
            return
        
        result = int(result_raw)
        # 確保是整數報數，不接受 1.5 之類的
        if result != result_raw:
            return
            
    except (InvalidExpression, SyntaxError, ZeroDivisionError, Exception):
        # 捕捉所有解析錯誤，代表這不是一個有效的報數，直接忽略不計
        return

    # 2. 獲取當前進度
    state = get_state(chat_id)
    current_num = state["current_number"]
    last_user = state["last_user_id"]
    target_num = current_num + 1

    # 3. 邏輯檢查
    try:
        # A. 檢查是否連續報數
        if user_id == last_user:
            await msg.reply_text(f"⚠️ {user_name}，你不能連續報數！")
            return

        # B. 數字正確
        if result == target_num:
            update_state(chat_id, target_num, user_id)
            try:
                await msg.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except:
                pass
            
        # C. 數字錯誤
        else:
            update_state(chat_id, 0, None)
            until_date = int(time.time() + MUTE_DURATION)
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await msg.reply_text(
                    f"💀 數錯了！結果應該是 {target_num}。\n"
                    f"{user_name} 已被禁言 1 小時，報數重頭開始！"
                )
            except (Forbidden, BadRequest):
                await msg.reply_text(f"⚠️ 數錯了！結果應該是 {target_num}。但我權限不足或你是管理員，無法禁言。數字已重置。")

    except TelegramError as te:
        logger.error(f"Telegram API 錯誤: {te}")
    except Exception as ge:
        logger.error(f"一般錯誤: {ge}")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.GROUPS, handle_message))
    logger.info("機器人已啟動 (修正匯入錯誤後)...")
    app.run_polling()