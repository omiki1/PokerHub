"""网络层。

HTTP 负责注册、资料、牌组和历史。
WebSocket 负责对局动作。动作会交给 engine.py，引擎说不合法就不执行。
进行中的牌局放在内存里；打完才写入 SQLite。
"""

import json
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
from .schemas import AuthIn, DeckIn, LoginOut, PlayIn, ProfileIn, UserOut

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


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


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/rules")
def rules():
    return rules_text()


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
            "mode": "单机" if row.mode == "solo" else "匹配",
            "result": "胜" if row.result == "win" else "负",
            "my_score": row.my_score,
            "opp_score": row.opp_score,
            "reason": row.reason,
            "created_at": row.created_at,
        }
        for row in rows
    ]


class Hub:
    """内存里的在线状态。初级 Demo 不引入 Redis。"""

    def __init__(self) -> None:
        self.sockets: dict[int, WebSocket] = {}
        self.games: dict[str, Game] = {}
        self.seat: dict[int, str] = {}
        self.waiting_user: int | None = None


hub = Hub()


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
    if game.status == "finished":
        for pid in game.order:
            if pid.isdigit():
                hub.seat.pop(int(pid), None)
        hub.games.pop(game.id, None)


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
    return user.id


async def on_start_solo(socket: WebSocket, user_id: int) -> None:
    if user_id in hub.seat:
        await send_error(socket, "请先结束当前对局（可以投降）")
        return
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
    if user_id in hub.seat:
        await send_error(socket, "请先结束当前对局（可以投降）")
        return
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


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
