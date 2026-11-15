import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from discord_dfy.host_tools import (
    MAX_SHOWN,
    Room,
    ToolError,
    describe_call,
    read_showcase,
    showcase_path,
    what_she_did,
)
from discord_dfy.now import Now, Watcher, ago
from discord_dfy.portrait import write_self
from discord_dfy.room_view import (
    NotShown,
    RoomView,
    compare,
    make_server,
    open_shown,
    scan_room,
    self_timeline,
)
from discord_dfy.widget import lines

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
NOW = datetime(2026, 11, 10, 12, 0).astimezone()
SECRET = "只写给自己看的一句话"


@pytest.fixture
def room(tmp_path):
    room = Room(tmp_path / "room", tmp_path / "stickers")
    room.write("诗/秋天.md", "叶子落了")
    room.write("诗/藏着.md", SECRET)
    room.write("便签.md", SECRET)
    room.write_diary(SECRET, datetime(2026, 11, 9, 22, 10))
    room.write_diary(SECRET, datetime(2026, 11, 9, 23, 30))
    room.write_diary(SECRET, datetime(2026, 11, 10, 1, 5))
    room.keep_picture(PNG, "海边")
    return room


def test_she_puts_things_in_the_showcase_and_takes_them_back(room):
    assert room.display("诗/秋天.md", "给你  看\n这首", NOW) == "摆进橱窗了：诗/秋天.md"
    assert read_showcase(room.root) == [
        {"path": "诗/秋天.md", "note": "给你 看 这首", "since": NOW.isoformat(timespec="seconds")}
    ]
    # The list is kept beside the room: nothing she writes in the room can change it.
    assert showcase_path(room.root).parent == room.root.parent
    assert room.list().endswith("橱窗里摆着：诗/秋天.md")
    assert "橱窗" not in room.list("诗")
    room.display("诗/秋天.md", "", NOW)  # Again: still one, the note replaced.
    assert [item["note"] for item in read_showcase(room.root)] == [""]
    assert room.put_away("诗/秋天.md") == "从橱窗收回来了：诗/秋天.md"
    assert room.put_away("诗/秋天.md") == "橱窗里本来就没有：诗/秋天.md"
    assert read_showcase(room.root) == [] and "橱窗" not in room.list()


def test_the_diary_folders_and_things_outside_never_go_on_show(room):
    with pytest.raises(ToolError, match="日记不摆橱窗"):
        room.display("diary/2026-11.md", "", NOW)
    for bad in ("诗", "没有.md", "../memory.md", ""):
        with pytest.raises(ToolError):
            room.display(bad, "", NOW)
    for number in range(MAX_SHOWN):
        room.write(f"多/{number}.md", "x")
        room.display(f"多/{number}.md", "", NOW)
    with pytest.raises(ToolError, match="摆满了"):
        room.display("诗/秋天.md", "", NOW)
    assert read_showcase(room.root)[0]["path"] == "多/0.md"


def test_showcase_calls_read_plainly_in_the_monitor_and_her_notes():
    assert (
        describe_call("room_show", {"path": "诗/秋天.md", "note": "看"})
        == "`room_show` 摆出 诗/秋天.md"
    )
    assert (
        describe_call("room_show", {"path": "诗/秋天.md", "show": False})
        == "`room_show` 收回 诗/秋天.md"
    )
    assert what_she_did("room_show", {"path": "诗/秋天.md"}) == "把 诗/秋天.md 摆进了橱窗"
    assert what_she_did("room_show", {"path": "诗/秋天.md", "show": False}) == (
        "把 诗/秋天.md 从橱窗收回来了"
    )


def test_from_outside_the_room_shows_names_and_sizes_never_words(room):
    room.display("诗/秋天.md", "给你看", NOW)
    (room.root / ".hidden").write_text("x", encoding="utf-8")
    (room.root / "空的").mkdir()
    seen = scan_room(room.root)
    assert SECRET not in json.dumps(seen, ensure_ascii=False)
    assert [(shelf["name"], [t["name"] for t in shelf["things"]]) for shelf in seen["shelves"]] == [
        ("空的", []),
        ("诗", ["秋天", "藏着"]),
    ]
    autumn, hidden = seen["shelves"][1]["things"]
    assert (autumn["chars"], autumn["kind"], autumn["shown"], hidden["shown"]) == (
        4,
        "text",
        True,
        False,
    )
    assert [t["path"] for t in seen["loose"]] == ["便签.md"]
    assert [(t["name"], t["kind"]) for t in seen["pictures"]] == [("海边", "picture")]
    assert seen["diary"] == {
        "entries": 3,
        "days": {"2026-11-09": 2, "2026-11-10": 1},
        "last": "2026-11-10T01:05",
        "chars": seen["diary"]["chars"],
    }
    (shown,) = seen["showcase"]
    assert (shown["path"], shown["note"]) == ("诗/秋天.md", "给你看")
    assert scan_room(room.root.parent / "nowhere")["shelves"] == []


def test_only_what_she_put_out_opens(room, tmp_path):
    room.display("诗/秋天.md", "", NOW)
    room.display("pictures/海边.png", "", NOW)
    assert open_shown(room.root, "诗/秋天.md") == ("text", "叶子落了".encode())
    assert open_shown(room.root, "pictures/海边.png") == ("png", PNG)
    (tmp_path / "memory.md").write_text(SECRET, encoding="utf-8")
    for closed in ("诗/藏着.md", "便签.md", "diary/2026-11.md", "../memory.md", "", "诗", None):
        with pytest.raises(NotShown):
            open_shown(room.root, closed)
    # Even a list edited by hand cannot open her diary or reach outside the room.
    showcase_path(room.root).write_text(
        json.dumps([{"path": "diary/2026-11.md"}, {"path": "../memory.md"}]), encoding="utf-8"
    )
    for closed in ("diary/2026-11.md", "../memory.md"):
        with pytest.raises(NotShown):
            open_shown(room.root, closed)
    assert scan_room(room.root)["showcase"] == []
    room.put_away("诗/秋天.md")
    with pytest.raises(NotShown):
        open_shown(room.root, "诗/秋天.md")


def test_comparing_two_versions_marks_what_went_and_what_is_new():
    old = "我喜欢麻辣烫。被夸会高兴。\n\n我有自己的口味。"
    new = "被夸会高兴，偶尔想再讨一句。\n\n我有自己的口味。\n\n温柔是我自己选的。"
    parts = compare(old, new)
    assert "".join(p["text"] for p in parts if p["op"] != "new") == old
    assert "".join(p["text"] for p in parts if p["op"] != "gone") == new
    assert {"op": "gone", "text": "我喜欢麻辣烫。"} in parts
    assert any(p["op"] == "new" and "温柔是我自己选的。" in p["text"] for p in parts)
    assert any(p["op"] == "same" and p["text"].startswith("被夸会高兴") for p in parts)
    assert compare("一样。", "一样。") == [{"op": "same", "text": "一样。"}]


def test_her_self_page_is_followed_from_rewrite_to_rewrite(tmp_path):
    memory = tmp_path / "memory.md"
    assert self_timeline(memory) == []
    path = tmp_path / "self.md"
    write_self(path, "我爱闹。", datetime(2026, 11, 1, 9))
    write_self(path, "我爱闹。也爱听故事。", datetime(2026, 11, 3, 21, 30))
    write_self(path, "我爱听故事。", datetime(2026, 11, 8, 4, 40, 52))
    first, second, latest = self_timeline(memory)
    assert (first["at"], first["first"], first["parts"]) == (
        None,
        True,
        [{"op": "same", "text": "我爱闹。"}],
    )
    assert (second["at"], second["added"], second["removed"]) == ("2026-11-03T21:30:00", 6, 0)
    assert (latest["at"], latest["chars"], latest["removed"]) == ("2026-11-08T04:40:52", 6, 4)


def event(kind, seconds_ago=0, source="personal", session="200", **data):
    return {
        "v": 1,
        "source": source,
        "type": kind,
        "timestamp": (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(),
        "session_id": session,
        "task_id": "t",
        "request_id": None,
        "data": data,
    }


def test_what_she_is_doing_is_a_label_and_never_her_words():
    now = Now()
    assert now.snapshot()["doing"] is None
    now.feed(event("task-status", status="running", text=SECRET))
    assert (now.snapshot()["doing"], now.snapshot()["where"]) == ("在看你说的话", "和你聊天")
    now.feed(event("reasoning-delta", text=SECRET))
    assert now.snapshot()["doing"] == "在想"
    now.feed(event("tool-call", session="自己待着", name="write_diary", text=""))
    seen = now.snapshot()
    assert (seen["doing"], seen["where"], seen["did"]) == ("在写日记", "自己待着", "写了日记")
    now.feed(event("tool-call", name="room_read", text="诗/藏着.md"))
    assert (now.snapshot()["doing"], now.snapshot()["did"]) == ("在房间里翻东西", "写了日记")
    now.feed(event("text-delta", source="social", text=SECRET))
    assert (now.snapshot()["doing"], now.snapshot()["where"]) == ("在说话", "群里")
    now.feed(event("task-status", status="idle"))
    seen = now.snapshot()
    assert (seen["doing"], seen["where"], seen["did"]) == (None, "", "写了日记")
    assert SECRET not in json.dumps(seen, ensure_ascii=False)
    # Something left hanging (a crash mid-turn) does not stay "doing" forever.
    stale = Now()
    stale.feed(event("reasoning-delta", seconds_ago=600, text="x"))
    assert stale.snapshot()["doing"] is None


def test_the_watcher_reads_monitor_files_and_her_status(tmp_path):
    monitor = tmp_path / "monitor"
    watcher = Watcher(monitor, tmp_path / "presence.json")
    assert watcher.poll() == {
        "doing": None,
        "where": "",
        "since": None,
        "did": None,
        "did_at": None,
        "status": {"text": "", "state": "online"},
        "watching": False,
    }
    monitor.mkdir()
    log = monitor / ("live-" + "0" * 32 + ".jsonl")
    log.write_text(
        json.dumps(event("tool-call", name="web_search", text="q"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "presence.json").write_text('{"text": "手还没松", "state": "idle"}', "utf-8")
    seen = Watcher(monitor, tmp_path / "presence.json").poll()  # History is read at start.
    assert (seen["doing"], seen["did"], seen["watching"]) == ("在上网搜东西", "上网搜了东西", True)
    assert seen["status"] == {"text": "手还没松", "state": "idle"}
    assert lines(seen) == ("在上网搜东西", "和你聊天", "「手还没松」")
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event("task-status", status="idle")) + "\n")
    watcher.poll()
    assert lines(watcher.poll())[:2] == ("发呆中", "刚刚上网搜了东西")


def test_the_widget_says_it_plainly():
    base = {"where": "", "since": None, "did": None, "did_at": None, "watching": True}
    quiet = {"text": "", "state": "online"}
    then = (datetime.now(UTC) - timedelta(minutes=12)).isoformat()
    assert lines({**base, "doing": None, "did": "写了日记", "did_at": then, "status": quiet}) == (
        "发呆中",
        "12 分钟前写了日记",
        "",
    )
    assert lines({**base, "doing": None, "watching": False, "status": quiet})[0] == (
        "看不到她在做什么"
    )
    assert ago(None) == "" and ago("不是时间") == ""
    assert ago((datetime.now(UTC) - timedelta(hours=5)).isoformat()) == "5 小时前"
    assert ago((datetime.now(UTC) - timedelta(days=3)).isoformat()) == "3 天前"


def fetch(port, path, host=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_the_page_is_served_to_this_computer_only(room, tmp_path):
    room.display("诗/秋天.md", "给你看", NOW)
    write_self(tmp_path / "self.md", "我爱闹。")
    view = RoomView(room.root, tmp_path / "memory.md", tmp_path / "monitor", tmp_path / "p.json")
    server = make_server(view, 0)
    port = server.server_address[1]
    assert server.server_address[0] == "127.0.0.1"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, headers, body = fetch(port, "/")
        assert status == 200 and "她的房间" in body.decode("utf-8")
        assert headers["Cache-Control"] == "no-store" and "default-src 'none'" in headers.get(
            "Content-Security-Policy"
        )
        status, _, body = fetch(port, "/api/room")
        assert status == 200 and SECRET not in body.decode("utf-8")
        assert json.loads(body)["showcase"][0]["note"] == "给你看"
        status, _, body = fetch(port, "/api/shown?path=%E8%AF%97/%E7%A7%8B%E5%A4%A9.md")
        assert (status, json.loads(body)) == (200, {"text": "叶子落了"})
        for closed in ("%E8%AF%97/%E8%97%8F%E7%9D%80.md", "diary/2026-11.md", "../memory.md", ""):
            assert fetch(port, f"/api/shown?path={closed}")[0] == 404
        assert json.loads(fetch(port, "/api/self")[2])[0]["parts"][0]["text"] == "我爱闹。"
        assert json.loads(fetch(port, "/api/now")[2])["watching"] is False
        assert fetch(port, "/data/room/diary/2026-11.md")[0] == 404
        # A page on another site that points a hostname at 127.0.0.1 gets nothing.
        assert fetch(port, "/api/room", host="evil.example")[0] == 403
        assert fetch(port, "/api/room", host=f"localhost:{port}")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
