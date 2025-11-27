from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    BigInteger,
    ForeignKey,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class GroupUserRelay(Base):
    """群组-用户关联表，存储每个群组-用户对的配置"""
    __tablename__ = "group_user_relay"

    group_id = Column(BigInteger, ForeignKey("relay_groups.group_id"), primary_key=True)
    user_id = Column(BigInteger, ForeignKey("target_users.user_id"), primary_key=True)
    tag = Column(String, nullable=True)  # 该用户在此群组中的标记（如 "A", "客服1"）
    current_index = Column(Integer, default=0)  # 该用户在此群组中的消息序号

    # 关联到群组和用户
    relay_group = relationship("RelayGroup", back_populates="user_relays")
    target_user = relationship("TargetUser", back_populates="group_relays")


class BotInstance(Base):
    """Bot 实例表：存储多个 bot 的配置信息"""
    __tablename__ = "bot_instances"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)  # Bot 名称（方便识别）
    token = Column(Text, nullable=False, unique=True)  # Bot Token
    username = Column(String, nullable=True)  # Bot 用户名（如 @MyBot）
    is_active = Column(Boolean, default=True)  # 是否启用
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # 关联的群组
    relay_groups = relationship("RelayGroup", back_populates="bot_instance", cascade="all, delete-orphan")


class TargetUser(Base):
    __tablename__ = "target_users"

    user_id = Column(BigInteger, primary_key=True, index=True)  # Telegram User ID
    name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)

    # 关联的群组（通过 GroupUserRelay）
    group_relays = relationship("GroupUserRelay", back_populates="target_user", cascade="all, delete-orphan")


class RelayGroup(Base):
    __tablename__ = "relay_groups"

    group_id = Column(BigInteger, primary_key=True, index=True)  # Telegram Group ID
    title = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    bot_id = Column(Integer, ForeignKey("bot_instances.id"), nullable=False)  # 所属 Bot

    # 关联的 Bot
    bot_instance = relationship("BotInstance", back_populates="relay_groups")

    # 关联的目标用户（通过 GroupUserRelay）
    user_relays = relationship("GroupUserRelay", back_populates="relay_group", cascade="all, delete-orphan")


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    user_tag = Column(String, index=True)  # 接收用户的标记
    recipient_id = Column(BigInteger, ForeignKey("target_users.user_id"))
    assigned_index = Column(Integer)
    original_sender_name = Column(String, nullable=True)
