"""三张表：用户、牌组、历史记录。

Mapped[...] 是 SQLAlchemy 2.0 的写法，表示这一列在 Python 里的类型。
启动时按这些类建表，不需要单独写 CREATE TABLE。
"""

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    salt: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(64))
    nickname: Mapped[str] = mapped_column(String(16))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)


class Deck(Base):
    __tablename__ = "decks"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    cards_json: Mapped[str] = mapped_column(Text)


class MatchRecord(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    opponent_name: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    result: Mapped[str] = mapped_column(String(8))
    my_score: Mapped[int]
    opp_score: Mapped[int]
    reason: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[str] = mapped_column(String(19))
