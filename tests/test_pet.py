import json
import random

from PIL import Image, ImageDraw

from discord_dfy.pet import PET_SECONDS, Rub, keep_picture, load_state, unseen
from discord_dfy.pet_body import (
    HEART_SECONDS,
    MASTER_HEIGHT,
    MAX_LEAN,
    Body,
    Painter,
    Spring,
    bubble_picture,
    cut_out,
    find_lantern,
    wrap,
)


def drawn_on_white(size=(200, 240)) -> Image.Image:
    """A stand-in for her picture: a dark-outlined pale figure with a white glint and a
    lantern, on a white background."""
    picture = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(picture)
    draw.ellipse((40, 30, 160, 210), fill=(236, 226, 230), outline=(40, 36, 60), width=4)
    draw.ellipse((90, 60, 104, 74), fill=(255, 255, 255))  # The light in her eye: hers.
    draw.ellipse((92, 150, 108, 166), fill=(255, 200, 90))  # Her lantern.
    return picture


def settle(body: Body, seconds: float, step: float = 1 / 60) -> None:
    for _ in range(round(seconds / step)):
        body.step(step)


def test_she_is_cut_out_of_a_white_background_and_keeps_her_own_white():
    cut = cut_out(drawn_on_white())
    assert cut.mode == "RGBA" and cut.size == (121, 181)  # Trimmed to her outline.
    alpha = cut.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0 and alpha.getpixel((120, 180)) == 0  # Corners: gone.
    assert alpha.getpixel((60, 90)) == 255  # Her pale body, close to white, stays solid.
    assert cut.getpixel((57, 37)) == (255, 255, 255, 255)  # And the glint in her eye.
    assert 0 < alpha.getpixel((60, 0)) <= 255  # The outline's edge is hers, softly.


def test_a_picture_that_is_already_see_through_is_only_trimmed():
    picture = Image.new("RGBA", (100, 2000), (0, 0, 0, 0))
    ImageDraw.Draw(picture).rectangle((20, 100, 79, 1899), fill=(90, 80, 140, 255))
    cut = cut_out(picture)
    assert cut.size == (30, MASTER_HEIGHT)  # 60 x 1800, scaled down to the kept height.
    assert cut.getpixel((15, 450)) == (90, 80, 140, 255)


def test_her_lantern_is_found_by_its_light():
    spot = find_lantern(cut_out(drawn_on_white()))
    assert abs(spot[0] - 60 / 121) < 0.03 and abs(spot[1] - 128 / 181) < 0.03
    assert find_lantern(Image.new("RGBA", (50, 50), (90, 80, 140, 255))) is None


def test_a_spring_wobbles_and_comes_to_rest():
    spring = Spring(300.0, 8.0)
    assert spring.resting()
    spring.kick(-3.0)
    low, high = 0.0, 0.0
    for _ in range(600):
        spring.step(1 / 120)
        low, high = min(low, spring.value), max(high, spring.value)
    assert low < -0.1 and high > 0.03  # Past rest and back: a wobble, not a slide.
    assert spring.resting() and abs(spring.value) < 0.002
    spring.step(5.0)  # One huge step (the computer slept) does not blow it up.
    assert abs(spring.value) < 0.01


def test_poked_she_squashes_wider_and_bounces_back():
    body = Body()
    assert body.shape()[:1] + body.shape()[2:] == (1.0, 0.0, 0.0)
    body.poke(side=1.0)
    settle(body, 0.08)
    wide, tall, lean, up = body.shape()
    assert tall < 0.9 and wide > 1.05 and lean < 0 and up == 0  # Flat, wide, pushed aside.
    assert not body.calm()
    settle(body, 4)
    wide, tall, lean, up = body.shape()
    assert body.calm() and abs(tall - 1) < 0.03 and abs(lean) < 0.01  # Only breathing now.


def test_picked_up_she_stretches_and_swings_and_lands_with_a_bounce():
    body = Body()
    body.pick_up()
    body.swing(50.0)  # Carried fast to the right.
    settle(body, 0.6)
    wide, tall, lean, up = body.shape()
    assert body.held and tall > 1.03 and 0 < lean <= MAX_LEAN + 0.05
    assert body.hang.value > 0.9
    body.hop()
    assert body.rising == 0.0  # No hopping out of his hand.
    body.put_down()
    settle(body, 0.05)
    assert not body.held and body.squash.speed < 0  # Landing presses her down.
    settle(body, 4)
    assert body.calm() and abs(body.hang.value) < 0.01


def test_a_hop_goes_up_comes_down_and_ends_in_a_squash():
    body = Body()
    body.hop()
    heights = []
    for _ in range(60):
        body.step(1 / 60)
        heights.append(body.height)
    assert 0.1 < max(heights) < 0.3 and heights[-1] == 0.0  # A fifth of her height or so.
    landed = heights.index(0.0, 5)
    assert landed < 30  # Quick: under half a second.
    body.hop()
    body.hop()  # A second wish while in the air is ignored.
    assert body.rising == 1.8


def test_rubbing_her_head_makes_hearts_that_float_away():
    body = Body()
    for _ in range(12):
        body.pet(random.Random(1))
    assert len(body.hearts) == 14  # Capped: a long rub is not a snowstorm.
    first = body.hearts[0].y
    settle(body, 0.5)
    assert body.hearts[0].y < first
    settle(body, HEART_SECONDS)
    assert body.hearts == [] and body.calm()


def test_her_lantern_is_low_when_idle_and_pulses_when_busy():
    body = Body()
    assert body.glow() == 0.22
    body.busy = True
    levels = []
    for _ in range(120):
        body.step(1 / 60)
        levels.append(body.glow())
    assert min(levels) < 0.4 and max(levels) > 0.9


def test_a_rub_is_back_and_forth_not_the_mouse_passing_by():
    rub = Rub()
    assert not any(rub.moved(x, x / 1000, 10) for x in range(0, 400, 20))  # One way: no.
    rub.left()
    rubs = [rub.moved(x, i * 0.1, 10) for i, x in enumerate([0, 40, 0, 40, 0, 40])]
    assert rubs == [False, False, False, True, True, True]  # Third turn on: each stroke.
    slow = Rub()
    late = [slow.moved(x, i * PET_SECONDS, 10) for i, x in enumerate([0, 40, 0, 40, 0])]
    assert not any(late)  # Too slow to be a rub.
    assert not any(Rub().moved(x, 0.0, 10) for x in (0, 3, 0, 3, 0, 3))  # A tremble: no.


def test_frames_have_one_size_and_she_stays_inside_them():
    master = cut_out(drawn_on_white())
    painter = Painter(master, 180)
    assert painter.sprite.size == (120, 180) and painter.lantern is not None
    body = Body()
    still = painter.frame(body)
    assert still.size == painter.size and still.mode == "RGBA"
    box = still.getchannel("A").getbbox()
    assert box[0] == painter.side and box[3] == painter.top + 180  # Standing on her feet.
    assert still.getpixel((2, 2))[3] == 0  # Empty around her: clicks fall through there.
    body.busy = True
    body.pick_up()
    body.swing(9.0)
    body.pet()
    bubble = bubble_picture("在写日记", "自己待着\n「手还没松」", 1.0, marked=True)
    for _ in range(40):
        body.step(1 / 60)
        frame = painter.frame(body, bubble, 1.07)
        assert frame.size == painter.size
    left, top, right, bottom = painter.bubble_box(bubble)
    assert 0 <= left < right <= painter.size[0] and 0 <= top < bottom < painter.top + 20
    assert frame.getpixel(((left + right) // 2, top + 6))[3] > 200  # The bubble is there.
    # Standing or held, the point she turns about does not move when she stretches.
    tall = (1.0, 1.3, 0.0, 0.0)
    assert painter.at((*tall, 0.0), 60, 180) == painter.at((1.0, 1.0, 0.0, 0.0, 0.0), 60, 180)
    held_at = 180 * (1 - 0.82)
    assert painter.at((*tall, 1.0), 60, held_at)[1] == painter.at((1, 1, 0, 0, 1.0), 60, held_at)[1]


def test_bubbles_grow_with_their_words_and_long_lines_are_broken():
    short = bubble_picture("在想", "", 1.0)
    long = bubble_picture(
        "橱窗里摆了新东西", "很长很长很长很长很长很长很长很长的一首\n点我去看", 1.0
    )
    assert long.width > short.width and long.height > short.height
    assert bubble_picture("在想", "", 2.0).width > short.width * 1.6  # Scaled displays.
    assert wrap("一二三四五六七", 3) == ["一二三", "四五六", "七"]
    assert len(wrap("字" * 100, 5)) == 4  # Never a wall of text over her head.


def test_showcase_news_is_what_he_has_not_looked_at_yet():
    out = [
        {"path": "诗/秋天.md", "note": "", "since": "2026-11-10T12:00:00+08:00"},
        {"path": "随笔/灯.md", "note": "", "since": "2026-11-11T09:00:00+08:00"},
    ]
    assert unseen(out, {}) == out
    assert unseen(out, {"诗/秋天.md": "2026-11-10T12:00:00+08:00"}) == out[1:]
    # Taken back and put out again later: news again.
    assert unseen(out[:1], {"诗/秋天.md": "2026-11-01T08:00:00+08:00"}) == out[:1]


def test_her_place_is_remembered_and_a_broken_file_is_just_forgotten(tmp_path):
    path = tmp_path / "pet.json"
    assert load_state(path) == {}
    path.write_text("not json", encoding="utf-8")
    assert load_state(path) == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert load_state(path) == {}
    path.write_text(json.dumps({"x": 10.5, "y": 20, "height": 300}), encoding="utf-8")
    assert load_state(path)["height"] == 300


def test_a_chosen_picture_is_cut_out_and_kept(tmp_path):
    source = tmp_path / "她.jpg"
    drawn_on_white().save(source, quality=95)
    target = tmp_path / "data" / "pet.png"
    keep_picture(source, target)
    with Image.open(target) as kept:
        assert kept.mode == "RGBA" and kept.getpixel((0, 0))[3] == 0
        assert abs(kept.width - 121) <= 3 and abs(kept.height - 181) <= 3
