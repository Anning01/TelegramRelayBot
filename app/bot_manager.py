"""
Bot 管理器：管理多个 Telegram Bot 实例
"""
import asyncio
import logging
from collections import defaultdict
from typing import Dict

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import TargetUser, RelayGroup, MessageLog, BotInstance, GroupUserRelay

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BotHandler:
    """单个 Bot 的消息处理器"""

    def __init__(self, bot_id: int, bot: Bot, dp: Dispatcher):
        self.bot_id = bot_id
        self.bot = bot
        self.dp = dp

        # 媒体组缓存：{media_group_id: [messages]}
        self.media_groups = defaultdict(list)
        # 媒体组处理任务：{media_group_id: asyncio.Task}
        self.media_group_tasks = {}
        # 单条消息延迟任务：{message_id: asyncio.Task}
        self.single_message_tasks = {}
        # 消息缓存：{(chat_id, message_id): message} 用于处理编辑
        self.message_cache = {}

        # 注册消息处理器
        self.register_handlers()

    def register_handlers(self):
        """注册消息处理器"""

        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            await self.handle_start(message)

        @self.dp.message(F.chat.type.in_({"group", "supergroup"}))
        async def handle_group_message(message: types.Message):
            await self.handle_group_msg(message)

        @self.dp.edited_message(F.chat.type.in_({"group", "supergroup"}))
        async def handle_edited_message(message: types.Message):
            await self.handle_message_edit(message)

    async def handle_start(self, message: types.Message):
        """处理 /start 命令"""
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
                )
                session.add(new_user)
                await session.commit()
                await message.answer(
                    f"✅ 欢迎！您已成功注册。\n\n您的 ID：<code>{user_id}</code>\n\n请联系管理员在后台启用您的账号，并将您添加到相应的群组转发列表中。"
                )
            else:
                await message.answer(f"您已注册过了。\n\n您的 ID：<code>{user_id}</code>")

    async def handle_group_msg(self, message: types.Message):
        """处理群组消息"""
        if not message.from_user:
            return

        chat_id = message.chat.id
        chat_title = message.chat.title or "Unknown Group"

        # Log the incoming message
        logger.info(
            f"[Bot {self.bot_id}] Received message in group {chat_title} ({chat_id}) from {message.from_user.full_name}: {message.text or message.caption or '[Media]'}"
        )

        # 立即同步群组信息到数据库（不延迟）
        await self.ensure_group_exists(chat_id, chat_title)

        # 检查是否是媒体组
        if message.media_group_id:
            media_group_id = message.media_group_id

            # 添加到媒体组缓存
            self.media_groups[media_group_id].append(message)
            # 缓存消息用于处理编辑
            self.message_cache[(chat_id, message.message_id)] = message

            # 取消之前的处理任务
            if media_group_id in self.media_group_tasks:
                self.media_group_tasks[media_group_id].cancel()

            # 创建新的延迟处理任务
            task = asyncio.create_task(
                self.process_media_group(media_group_id, chat_id, chat_title)
            )
            self.media_group_tasks[media_group_id] = task
        else:
            # 单条消息，延迟转发
            message_id = message.message_id

            # 缓存消息用于处理编辑
            self.message_cache[(chat_id, message_id)] = message

            # 取消之前的处理任务（如果有）
            if message_id in self.single_message_tasks:
                self.single_message_tasks[message_id].cancel()

            # 创建新的延迟处理任务
            task = asyncio.create_task(
                self.process_single_message(message, chat_id, chat_title)
            )
            self.single_message_tasks[message_id] = task

    async def handle_message_edit(self, message: types.Message):
        """处理消息编辑"""
        if not message.from_user:
            return

        chat_id = message.chat.id
        message_id = message.message_id
        cache_key = (chat_id, message_id)

        logger.info(
            f"[Bot {self.bot_id}] 消息被编辑: chat_id={chat_id}, message_id={message_id}"
        )

        # 如果消息在缓存中，更新它
        if cache_key in self.message_cache:
            logger.info(f"[Bot {self.bot_id}] 更新缓存中的消息内容")
            self.message_cache[cache_key] = message

            # 如果是媒体组中的消息，也更新媒体组缓存
            if message.media_group_id:
                media_group_id = message.media_group_id
                if media_group_id in self.media_groups:
                    # 找到并替换该消息
                    for i, msg in enumerate(self.media_groups[media_group_id]):
                        if msg.message_id == message_id:
                            self.media_groups[media_group_id][i] = message
                            logger.info(f"[Bot {self.bot_id}] 更新媒体组缓存中的消息")
                            break
        else:
            logger.info(f"[Bot {self.bot_id}] 消息不在缓存中，可能已经转发或不需要转发")

    async def ensure_group_exists(self, chat_id: int, chat_title: str):
        """确保群组在数据库中存在（立即执行，不延迟）"""
        async with AsyncSessionLocal() as session:
            # 查找群组
            group_res = await session.execute(
                select(RelayGroup)
                .where(RelayGroup.group_id == chat_id, RelayGroup.bot_id == self.bot_id)
            )
            group = group_res.scalars().first()

            if not group:
                # 创建新群组
                group = RelayGroup(
                    group_id=chat_id,
                    title=chat_title,
                    is_active=True,
                    bot_id=self.bot_id
                )
                session.add(group)
                try:
                    await session.commit()
                    logger.info(f"✅ Created new group: {chat_title} ({chat_id})")
                except IntegrityError:
                    await session.rollback()
                    logger.warning(f"Group {chat_id} already exists (race condition)")

    async def process_single_message(self, message: types.Message, chat_id: int, chat_title: str):
        """处理单条消息（延迟 2 分钟后执行）"""
        await asyncio.sleep(20.0)

        # 检查消息是否仍在缓存中
        cache_key = (chat_id, message.message_id)
        if cache_key not in self.message_cache:
            logger.info(f"[Bot {self.bot_id}] 消息 {message.message_id} 不在缓存中，可能已被删除，取消转发")
            if message.message_id in self.single_message_tasks:
                del self.single_message_tasks[message.message_id]
            return

        # 从缓存中获取最新的消息（可能已被编辑）
        latest_message = self.message_cache[cache_key]

        # 先尝试点赞来验证消息是否还存在
        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message.message_id,
                reaction=[{"type": "emoji", "emoji": "👍"}]
            )
            logger.info(f"✅ [Bot {self.bot_id}] 消息 {message.message_id} 存在，准备转发")
        except Exception as e:
            # 点赞失败，说明消息已被删除
            logger.info(f"⚠️ [Bot {self.bot_id}] 消息 {message.message_id} 已被删除（点赞失败: {e}），取消转发")
            # 清理缓存
            if cache_key in self.message_cache:
                del self.message_cache[cache_key]
            if message.message_id in self.single_message_tasks:
                del self.single_message_tasks[message.message_id]
            return

        # 转发消息（使用最新版本）
        logger.info(f"[Bot {self.bot_id}] 开始转发消息 {message.message_id}（可能已被编辑）")
        await self.forward_messages_to_users([latest_message], chat_id, chat_title)

        # 清理缓存
        if cache_key in self.message_cache:
            del self.message_cache[cache_key]
        if message.message_id in self.single_message_tasks:
            del self.single_message_tasks[message.message_id]

    async def process_media_group(self, media_group_id: str, chat_id: int, chat_title: str):
        """处理媒体组（延迟 2 分钟后执行）"""
        await asyncio.sleep(20.0)

        messages = self.media_groups.get(media_group_id, [])
        if not messages:
            logger.warning(f"[Bot {self.bot_id}] 媒体组 {media_group_id} 为空")
            return

        # 找到第一个有 caption 的消息用于点赞
        message_to_react = None
        for msg in messages:
            if msg.caption:
                message_to_react = msg
                break

        # 如果没有找到有 caption 的消息，使用第一条消息
        if not message_to_react:
            message_to_react = messages[0]

        # 先尝试点赞来验证消息是否还存在
        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_to_react.message_id,
                reaction=[{"type": "emoji", "emoji": "👍"}]
            )
            logger.info(f"✅ [Bot {self.bot_id}] 媒体组 {media_group_id} 存在，准备转发（点赞消息ID: {message_to_react.message_id}）")
        except Exception as e:
            # 点赞失败，说明消息已被删除
            logger.info(f"⚠️ [Bot {self.bot_id}] 媒体组 {media_group_id} 已被删除（点赞失败: {e}），取消转发")
            # 清理缓存
            for msg in messages:
                cache_key = (chat_id, msg.message_id)
                if cache_key in self.message_cache:
                    del self.message_cache[cache_key]
            del self.media_groups[media_group_id]
            if media_group_id in self.media_group_tasks:
                del self.media_group_tasks[media_group_id]
            return

        # 从缓存中获取最新版本的消息（可能已被编辑）
        updated_messages = []
        for msg in messages:
            cache_key = (chat_id, msg.message_id)
            if cache_key in self.message_cache:
                # 使用缓存中的最新版本
                updated_messages.append(self.message_cache[cache_key])
            else:
                # 如果不在缓存中，说明可能被删除了
                logger.warning(f"[Bot {self.bot_id}] 媒体组中的消息 {msg.message_id} 不在缓存中")

        if not updated_messages:
            logger.info(f"[Bot {self.bot_id}] 媒体组 {media_group_id} 中所有消息都被删除，取消转发")
            # 清理缓存
            del self.media_groups[media_group_id]
            if media_group_id in self.media_group_tasks:
                del self.media_group_tasks[media_group_id]
            return

        logger.info(
            f"[Bot {self.bot_id}] Processing media group {media_group_id} with {len(updated_messages)} items"
        )
        # 打印每条消息的类型用于调试
        for i, msg in enumerate(updated_messages):
            msg_type = "unknown"
            if msg.photo:
                msg_type = "photo"
            elif msg.video:
                msg_type = "video"
            elif msg.document:
                msg_type = "document"
            elif msg.audio:
                msg_type = "audio"
            elif msg.text:
                msg_type = "text"
            logger.info(f"  Message {i}: type={msg_type}, caption={msg.caption or 'None'}")

        # 转发消息（使用最新版本）
        logger.info(f"[Bot {self.bot_id}] 开始转发媒体组 {media_group_id}")
        await self.forward_messages_to_users(updated_messages, chat_id, chat_title)

        # 清理缓存
        for msg in messages:
            cache_key = (chat_id, msg.message_id)
            if cache_key in self.message_cache:
                del self.message_cache[cache_key]

        del self.media_groups[media_group_id]
        if media_group_id in self.media_group_tasks:
            del self.media_group_tasks[media_group_id]

    async def forward_messages_to_users(
        self, messages: list[types.Message], chat_id: int, chat_title: str
    ):
        """转发单条或多条消息到配置的用户"""
        if not messages:
            return

        first_message = messages[0]
        if not first_message.from_user:
            return

        async with AsyncSessionLocal() as session:
            # 1. 查找或创建群组（必须属于当前 bot）
            group_res = await session.execute(
                select(RelayGroup)
                .options(
                    selectinload(RelayGroup.user_relays).selectinload(GroupUserRelay.target_user)
                )
                .where(RelayGroup.group_id == chat_id, RelayGroup.bot_id == self.bot_id)
            )
            group = group_res.scalars().first()

            if not group:
                # 创建新群组
                group = RelayGroup(
                    group_id=chat_id,
                    title=chat_title,
                    is_active=True,
                    bot_id=self.bot_id
                )
                session.add(group)
                try:
                    await session.commit()
                    # 重新查询
                    group_res = await session.execute(
                        select(RelayGroup)
                        .options(
                            selectinload(RelayGroup.user_relays).selectinload(GroupUserRelay.target_user)
                        )
                        .where(RelayGroup.group_id == chat_id, RelayGroup.bot_id == self.bot_id)
                    )
                    group = group_res.scalars().first()
                except IntegrityError:
                    await session.rollback()
                    group_res = await session.execute(
                        select(RelayGroup)
                        .options(
                            selectinload(RelayGroup.user_relays).selectinload(GroupUserRelay.target_user)
                        )
                        .where(RelayGroup.group_id == chat_id, RelayGroup.bot_id == self.bot_id)
                    )
                    group = group_res.scalars().first()
                    if not group:
                        logger.error(f"Failed to find group {chat_id} after IntegrityError")
                        return

            if not group.is_active:
                return

            # 2. 获取该群组配置的目标用户（通过关联表）
            active_relays = [relay for relay in group.user_relays if relay.target_user.is_active]

            if not active_relays:
                logger.info(f"No active target users configured for group {chat_title}")
                return

            # 3. 转发消息
            is_media_group = len(messages) > 1

            logger.info(f"[Bot {self.bot_id}] Forwarding to {len(active_relays)} users, is_media_group={is_media_group}")

            # 追踪是否实际发送了消息
            actually_sent = False

            for relay in active_relays:
                # Increment Index
                relay.current_index += 1
                idx = relay.current_index

                # Format Message Suffix - 使用该群组-用户关系中的自定义标记
                if relay.tag:
                    suffix = f"{relay.tag}{idx}"
                else:
                    # 如果没有设置标记，只显示序号
                    suffix = f"#{idx}"

                target_user_id = relay.target_user.user_id
                sent_to_user = False  # 追踪是否给这个用户发送了消息

                try:
                    if is_media_group:
                        # 媒体组：发送多个媒体
                        media_list = []

                        # 找到第一个有实际内容的 caption
                        first_caption_index = -1
                        for i, msg in enumerate(messages):
                            if msg.caption:
                                first_caption_index = i
                                break

                        for i, msg in enumerate(messages):
                            # 保留原始caption，只在第一个有内容的caption后面添加标记
                            original_caption = msg.caption

                            if i == first_caption_index and original_caption:
                                # 第一个有内容的caption：添加标记和序号
                                caption = f"{original_caption}     {suffix}"
                            elif original_caption:
                                # 其他有内容的caption：保留原始
                                caption = original_caption
                            else:
                                # caption为None：跳过（设为None）
                                caption = None

                            # 只处理支持的媒体类型
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
                            else:
                                # 跳过不支持的消息类型
                                logger.warning(f"[Bot {self.bot_id}] 跳过不支持的媒体类型 for user {target_user_id}")

                        if media_list:
                            await self.bot.send_media_group(target_user_id, media_list)
                            logger.info(f"✅ Sent media group with {len(media_list)} items to {target_user_id} ({suffix})")
                            sent_to_user = True
                        else:
                            logger.warning(f"[Bot {self.bot_id}] 媒体组中所有消息都是不支持的类型，跳过用户 {target_user_id}")
                    else:
                        # 单条消息
                        message = messages[0]
                        text_content = message.text or message.caption or ""
                        final_text = f"{text_content}     {suffix}" if text_content else suffix

                        if message.text:
                            await self.bot.send_message(target_user_id, final_text)
                            sent_to_user = True
                        elif message.photo:
                            await self.bot.send_photo(
                                target_user_id,
                                message.photo[-1].file_id,
                                caption=final_text,
                            )
                            sent_to_user = True
                        elif message.video:
                            await self.bot.send_video(
                                target_user_id, message.video.file_id, caption=final_text
                            )
                            sent_to_user = True
                        elif message.document:
                            await self.bot.send_document(
                                target_user_id, message.document.file_id, caption=final_text
                            )
                            sent_to_user = True
                        elif message.voice:
                            await self.bot.send_voice(
                                target_user_id, message.voice.file_id, caption=final_text
                            )
                            sent_to_user = True
                        elif message.audio:
                            await self.bot.send_audio(
                                target_user_id, message.audio.file_id, caption=final_text
                            )
                            sent_to_user = True
                        else:
                            # 跳过不支持的媒体类型，不发送任何消息
                            logger.warning(f"[Bot {self.bot_id}] 跳过不支持的媒体类型 for user {target_user_id}")

                    # 只有实际发送了消息才记录日志
                    if sent_to_user:
                        actually_sent = True
                        log = MessageLog(
                            user_tag=relay.tag or "",
                            recipient_id=target_user_id,
                            assigned_index=idx,
                            original_sender_name=first_message.from_user.full_name,
                        )
                        session.add(log)

                except Exception as e:
                    logger.error(f"Failed to send to {target_user_id}: {e}")

            # 消息已在 process_single_message/process_media_group 中点赞过了
            # 不需要重复点赞
            if actually_sent:
                logger.info(f"✅ [Bot {self.bot_id}] 成功转发消息到 {len(active_relays)} 个用户")
            else:
                logger.warning(f"⚠️ [Bot {self.bot_id}] 没有发送任何消息（可能都是不支持的媒体类型）")

            await session.commit()


class BotManager:
    """Bot 管理器：管理多个 Bot 实例"""

    def __init__(self):
        self.bot_handlers: Dict[int, BotHandler] = {}  # bot_id -> BotHandler
        self.polling_tasks: Dict[int, asyncio.Task] = {}  # bot_id -> polling task

    async def load_bots_from_db(self):
        """从数据库加载所有启用的 bot"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotInstance).where(BotInstance.is_active == True)
            )
            bot_instances = result.scalars().all()

            for bot_instance in bot_instances:
                await self.add_bot(
                    bot_id=bot_instance.id,
                    token=bot_instance.token,
                    name=bot_instance.name
                )

    async def add_bot(self, bot_id: int, token: str, name: str):
        """添加一个 bot 实例"""
        if bot_id in self.bot_handlers:
            logger.warning(f"Bot {bot_id} ({name}) already exists")
            return

        try:
            # 创建 Bot 和 Dispatcher
            bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML)
            )
            dp = Dispatcher()

            # 获取 bot 信息并更新数据库中的 username
            try:
                bot_info = await bot.get_me()
                bot_username = bot_info.username

                # 更新数据库中的 username
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(BotInstance).where(BotInstance.id == bot_id)
                    )
                    bot_instance = result.scalars().first()
                    if bot_instance:
                        bot_instance.username = bot_username
                        await session.commit()
                        logger.info(f"📝 Updated bot username: @{bot_username}")
            except Exception as e:
                logger.warning(f"Failed to get bot info: {e}")

            # 创建处理器
            handler = BotHandler(bot_id=bot_id, bot=bot, dp=dp)
            self.bot_handlers[bot_id] = handler

            # 启动 polling
            polling_task = asyncio.create_task(
                self._start_polling(bot_id, name, bot, dp)
            )
            self.polling_tasks[bot_id] = polling_task

            logger.info(f"✅ Bot {bot_id} ({name}) started successfully")

        except Exception as e:
            logger.error(f"❌ Failed to start bot {bot_id} ({name}): {e}")

    async def _start_polling(self, bot_id: int, name: str, bot: Bot, dp: Dispatcher):
        """启动单个 bot 的 polling"""
        try:
            logger.info(f"🚀 Starting polling for bot {bot_id} ({name})...")
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, handle_signals=False)
        except Exception as e:
            logger.error(f"💥 Polling error for bot {bot_id} ({name}): {e}")

    async def remove_bot(self, bot_id: int):
        """移除一个 bot 实例"""
        if bot_id not in self.bot_handlers:
            return

        # 停止 polling
        if bot_id in self.polling_tasks:
            self.polling_tasks[bot_id].cancel()
            try:
                await self.polling_tasks[bot_id]
            except asyncio.CancelledError:
                pass
            del self.polling_tasks[bot_id]

        # 关闭 bot session
        handler = self.bot_handlers[bot_id]
        await handler.bot.session.close()

        del self.bot_handlers[bot_id]
        logger.info(f"🗑️ Bot {bot_id} removed")

    async def stop_all(self):
        """停止所有 bot"""
        logger.info("🛑 Stopping all bots...")

        # 停止所有 polling tasks
        for bot_id, task in list(self.polling_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 关闭所有 bot sessions
        for bot_id, handler in list(self.bot_handlers.items()):
            await handler.bot.session.close()

        self.bot_handlers.clear()
        self.polling_tasks.clear()

        logger.info("✅ All bots stopped")


# 全局 Bot 管理器实例
bot_manager = BotManager()
