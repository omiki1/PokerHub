"""规则引擎。

这个文件只回答三件事：牌怎么发、这一步合不合法、谁赢了。
它不导入 FastAPI，也不读写数据库。
网页、WebSocket、单机自检都调用这里，所以规则只有一份。
"""

from dataclasses import dataclass, field
import random

# 花色代码：黑桃 S、红心 H、方块 D、梅花 C。牌写成像 "SA"、"H10" 这样的短字符串。
SUIT_NAME = {"S": "黑桃", "H": "红心", "D": "方块", "C": "梅花"}
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
POINTS = {str(n): n for n in range(2, 11)}
POINTS.update({"J": 11, "Q": 12, "K": 13, "A": 14})

DECK_SIZE = 10
OPENING_HAND = 3
HAND_LIMIT = 5
WIN_SCORE = 3


def all_cards() -> list[str]:
    """标准 52 张牌。花色在前，点数在后。"""
    cards = []
    for suit in "SHDC":
        for rank in RANKS:
            cards.append(suit + rank)
    return cards


def card_point(card: str) -> int:
    """点数。A 最大，2 最小。花色不参与比较。"""
    return POINTS[card[1:]]


def card_name(card: str) -> str:
    return SUIT_NAME[card[0]] + card[1:]


def card_info(card: str) -> dict:
    return {
        "code": card,
        "suit": card[0],
        "rank": card[1:],
        "name": card_name(card),
        "point": card_point(card),
        "red": card[0] in "HD",
    }


def check_deck(cards: list[str]) -> str | None:
    """牌组合不合法。合法时返回 None，方便直接当错误信息用。"""
    if len(cards) != DECK_SIZE:
        return f"牌组必须正好 {DECK_SIZE} 张"
    if len(set(cards)) != DECK_SIZE:
        return "牌组里不能有重复的牌"
    valid = set(all_cards())
    for card in cards:
        if card not in valid:
            return f"不存在的牌：{card}"
    return None


def default_deck() -> list[str]:
    """新用户的 10 张牌：黑桃、红心交替，从 A 往下。"""
    cards = []
    for index, rank in enumerate(["A", "K", "Q", "J", "10", "9", "8", "7", "6", "5"]):
        suit = "S" if index % 2 == 0 else "H"
        cards.append(suit + rank)
    return cards


def bot_deck() -> list[str]:
    """电脑的 10 张牌，和默认牌组不重复，方便看日志。"""
    cards = []
    for index, rank in enumerate(["A", "K", "Q", "J", "10", "9", "8", "7", "6", "5"]):
        suit = "D" if index % 2 == 0 else "C"
        cards.append(suit + rank)
    return cards


def rules_text() -> dict:
    """规则说明的正文。网页读这份，避免和引擎里的数字各写一遍。"""
    return {
        "title": "抽牌比点",
        "deck_size": DECK_SIZE,
        "opening_hand": OPENING_HAND,
        "hand_limit": HAND_LIMIT,
        "win_score": WIN_SCORE,
        "lines": [
            "这是双人初级牌戏，用来演示发牌、抓牌、合法性、联网和胜负判定。",
            f"每位玩家用自己的 {DECK_SIZE} 张牌组。可以在「牌组管理」里改，下一局生效。",
            f"统一发牌：开局只由服务器发牌，双方各 {OPENING_HAND} 张，客户端不能自己发。",
            f"个性化抓牌：轮到你时，可以从自己的牌组抓 1 张。手牌最多 {HAND_LIMIT} 张，每回合限 1 次。",
            "出牌：打出手里的 1 张。牌不在手牌里、还没轮到你、对局已结束，服务器都会拒绝。",
            "双方各出 1 张后比点数。A 最大，2 最小，花色不计。更大的得 1 分，相同算平局。",
            f"先得到 {WIN_SCORE} 分的人获胜。",
            "轮到你时，手里和牌组都没有牌，判你输。",
            "投降会立刻判负，并写入历史记录。",
            "开始游戏是对电脑。房间匹配需要两位已经登录的玩家。",
            "谁赢了只由服务器计算。网页只负责把你的动作送上去。",
        ],
    }


@dataclass
class Player:
    pid: str
    name: str
    deck: list[str]
    hand: list[str] = field(default_factory=list)
    discard: list[str] = field(default_factory=list)
    score: int = 0
    table_card: str | None = None
    drew_this_turn: bool = False
    is_bot: bool = False


@dataclass
class Game:
    id: str
    mode: str
    players: dict[str, Player]
    order: list[str]
    current: str
    status: str = "playing"
    winner: str | None = None
    reason: str = ""
    log: list[str] = field(default_factory=list)
    round_no: int = 1
    last_trick: dict[str, str] | None = None
    last_trick_winner: str | None = None
    saved: bool = False


def other(game: Game, pid: str) -> str:
    if pid == game.order[0]:
        return game.order[1]
    return game.order[0]


def _say(game: Game, text: str) -> None:
    game.log.append(text)
    if len(game.log) > 40:
        del game.log[:-40]


def assert_conserved(game: Game) -> None:
    """每名玩家的牌永远是牌组张数。用来抓住发牌、抓牌、出牌写错。"""
    for player in game.players.values():
        zone = list(player.deck) + list(player.hand) + list(player.discard)
        if player.table_card:
            zone.append(player.table_card)
        if len(zone) != DECK_SIZE or len(set(zone)) != DECK_SIZE:
            raise RuntimeError(f"{player.name} 的牌张数不对：{len(zone)}")


def new_game(
    game_id: str,
    mode: str,
    a_id: str,
    a_name: str,
    a_deck: list[str],
    b_id: str,
    b_name: str,
    b_deck: list[str],
    b_is_bot: bool = False,
    shuffle: bool = True,
) -> Game:
    for name, deck in ((a_name, a_deck), (b_name, b_deck)):
        problem = check_deck(deck)
        if problem:
            raise ValueError(f"{name}的牌组不合法：{problem}")

    deck_a = list(a_deck)
    deck_b = list(b_deck)
    if shuffle:
        random.shuffle(deck_a)
        random.shuffle(deck_b)

    players = {
        a_id: Player(a_id, a_name, deck_a),
        b_id: Player(b_id, b_name, deck_b, is_bot=b_is_bot),
    }
    game = Game(
        id=game_id,
        mode=mode,
        players=players,
        order=[a_id, b_id],
        current=a_id,
    )
    deal_opening(game)
    label = "单机" if mode == "solo" else "匹配"
    _say(game, f"{label}对局开始。服务器已统一发牌，双方各 {OPENING_HAND} 张。")
    return game


def deal_opening(game: Game) -> None:
    """统一发牌：两位玩家走同一个循环，张数都是 OPENING_HAND。

    牌组列表的最后一张当作牌顶。
    """
    for pid in game.order:
        player = game.players[pid]
        for _ in range(OPENING_HAND):
            player.hand.append(player.deck.pop())
    assert_conserved(game)


def _turn_error(game: Game, pid: str) -> str | None:
    if game.status != "playing":
        return "对局已经结束"
    if pid not in game.players:
        return "你不在这局游戏里"
    if game.current != pid:
        return "还没轮到你"
    return None


def _knock_out_if_stuck(game: Game, pid: str) -> None:
    """轮到这位玩家、但他没有任何牌时，直接判负。"""
    if game.status != "playing":
        return
    player = game.players[pid]
    if not player.hand and not player.deck:
        finish(game, other(game, pid), f"{player.name} 没有牌可出")


def draw(game: Game, pid: str) -> str | None:
    """个性化抓牌：只从这名玩家自己的牌组顶抽 1 张。

    成功返回 None，失败返回给玩家看的原因。
    """
    problem = _turn_error(game, pid)
    if problem:
        return problem
    player = game.players[pid]
    if player.drew_this_turn:
        return "本回合已经抓过牌了"
    if len(player.hand) >= HAND_LIMIT:
        return f"手牌已满（最多 {HAND_LIMIT} 张）"
    if not player.deck:
        return "你的牌组已经空了"
    player.hand.append(player.deck.pop())
    player.drew_this_turn = True
    _say(game, f"{player.name} 从自己的牌组抓了 1 张")
    assert_conserved(game)
    return None


def play(game: Game, pid: str, card: str) -> str | None:
    """出一张手牌。合法性只在服务器判断，调用方不用自己先算。"""
    problem = _turn_error(game, pid)
    if problem:
        return problem
    player = game.players[pid]
    if card not in player.hand:
        return "这张牌不在你的手牌里"

    game.last_trick = None
    game.last_trick_winner = None
    player.hand.remove(card)
    player.table_card = card
    player.drew_this_turn = False
    _say(game, f"{player.name} 打出 {card_name(card)}")

    rival = game.players[other(game, pid)]
    if rival.table_card is None:
        game.current = rival.pid
        _knock_out_if_stuck(game, rival.pid)
        assert_conserved(game)
        return None

    _resolve_round(game)
    assert_conserved(game)
    return None


def compare(card_a: str, card_b: str) -> str:
    """比较两张牌。返回 a、b 或 draw。"""
    point_a = card_point(card_a)
    point_b = card_point(card_b)
    if point_a > point_b:
        return "a"
    if point_b > point_a:
        return "b"
    return "draw"


def _resolve_round(game: Game) -> None:
    """双方都出过牌后，判定这一回合谁得分。"""
    a = game.players[game.order[0]]
    b = game.players[game.order[1]]
    outcome = compare(a.table_card, b.table_card)
    _say(
        game,
        f"比点：{a.name} 的 {card_name(a.table_card)} 对 {b.name} 的 {card_name(b.table_card)}",
    )
    game.last_trick = {a.pid: a.table_card, b.pid: b.table_card}
    if outcome == "a":
        a.score += 1
        game.last_trick_winner = a.pid
        _say(game, f"{a.name} 点数更大，得 1 分（{a.score} : {b.score}）")
    elif outcome == "b":
        b.score += 1
        game.last_trick_winner = b.pid
        _say(game, f"{b.name} 点数更大，得 1 分（{a.score} : {b.score}）")
    else:
        game.last_trick_winner = None
        _say(game, f"点数相同，本回合平局（{a.score} : {b.score}）")

    a.discard.append(a.table_card)
    b.discard.append(b.table_card)
    a.table_card = None
    b.table_card = None

    if a.score >= WIN_SCORE:
        finish(game, a.pid, f"{a.name} 先得到 {WIN_SCORE} 分")
        return
    if b.score >= WIN_SCORE:
        finish(game, b.pid, f"{b.name} 先得到 {WIN_SCORE} 分")
        return

    game.round_no += 1
    game.current = a.pid
    _knock_out_if_stuck(game, a.pid)


def surrender(game: Game, pid: str) -> str | None:
    if pid not in game.players:
        return "你不在这局游戏里"
    if game.status != "playing":
        return "对局已经结束"
    finish(game, other(game, pid), f"{game.players[pid].name} 投降")
    return None


def finish(game: Game, winner: str, reason: str) -> None:
    if game.status == "finished":
        return
    game.status = "finished"
    game.winner = winner
    game.reason = reason
    game.current = ""
    _say(game, f"对局结束，{game.players[winner].name} 获胜：{reason}")


def bot_act(game: Game) -> None:
    """电脑的一个回合：手牌少于 3 张就先抓，然后出点数最大的牌。"""
    bot_ids = [pid for pid, player in game.players.items() if player.is_bot]
    if not bot_ids:
        return
    pid = bot_ids[0]
    if game.status != "playing" or game.current != pid:
        return
    me = game.players[pid]
    if len(me.hand) < 3 and me.deck:
        draw(game, pid)
    if game.status != "playing" or game.current != pid:
        return
    if not me.hand:
        finish(game, other(game, pid), f"{me.name} 没有牌可出")
        return
    play(game, pid, max(me.hand, key=card_point))


def view_for(game: Game, pid: str) -> dict:
    """发给某一位玩家的状态。对手的手牌只给张数，不给具体是什么。"""
    me = game.players[pid]
    rival = game.players[other(game, pid)]
    if game.status != "finished":
        winner = None
    elif game.winner == pid:
        winner = "you"
    else:
        winner = "opponent"

    trick = None
    if game.last_trick:
        if game.last_trick_winner is None:
            result = "draw"
        elif game.last_trick_winner == pid:
            result = "you"
        else:
            result = "opponent"
        trick = {
            "you": game.last_trick[pid],
            "opponent": game.last_trick[other(game, pid)],
            "result": result,
        }

    return {
        "mode": game.mode,
        "status": game.status,
        "round_no": game.round_no,
        "target_score": WIN_SCORE,
        "limits": {
            "hand": HAND_LIMIT,
            "deck": DECK_SIZE,
            "opening": OPENING_HAND,
            "win_score": WIN_SCORE,
        },
        "you": {
            "name": me.name,
            "hand": list(me.hand),
            "deck_left": len(me.deck),
            "score": me.score,
            "drew_this_turn": me.drew_this_turn,
            "is_turn": game.status == "playing" and game.current == pid,
        },
        "opponent": {
            "name": rival.name,
            "hand_count": len(rival.hand),
            "deck_left": len(rival.deck),
            "score": rival.score,
            "is_bot": rival.is_bot,
        },
        "table": {"you": me.table_card, "opponent": rival.table_card},
        "last_trick": trick,
        "log": list(game.log[-12:]),
        "winner": winner,
        "reason": game.reason,
    }
