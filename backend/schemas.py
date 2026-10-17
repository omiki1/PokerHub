"""进出接口的数据形状。Pydantic 会在进函数之前检查类型和范围。"""

from pydantic import BaseModel, Field, field_validator

from .engine import check_deck


class AuthIn(BaseModel):
    username: str = Field(min_length=3, max_length=16)
    password: str = Field(min_length=4, max_length=32)

    @field_validator("username")
    @classmethod
    def username_chars(cls, value: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
        if any(ch not in allowed for ch in value):
            raise ValueError("用户名只能用字母、数字和下划线")
        return value


class ProfileIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=16)


class PlayIn(BaseModel):
    card: str


class DdzBidIn(BaseModel):
    """斗地主叫分。0 表示不叫，最高 3 分。"""

    score: int = Field(ge=0, le=3)


class DdzPlayIn(BaseModel):
    """斗地主出牌。一次可以出好几张，是不是合法牌型由引擎判断。"""

    cards: list[str] = Field(min_length=1, max_length=20)


class DeckIn(BaseModel):
    cards: list[str]

    @field_validator("cards")
    @classmethod
    def must_be_legal(cls, cards: list[str]) -> list[str]:
        problem = check_deck(cards)
        if problem:
            raise ValueError(problem)
        return cards


class UserOut(BaseModel):
    id: int
    username: str
    nickname: str
    wins: int
    losses: int


class LoginOut(BaseModel):
    token: str
    user: UserOut
