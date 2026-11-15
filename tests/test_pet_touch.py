from datetime import datetime, timedelta, timezone

from discord_dfy.pet_touch import (
    MAX_BYTES,
    PAT,
    RUB,
    RUB_GAP_SECONDS,
    STALE,
    Touches,
    record,
    take,
    touch_note,
    touch_path,
)

NOON = datetime(2026, 11, 12, 12, 0).astimezone()  # Noon where the tests run.


def test_touches_are_written_down_and_taken_once(tmp_path):
    path = touch_path(tmp_path / "data")
    assert take(path) == []  # Nothing yet: no file, no fuss.
    record(path, RUB, NOON)
    record(path, PAT, NOON + timedelta(seconds=30))
    assert take(path) == [(RUB, NOON), (PAT, NOON + timedelta(seconds=30))]
    assert take(path) == [] and list(path.parent.iterdir()) == []  # Told once, then gone.
    record(path, PAT, NOON)  # The next touch starts over.
    assert take(path) == [(PAT, NOON)]


def test_a_spoiled_line_does_not_lose_the_rest(tmp_path):
    path = touch_path(tmp_path)
    record(path, PAT, NOON)
    with path.open("a", encoding="utf-8") as file:
        file.write('not json\n{"kind": "kick", "at": "2026-11-12T12:00:00+08:00"}\n')
        file.write('{"kind": "pat"}\n')
        file.write('{"kind": "pat", "at": "2026-11-12T12:00:00"}\n[1]\n')  # No time zone.
    record(path, RUB, NOON + timedelta(minutes=1))
    assert take(path) == [(PAT, NOON), (RUB, NOON + timedelta(minutes=1))]


def test_the_file_stops_growing_while_nobody_takes_it(tmp_path):
    path = touch_path(tmp_path)
    path.write_text("x" * (MAX_BYTES + 1), encoding="utf-8")
    record(path, PAT, NOON)
    assert path.stat().st_size == MAX_BYTES + 1
    record(tmp_path, PAT, NOON)  # A place that cannot be written: he just is not heard.


def test_she_is_told_in_one_line_what_he_did_and_when():
    assert touch_note([], NOON) == ""
    one = touch_note([(RUB, NOON - timedelta(minutes=5))], NOON)
    assert one == "主人在桌面上摸了摸你的头（11-12 11:55，是他伸手碰了桌面上的你，不是他说的话）"
    assert touch_note([(PAT, NOON)], NOON).startswith("主人在桌面上拍了你一下（11-12 12:00，")
    many = [(PAT, NOON - timedelta(minutes=40 - i)) for i in range(5)]
    many += [(RUB, NOON - timedelta(minutes=30)), (RUB, NOON - timedelta(minutes=10))]
    note = touch_note(sorted(many, key=lambda touch: touch[1]), NOON)
    assert note.startswith("主人在桌面上摸了 2 回你的头、拍了你 5 下（11-12 11:20–11:50，")
    # Written in another time zone, told in hers.
    abroad = NOON.astimezone(timezone(timedelta(hours=13)))
    assert "（11-12 12:00，" in touch_note([(PAT, abroad)], NOON)


def test_what_he_did_long_ago_is_not_brought_up():
    old = (PAT, NOON - STALE - timedelta(minutes=1))
    assert touch_note([old], NOON) == ""
    note = touch_note([old, (RUB, NOON - timedelta(hours=1))], NOON)
    assert "拍" not in note and "摸了摸你的头（11-12 11:00，" in note
    late = datetime(2026, 11, 13, 0, 10).astimezone()
    over_midnight = [(PAT, late - timedelta(minutes=30)), (PAT, late)]
    assert "（11-12 23:40–11-13 00:10，" in touch_note(over_midnight, late)


def test_every_click_is_a_pat_and_a_spell_of_rubbing_is_one_rub(tmp_path):
    path = touch_path(tmp_path)
    touches = Touches(path)
    touches.pat()
    touches.pat()
    for stroke in range(20):  # Rubbing on and on, a stroke every half second.
        touches.rub(100.0 + stroke * 0.5)
    touches.rub(110.0 + RUB_GAP_SECONDS + 0.1)  # He came back for another.
    kinds = [kind for kind, _ in take(path)]
    assert kinds.count(PAT) == 2 and kinds.count(RUB) == 2
    now = datetime.now().astimezone()
    touches.pat()
    ((_, at),) = take(path)
    assert abs(now - at) < timedelta(seconds=5)  # Stamped with the real time.
