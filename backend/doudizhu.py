"""斗地主规则。

这个文件只回答：牌怎么发、叫分、这一手合不合法、能不能压过、谁赢了、机器人选哪一手。
它不导入 FastAPI，也不读写数据库。网页和接口都调用这里，所以规则只有一份。
"""

from collections import Counter
from dataclasses import dataclass, field
import random

SEATS = 3

# 3 最小，2 比 A 大，小王、大王最大。顺子到 A 为止，2 和王不能进顺子。
RANKS = ["3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2"]
CHAIN = RANKS[:-1]
POINT = {rank: index for index, rank in enumerate(RANKS)}
POINT["BJ"] = len(RANKS)
POINT["RJ"] = len(RANKS) + 1
RANK_OF_POINT = {value: rank for rank, value in POINT.items()}
RANK_OF_POINT[POINT["BJ"]] = "小王"
RANK_OF_POINT[POINT["RJ"]] = "大王"

KIND_NAME = {
    "solo": "单张",
    "pair": "对子",
    "triple": "三张",
    "triple_one": "三带一",
    "triple_two": "三带二",
    "straight": "顺子",
    "pair_straight": "连对",
    "plane": "飞机",
    "plane_one": "飞机带单",
    "plane_two": "飞机带对",
    "four_two": "四带二",
    "four_two_pair": "四带两对",
    "bomb": "炸弹",
    "rocket": "王炸",
}


def rank_of(card: str) -> str:
    if card in ("BJ", "RJ"):
        return card
    return card[1:]


def rank_label(card: str) -> str:
    rank = rank_of(card)
    if rank == "BJ":
        return "小王"
    if rank == "RJ":
        return "大王"
    return rank


@dataclass
class Combo:
    kind: str
    power: int
    length: int
    cards: list[str]
    label: str


@dataclass
class Player:
    id: str
    name: str
    bot: bool
    hand: list[str] = field(default_factory=list)
    bid: int | None = None
    last_play: list[str] = field(default_factory=list)
    passed: bool = False
    played_any: bool = False


@dataclass
class Game:
    id: str
    mode: str
    order: list[str]
    players: dict[str, Player]
    phase: str = "bidding"
    turn: str = ""
    bid_high: int = 0
    bid_leader: str | None = None
    passes: int = 0
    landlord: str | None = None
    bottom: list[str] = field(default_factory=list)
    base: int = 0
    bombs: int = 0
    spring: bool = False
    top: Combo | None = None
    top_owner: str | None = None
    winner: str | None = None
    winner_side: str | None = None
    reason: str = ""
    log: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    saved: bool = False


def fresh_deck() -> list[str]:
    cards = [suit + rank for suit in "SHDC" for rank in RANKS]
    cards += ["BJ", "RJ"]
    return cards


def _label(kind: str, power: int, length: int) -> str:
    rank = RANK_OF_POINT[power]
    if kind == "rocket":
        return "王炸"
    if kind == "straight":
        return f"顺子 {length} 张"
    if kind == "pair_straight":
        return f"连对 {length} 对"
    if kind in ("plane", "plane_one", "plane_two"):
        return f"{KIND_NAME[kind]} {length} 段"
    return f"{KIND_NAME[kind]} {rank}"


def _combo(kind: str, power: int, length: int, cards: list[str]) -> Combo:
    return Combo(kind, power, length, cards, _label(kind, power, length))


def identify(cards: list[str]) -> Combo | None:
    """认出这一手是什么牌型。认不出就返回 None。"""
    if not cards:
        return None
    groups: dict[str, list[str]] = {}
    for card in cards:
        groups.setdefault(rank_of(card), []).append(card)
    counts = {rank: len(held) for rank, held in groups.items()}
    size = len(cards)

    if size == 2 and set(counts) == {"BJ", "RJ"}:
        return _combo("rocket", POINT["RJ"], 1, cards[:])

    if size == 4 and len(counts) == 1:
        rank = next(iter(counts))
        return _combo("bomb", POINT[rank], 1, cards[:])

    if size == 1:
        rank = next(iter(counts))
        return _combo("solo", POINT[rank], 1, cards[:])

    if size == 2 and len(counts) == 1:
        rank = next(iter(counts))
        return _combo("pair", POINT[rank], 1, cards[:])

    if size == 3 and len(counts) == 1:
        rank = next(iter(counts))
        return _combo("triple", POINT[rank], 1, cards[:])

    chain = _as_chain(counts, need=1, min_len=5)
    if chain and size == len(chain):
        return _combo("straight", POINT[chain[-1]], len(chain), cards[:])

    pairs = _as_chain(counts, need=2, min_len=3)
    if pairs and size == len(pairs) * 2 and all(counts[rank] == 2 for rank in counts):
        return _combo("pair_straight", POINT[pairs[-1]], len(pairs), cards[:])

    triples = _as_chain(counts, need=3, min_len=2)
    if triples and size == len(triples) * 3 and all(counts[rank] == 3 for rank in counts):
        return _combo("plane", POINT[triples[-1]], len(triples), cards[:])

    if size == 4 and 3 in counts.values():
        rank = next(item for item, count in counts.items() if count == 3)
        return _combo("triple_one", POINT[rank], 1, cards[:])

    if size == 5 and sorted(counts.values()) == [2, 3]:
        rank = next(item for item, count in counts.items() if count == 3)
        return _combo("triple_two", POINT[rank], 1, cards[:])

    if size == 6 and 4 in counts.values():
        rank = next(item for item, count in counts.items() if count == 4)
        return _combo("four_two", POINT[rank], 1, cards[:])

    if size == 8 and sorted(counts.values()) == [2, 2, 4]:
        rank = next(item for item, count in counts.items() if count == 4)
        return _combo("four_two_pair", POINT[rank], 1, cards[:])

    winged = _winged_plane(counts)
    if winged:
        kind, power, length = winged
        return _combo(kind, power, length, cards[:])
    return None


def _as_chain(counts: dict[str, int], need: int, min_len: int) -> list[str] | None:
    ranks = [rank for rank, count in counts.items() if count >= need and rank in CHAIN]
    if len(ranks) < min_len or len(ranks) != len(counts):
        return None
    ranks.sort(key=lambda rank: POINT[rank])
    if POINT[ranks[-1]] - POINT[ranks[0]] != len(ranks) - 1:
        return None
    if any(counts[rank] != need for rank in ranks):
        return None
    return ranks


def _winged_plane(counts: dict[str, int]) -> tuple[str, int, int] | None:
    """连续三张加上同样段数的单牌或对子。"""
    body = [rank for rank, count in counts.items() if count >= 3 and rank in CHAIN]
    body.sort(key=lambda rank: POINT[rank])
    if len(body) < 2:
        return None
    best = None
    for start in range(len(body)):
        for end in range(start + 2, len(body) + 1):
            group = body[start:end]
            if POINT[group[-1]] - POINT[group[0]] != len(group) - 1:
                continue
            rest = dict(counts)
            for rank in group:
                rest[rank] -= 3
                if rest[rank] == 0:
                    del rest[rank]
            length = len(group)
            leftover = sum(rest.values())
            kind = None
            if leftover == 0:
                kind = "plane"
            elif leftover == length and all(count == 1 for count in rest.values()):
                kind = "plane_one"
            elif leftover == length * 2 and all(count == 2 for count in rest.values()):
                kind = "plane_two"
            if kind:
                best = (kind, POINT[group[-1]], length)
    return best


def beats(new: Combo, old: Combo | None) -> bool:
    if old is None:
        return True
    if new.kind == "rocket":
        return True
    if old.kind == "rocket":
        return False
    if new.kind == "bomb" and old.kind != "bomb":
        return True
    if new.kind == "bomb" and old.kind == "bomb":
        return new.power > old.power
    if old.kind == "bomb":
        return False
    return new.kind == old.kind and new.length == old.length and new.power > old.power


def _grouped(hand: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for card in hand:
        groups.setdefault(rank_of(card), []).append(card)
    return groups


def _pick(groups: dict[str, list[str]], ranks: list[str], count: int) -> list[str] | None:
    chosen = []
    for rank in ranks:
        held = groups.get(rank, [])
        if len(held) < count:
            return None
        chosen.extend(held[:count])
    return chosen


def generate(hand: list[str]) -> list[Combo]:
    """从手牌里找出一批能出的牌型。同一种只留一手，给提示和机器人用。"""
    groups = _grouped(hand)
    found: list[Combo] = []
    seen: set[tuple] = set()

    def add(combo: Combo | None) -> None:
        if combo is None:
            return
        key = (combo.kind, combo.power, combo.length)
        if key in seen:
            return
        seen.add(key)
        found.append(combo)

    if "BJ" in groups and "RJ" in groups:
        add(_combo("rocket", POINT["RJ"], 1, ["BJ", "RJ"]))

    for rank, held in groups.items():
        if len(held) >= 1:
            add(_combo("solo", POINT[rank], 1, held[:1]))
        if len(held) >= 2:
            add(_combo("pair", POINT[rank], 1, held[:2]))
        if len(held) >= 3:
            add(_combo("triple", POINT[rank], 1, held[:3]))
            kicker = _lowest_kicker(groups, rank, 1, 1)
            if kicker:
                add(_combo("triple_one", POINT[rank], 1, held[:3] + kicker))
            pair = _lowest_kicker(groups, rank, 2, 1)
            if pair:
                add(_combo("triple_two", POINT[rank], 1, held[:3] + pair))
        if len(held) >= 4:
            add(_combo("bomb", POINT[rank], 1, held[:4]))
            kickers = _lowest_kicker(groups, rank, 1, 2)
            if kickers:
                add(_combo("four_two", POINT[rank], 1, held[:4] + kickers))
            pairs = _lowest_kicker(groups, rank, 2, 2)
            if pairs:
                add(_combo("four_two_pair", POINT[rank], 1, held[:4] + pairs))

    for length in range(5, 13):
        for start in range(0, len(CHAIN) - length + 1):
            ranks = CHAIN[start:start + length]
            chosen = _pick(groups, ranks, 1)
            if chosen:
                add(_combo("straight", POINT[ranks[-1]], length, chosen))

    for length in range(3, 11):
        for start in range(0, len(CHAIN) - length + 1):
            ranks = CHAIN[start:start + length]
            chosen = _pick(groups, ranks, 2)
            if chosen:
                add(_combo("pair_straight", POINT[ranks[-1]], length, chosen))

    for length in range(2, 7):
        for start in range(0, len(CHAIN) - length + 1):
            ranks = CHAIN[start:start + length]
            body = _pick(groups, ranks, 3)
            if not body:
                continue
            add(_combo("plane", POINT[ranks[-1]], length, body))
            singles = _lowest_kicker(groups, ranks, 1, length)
            if singles:
                add(_combo("plane_one", POINT[ranks[-1]], length, body + singles))
            pairs = _lowest_kicker(groups, ranks, 2, length)
            if pairs:
                add(_combo("plane_two", POINT[ranks[-1]], length, body + pairs))

    found.sort(key=lambda combo: (combo.kind in ("bomb", "rocket"), combo.power, len(combo.cards)))
    return found


def _lowest_kicker(groups: dict[str, list[str]], skip, need: int, count: int) -> list[str] | None:
    blocked = {skip} if isinstance(skip, str) else set(skip)
    pool = sorted(
        (rank for rank, held in groups.items() if rank not in blocked and len(held) >= need),
        key=lambda rank: POINT[rank],
    )
    if len(pool) < count:
        return None
    chosen = []
    for rank in pool[:count]:
        chosen.extend(groups[rank][:need])
    return chosen


def hint_options(hand: list[str], top: Combo | None) -> list[dict]:
    options = [combo for combo in generate(hand) if beats(combo, top)]
    return [{"cards": combo.cards[:], "label": combo.label} for combo in options[:8]]


def rules_text() -> dict:
    return {
        "title": "斗地主",
        "lines": [
            "一副 54 张，三家各 17 张，留 3 张底牌。",
            "点数从 3 到 A，然后是 2，最后是小王、大王。花色不分大小。",
            "叫分按座位轮流，可以不叫，也可以叫比当前更高的 1 到 3 分。叫到 3 分就停。",
            "叫得最高的人当地主，叫到的分就是底分。三家都不叫就重新发牌。",
            "地主拿走底牌，共 20 张，并且先出牌。",
            "牌型有单张、对子、三张、三带一、三带二、顺子（5 张起）、连对（3 对起）、飞机、四带二、四带两对、炸弹和王炸。",
            "顺子、连对、飞机都不能带 2 和大小王。",
            "牌型和张数都一样才能比大小。炸弹压普通牌型，王炸压炸弹。",
            "压不过就不出。两家都不出，由上一手出牌的人重新起头。",
            "谁先出完手牌，谁那一方赢。地主一个人一方，两个农民一方。",
            "分数是底分乘倍数。炸弹、王炸和春天各翻一倍。地主赢拿两份，农民各拿一份。",
            "记牌器算的是还没露面的牌：自己手里的和已经打出的都不算。",
        ],
    }


def new_game(game_id: str, mode: str, seats: list[tuple[str, str, bool]]) -> Game:
    players = {pid: Player(id=pid, name=name, bot=bot) for pid, name, bot in seats}
    game = Game(id=game_id, mode=mode, order=[pid for pid, _, _ in seats], players=players)
    _deal(game)
    game.turn = game.order[0]
    game.log.append("发牌结束，开始叫分")
    return game


def _deal(game: Game) -> None:
    deck = fresh_deck()
    random.shuffle(deck)
    for index, pid in enumerate(game.order):
        game.players[pid].hand = deck[index * 17:(index + 1) * 17]
    game.bottom = deck[51:]


def _next(game: Game, pid: str) -> str:
    order = game.order
    return order[(order.index(pid) + 1) % len(order)]


def _reset_table(game: Game) -> None:
    for player in game.players.values():
        player.last_play = []
        player.passed = False


def bid(game: Game, pid: str, score: int) -> str | None:
    if game.phase != "bidding":
        return "现在不是叫分"
    if game.turn != pid:
        return "还没轮到你"
    if score < 0 or score > 3:
        return "叫分只能是 0 到 3"
    player = game.players[pid]
    if score == 0:
        player.bid = 0
        game.passes += 1
        game.log.append(f"{player.name} 不叫")
    else:
        if score <= game.bid_high:
            return "叫分必须比当前更高"
        player.bid = score
        game.bid_high = score
        game.bid_leader = pid
        game.base = score
        game.passes = 0
        game.log.append(f"{player.name} 叫 {score} 分")
    if score == 3 or (game.bid_leader and game.passes >= 2):
        _crown(game)
        return None
    if game.bid_leader is None and game.passes >= SEATS:
        _redeal(game)
        return None
    game.turn = _next(game, pid)
    return None


def _crown(game: Game) -> None:
    landlord = game.bid_leader or game.order[0]
    game.landlord = landlord
    game.base = game.bid_high or 1
    player = game.players[landlord]
    player.hand.extend(game.bottom)
    game.phase = "playing"
    game.turn = landlord
    game.top = None
    game.top_owner = None
    game.passes = 0
    _reset_table(game)
    game.log.append(f"{player.name} 以 {game.base} 分当地主")


def _redeal(game: Game) -> None:
    game.log.append("三家都不叫，重新发牌")
    for player in game.players.values():
        player.hand = []
        player.bid = None
        player.last_play = []
        player.passed = False
        player.played_any = False
    game.bid_high = 0
    game.bid_leader = None
    game.base = 0
    game.passes = 0
    game.bottom = []
    _deal(game)
    game.turn = game.order[0]


def play(game: Game, pid: str, cards: list[str]) -> str | None:
    if game.phase != "playing":
        return "现在不能出牌"
    if game.turn != pid:
        return "还没轮到你"
    if len(cards) != len(set(cards)):
        return "牌不能重复"
    player = game.players[pid]
    if any(card not in player.hand for card in cards):
        return "这些牌不在你的手里"
    combo = identify(cards)
    if combo is None:
        return "这不是能出的牌型"
    if not beats(combo, game.top):
        return "压不过上家"
    for card in cards:
        player.hand.remove(card)
    player.last_play = cards[:]
    player.passed = False
    player.played_any = True
    game.discarded.extend(cards)
    game.top = combo
    game.top_owner = pid
    game.passes = 0
    if combo.kind in ("bomb", "rocket"):
        game.bombs += 1
    game.log.append(f"{player.name} 出了 {combo.label}")
    if not player.hand:
        side = "地主" if pid == game.landlord else "农民"
        _finish(game, pid, f"{side}先出完了手牌")
        return None
    for other in game.players.values():
        if other.id != pid:
            other.passed = False
    game.turn = _next(game, pid)
    return None


def pass_turn(game: Game, pid: str) -> str | None:
    if game.phase != "playing":
        return "现在不能不出"
    if game.turn != pid:
        return "还没轮到你"
    if game.top is None:
        return "这一手由你先出，不能不出"
    player = game.players[pid]
    player.passed = True
    player.last_play = []
    game.passes += 1
    game.log.append(f"{player.name} 不出")
    if game.passes >= SEATS - 1:
        leader = game.top_owner or pid
        game.top = None
        game.passes = 0
        game.turn = leader
        _reset_table(game)
        game.log.append(f"轮到 {game.players[leader].name} 重新出牌")
        return None
    game.turn = _next(game, pid)
    return None


def surrender(game: Game, pid: str) -> str | None:
    if game.phase != "playing" or not game.landlord:
        return "叫分结束之后才能认输"
    if pid not in game.players:
        return "你不在这局游戏里"
    name = game.players[pid].name
    if pid == game.landlord:
        rival = next(other for other in game.order if other != pid)
        _finish(game, rival, f"{name} 认输")
    else:
        _finish(game, game.landlord, f"{name} 认输")
    return None


def _finish(game: Game, winner: str, reason: str) -> None:
    game.phase = "finished"
    game.winner = winner
    game.winner_side = "landlord" if winner == game.landlord else "farmer"
    farmers = [player for pid, player in game.players.items() if pid != game.landlord]
    game.spring = game.winner_side == "landlord" and not any(player.played_any for player in farmers)
    if game.spring:
        reason += "，春天"
    game.reason = reason
    game.turn = ""
    game.log.append(reason)


def multiplier(game: Game) -> int:
    value = 1
    for _ in range(game.bombs):
        value *= 2
    if game.spring:
        value *= 2
    return value


def you_win(game: Game, pid: str) -> bool:
    if game.winner_side == "landlord":
        return pid == game.landlord
    return pid != game.landlord


def score_for(game: Game, pid: str) -> int:
    amount = max(game.base, 1) * multiplier(game)
    if pid == game.landlord:
        amount *= 2
    return amount if you_win(game, pid) else -amount


def is_bot_turn(game: Game) -> bool:
    if game.phase not in ("bidding", "playing") or not game.turn:
        return False
    return game.players[game.turn].bot


def bot_step(game: Game) -> None:
    if not is_bot_turn(game):
        return
    pid = game.turn
    player = game.players[pid]
    if game.phase == "bidding":
        bid(game, pid, _bot_bid(game, player))
        return
    choice = _bot_play(game, player)
    if choice is None:
        pass_turn(game, pid)
    else:
        play(game, pid, choice)


def _bot_bid(game: Game, player: Player) -> int:
    groups = _grouped(player.hand)
    strength = 0
    for rank, held in groups.items():
        if len(held) == 4:
            strength += 6
        if rank in ("BJ", "RJ"):
            strength += 3
        elif rank == "2":
            strength += len(held) * 2
        elif rank == "A":
            strength += len(held)
    if "BJ" in groups and "RJ" in groups:
        strength += 4
    if strength >= 10:
        want = 3
    elif strength >= 6:
        want = 2
    elif strength >= 3:
        want = 1
    else:
        want = 0
    if want <= game.bid_high:
        return 0
    return want


def _bot_play(game: Game, player: Player) -> list[str] | None:
    options = [combo for combo in generate(player.hand) if beats(combo, game.top)]
    if not options:
        return None
    if game.top is None:
        return _choose_lead(options).cards
    partner = (
        game.landlord is not None
        and player.id != game.landlord
        and game.top_owner not in (None, player.id, game.landlord)
    )
    finishers = [combo for combo in options if len(combo.cards) == len(player.hand)]
    if finishers:
        finishers.sort(key=lambda combo: (combo.kind in ("bomb", "rocket"), combo.power))
        return finishers[0].cards
    if partner:
        return None
    plain = [combo for combo in options if combo.kind not in ("bomb", "rocket")]
    if plain:
        plain.sort(key=lambda combo: (combo.power, len(combo.cards)))
        return plain[0].cards
    if len(player.hand) <= 6 or (game.top and game.top.kind == "bomb"):
        bombs = [combo for combo in options if combo.kind in ("bomb", "rocket")]
        bombs.sort(key=lambda combo: (combo.kind == "rocket", combo.power))
        return bombs[0].cards
    return None


def _choose_lead(options: list[Combo]) -> Combo:
    straights = [combo for combo in options if combo.kind == "straight" and combo.power <= POINT["10"]]
    if straights:
        straights.sort(key=lambda combo: (-combo.length, combo.power))
        return straights[0]
    pairs = [combo for combo in options if combo.kind == "pair" and combo.power <= POINT["9"]]
    if pairs:
        return min(pairs, key=lambda combo: combo.power)
    solos = [combo for combo in options if combo.kind == "solo"]
    if solos:
        return min(solos, key=lambda combo: combo.power)
    plain = [combo for combo in options if combo.kind not in ("bomb", "rocket")]
    if plain:
        return min(plain, key=lambda combo: (combo.power, len(combo.cards)))
    return min(options, key=lambda combo: (combo.kind == "rocket", combo.power))


def _remaining(hand: list[str], discarded: list[str]) -> list[dict]:
    seen = Counter(rank_label(card) for card in list(hand) + list(discarded))
    rows = []
    for rank in RANKS:
        rows.append({"rank": rank, "left": 4 - seen[rank], "total": 4})
    for rank in ("小王", "大王"):
        rows.append({"rank": rank, "left": 1 - seen[rank], "total": 1})
    return rows


def view_for(game: Game, pid: str) -> dict:
    you = game.players[pid]
    index = game.order.index(pid)
    right = game.order[(index + 1) % SEATS]
    left = game.order[(index + 2) % SEATS]

    def seat(other_id: str, side: str) -> dict:
        other = game.players[other_id]
        return {
            "seat": other_id,
            "name": other.name,
            "side": side,
            "is_turn": game.phase == "playing" and game.turn == other_id,
            "is_bid_turn": game.phase == "bidding" and game.turn == other_id,
            "is_landlord": other_id == game.landlord,
            "hand_count": len(other.hand),
            "bid": other.bid,
            "last_play": other.last_play[:],
            "passed": other.passed,
        }

    top = None
    if game.top is not None:
        top = {
            "label": game.top.label,
            "by_you": game.top_owner == pid,
            "cards": game.top.cards[:],
        }
    result = None
    if game.phase == "finished":
        result = {
            "you_win": you_win(game, pid),
            "reason": game.reason,
            "score": score_for(game, pid),
        }
    return {
        "mode": game.mode,
        "phase": game.phase,
        "base": game.base,
        "bid_high": game.bid_high,
        "multiplier": multiplier(game),
        "bombs": game.bombs,
        "bottom": game.bottom[:],
        "bottom_hidden": game.phase == "bidding",
        "remaining": _remaining(you.hand, game.discarded),
        "others": [seat(right, "right"), seat(left, "left")],
        "top": top,
        "you": {
            "name": you.name,
            "hand": you.hand[:],
            "hand_count": len(you.hand),
            "is_turn": game.phase == "playing" and game.turn == pid,
            "is_bid_turn": game.phase == "bidding" and game.turn == pid,
            "is_landlord": pid == game.landlord,
            "bid": you.bid,
        },
        "result": result,
        "log": game.log[:],
        "bot_thinking": is_bot_turn(game),
    }
