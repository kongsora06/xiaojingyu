# Idea: tiered memory (not built)

Proposed 2026-10-24, merged 2026-10-25 with 小鲸鱼's own idea from the DMs (sort by importance,
not only by time; start simple). Today she reads the whole `data/memory.md` before every reply,
writes to it from two places, and the 8k-char tidy step compresses away details. The idea: keep
recent days in detail, keep what matters forever, compact the rest into an archive she opens
only when asked about older things.

Three layers: who 老爹 is, who she is, who the group friends are and the moments that matter
must always be loaded; only the details of ordinary days should fade.

## Layout

```text
data/memory/
  core.md              always loaded: 老爹, 我们之间, 我是什么样的, 我自己, 群里的人和事, 做事备忘
  recent/2026-10-24.md one file per day: detailed, timed entries
  archive/2026-W43.md  one summary per past week
  archive/index.md     one line per week, always loaded
```

- **Core:** the current `memory.md` moves here as-is. Facts that stay true, plus the moments
  that matter (promises, things 老爹 said that she wants to keep). Hard size cap (about 6000
  chars): when it is over, she must choose what to merge or drop. That cap is what makes
  "important" mean something.
- **Recent:** entries like `22:46 [群里] Epric 叫我大肥鱼，我去私信老爹告状了`. Each entry is
  tagged 私聊 or 群里 so the secrets rule still applies.
- **Archive index:** a short line per week (`10-13~10-19：老爹考试周、Epric 生日、换了头像`), so
  she knows *that* something happened and roughly when, and knows to look it up.

## Importance

- When writing a journal entry she may mark it ★: "this might matter". A mark is only a
  candidate, not a final score.
- The real choice happens at compaction, looking back a week or two later: which ★ entries
  still matter, and did anything unmarked turn out to matter (it came up again, 老爹
  referred back to it)?
- Why not score at write time: a model scoring in the moment rates nearly every tender moment
  as top importance, and the always-loaded layer bloats. Hindsight is a better judge, and it is
  how people remember too.

## Writing: one path

- Today she writes memory twice: by `edit_file` in the middle of a chat, and again in the
  reflection after `AGENT_REFLECT_SECONDS` of quiet. That causes the duplicate entries she
  noticed herself on 10-24.
- New rule: only the memory code writes memory files.
  - DMs: reflection writes the day's journal (with ★ marks). Mid-chat she can use a small
    `remember(note, important=False)` tool that appends through the same code, deduplicated.
    The rules tell her not to `edit_file` the memory files.
  - Group: `remember` goes through the same code into the journal; `我：…` notes and facts
    about people go to core.
  - Compaction is the only thing that rewrites core and the archive.

## Reading

- Every reply: core + last 14 days of journal + archive index. The group side gets the same,
  minus 做事备忘.
- Recall in DMs: a `recall(question)` tool.
- Recall in the group: a `recall` field in the plan, same pattern as `search` (one look-up,
  then answer).
- Recall searches with a cheap DeepSeek call that reads the whole archive and returns the
  matching parts. Plain keyword search misses too much in Chinese (no word boundaries,
  varying nicknames). The archive stays small enough for one call for years.
- Going deeper: recall can also read the raw chat logs already in `data/personal.sqlite3` and
  `data/bot.sqlite3`, for "那天具体说了啥".

## Compaction

- A daily check (about 05:00, or at startup if missed).
- Each week whose days are all older than 14 days becomes one archive file and one index line.
- Entries that still matter (see Importance) go into core close to word for word, not
  summarised. Everything else is summarised into the archive.
- If core is over its cap, the same pass merges or drops its least important lines and says
  what it dropped (into the archive, so nothing is lost outright).
- Day files move to `recent/old/`, never deleted; back up before writing.

## Daily new conversation

The real token cost is not the memory file (a few thousand chars, prefix-cached) but the DM
conversation: with `AGENT_COMPACT_TOKENS=0` it keeps growing, and every message resends the
whole history. With a reliable 14-day journal, starting a fresh DM conversation once a day
loses little.

- At the first owner message after the day boundary (e.g. 05:00, or after a long idle), start a
  new conversation automatically, as `!new` does, after reflection has written that day's journal.
- The new conversation loads fresh memory, which also solves the stale frozen memory block.
- Setting to turn it off (`AGENT_DAILY_NEW=false`); `!new` still works by hand.

## Commands

- `!memory`: core plus this week.
- `!archive`: the index.
- `!consolidate`: run compaction now.

## Build order

1. Add the `data/memory/` layout; move `memory.md` into `core.md`.
2. One write path: the memory code, the `remember` tool, ★ marks; stop `edit_file` on memory.
3. Assemble the prompt for both sides (core, 14 days, index).
4. Add compaction: archive, promotion by hindsight, the core size cap.
5. Add the daily new conversation.
6. Add `recall` (DM tool, group plan field) and the raw-log lookup.
7. Commands, docs, tests (temporary directories only, never real `data/`).

Start simple, as she suggested: ★ marks plus time-based fading. Run it for a while, then tune.

## Defaults

- 14-day window.
- Core capped at about 6000 chars.
- One brain: the group sees DM journal entries but keeps them secret, as now.
- Raw day files are kept forever.
- Daily new conversation on.

## Checking it works

- After each compaction, show 老爹 what moved into core, what was archived and what was dropped
  (the monitor, or a short DM), so mistakes are visible early.
- Spot check: ask her about something from two or three weeks ago and see whether `recall`
  finds it.

## Trade-offs

- She only remembers old things when something prompts a recall. If she doesn't realise she
  should look, she'll say she doesn't remember. That's fairly human, and the index keeps it rare.
- Choosing importance in hindsight means a moment can feel unremembered for up to two weeks,
  though it stays in the journal, word for word, the whole time.
