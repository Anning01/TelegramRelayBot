import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload

from app.bot import dp, bot
from app.database import init_db, get_db
from app.models import TargetUser, RelayGroup, MessageLog

logger = logging.getLogger(__name__)

load_dotenv()


# Lifecycle to run bot polling
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    polling_task = None

    if bot:
        logger.info("🚀 Starting Telegram Bot Polling...")
        await bot.delete_webhook(drop_pending_updates=False)

        # Start polling in background
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))

    yield

    # Graceful shutdown
    logger.info("🛑 Shutting down...")

    # Stop polling and close bot
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            logger.info("✅ Polling task cancelled")

    if bot:
        await bot.session.close()
        logger.info("✅ Bot session closed")

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
    # List Relay Groups (with target_users loaded)
    groups = (
        (
            await db.execute(
                select(RelayGroup).options(selectinload(RelayGroup.target_users))
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
        select(MessageLog.sender_tag, func.count(MessageLog.id).label("count"))
        .group_by(MessageLog.sender_tag)
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
                .where(MessageLog.sender_tag == tag)
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
    # Get group with target users loaded
    group_result = await db.execute(
        select(RelayGroup)
        .options(selectinload(RelayGroup.target_users))
        .where(RelayGroup.group_id == group_id)
    )
    group = group_result.scalars().first()

    if not group:
        return RedirectResponse(url="/", status_code=303)

    # Get all users
    all_users = (await db.execute(select(TargetUser))).scalars().all()

    # Get IDs of users already assigned to this group
    selected_user_ids = {user.user_id for user in group.target_users}

    return templates.TemplateResponse(
        "group_config.html",
        {
            "request": request,
            "group": group,
            "all_users": all_users,
            "selected_user_ids": selected_user_ids,
        },
    )


@app.post("/group/{group_id}/config")
async def group_config_save(
    group_id: int, user_ids: List[int] = Form([]), db=Depends(get_db)
):
    # Get group with target users loaded
    group_result = await db.execute(
        select(RelayGroup)
        .options(selectinload(RelayGroup.target_users))
        .where(RelayGroup.group_id == group_id)
    )
    group = group_result.scalars().first()

    if not group:
        return RedirectResponse(url="/", status_code=303)

    # Get selected users
    selected_users = (
        (await db.execute(select(TargetUser).where(TargetUser.user_id.in_(user_ids))))
        .scalars()
        .all()
        if user_ids
        else []
    )

    # Update the relationship
    group.target_users = selected_users
    await db.commit()

    return RedirectResponse(url="/", status_code=303)
