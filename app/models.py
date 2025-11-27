from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    BigInteger,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

# 群组-用户关联表 (多对多)
group_user_relay = Table(
    "group_user_relay",
    Base.metadata,
    Column(
        "group_id", BigInteger, ForeignKey("relay_groups.group_id"), primary_key=True
    ),
    Column("user_id", BigInteger, ForeignKey("target_users.user_id"), primary_key=True),
)


class TargetUser(Base):
    __tablename__ = "target_users"

    user_id = Column(BigInteger, primary_key=True, index=True)  # Telegram User ID
    name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    current_index = Column(
        Integer, default=0
    )  # Monotonically increasing index for this user

    # 关联的群组
    relay_groups = relationship(
        "RelayGroup", secondary=group_user_relay, back_populates="target_users"
    )


class RelayGroup(Base):
    __tablename__ = "relay_groups"

    group_id = Column(BigInteger, primary_key=True, index=True)  # Telegram Group ID
    title = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)

    # 关联的目标用户
    target_users = relationship(
        "TargetUser", secondary=group_user_relay, back_populates="relay_groups"
    )


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    sender_tag = Column(String, index=True)  # The [TAG]
    recipient_id = Column(BigInteger, ForeignKey("target_users.user_id"))
    assigned_index = Column(Integer)
    original_sender_name = Column(String, nullable=True)
