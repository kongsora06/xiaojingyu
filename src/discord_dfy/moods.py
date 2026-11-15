"""How she takes a serious question this time: mostly brushing it off, now and then dead on.

The model alone does not keep odds (asked the same way, it answers the same way), so the
host rolls and tells her the result; she uses it only if someone is actually quizzing her.
One roll holds for a while in each channel, so a question being pushed at her gets the
same attitude from start to finish instead of a new one every message.

Other bots get a dice of their own: how she takes a bot chiming in, held through a topic.
Her tone in the group (gentle or sharp-tongued) is a third one, thrown at every look,
and so is whether she joins in when people are repeating one line.
"""

import random
import time

# Out of ten serious questions: 4 too lazy to do it, 3 brushing it off, 2 seeing through
# it or passing it on, 1 a sudden, sharp answer.
MOODS = (("嫌麻烦", 4), ("敷衍", 3), ("识破甩锅", 2), ("认真", 1))
# Another bot talking, out of ten: 2 she lets it be, 4 a gentle tease, 2 friendly
# bickering, 2 agreement.
BOT_MOODS = (("不理", 2), ("打趣", 4), ("拌嘴", 2), ("附和", 2))
IGNORE_BOT = "不理"
# How she talks in the group, rolled anew each time she looks: 3 gentle, 7 sharp-tongued.
TONES = (("温柔", 3), ("毒舌", 7))
# People in the group repeating one line: how often she repeats it too, each time she looks.
ECHO_ODDS = 0.5
# A roll holds while the chat keeps going: until it has been quiet this long,
HOLD_SECONDS = 600.0
# and never longer than this, so a long evening still gets fresh rolls.
MAX_HOLD_SECONDS = 1800.0


def roll(table, rng=random) -> str:
    names, weights = zip(*table, strict=True)
    return rng.choices(names, weights)[0]


def serious_mood(rng=random) -> str:
    return roll(MOODS, rng)


def bot_mood(rng=random) -> str:
    return roll(BOT_MOODS, rng)


def group_tone(rng=random) -> str:
    return roll(TONES, rng)


def joins_echo(rng=random) -> bool:
    return rng.random() < ECHO_ODDS


class MoodDice:
    def __init__(self, clock=time.monotonic, roll=None):
        """roll: the dice to throw (default serious_mood, looked up at each throw)."""
        self.clock = clock
        self.roll = roll
        self.held: dict[object, tuple[str, float, float]] = {}  # mood, rolled at, last used

    def mood(self, key) -> str:
        now = self.clock()
        held = self.held.get(key)
        if held and now - held[2] < HOLD_SECONDS and now - held[1] < MAX_HOLD_SECONDS:
            self.held[key] = (held[0], held[1], now)
            return held[0]
        mood = (self.roll or serious_mood)()
        self.held[key] = (mood, now, now)
        return mood
