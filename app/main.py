import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc, update
from sqlalchemy.orm import selectinload

from app.bot_manager import bot_manager
from app.database import init_db, get_db, AsyncSessionLocal
from app.models import TargetUser, RelayGroup, MessageLog, BotInstance, GroupUserRelay

logger = logging.getLogger(__name__)

load_dotenv()

# 创建定时任务调度器
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def reset_user_indexes():
    """重置所有用户的消息序号"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(TargetUser).values(current_index=0)
        )
        await session.commit()
        logger.info("🔄 已重置所有用户的消息序号为 0")


async def init_bots_from_env():
    """从环境变量初始化 bot tokens"""
    async with AsyncSessionLocal() as session:
        # 支持多个 token：BOT_TOKEN, BOT_TOKEN_1, BOT_TOKEN_2, ...
        tokens = []

        # 读取主 token
        main_token = os.getenv("BOT_TOKEN")
        if main_token:
            tokens.append(("主机器人", main_token))

        # 读取编号的 tokens
        i = 1
        while True:
            token = os.getenv(f"BOT_TOKEN_{i}")
            if not token:
                break
            tokens.append((f"机器人 {i}", token))
            i += 1

        if not tokens:
            logger.warning("⚠️ 未找到 BOT_TOKEN，请在 .env 文件中配置")
            return

        # 将 tokens 导入数据库
        for name, token in tokens:
            # 检查是否已存在
            result = await session.execute(
                select(BotInstance).where(BotInstance.token == token)
            )
            existing = result.scalars().first()

            if not existing:
                bot_instance = BotInstance(
                    name=name,
                    token=token,
                    is_active=True
                )
                session.add(bot_instance)
                logger.info(f"➕ 添加新 Bot: {name}")

        await session.commit()


# Lifecycle to run bot polling
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据库
    await init_db()

    # 从环境变量导入 bot tokens
    await init_bots_from_env()

    # 加载并启动所有 bots
    await bot_manager.load_bots_from_db()

    # 配置定时任务：每天重置序号
    reset_time = os.getenv("RESET_TIME", "08:00")  # 默认早上8点
    hour, minute = reset_time.split(":")
    scheduler.add_job(
        reset_user_indexes,
        CronTrigger(hour=int(hour), minute=int(minute)),
        id="reset_indexes",
        name="重置用户消息序号"
    )
    scheduler.start()
    logger.info(f"⏰ 定时任务已启动：每天 {reset_time} (北京时间) 重置序号")

    yield

    # Graceful shutdown
    logger.info("🛑 Shutting down...")
    scheduler.shutdown()
    await bot_manager.stop_all()
    logger.info("👋 Shutdown complete")


app = FastAPI(lifespan=lifespan)

# 没有static文件夹自动创建
if not os.path.exists("app/static"):
    os.makedirs("app/static")

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db=Depends(get_db)):
    # List Relay Groups (with user_relays and bot_instance loaded)
    groups = (
        (
            await db.execute(
                select(RelayGroup)
                .options(
                    selectinload(RelayGroup.user_relays).selectinload(GroupUserRelay.target_user),
                    selectinload(RelayGroup.bot_instance)
                )
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "groups": groups}
    )


@app.post("/toggle_group/{group_id}")
async def toggle_group(group_id: int, db=Depends(get_db)):
    group = await db.get(RelayGroup, group_id)
    if group:
        group.is_active = not group.is_active
        await db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def manage_users(request: Request, db=Depends(get_db)):
    users = (await db.execute(select(TargetUser))).scalars().all()
    return templates.TemplateResponse(
        "users.html", {"request": request, "users": users}
    )


@app.post("/toggle_user/{user_id}")
async def toggle_user(user_id: int, db=Depends(get_db)):
    user = await db.get(TargetUser, user_id)
    if user:
        user.is_active = not user.is_active
        await db.commit()
    return RedirectResponse(url="/users", status_code=303)


@app.get("/stats", response_class=HTMLResponse)
async def stats_overview(request: Request, db=Depends(get_db)):
    # Get distinct tags with counts
    stmt = (
        select(MessageLog.user_tag, func.count(MessageLog.id).label("count"))
        .group_by(MessageLog.user_tag)
        .order_by(desc("count"))
    )
    results = (await db.execute(stmt)).all()
    return templates.TemplateResponse(
        "stats_overview.html", {"request": request, "stats": results}
    )


@app.get("/stats/{tag}", response_class=HTMLResponse)
async def stats_detail(request: Request, tag: str, db=Depends(get_db)):
    # Get logs for this tag
    logs = (
        (
            await db.execute(
                select(MessageLog)
                .where(MessageLog.user_tag == tag)
                .order_by(MessageLog.timestamp)
            )
        )
        .scalars()
        .all()
    )

    # Prepare data for charts
    # 1. Time Series (Count per Day or Hour)
    # 2. Time Distribution (Hour of day)

    # Simple Python pre-processing for template
    timestamps = [log.timestamp for log in logs]

    return templates.TemplateResponse(
        "stats_detail.html",
        {"request": request, "tag": tag, "logs": logs, "total": len(logs)},
    )


@app.get("/group/{group_id}/config", response_class=HTMLResponse)
async def group_config_page(request: Request, group_id: int, db=Depends(get_db)):
    # Get group with user relays loaded
    group_result = await db.execute(
        select(RelayGroup)
        .options(selectinload(RelayGroup.user_relays).selectinload(GroupUserRelay.target_user))
        .where(RelayGroup.group_id == group_id)
    )
    group = group_result.scalars().first()

    if not group:
        return RedirectResponse(url="/", status_code=303)

    # Get all users
    all_users = (await db.execute(select(TargetUser))).scalars().all()

    # 构建用户配置字典 {user_id: tag}
    user_configs = {relay.user_id: relay.tag for relay in group.user_relays}

    return templates.TemplateResponse(
        "group_config.html",
        {
            "request": request,
            "group": group,
            "all_users": all_users,
            "user_configs": user_configs,
        },
    )


@app.post("/group/{group_id}/config")
async def group_config_save(
    request: Request, group_id: int, db=Depends(get_db)
):
    """保存群组的用户配置（包括标记）"""
    # 获取表单数据
    form_data = await request.form()

    # Get group
    group = await db.get(RelayGroup, group_id)
    if not group:
        return RedirectResponse(url="/", status_code=303)

    # 删除现有的所有关联
    await db.execute(
        select(GroupUserRelay).where(GroupUserRelay.group_id == group_id)
    )
    existing_relays = (await db.execute(
        select(GroupUserRelay).where(GroupUserRelay.group_id == group_id)
    )).scalars().all()

    for relay in existing_relays:
        await db.delete(relay)

    # 创建新的关联
    # 表单数据格式: user_{user_id}=on, tag_{user_id}=标记值
    for key in form_data.keys():
        if key.startswith("user_"):
            user_id = int(key.replace("user_", ""))
            tag_key = f"tag_{user_id}"
            tag = form_data.get(tag_key, "").strip() or None

            # 创建新的关联
            new_relay = GroupUserRelay(
                group_id=group_id,
                user_id=user_id,
                tag=tag,
                current_index=0
            )
            db.add(new_relay)

    await db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/bots", response_class=HTMLResponse)
async def manage_bots(request: Request, db=Depends(get_db)):
    """Bot 管理页面"""
    bots = (
        (await db.execute(
            select(BotInstance).options(selectinload(BotInstance.relay_groups))
        ))
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "bots.html", {"request": request, "bots": bots}
    )


@app.post("/toggle_bot/{bot_id}")
async def toggle_bot(bot_id: int, db=Depends(get_db)):
    """启用/禁用 bot"""
    bot_instance = await db.get(BotInstance, bot_id)
    if bot_instance:
        bot_instance.is_active = not bot_instance.is_active
        await db.commit()

        # 如果是启用，则添加到 bot_manager
        if bot_instance.is_active:
            await bot_manager.add_bot(
                bot_id=bot_instance.id,
                token=bot_instance.token,
                name=bot_instance.name
            )
        else:
            # 如果是禁用，则从 bot_manager 移除
            await bot_manager.remove_bot(bot_id)

    return RedirectResponse(url="/bots", status_code=303)
