# Live Monitor

一个只读的本地终端观察器：实时显示群聊决策（想了什么、说了什么、发没发表情、要不要私信）、
私聊里的思考流、工具调用和结果、以及出错信息。它不能控制 bot，关掉也不影响 bot。

## 开启

在 `.env` 里打开事件采集，然后重启 bot：

```dotenv
MONITOR_ENABLED=true
MONITOR_DIRECTORY=data/monitor
```

bot 会把事件写成 `data/monitor/live-*.jsonl`（已脱敏：配置里的 token 和 key 会被替换）。

## 查看

另开一个终端：

```powershell
uv run dfy-monitor --history 20
# 需要排查时再看任务/模型状态、token 用量和数据库快照
uv run dfy-monitor --verbose --show-db --history 20
# 不要颜色（重定向到文件时也会自动变成纯文本）
uv run dfy-monitor --no-color --once --history 20
```

| 参数 | 说明 |
| --- | --- |
| `--history N` | 先回看最近 N 个事件（0–200，默认 0 只看新的） |
| `--once` | 显示一次就退出 |
| `--verbose` | 另外显示任务/模型状态、token 用量和决策详情 |
| `--show-db` | 另外显示只读的 SQLite 快照；`--rows` 控制每表行数（1–10） |
| `--interval` | 读取事件文件的间隔，0.2–10 秒 |
| `--refresh-rate` | 显示刷新上限，1–20 次/秒 |
| `--no-color` | 不用颜色 |

颜色：任务状态蓝色，思考灰色，工具调用黄色，回答和成功的工具结果绿色，错误红色。
模型文字原样显示（只做脱敏和控制字符过滤），不会被当成 Rich markup 解析。

`scripts/monitor_demo.py` 用合成数据演示显示效果，不启动 bot：

```powershell
uv run python scripts/monitor_demo.py --html data/monitor-demo.html
```
