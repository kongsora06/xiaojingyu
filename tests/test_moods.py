import random
from collections import Counter

from discord_dfy import moods
from discord_dfy.moods import HOLD_SECONDS, MAX_HOLD_SECONDS, MOODS, MoodDice, serious_mood


def test_serious_moods_follow_the_four_three_two_one_odds():
    rng = random.Random(7)
    counts = Counter(serious_mood(rng) for _ in range(10000))
    assert set(counts) == {name for name, _ in MOODS}
    for name, weight in MOODS:
        assert abs(counts[name] / 10000 - weight / 10) < 0.02


def test_a_mood_holds_through_a_chat_then_rolls_again(monkeypatch):
    rolls = iter(["嫌麻烦", "认真", "敷衍"])
    monkeypatch.setattr(moods, "serious_mood", lambda: next(rolls))
    now = [0.0]
    dice = MoodDice(lambda: now[0])
    assert dice.mood("群") == "嫌麻烦"
    now[0] = HOLD_SECONDS - 1
    assert dice.mood("群") == "嫌麻烦"  # Still the same question going on.
    assert dice.mood("私聊") == "认真"  # Each channel has its own.
    now[0] += HOLD_SECONDS  # Quiet for a while: a fresh roll.
    assert dice.mood("群") == "敷衍"


def test_a_mood_does_not_hold_all_evening(monkeypatch):
    rolls = iter(["嫌麻烦", "认真"])
    monkeypatch.setattr(moods, "serious_mood", lambda: next(rolls))
    now = [0.0]
    dice = MoodDice(lambda: now[0])
    dice.mood("群")
    while now[0] < MAX_HOLD_SECONDS - 60:
        now[0] += 60
        assert dice.mood("群") == "嫌麻烦"
    now[0] += 120
    assert dice.mood("群") == "认真"
