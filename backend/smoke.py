"""把网页接口和 WebSocket 走一遍。不打开浏览器。

在项目根目录执行：

    python -m backend.smoke
"""

import os
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette.testclient` is deprecated",
)

_db = Path(tempfile.gettempdir()) / "pokerhub-smoke.db"
if _db.exists():
    _db.unlink()
os.environ["POKERHUB_DB"] = str(_db)

from fastapi.testclient import TestClient

from .engine import OPENING_HAND
from .main import app


def expect(condition: bool, text: str) -> None:
    if not condition:
        raise SystemExit("失败: " + text)


def guest_round(client: TestClient) -> None:
    """免登录人机接口：不带令牌也能发牌、抓牌、拒绝非法牌、投降。"""
    opened = client.post("/api/test/solo")
    expect(opened.status_code == 200, "免登录开局失败: " + opened.text)
    state = opened.json()
    play_id = state["play_id"]
    expect(state["you"]["name"] == "游客", "免登录对局应该是游客")
    expect(len(state["you"]["hand"]) == OPENING_HAND, "试玩发牌张数不对")
    expect("hand" not in state["opponent"], "试玩不应泄露对手手牌")

    refused = client.post(f"/api/test/solo/{play_id}/play", json={"card": "NOPE"})
    expect(refused.status_code == 400, "试玩非法出牌应该被拒绝")

    drawn = client.post(f"/api/test/solo/{play_id}/draw")
    expect(drawn.status_code == 200, "试玩抓牌失败: " + drawn.text)
    expect(len(drawn.json()["you"]["hand"]) == OPENING_HAND + 1, "试玩抓牌后张数不对")

    card = drawn.json()["you"]["hand"][0]
    played = client.post(f"/api/test/solo/{play_id}/play", json={"card": card})
    expect(played.status_code == 200 and played.json()["last_trick"], "试玩电脑没有结算")

    ended = client.post(f"/api/test/solo/{play_id}/surrender")
    expect(ended.status_code == 200 and ended.json()["winner"] == "opponent", "试玩投降失败")
    gone = client.get(f"/api/test/solo/{play_id}")
    expect(gone.status_code == 404, "结束后的试玩局不应再能查询")


def register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/register",
        json={"username": username, "password": "pass1234"},
    )
    expect(response.status_code == 200, username + " 注册失败: " + response.text)
    return response.json()["token"]


def main() -> None:
    with TestClient(app) as client:
        expect(client.get("/api/health").json()["ok"] is True, "健康检查失败")
        guest_round(client)
        expect("统一发牌" in client.get("/api/rules").text, "规则接口缺少统一发牌")
        expect(len(client.get("/api/cards").json()) == 52, "应该有 52 张牌")
        page = client.get("/")
        expect(page.status_code == 200 and "开始游戏" in page.text, "首页没有加载出来")

        bad = client.post("/api/register", json={"username": "ab", "password": "123"})
        expect(bad.status_code == 400, "过短的用户名应该被拒绝")

        token = register(client, "alice")
        headers = {"Authorization": "Bearer " + token}
        me = client.get("/api/me", headers=headers).json()
        expect(me["username"] == "alice", "个人信息不是刚注册的用户")

        renamed = client.patch("/api/me", headers=headers, json={"nickname": "爱丽丝"})
        expect(renamed.json()["nickname"] == "爱丽丝", "昵称没有保存")

        short_deck = client.put(
            "/api/me/deck",
            headers=headers,
            json={"cards": ["SA", "SK"]},
        )
        expect(short_deck.status_code == 400, "不完整的牌组应该被拒绝")

        with client.websocket_connect("/ws") as socket:
            socket.send_json({"type": "hello", "token": token})
            ready = socket.receive_json()
            expect(ready["type"] == "ready", "WebSocket 没有登录成功")

            socket.send_json({"type": "start_solo"})
            state = socket.receive_json()
            expect(state["type"] == "state", "开始游戏没有返回局面")
            expect(len(state["you"]["hand"]) == OPENING_HAND, "开局手牌张数不对")
            expect(state["you"]["deck_left"] == 7, "开局牌组剩余不对")
            expect("hand" not in state["opponent"], "不应该把对手的手牌发下来")

            socket.send_json({"type": "play", "card": "NOPE"})
            refused = socket.receive_json()
            expect(refused["type"] == "error", "非法出牌没有被拒绝")

            socket.send_json({"type": "draw"})
            drawn = socket.receive_json()
            expect(drawn["type"] == "state", "抓牌没有返回局面")
            expect(len(drawn["you"]["hand"]) == OPENING_HAND + 1, "抓牌后手牌没有增加")

            socket.send_json({"type": "draw"})
            twice = socket.receive_json()
            expect(twice["type"] == "error", "同一回合抓两次应该被拒绝")

            card = drawn["you"]["hand"][0]
            socket.send_json({"type": "play", "card": card})
            after = socket.receive_json()
            expect(after["type"] == "state", "出牌没有返回局面")
            expect(after["last_trick"] is not None, "电脑没有跟牌并结算")

            socket.send_json({"type": "surrender"})
            ended = socket.receive_json()
            expect(ended["winner"] == "opponent", "投降后应该判负")

        history = client.get("/api/me/history", headers=headers).json()
        expect(len(history) == 1 and history[0]["result"] == "负", "历史没有记下投降")

        token_b = register(client, "bob")
        with client.websocket_connect("/ws") as left, client.websocket_connect("/ws") as right:
            left.send_json({"type": "hello", "token": token})
            expect(left.receive_json()["type"] == "ready", "爱丽丝重连失败")
            left.send_json({"type": "start_match"})
            expect(left.receive_json()["type"] == "waiting", "一个人不应该直接开局")

            right.send_json({"type": "hello", "token": token_b})
            expect(right.receive_json()["type"] == "ready", "鲍勃登录失败")
            right.send_json({"type": "start_match"})
            # 服务器先把局面推给先排队的人，所以先读左边，避免两边互相等待。
            left_state = left.receive_json()
            right_state = right.receive_json()
            expect(left_state["mode"] == "match" and right_state["mode"] == "match", "没有配成一局")
            expect("hand" not in left_state["opponent"], "匹配局泄露了对手手牌")
            expect(left_state["you"]["is_turn"] is True, "先进入房间的人应该先手")
            expect(right_state["you"]["is_turn"] is False, "后进入房间的人不该先手")

            left.send_json({"type": "surrender"})
            expect(left.receive_json()["winner"] == "opponent", "爱丽丝投降后应判负")
            expect(right.receive_json()["winner"] == "you", "鲍勃应该因此获胜")

        bob_headers = {"Authorization": "Bearer " + token_b}
        bob_history = client.get("/api/me/history", headers=bob_headers).json()
        expect(bob_history and bob_history[0]["result"] == "胜", "鲍勃的历史没有胜局")

        client.post("/api/logout", headers=headers)
        hidden = client.get("/api/me", headers=headers)
        expect(hidden.status_code == 401, "退出后旧令牌还能用")

    print("接口自检通过。")


if __name__ == "__main__":
    main()
