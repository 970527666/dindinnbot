import os
import sys
import json
import time
import uuid
import logging
import asyncio
from datetime import datetime
from collections import deque
import pytz

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatMemberUpdated
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters
)

# ----------------- 配置信息 -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "9218776179:AAGD7K1hhvY8xVrtioz-hsP0ZuXjoDkAzjo")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004333341021")) # Caryn Fans Club
ADMIN_ID = int(os.getenv("ADMIN_ID", "7874516641"))          # 管理员 dinasour👀
TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Kuala_Lumpur")   # 时区 GMT+8

# 防刷粉阈值：3 秒内涌入超过 5 个人触发警报
BURST_SECONDS = 3
BURST_THRESHOLD = 5

SCHEDULE_FILE = "schedules.json"

# 日志设置
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("CarynGuardBot")

# ----------------- 全局状态 -----------------
event_mode = False          # 抽奖/活动模式开关（开启时不弹刷粉警报）
recent_joins = deque()      # 滑动窗口记录进群时间戳
pending_batches = {}        # 待处理的刷粉批次 {batch_id: [user_dict, ...]}

# ----------------- 定时发帖持久化 -----------------
def load_schedules():
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载定时任务出错: {e}")
    return []

def save_schedules(schedules):
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(schedules, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存定时任务出错: {e}")

# ----------------- 防刷粉监听 -----------------
def extract_status_change(chat_member_update: ChatMemberUpdated):
    status_change = chat_member_update.difference().get("status")
    old_is_member = chat_member_update.old_chat_member.status in [
        ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR
    ]
    new_is_member = chat_member_update.new_chat_member.status in [
        ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR
    ]
    return not old_is_member and new_is_member

async def track_joins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global event_mode, recent_joins, pending_batches
    
    chat_member = update.chat_member
    if not chat_member or chat_member.chat.id != CHANNEL_ID:
        return

    # 判断是否为新人加入
    if not extract_status_change(chat_member):
        return

    user = chat_member.new_chat_member.user
    if user.is_bot:
        return

    now = time.time()
    user_info = {
        "id": user.id,
        "first_name": user.first_name or "",
        "username": user.username or "无用户名",
        "time": now
    }

    recent_joins.append(user_info)

    # 清理超过 3 秒的老数据
    while recent_joins and now - recent_joins[0]["time"] > BURST_SECONDS:
        recent_joins.popleft()

    # 检查是否在 3 秒内达到触发阈值
    if len(recent_joins) >= BURST_THRESHOLD:
        if event_mode:
            logger.info("活动模式开启中，忽略突增流量。")
            return

        batch_id = str(uuid.uuid4())[:8]
        batch_users = list(recent_joins)
        pending_batches[batch_id] = batch_users
        recent_joins.clear() # 清空防止重复报警

        # 构造警报消息
        user_list_text = "\n".join([
            f"• {u['first_name']} (@{u['username']}) [ID: {u['id']}]"
            for u in batch_users[:10]
        ])
        if len(batch_users) > 10:
            user_list_text += f"\n... 以及其他 {len(batch_users) - 10} 个账号"

        keyboard = [
            [
                InlineKeyboardButton(" 没做活动！一键封禁这批假号", callback_data=f"ban_{batch_id}"),
            ],
            [
                InlineKeyboardButton("✅ 我在做活动，放行忽略", callback_data=f"ignore_{batch_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        alert_text = (
            f"🚨 **【突发流量警报！】**\n\n"
            f"检测到在 **{BURST_SECONDS} 秒内涌入 {len(batch_users)} 个账号**！\n\n"
            f"**部分涌入账号：**\n{user_list_text}\n\n"
            f"⚠️ 请核实当前是否在做抽奖/引流活动："
        )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=alert_text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"发送警报失败: {e}")

# ----------------- 警报按钮回调 -----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("ban_"):
        batch_id = data.replace("ban_", "")
        users = pending_batches.pop(batch_id, [])
        banned_count = 0
        for u in users:
            try:
                await context.bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=u["id"])
                banned_count += 1
            except Exception as e:
                logger.error(f"封禁用户 {u['id']} 失败: {e}")

        await query.edit_message_text(
            f"✅ **处理完毕！**\n已成功自动封禁拉黑 **{banned_count}** 个恶意刷粉账号！",
            parse_mode="Markdown"
        )

    elif data.startswith("ignore_"):
        batch_id = data.replace("ignore_", "")
        pending_batches.pop(batch_id, None)
        await query.edit_message_text("✅ **已忽略并放行**：这批用户已正常进入频道。")

# ----------------- 定时排行程发帖 -----------------
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    tz = pytz.timezone(TIMEZONE_STR)
    # 支持两种用法：
    # 1. 回复一条带图片/视频的消息：/schedule 2026-11-08 18:00
    # 2. 直接发文字指令：/schedule 2026-11-08 18:00 帖子文案内容
    
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "ℹ️ **排行程发帖用法：**\n\n"
            "**用法 1（发纯文字）：**\n"
            "`/schedule 2026-11-08 18:00 这是要发布的文案内容`\n\n"
            "**用法 2（发图文/视频）：**\n"
            "先在私聊里发图片或视频给机器人，然后**回复该消息**发送：\n"
            "`/schedule 2026-11-08 18:00`",
            parse_mode="Markdown"
        )
        return

    time_str = f"{args[0]} {args[1]}"
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        dt_localized = tz.localize(dt)
        post_timestamp = dt_localized.timestamp()
    except Exception:
        await update.message.reply_text("❌ 时间格式错误！请使用标准格式：`YYYY-MM-DD HH:MM`（例如 `2026-11-08 18:00`）")
        return

    now_ts = time.time()
    if post_timestamp <= now_ts:
        await update.message.reply_text("❌ 设定的时间必须是将来的时间！")
        return

    job_id = str(uuid.uuid4())[:8]
    schedules = load_schedules()

    if update.message.reply_to_message:
        # 回复了消息
        rep = update.message.reply_to_message
        job = {
            "id": job_id,
            "type": "forward_copy",
            "from_chat_id": rep.chat.id,
            "message_id": rep.message_id,
            "post_time": post_timestamp,
            "time_str": time_str,
            "preview": rep.caption or rep.text or "[媒体消息]"
        }
    else:
        # 纯文本消息
        content = " ".join(args[2:])
        if not content:
            await update.message.reply_text("❌ 请输入要发布的文案内容，或者回复一条包含图片/视频的消息！")
            return
        job = {
            "id": job_id,
            "type": "text",
            "content": content,
            "post_time": post_timestamp,
            "time_str": time_str,
            "preview": content[:30]
        }

    schedules.append(job)
    save_schedules(schedules)

    await update.message.reply_text(
        f"✅ **排行程成功！**\n"
        f"**任务 ID:** `{job_id}`\n"
        f"**计划发送时间:** `{time_str}`\n"
        f"**内容预览:** {job['preview']}",
        parse_mode="Markdown"
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    schedules = load_schedules()
    if not schedules:
        await update.message.reply_text("📭 当前没有待发送的定时任务。")
        return

    lines = ["📅 **待发送的排行程任务：**\n"]
    for s in schedules:
        lines.append(f"• ID: `{s['id']}` | 时间: `{s['time_str']}`\n  预览: {s['preview']}")

    lines.append("\n取消任务指令：`/cancel <任务ID>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("用法: `/cancel <任务ID>`")
        return

    job_id = context.args[0]
    schedules = load_schedules()
    new_schedules = [s for s in schedules if s["id"] != job_id]

    if len(new_schedules) == len(schedules):
        await update.message.reply_text(f"❌ 未找到任务 ID: `{job_id}`")
    else:
        save_schedules(new_schedules)
        await update.message.reply_text(f"✅ 已成功取消任务: `{job_id}`")

# ----------------- 模式切换与状态 -----------------
async def cmd_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global event_mode
    if update.effective_user.id != ADMIN_ID:
        return

    event_mode = not event_mode
    status_str = "🟢 **已开启【活动/抽奖保护模式】**（期间突增流量不弹警报，绝不影响活动）" if event_mode else "🔴 **已恢复【日常防刷巡逻模式】**（异常刷粉将立即报警）"
    await update.message.reply_text(status_str, parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    schedules = load_schedules()
    status_text = (
        f"🤖 **Caryn 守护机器人运行正常**\n\n"
        f"**频道:** `Caryn Fans Club` (`{CHANNEL_ID}`)\n"
        f"**当前模式:** {'🟢 活动免扰' if event_mode else '🔴 防刷守护'}\n"
        f"**待发排程:** {len(schedules)} 条\n"
        f"**时区:** {TIMEZONE_STR}"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "👋 你好！我是 **Caryn Fans Club 守护机器人**。\n\n"
        "**核心功能指令：**\n"
        "📅 `/schedule 2026-11-08 18:00 文案` - 排行程发帖\n"
        "📋 `/list` - 查看待发送的任务列表\n"
        "❌ `/cancel <ID>` - 取消指定任务\n"
        "🎉 `/event` - 切换活动模式（抽奖期间开启，避免误报）\n"
        "📊 `/status` - 查看当前守护状态",
        parse_mode="Markdown"
    )

# ----------------- 后台定时发帖轮询 -----------------
async def scheduler_worker(app):
    while True:
        try:
            now = time.time()
            schedules = load_schedules()
            remaining = []
            for s in schedules:
                if s["post_time"] <= now:
                    try:
                        if s["type"] == "text":
                            await app.bot.send_message(
                                chat_id=CHANNEL_ID,
                                text=s["content"]
                            )
                        elif s["type"] == "forward_copy":
                            await app.bot.copy_message(
                                chat_id=CHANNEL_ID,
                                from_chat_id=s["from_chat_id"],
                                message_id=s["message_id"]
                            )
                        logger.info(f"成功发送定时帖子 ID: {s['id']}")
                    except Exception as e:
                        logger.error(f"发送任务 {s['id']} 失败: {e}")
                else:
                    remaining.append(s)

            if len(remaining) != len(schedules):
                save_schedules(remaining)

        except Exception as e:
            logger.error(f"定时检查异常: {e}")

        await asyncio.sleep(5)

# ----------------- 主程序入口 -----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 注册命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("event", cmd_event))
    app.add_handler(CommandHandler("status", cmd_status))

    # 监听进群
    app.add_handler(ChatMemberHandler(track_joins, ChatMemberHandler.CHAT_MEMBER))

    # 监听按钮回调
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 启动后台任务调度器
    async def on_startup(application):
        asyncio.create_task(scheduler_worker(application))

    app.post_init = on_startup

    logger.info("Bot 正在启动监听...")
    app.run_polling(allowed_updates=["message", "chat_member", "callback_query"])

if __name__ == "__main__":
    main()
