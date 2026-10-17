"""连接 SQLite。

数据库文件默认放在 backend/pokerhub.db。
自检会改环境变量 POKERHUB_DB，避免把测试数据写进正式文件。
"""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = Path(os.environ.get("POKERHUB_DB", Path(__file__).resolve().parent / "pokerhub.db"))

# check_same_thread=False：FastAPI 会在不同线程里处理请求，SQLite 默认不允许这样。
engine = create_engine(
    "sqlite:///" + DB_PATH.as_posix(),
    connect_args={"check_same_thread": False},
)


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
