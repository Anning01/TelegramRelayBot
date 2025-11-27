import asyncio
import logging
import os
import random
import string
from collections import defaultdict

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import TargetUser, RelayGroup, MessageLog

load_dotenv()
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")

bot = (
    Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if TOKEN
    else None
)
dp = Dispatcher()

# 媒体组缓存：{media_group_id: [messages]}
media_groups = defaultdict(list)
# 媒体组处理任务：{media_group_id: asyncio.Task}
media_group_tasks = {}


def is_english_word(word: str) -> bool:
    """检查单词是否为英文（至少50%是拉丁字母）"""
    if not word:
        return False
    latin_chars = sum(1 for c in word if c.isascii() and c.isalpha())
    return latin_chars >= len(word) * 0.5


def generate_random_tag() -> str:
    """生成随机3-4个大写字母的标记"""
    length = random.randint(3, 4)
    return "".join(random.choices(string.ascii_uppercase, k=length))


def get_sender_tag(user: types.User) -> str:
    """从用户名生成发送者标记"""
    if not user:
        return generate_random_tag()

    full_name = user.full_name.strip()
    if not full_name:
        return generate_random_tag()

    # 取第一个单词（而非最后一个）
    first_word = full_name.split()[0]

    # 如果第一个单词不是英文，随机生成
    if not is_english_word(first_word):
        return generate_random_tag()

    return first_word.upper()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Register user as a potential target
    async with AsyncSessionLocal() as session:
        user_id = message.from_user.id
        username = message.from_user.full_name

        result = await session.execute(
            select(TargetUser).where(TargetUser.user_id == user_id)
        )
        user = result.scalars().first()

        if not user:
            new_user = TargetUser(
                user_id=user_id, name=username, is_active=False
            )  # Default inactive until admin approves
            session.add(new_user)
            await session.commit()
            await message.answer(
                f"✅ 欢迎！您已成功注册。\n\n您的 ID：<code>{user_id}</code>\n\n请联系管理员在后台启用您的账号，并将您添加到相应的群组转发列表中。"
            )
        else:
            await message.answer(f"您已注册过了。\n\n您的 ID：<code>{user_id}</code>")


async def forward_messages_to_users(
    messages: list[types.Message], chat_id: int, chat_title: str
):
    """转发单条或多条消息到配置的用户"""
    if not messages:
        return

    # 使用第一条消息获取发送者信息
    first_message = messages[0]
    if not first_message.from_user:
        return

    async with AsyncSessionLocal() as session:
        # 1. Update/Check Group Status (加载关联的用户)
        group_res = await session.execute(
            select(RelayGroup)
            .options(selectinload(RelayGroup.target_users))
            .where(RelayGroup.group_id == chat_id)
        )
        group = group_res.scalars().first()

        if not group:
            group = RelayGroup(group_id=chat_id, title=chat_title, is_active=True)
            session.add(group)
            try:
                await session.commit()
                # 重新查询以加载关系
                group_res = await session.execute(
                    select(RelayGroup)
                    .options(selectinload(RelayGroup.target_users))
                    .where(RelayGroup.group_id == chat_id)
                )
                group = group_res.scalars().first()
            except IntegrityError:
                # Group was inserted by another concurrent request, rollback and re-query
                await session.rollback()
                group_res = await session.execute(
                    select(RelayGroup)
                    .options(selectinload(RelayGroup.target_users))
                    .where(RelayGroup.group_id == chat_id)
                )
                group = group_res.scalars().first()
                if not group:
                    logger.error(f"Failed to find group {chat_id} after IntegrityError")
                    return

        if not group.is_active:
            return

        # 2. 获取该群组配置的目标用户（只转发给激活的用户）
        target_users = [user for user in group.target_users if user.is_active]

        if not target_users:
            logger.info(f"No active target users configured for group {chat_title}")
            return

        # 3. Process and Forward
        sender_tag = get_sender_tag(first_message.from_user)
        is_media_group = len(messages) > 1

        for target in target_users:
            # Increment Index
            target.current_index += 1
            idx = target.current_index

            # Format Message Header
            if is_english_word(sender_tag):
                header = f"<b>{sender_tag[0]}{idx}</b>"
            else:
                header = f"<b>#{idx}</b>"

            try:
                if is_media_group:
                    # 媒体组：发送多个媒体
                    from aiogram.types import (
                        InputMediaPhoto,
                        InputMediaVideo,
                        InputMediaDocument,
                        InputMediaAudio,
                    )

                    media_list = []
                    for i, msg in enumerate(messages):
                        caption = None
                        # 只在第一个媒体添加 caption
                        if i == 0:
                            text_content = msg.text or msg.caption or ""
                            caption = (
                                f"{header}\n{text_content}" if text_content else header
                            )

                        # 构建 InputMedia 对象
                        if msg.photo:
                            media_list.append(
                                InputMediaPhoto(
                                    media=msg.photo[-1].file_id,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                )
                            )
                        elif msg.video:
                            media_list.append(
                                InputMediaVideo(
                                    media=msg.video.file_id,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                )
                            )
                        elif msg.document:
                            media_list.append(
                                InputMediaDocument(
                                    media=msg.document.file_id,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                )
                            )
                        elif msg.audio:
                            media_list.append(
                                InputMediaAudio(
                                    media=msg.audio.file_id,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                )
                            )

                    if media_list:
                        await bot.send_media_group(target.user_id, media_list)
                else:
                    # 单条消息
                    message = messages[0]
                    text_content = message.text or message.caption or ""
                    final_text = f"{header}\n{text_content}" if text_content else header

                    if message.text:
                        await bot.send_message(target.user_id, final_text)
                    elif message.photo:
                        await bot.send_photo(
                            target.user_id,
                            message.photo[-1].file_id,
                            caption=final_text,
                        )
                    elif message.video:
                        await bot.send_video(
                            target.user_id, message.video.file_id, caption=final_text
                        )
                    elif message.document:
                        await bot.send_document(
                            target.user_id, message.document.file_id, caption=final_text
                        )
                    elif message.voice:
                        await bot.send_voice(
                            target.user_id, message.voice.file_id, caption=final_text
                        )
                    elif message.audio:
                        await bot.send_audio(
                            target.user_id, message.audio.file_id, caption=final_text
                        )
                    else:
                        await bot.send_message(
                            target.user_id, f"{header}\n[不支持的媒体类型]"
                        )

                # Log Stats
                log = MessageLog(
                    sender_tag=sender_tag,
                    recipient_id=target.user_id,
                    assigned_index=idx,
                    original_sender_name=first_message.from_user.full_name,
                )
                session.add(log)

            except Exception as e:
                logger.error(f"Failed to send to {target.user_id}: {e}")

        await session.commit()


async def process_media_group(media_group_id: str, chat_id: int, chat_title: str):
    """处理媒体组（延迟 1 秒后执行）"""
    await asyncio.sleep(1.0)  # 等待 1 秒，确保收到所有媒体

    messages = media_groups.get(media_group_id, [])
    if messages:
        logger.info(
            f"Processing media group {media_group_id} with {len(messages)} items"
        )
        await forward_messages_to_users(messages, chat_id, chat_title)

        # 清理缓存
        del media_groups[media_group_id]
        if media_group_id in media_group_tasks:
            del media_group_tasks[media_group_id]


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_message(message: types.Message):
    if not message.from_user:
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "Unknown Group"

    # Log the incoming message
    logger.info(
        f"Received message in group {chat_title} ({chat_id}) from {message.from_user.full_name} ({message.from_user.id}): {message.text or message.caption or '[Media]'}"
    )

    # 检查是否是媒体组
    if message.media_group_id:
        media_group_id = message.media_group_id

        # 添加到媒体组缓存
        media_groups[media_group_id].append(message)

        # 取消之前的处理任务（如果存在）
        if media_group_id in media_group_tasks:
            media_group_tasks[media_group_id].cancel()

        # 创建新的延迟处理任务
        task = asyncio.create_task(
            process_media_group(media_group_id, chat_id, chat_title)
        )
        media_group_tasks[media_group_id] = task
    else:
        # 单条消息，直接转发
        await forward_messages_to_users([message], chat_id, chat_title)
