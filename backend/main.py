"""网络层。

HTTP 负责注册、资料、牌组和历史。
WebSocket 负责对局动作。动作会交给引擎，引擎说不合法就不执行。
进行中的牌局放在内存里；打完才写入 SQLite。

两套玩法各有一个引擎：engine.py 管抽牌比点，doudizhu.py 管斗地主。
两边各用各的房间和座位，互不影响。

斗地主的机器人不是一口气走完的：每走一步要停一下，网页上才看得清谁出了什么牌。
联网的时候由服务器自己按节拍推消息，免登录的时候由网页一步一步催 /advance。
"""

import asyncio
import json
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import doudizhu as ddz
from .auth import hash_password, new_salt, new_token, verify_password
from .db import Base, SessionLocal, engine
from .engine import (
    Game,
    bot_act,
    bot_deck,
    card_info,
    all_cards,
    default_deck,
    new_game,
    other,
    rules_text,
    draw,
    play,
    surrender,
    view_for,
)
from .models import Deck, MatchRecord, User
from .schemas import AuthIn, DdzBidIn, DdzPlayIn, DeckIn, LoginOut, PlayIn, ProfileIn, UserOut

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# 两位机器人看守的名字，沿用抽牌比点里的「灰烬看守」。
DDZ_BOT_NAMES = ["灰烬看守", "余烬看守"]

# 机器人"思考"多久。出牌比叫分慢一点，网页上才有出牌的节奏感。
# 自检里用 POKERHUB_BOT_SPEED 把它调快，不然跑一局要等半分钟。
BOT_SPEED = float(os.environ.get("POKERHUB_BOT_SPEED", "1"))
DDZ_BID_PAUSE = 0.7 * BOT_SPEED
DDZ_PLAY_PAUSE = 1.1 * BOT_SPEED

# 免登录的试玩局堆在内存里。给个上限，免得一直开新局把内存吃满。
MAX_GUEST_GAMES = 200

# 历史记录里 mode 字段显示成什么。
MODE_LABELS = {
    "solo": "单机",
    "match": "匹配",
    "ddz_solo": "斗地主 · 单机",
    "ddz_match": "斗地主 · 匹配",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="PokerHub 初级 Demo", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def bad_input(_request, exc: RequestValidationError):
    message = "参数不合法"
    errors = exc.errors()
    if errors:
        message = str(errors[0].get("msg", message))
        message = message.removeprefix("Value error, ")
        if message == "Field required":
            message = "缺少必填项"
    return JSONResponse(status_code=400, content={"detail": message})


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    token = authorization[7:]
    user = db.scalar(select(User).where(User.token == token))
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_counts(db: Session, user_id: int) -> tuple[int, int]:
    wins = db.scalar(
        select(func.count()).select_from(MatchRecord).where(
            MatchRecord.user_id == user_id,
            MatchRecord.result == "win",
        )
    )
    losses = db.scalar(
        select(func.count()).select_from(MatchRecord).where(
            MatchRecord.user_id == user_id,
            MatchRecord.result == "lose",
        )
    )
    return int(wins or 0), int(losses or 0)


def to_user_out(db: Session, user: User) -> UserOut:
    wins, losses = record_counts(db, user.id)
    return UserOut(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        wins=wins,
        losses=losses,
    )


def load_deck(db: Session, user_id: int) -> list[str]:
    row = db.get(Deck, user_id)
    if row is None:
        cards = default_deck()
        db.add(Deck(user_id=user_id, cards_json=json.dumps(cards)))
        db.commit()
        return cards
    return json.loads(row.cards_json)


def save_match(db: Session, game: Game) -> None:
    """对局结束时给每位人类玩家写一条历史。电脑没有用户 id。"""
    if game.saved or game.status != "finished":
        return
    game.saved = True
    for pid in game.order:
        if not pid.isdigit():
            continue
        me = game.players[pid]
        rival = game.players[other(game, pid)]
        result = "win" if game.winner == pid else "lose"
        db.add(
            MatchRecord(
                user_id=int(pid),
                opponent_name=rival.name,
                mode=game.mode,
                result=result,
                my_score=me.score,
                opp_score=rival.score,
                reason=game.reason[:80],
                created_at=now_text(),
            )
        )
    db.commit()


def save_ddz_match(db: Session, game: ddz.Game) -> None:
    """斗地主打完，给每位真人写一条历史。

    地主一个人一方，两个农民一方，所以两个人的"对手"写法不一样。
    赢多少、输多少用 my_score 和 opp_score 两个正数表示，网页相减就是净胜分。
    """
    if game.saved or game.phase != "finished":
        return
    game.saved = True
    landlord_name = game.players[game.landlord].name if game.landlord else ""
    side = "地主" if game.winner_side == "landlord" else "农民"
    reason = f"{side}获胜 · 底分 {game.base} · 倍数 {ddz.multiplier(game)}"
    if game.spring:
        reason += " · 春天"
    for pid in game.order:
        if not pid.isdigit():
            continue
        score = ddz.score_for(game, pid)
        rival = "两位农民" if pid == game.landlord else f"地主 {landlord_name}"
        db.add(
            MatchRecord(
                user_id=int(pid),
                opponent_name=rival[:32],
                mode="ddz_solo" if game.mode == "solo" else "ddz_match",
                result="win" if ddz.you_win(game, pid) else "lose",
                my_score=max(score, 0),
                opp_score=max(-score, 0),
                reason=reason[:80],
                created_at=now_text(),
            )
        )
    db.commit()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/rules")
def rules():
    return rules_text()


@app.get("/api/rules/ddz")
def ddz_rules():
    """斗地主的规则说明。和抽牌比点分开，网页按玩法切换。"""
    return ddz.rules_text()


@app.get("/api/cards")
def cards():
    return [card_info(card) for card in all_cards()]


@app.post("/api/register", response_model=LoginOut)
def register(body: AuthIn, db: Session = Depends(get_db)):
    salt = new_salt()
    user = User(
        username=body.username,
        salt=salt,
        password_hash=hash_password(body.password, salt),
        nickname=body.username,
        token=new_token(),
    )
    db.add(user)
    try:
        db.flush()
        db.add(Deck(user_id=user.id, cards_json=json.dumps(default_deck())))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="用户名已存在")
    db.refresh(user)
    return LoginOut(token=user.token, user=to_user_out(db, user))


@app.post("/api/login", response_model=LoginOut)
def login(body: AuthIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.salt, user.password_hash):
        raise HTTPException(status_code=400, detail="用户名或密码不正确")
    user.token = new_token()
    db.commit()
    return LoginOut(token=user.token, user=to_user_out(db, user))


@app.post("/api/logout")
def logout(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.token = new_token()
    db.commit()
    return {"ok": True}


@app.get("/api/me", response_model=UserOut)
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return to_user_out(db, user)


@app.patch("/api/me", response_model=UserOut)
def update_me(
    body: ProfileIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    nickname = body.nickname.strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="昵称不能为空")
    user.nickname = nickname
    db.commit()
    seat = hub.seat.get(user.id)
    if seat and seat in hub.games:
        hub.games[seat].players[str(user.id)].name = nickname
    ddz_seat = hub.ddz_seat.get(user.id)
    if ddz_seat and ddz_seat in hub.ddz_games:
        hub.ddz_games[ddz_seat].players[str(user.id)].name = nickname
    return to_user_out(db, user)


@app.get("/api/me/deck")
def my_deck(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"cards": load_deck(db, user.id)}


@app.put("/api/me/deck")
def update_deck(
    body: DeckIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row = db.get(Deck, user.id)
    payload = json.dumps(body.cards)
    if row is None:
        db.add(Deck(user_id=user.id, cards_json=payload))
    else:
        row.cards_json = payload
    db.commit()
    return {"cards": body.cards}


@app.get("/api/me/history")
def history(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(MatchRecord)
        .where(MatchRecord.user_id == user.id)
        .order_by(MatchRecord.id.desc())
        .limit(20)
    ).all()
    return [
        {
            "id": row.id,
            "opponent_name": row.opponent_name,
            "mode": MODE_LABELS.get(row.mode, row.mode),
            "game": "ddz" if row.mode.startswith("ddz") else "poker",
            "result": "胜" if row.result == "win" else "负",
            "my_score": row.my_score,
            "opp_score": row.opp_score,
            "reason": row.reason,
            "created_at": row.created_at,
        }
        for row in rows
    ]


class Hub:
    """内存里的在线状态。初级 Demo 不引入 Redis。

    抽牌比点是两个人的房间，斗地主是三个人的房间，所以各存一套，
    免得同一个用户在两种玩法里的座位互相覆盖。
    """

    def __init__(self) -> None:
        self.sockets: dict[int, WebSocket] = {}
        self.games: dict[str, Game] = {}
        self.seat: dict[int, str] = {}
        self.waiting_user: int | None = None
        self.ddz_games: dict[str, ddz.Game] = {}
        self.ddz_seat: dict[int, str] = {}
        self.ddz_waiting: list[int] = []
        # 打完的匹配局先留一口气，等大家点「再来一局」。key 是局号，value 是点头的人。
        self.rematch: dict[str, set[int]] = {}


hub = Hub()

# 正在跑节拍器的斗地主局号。同一局只跑一个，免得机器人被推两步。
_ddz_pumps: set[str] = set()


def load_user(user_id: int) -> User | None:
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def trim_games(store: dict) -> None:
    """免登录的局没人管，超过上限就从最早的那一局开始丢。"""
    while len(store) > MAX_GUEST_GAMES:
        store.pop(next(iter(store)))


def ddz_pause(game: ddz.Game) -> float:
    """机器人这一步该停多久。叫分快一点，出牌慢一点，牌桌上才有节奏。"""
    return DDZ_BID_PAUSE if game.phase == "bidding" else DDZ_PLAY_PAUSE


def drop_if_abandoned(store: dict, seats: dict, game) -> None:
    """如果没人再指着这一局，就把它从内存里删掉。"""
    if game is None:
        return
    if any(seat == game.id for seat in seats.values()):
        return
    store.pop(game.id, None)
    hub.rematch.pop(game.id, None)


def new_guest_game() -> Game:
    """免登录人机。玩家 id 以 guest- 开头，因此不会写入用户表。"""
    player_id = "guest-" + new_token()[:8]
    game = new_game(
        game_id=new_token()[:8],
        mode="solo",
        a_id=player_id,
        a_name="游客",
        a_deck=default_deck(),
        b_id="bot",
        b_name="电脑",
        b_deck=bot_deck(),
        b_is_bot=True,
    )
    hub.games[game.id] = game
    trim_games(hub.games)
    return game


def guest_game(play_id: str) -> Game:
    game = hub.games.get(play_id)
    if game is None or game.mode != "solo" or not game.order[0].startswith("guest-"):
        raise HTTPException(status_code=404, detail="这局不存在或已经结束")
    return game


def guest_view(game: Game) -> dict:
    return {"play_id": game.id, **view_for(game, game.order[0])}


def guest_move(play_id: str, kind: str, card: str | None = None) -> dict:
    game = guest_game(play_id)
    pid = game.order[0]
    if kind == "draw":
        problem = draw(game, pid)
    elif kind == "play":
        problem = play(game, pid, card or "")
    else:
        problem = surrender(game, pid)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if game.status == "playing":
        bot_act(game)
    payload = guest_view(game)
    if game.status == "finished":
        hub.games.pop(game.id, None)
    return payload


@app.post("/api/test/solo")
def test_solo_start():
    """人机试玩。不需要登录，不写历史。"""
    return guest_view(new_guest_game())


@app.get("/api/test/solo/{play_id}")
def test_solo_state(play_id: str):
    return guest_view(guest_game(play_id))


@app.post("/api/test/solo/{play_id}/draw")
def test_solo_draw(play_id: str):
    return guest_move(play_id, "draw")


@app.post("/api/test/solo/{play_id}/play")
def test_solo_play(play_id: str, body: PlayIn):
    return guest_move(play_id, "play", body.card)


@app.post("/api/test/solo/{play_id}/surrender")
def test_solo_surrender(play_id: str):
    return guest_move(play_id, "surrender")


# ---------------------------------------------------------------------------
# 斗地主：免登录人机接口
# ---------------------------------------------------------------------------


def new_guest_ddz_game() -> ddz.Game:
    """免登录斗地主。玩家 id 同样以 guest- 开头，所以不写用户表。

    座位打乱，免得每次都是玩家先叫分。
    """
    player_id = "guest-" + new_token()[:8]
    seats: list[tuple[str, str, bool]] = [(player_id, "游客", False)]
    seats += [(f"bot{index}", name, True) for index, name in enumerate(DDZ_BOT_NAMES, start=1)]
    random.shuffle(seats)
    game = ddz.new_game(game_id=new_token()[:8], mode="solo", seats=seats)
    hub.ddz_games[game.id] = game
    trim_games(hub.ddz_games)
    return game


def guest_ddz_game(play_id: str) -> ddz.Game:
    game = hub.ddz_games.get(play_id)
    if game is None or game.mode != "solo" or not any(pid.startswith("guest-") for pid in game.order):
        raise HTTPException(status_code=404, detail="这局不存在或已经结束")
    return game


def guest_ddz_human(game: ddz.Game) -> str:
    return next(pid for pid in game.order if pid.startswith("guest-"))


def guest_ddz_view(game: ddz.Game) -> dict:
    # bot_pause 告诉网页：等机器人出牌时该停多久。服务器说了算，网页照着等。
    return {"play_id": game.id, "bot_pause": ddz_pause(game), **ddz.view_for(game, guest_ddz_human(game))}


def guest_ddz_move(play_id: str, kind: str, payload=None) -> dict:
    """网页发来一步。

    这里只走人的那一步，机器人交给网页停一下再调 /advance，
    否则玩家一出牌，两个机器人立刻跟着出，牌桌上什么都看不清。
    """
    game = guest_ddz_game(play_id)
    pid = guest_ddz_human(game)
    if kind == "bid":
        problem = ddz.bid(game, pid, payload)
    elif kind == "play":
        problem = ddz.play(game, pid, payload)
    elif kind == "pass":
        problem = ddz.pass_turn(game, pid)
    else:
        problem = ddz.surrender(game, pid)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    state = guest_ddz_view(game)
    if game.phase == "finished":
        hub.ddz_games.pop(game.id, None)
    return state


@app.post("/api/test/ddz")
def test_ddz_start():
    """斗地主试玩。不需要登录，不写历史。"""
    return guest_ddz_view(new_guest_ddz_game())


@app.get("/api/test/ddz/{play_id}")
def test_ddz_state(play_id: str):
    return guest_ddz_view(guest_ddz_game(play_id))


@app.post("/api/test/ddz/{play_id}/advance")
def test_ddz_advance(play_id: str):
    """让机器人走一步。网页每停一下催一次，出牌顺序才看得清。

    现在不是机器人动的时候，就把当前局面原样还回去，不改动任何东西。
    """
    game = guest_ddz_game(play_id)
    if not ddz.is_bot_turn(game):
        return guest_ddz_view(game)
    ddz.bot_step(game)
    state = guest_ddz_view(game)
    if game.phase == "finished":
        hub.ddz_games.pop(game.id, None)
    return state


@app.post("/api/test/ddz/{play_id}/bid")
def test_ddz_bid(play_id: str, body: DdzBidIn):
    return guest_ddz_move(play_id, "bid", body.score)


@app.post("/api/test/ddz/{play_id}/play")
def test_ddz_play(play_id: str, body: DdzPlayIn):
    return guest_ddz_move(play_id, "play", body.cards)


@app.post("/api/test/ddz/{play_id}/pass")
def test_ddz_pass(play_id: str):
    return guest_ddz_move(play_id, "pass")


@app.post("/api/test/ddz/{play_id}/surrender")
def test_ddz_surrender(play_id: str):
    return guest_ddz_move(play_id, "surrender")


@app.post("/api/test/ddz/{play_id}/hint")
def test_ddz_hint(play_id: str):
    """提示：服务器算出几种能打的牌，网页拿去点亮手牌。这一步不改局面。"""
    game = guest_ddz_game(play_id)
    pid = guest_ddz_human(game)
    if game.phase != "playing" or game.turn != pid:
        raise HTTPException(status_code=400, detail="现在不是你出牌")
    return {"options": ddz.hint_options(game.players[pid].hand, game.top)}


async def push(user_id: int, payload: dict) -> None:
    socket = hub.sockets.get(user_id)
    if socket is None:
        return
    try:
        await socket.send_json(payload)
    except Exception:
        return


async def push_game(game: Game) -> None:
    for pid in game.order:
        if not pid.isdigit():
            continue
        await push(int(pid), {"type": "state", **view_for(game, pid)})


def user_by_token(token: str) -> User | None:
    db = SessionLocal()
    try:
        return db.scalar(select(User).where(User.token == token))
    finally:
        db.close()


def remember_if_finished(game: Game) -> None:
    db = SessionLocal()
    try:
        save_match(db, game)
    finally:
        db.close()
    if game.status != "finished":
        return
    if game.mode == "solo":
        # 人机局只有自己一个人，打完就收掉。
        for pid in game.order:
            if pid.isdigit():
                hub.seat.pop(int(pid), None)
        hub.games.pop(game.id, None)
        return
    # 匹配局先留一口气，两位玩家可能想再来一局。谁开新局，谁就把旧的摘掉。
    hub.rematch.setdefault(game.id, set())


def active_game(user_id: int) -> Game | None:
    """这位玩家手上还没打完的比点局。打完的不算。"""
    seat = hub.seat.get(user_id)
    game = hub.games.get(seat) if seat else None
    if game is None or game.status == "finished":
        return None
    return game


def release_poker_seat(user_id: int) -> None:
    """把已经打完的旧局从这位玩家身上摘掉，让他能开新局。"""
    seat = hub.seat.get(user_id)
    game = hub.games.get(seat) if seat else None
    if game is None or game.status != "finished":
        return
    hub.seat.pop(user_id, None)
    drop_if_abandoned(hub.games, hub.seat, game)


async def send_error(socket: WebSocket, message: str) -> None:
    await socket.send_json({"type": "error", "message": message})


def start_solo_game(user: User) -> Game:
    db = SessionLocal()
    try:
        cards = load_deck(db, user.id)
    finally:
        db.close()
    game = new_game(
        game_id=new_token()[:8],
        mode="solo",
        a_id=str(user.id),
        a_name=user.nickname,
        a_deck=cards,
        b_id="bot",
        b_name="电脑",
        b_deck=bot_deck(),
        b_is_bot=True,
    )
    hub.games[game.id] = game
    hub.seat[user.id] = game.id
    return game


def start_match_game(first: User, second: User) -> Game:
    db = SessionLocal()
    try:
        first_cards = load_deck(db, first.id)
        second_cards = load_deck(db, second.id)
    finally:
        db.close()
    game = new_game(
        game_id=new_token()[:8],
        mode="match",
        a_id=str(first.id),
        a_name=first.nickname,
        a_deck=first_cards,
        b_id=str(second.id),
        b_name=second.nickname,
        b_deck=second_cards,
    )
    hub.games[game.id] = game
    hub.seat[first.id] = game.id
    hub.seat[second.id] = game.id
    return game


# ---------------------------------------------------------------------------
# 斗地主：联网对局
# ---------------------------------------------------------------------------


async def push_ddz(game: ddz.Game) -> None:
    """把局面推给三位玩家。每人看到的是自己那一份，别家的手牌只有张数。"""
    for pid in game.order:
        if pid.isdigit():
            await push(int(pid), ddz_payload(game, pid))


def ddz_payload(game: ddz.Game, pid: str) -> dict:
    """一份完整的斗地主消息。bot_pause 是让网页知道该等多久。"""
    return {"type": "ddz_state", "bot_pause": ddz_pause(game), **ddz.view_for(game, pid)}


def remember_ddz_if_finished(game: ddz.Game) -> None:
    db = SessionLocal()
    try:
        save_ddz_match(db, game)
    finally:
        db.close()
    if game.phase != "finished":
        return
    if game.mode == "solo":
        # 人机局只有自己一个人，打完就收掉。
        for pid in game.order:
            if pid.isdigit():
                hub.ddz_seat.pop(int(pid), None)
        hub.ddz_games.pop(game.id, None)
        return
    # 匹配局留着，等大家点「再来一局」。
    hub.rematch.setdefault(game.id, set())


def active_ddz_game(user_id: int) -> ddz.Game | None:
    """这位玩家手上还没打完的斗地主。打完的不算。"""
    seat = hub.ddz_seat.get(user_id)
    game = hub.ddz_games.get(seat) if seat else None
    if game is None or game.phase == "finished":
        return None
    return game


def release_ddz_seat(user_id: int) -> None:
    """把已经打完的旧局从这位玩家身上摘掉。"""
    seat = hub.ddz_seat.get(user_id)
    game = hub.ddz_games.get(seat) if seat else None
    if game is None or game.phase != "finished":
        return
    hub.ddz_seat.pop(user_id, None)
    drop_if_abandoned(hub.ddz_games, hub.ddz_seat, game)


def abandon_solo_games(user_id: int) -> None:
    """人机局里只有自己一个真人。网页一关，这局就没人要了，直接收掉。

    不收的话，这个人下次点「开始游戏」会被"你还有一局没打完"挡住，再也开不了新局。
    """
    seat = hub.seat.get(user_id)
    game = hub.games.get(seat) if seat else None
    if game is not None and game.mode == "solo":
        hub.games.pop(game.id, None)
        hub.seat.pop(user_id, None)

    ddz_seat = hub.ddz_seat.get(user_id)
    ddz_game = hub.ddz_games.get(ddz_seat) if ddz_seat else None
    if ddz_game is not None and ddz_game.mode == "solo":
        hub.ddz_games.pop(ddz_game.id, None)
        hub.ddz_seat.pop(user_id, None)


def start_ddz_pump(game: ddz.Game) -> None:
    """让机器人一步一步走。同一局同时只跑一个节拍器。"""
    if game.id in _ddz_pumps:
        return
    _ddz_pumps.add(game.id)
    asyncio.create_task(_ddz_pump(game))


async def _ddz_pump(game: ddz.Game) -> None:
    """机器人走一步，停一下，再走一步。

    停的这一下是有意的：玩家出完牌，先看到自己的牌落到桌上，
    再看到下家"想"一会儿才出牌。一口气跑完的话，牌桌上什么都看不清。
    """
    try:
        while True:
            if game.phase == "finished" or not ddz.is_bot_turn(game):
                return
            await asyncio.sleep(ddz_pause(game))
            # 睡着的这段时间里可能有人认输，局面已经结束了。
            if game.phase == "finished" or not ddz.is_bot_turn(game):
                return
            ddz.bot_step(game)
            if game.phase == "finished":
                remember_ddz_if_finished(game)
            await push_ddz(game)
    finally:
        _ddz_pumps.discard(game.id)


def start_ddz_solo_game(user: User) -> ddz.Game:
    """人机斗地主：一位真人加两个机器人。座位打乱，免得总是玩家先叫分。"""
    seats: list[tuple[str, str, bool]] = [(str(user.id), user.nickname, False)]
    seats += [(f"bot{index}", name, True) for index, name in enumerate(DDZ_BOT_NAMES, start=1)]
    random.shuffle(seats)
    game = ddz.new_game(game_id=new_token()[:8], mode="solo", seats=seats)
    hub.ddz_games[game.id] = game
    hub.ddz_seat[user.id] = game.id
    return game


def start_ddz_match_game(user_ids: list[int]) -> ddz.Game | None:
    """三人匹配：凑满三个人就开一局，凑不齐就返回 None。"""
    db = SessionLocal()
    try:
        users = [db.get(User, user_id) for user_id in user_ids]
    finally:
        db.close()
    if any(user is None for user in users):
        return None
    game = ddz.new_game(
        game_id=new_token()[:8],
        mode="match",
        seats=[(str(user.id), user.nickname, False) for user in users],
    )
    hub.ddz_games[game.id] = game
    for user in users:
        hub.ddz_seat[user.id] = game.id
    return game


async def on_hello(socket: WebSocket, token: str) -> int | None:
    user = user_by_token(token)
    if user is None:
        await send_error(socket, "登录已失效，请重新登录")
        return None
    hub.sockets[user.id] = socket
    await socket.send_json({"type": "ready", "name": user.nickname})
    seat = hub.seat.get(user.id)
    if seat and seat in hub.games:
        game = hub.games[seat]
        await socket.send_json({"type": "state", **view_for(game, str(user.id))})
    elif hub.waiting_user == user.id:
        await socket.send_json({"type": "waiting", "message": "仍在等待另一位玩家…"})
    # 斗地主那边可能有没打完的局，刷新页面也要接回来。
    ddz_seat = hub.ddz_seat.get(user.id)
    if ddz_seat and ddz_seat in hub.ddz_games:
        await socket.send_json(ddz_payload(hub.ddz_games[ddz_seat], str(user.id)))
        # 接回来的时候机器人可能正等着动。
        start_ddz_pump(hub.ddz_games[ddz_seat])
    return user.id


async def on_start_solo(socket: WebSocket, user_id: int) -> None:
    if active_game(user_id) is not None:
        await send_error(socket, "请先结束当前对局（可以投降）")
        return
    release_poker_seat(user_id)
    if hub.waiting_user == user_id:
        hub.waiting_user = None
    user = None
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
    finally:
        db.close()
    if user is None:
        await send_error(socket, "找不到这个用户")
        return
    game = start_solo_game(user)
    await push_game(game)


async def on_start_match(socket: WebSocket, user_id: int) -> None:
    if active_game(user_id) is not None:
        await send_error(socket, "请先结束当前对局（可以投降）")
        return
    release_poker_seat(user_id)
    if hub.waiting_user == user_id:
        await send_error(socket, "已经在等待对手")
        return
    if hub.waiting_user is None or hub.waiting_user not in hub.sockets:
        hub.waiting_user = user_id
        await socket.send_json({"type": "waiting", "message": "正在等待另一位玩家进入房间…"})
        return

    other_id = hub.waiting_user
    hub.waiting_user = None
    db = SessionLocal()
    try:
        first = db.get(User, other_id)
        second = db.get(User, user_id)
    finally:
        db.close()
    if first is None or second is None:
        await send_error(socket, "匹配失败，请再试一次")
        return
    game = start_match_game(first, second)
    await push_game(game)


async def on_cancel_match(socket: WebSocket, user_id: int) -> None:
    if hub.waiting_user == user_id:
        hub.waiting_user = None
    await socket.send_json({"type": "cancelled", "message": "已取消匹配"})


async def on_action(socket: WebSocket, user_id: int, kind: str, card: str | None) -> None:
    seat = hub.seat.get(user_id)
    game = hub.games.get(seat) if seat else None
    if game is None or game.status != "playing":
        await send_error(socket, "当前没有进行中的对局")
        return

    pid = str(user_id)
    if kind == "draw":
        problem = draw(game, pid)
    elif kind == "play":
        problem = play(game, pid, card or "")
    else:
        problem = surrender(game, pid)
    if problem:
        await send_error(socket, problem)
        return

    # 单机时，玩家走完就让电脑立刻走。匹配对战没有电脑。
    if game.mode == "solo" and game.status == "playing":
        bot_act(game)
    remember_if_finished(game)
    await push_game(game)


async def on_rematch(socket: WebSocket, user_id: int) -> None:
    """再来一局。人机局自己说了算，匹配局要两个人都点头。"""
    seat = hub.seat.get(user_id)
    game = hub.games.get(seat) if seat else None
    if game is not None and game.status != "finished":
        await send_error(socket, "这一局还没打完")
        return

    # 人机局打完就收掉了，所以"找不到刚打完的局"也按人机局处理。
    if game is None or game.mode == "solo":
        release_poker_seat(user_id)
        user = load_user(user_id)
        if user is None:
            await send_error(socket, "找不到这个用户")
            return
        fresh = start_solo_game(user)
        await push_game(fresh)
        return

    players = [pid for pid in game.order if pid.isdigit()]
    votes = hub.rematch.setdefault(game.id, set())
    votes.add(user_id)
    if len(votes) < len(players):
        for pid in players:
            await push(
                int(pid),
                {
                    "type": "rematch_waiting",
                    "have": len(votes),
                    "need": len(players),
                    "message": f"有人想再来一局（{len(votes)}/{len(players)}），等对面点头",
                },
            )
        return

    # 都点头了，用原班人马开新局
    db = SessionLocal()
    try:
        users = [db.get(User, int(pid)) for pid in players]
    finally:
        db.close()
    if any(item is None for item in users):
        await send_error(socket, "对手已经离开，匹配不上了")
        return
    hub.rematch.pop(game.id, None)
    for pid in players:
        hub.seat.pop(int(pid), None)
    hub.games.pop(game.id, None)
    fresh = start_match_game(users[0], users[1])
    await push_game(fresh)


# ---------------------------------------------------------------------------
# 斗地主：WebSocket 指令
# ---------------------------------------------------------------------------


async def on_ddz_solo(socket: WebSocket, user_id: int) -> None:
    if active_ddz_game(user_id) is not None:
        await send_error(socket, "你还有一局斗地主没打完（可以认输）")
        return
    release_ddz_seat(user_id)
    user = load_user(user_id)
    if user is None:
        await send_error(socket, "找不到这个用户")
        return
    game = start_ddz_solo_game(user)
    # 先把牌桌推过去，机器人交给节拍器一步一步走。
    await push_ddz(game)
    start_ddz_pump(game)


async def on_ddz_match(socket: WebSocket, user_id: int) -> None:
    """三个人凑一桌。没凑满就排队等着。"""
    if active_ddz_game(user_id) is not None:
        await send_error(socket, "你还有一局斗地主没打完（可以认输）")
        return
    release_ddz_seat(user_id)
    if user_id in hub.ddz_waiting:
        await send_error(socket, "已经在等待其他玩家")
        return
    # 掉线的人不该继续占着位子。
    hub.ddz_waiting = [pid for pid in hub.ddz_waiting if pid in hub.sockets]
    hub.ddz_waiting.append(user_id)

    if len(hub.ddz_waiting) < ddz.SEATS:
        await socket.send_json(
            {
                "type": "waiting",
                "game": "ddz",
                "message": f"正在等待其他玩家（{len(hub.ddz_waiting)}/{ddz.SEATS}）…",
            }
        )
        return

    waiting = hub.ddz_waiting[: ddz.SEATS]
    hub.ddz_waiting = []
    game = start_ddz_match_game(waiting)
    if game is None:
        await send_error(socket, "匹配失败，请再试一次")
        return
    await push_ddz(game)


async def on_ddz_cancel_match(socket: WebSocket, user_id: int) -> None:
    if user_id in hub.ddz_waiting:
        hub.ddz_waiting.remove(user_id)
    await socket.send_json({"type": "cancelled", "game": "ddz", "message": "已取消匹配"})


async def on_ddz_action(socket: WebSocket, user_id: int, kind: str, message: dict) -> None:
    seat = hub.ddz_seat.get(user_id)
    game = hub.ddz_games.get(seat) if seat else None
    if game is None or game.phase == "finished":
        await send_error(socket, "当前没有进行中的斗地主")
        return

    pid = str(user_id)
    if kind == "bid":
        problem = ddz.bid(game, pid, int(message.get("score", 0)))
    elif kind == "play":
        problem = ddz.play(game, pid, list(message.get("cards") or []))
    elif kind == "pass":
        problem = ddz.pass_turn(game, pid)
    else:
        problem = ddz.surrender(game, pid)
    if problem:
        await send_error(socket, problem)
        return

    # 玩家这一步先推回去，让他看见自己出的牌落到桌上；
    # 机器人接着由节拍器一步一步走，不会一下子全出完。
    if game.phase == "finished":
        remember_ddz_if_finished(game)
    await push_ddz(game)
    start_ddz_pump(game)


async def on_ddz_rematch(socket: WebSocket, user_id: int) -> None:
    """斗地主再来一局。人机局直接开，匹配局要三个人都点头。"""
    seat = hub.ddz_seat.get(user_id)
    game = hub.ddz_games.get(seat) if seat else None
    if game is not None and game.phase != "finished":
        await send_error(socket, "这一局还没打完")
        return

    # 人机局打完就收掉了，所以"找不到刚打完的局"也按人机局处理。
    if game is None or game.mode == "solo":
        release_ddz_seat(user_id)
        user = load_user(user_id)
        if user is None:
            await send_error(socket, "找不到这个用户")
            return
        fresh = start_ddz_solo_game(user)
        await push_ddz(fresh)
        start_ddz_pump(fresh)
        return

    players = [pid for pid in game.order if pid.isdigit()]
    votes = hub.rematch.setdefault(game.id, set())
    votes.add(user_id)
    if len(votes) < len(players):
        for pid in players:
            await push(
                int(pid),
                {
                    "type": "rematch_waiting",
                    "game": "ddz",
                    "have": len(votes),
                    "need": len(players),
                    "message": f"有人想再来一局（{len(votes)}/{len(players)}），等其他人点头",
                },
            )
        return

    db = SessionLocal()
    try:
        users = [db.get(User, int(pid)) for pid in players]
    finally:
        db.close()
    if any(item is None for item in users):
        await send_error(socket, "有人已经离开，匹配不上了")
        return
    hub.rematch.pop(game.id, None)
    for pid in players:
        hub.ddz_seat.pop(int(pid), None)
    hub.ddz_games.pop(game.id, None)
    fresh = start_ddz_match_game([item.id for item in users])
    if fresh is None:
        await send_error(socket, "匹配失败，请再试一次")
        return
    await push_ddz(fresh)


async def on_ddz_hint(socket: WebSocket, user_id: int) -> None:
    seat = hub.ddz_seat.get(user_id)
    game = hub.ddz_games.get(seat) if seat else None
    if game is None or game.phase != "playing":
        await send_error(socket, "现在不能提示")
        return
    pid = str(user_id)
    if game.turn != pid:
        await send_error(socket, "还没轮到你")
        return
    await socket.send_json(
        {
            "type": "hint",
            "options": ddz.hint_options(game.players[pid].hand, game.top),
        }
    )


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket):
    await socket.accept()
    user_id: int | None = None
    try:
        while True:
            message = await socket.receive_json()
            kind = message.get("type")
            if user_id is None:
                if kind != "hello":
                    await send_error(socket, "请先登录")
                    continue
                user_id = await on_hello(socket, str(message.get("token", "")))
                continue
            if kind == "hello":
                user_id = await on_hello(socket, str(message.get("token", "")))
            elif kind == "start_solo":
                await on_start_solo(socket, user_id)
            elif kind == "start_match":
                await on_start_match(socket, user_id)
            elif kind == "cancel_match":
                await on_cancel_match(socket, user_id)
            elif kind in ("draw", "play", "surrender"):
                await on_action(socket, user_id, kind, message.get("card"))
            elif kind == "rematch":
                await on_rematch(socket, user_id)
            elif kind == "ddz_solo":
                await on_ddz_solo(socket, user_id)
            elif kind == "ddz_match":
                await on_ddz_match(socket, user_id)
            elif kind == "ddz_cancel_match":
                await on_ddz_cancel_match(socket, user_id)
            elif kind == "ddz_hint":
                await on_ddz_hint(socket, user_id)
            elif kind == "ddz_rematch":
                await on_ddz_rematch(socket, user_id)
            elif kind in ("ddz_bid", "ddz_play", "ddz_pass", "ddz_surrender"):
                await on_ddz_action(socket, user_id, kind.removeprefix("ddz_"), message)
            else:
                await send_error(socket, "不认识的指令")
    except WebSocketDisconnect:
        pass
    finally:
        # 只清掉自己的连接。刷新页面后，新连接会用 hello 把未结束的牌局接回来。
        if user_id is not None and hub.sockets.get(user_id) is socket:
            hub.sockets.pop(user_id, None)
            if hub.waiting_user == user_id:
                hub.waiting_user = None
            if user_id in hub.ddz_waiting:
                hub.ddz_waiting.remove(user_id)
            abandon_solo_games(user_id)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
