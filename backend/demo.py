"""不启动网页，直接把规则引擎跑一遍。

在项目根目录执行：

    python -m backend.demo
"""

import random

from .engine import (
    DECK_SIZE,
    OPENING_HAND,
    WIN_SCORE,
    assert_conserved,
    bot_deck,
    check_deck,
    compare,
    default_deck,
    draw,
    new_game,
    play,
    surrender,
)


def expect(condition: bool, text: str) -> None:
    if not condition:
        raise SystemExit("失败: " + text)


def scripted_round() -> None:
    """洗牌关掉，这样能知道开局手里是哪三张。"""
    deck_a = ["SA", "SK", "SQ", "SJ", "S10", "S9", "S8", "S7", "S6", "S5"]
    deck_b = ["HA", "HK", "HQ", "HJ", "H10", "H9", "H8", "H7", "H6", "H5"]
    game = new_game(
        "demo",
        "solo",
        "1",
        "甲",
        deck_a,
        "bot",
        "乙",
        deck_b,
        b_is_bot=True,
        shuffle=False,
    )
    me = game.players["1"]
    # 牌顶在列表末尾，所以先发出 S5、S6、S7。
    expect(me.hand == ["S5", "S6", "S7"], "统一发牌张数或顺序不对")
    expect(len(me.deck) == DECK_SIZE - OPENING_HAND, "发牌后牌组剩余不对")
    expect(play(game, "1", "SA") == "这张牌不在你的手牌里", "不在手里的牌应该被拒绝")
    expect(me.hand == ["S5", "S6", "S7"], "非法出牌不应改手牌")
    expect(draw(game, "bot") == "还没轮到你", "没轮到的人不能抓牌")
    expect(draw(game, "1") is None, "自己的回合应该能抓牌")
    expect(me.hand[-1] == "S8", "抓到的应该是自己的牌顶")
    expect(draw(game, "1") == "本回合已经抓过牌了", "一回合只能抓一次")
    expect(play(game, "1", "S8") is None, "手里的牌应该能打出")
    expect(game.current == "bot", "出牌后应该轮到对手")
    print("脚本对局通过：非法出牌被拒绝，抓牌只来自自己的牌组。")
    for line in game.log:
        print("  " + line)

    expect(surrender(game, "1") is None, "投降应该成功")
    expect(game.winner == "bot", "投降后应该是对手获胜")
    expect(surrender(game, "1") == "对局已经结束", "结束后不能再投降")
    assert_conserved(game)


def random_games() -> None:
    random.seed(7)
    for index in range(30):
        game = new_game(
            f"r{index}",
            "solo",
            "1",
            "甲",
            default_deck(),
            "2",
            "乙",
            bot_deck(),
            shuffle=True,
        )
        steps = 0
        while game.status == "playing" and steps < 40:
            steps += 1
            pid = game.current
            player = game.players[pid]
            if not player.hand and player.deck:
                expect(draw(game, pid) is None, "没手牌时应该能抓牌")
            if game.status != "playing":
                break
            expect(bool(player.hand), "有牌可抓时不应空手出牌")
            expect(play(game, pid, player.hand[0]) is None, "打出自己的手牌不应失败")
        expect(game.status == "finished", "牌打完后对局应该结束")
        expect(game.winner in game.players, "获胜者必须是这局里的人")
        reason_ok = (
            game.players[game.winner].score >= WIN_SCORE or "没有牌" in game.reason
        )
        expect(reason_ok, "获胜原因和分数对不上")
        assert_conserved(game)
    print(f"随机对局通过：30 局都分出了胜负，先到 {WIN_SCORE} 分或无牌可出。")


def main() -> None:
    expect(check_deck(default_deck()) is None, "默认牌组应该合法")
    expect(compare("SA", "HA") == "draw", "同点应该平局")
    expect(compare("SA", "SK") == "a", "A 应该大于 K")
    expect(compare("H2", "DA") == "b", "2 应该小于 A")
    scripted_round()
    random_games()
    print("引擎自检通过。")


if __name__ == "__main__":
    main()
