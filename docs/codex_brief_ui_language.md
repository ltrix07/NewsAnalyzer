# Codex brief — Configurable UI language for Telegram controls

## Goal

For demo/recording purposes, add a config setting that renders the **interaction UI**
(inline button labels + the callback acks/prompts triggered by those buttons) in English,
so a non-Russian-speaking viewer can follow the button logic. **Digest content stays in
the profile's language** (`profile.output_language`) — this setting does NOT touch digest
text, LLM discussion/research answers, or the formatter's in-digest section headers.

New setting defaults to Russian, so current behavior is unchanged unless explicitly
switched.

## Scope (exactly these two layers)

- **(1) Button labels** — `delivery/keyboards.py`.
- **(2) Button-triggered acks / prompts / short chrome messages** — `delivery/listener/handlers.py`,
  `delivery/research.py`, `delivery/discussion.py`.

Explicitly OUT of scope (leave as-is): digest body (`delivery/formatter.py` incl. its
`Почему это важно:` / `Источники:` headers — those follow `output_language` already), the
LLM-generated discussion and research answer bodies, and any internal log/comment strings
(e.g. the comment at `delivery/listener/service.py:141`).

## Config

`engine/config.py`, add:

```python
ui_language: Literal["ru", "en"] = "ru"
```

Default `"ru"` = today's behavior, no change.

## Localization module

New pure module `delivery/strings.py` (no settings import — keep it dependency-free):

```python
UI_STRINGS: dict[str, dict[str, str]] = { ... }  # key -> {"ru": ..., "en": ...}

def t(key: str, lang: str) -> str:
    entry = UI_STRINGS[key]
    return entry.get(lang, entry["ru"])   # fall back to ru for unknown lang
```

Move every current Russian literal listed below into `UI_STRINGS[...]["ru"]` verbatim
(byte-for-byte, including emoji and the trailing `✓`/`…`), and add the `"en"` value.

### String table (key | current ru — keep verbatim | en)

Buttons (`keyboards.py`):
- `btn_like` | `👍 Интересно` | `👍 Interesting`
- `btn_dislike` | `👎 Не интересно` | `👎 Not interesting`
- `btn_off_topic` | `📌 Не моя тема` | `📌 Not my topic`
- `btn_weak_analysis` | `🛠 Слабый разбор` | `🛠 Weak analysis`
- `btn_discussion` | `💬 Обсудить` | `💬 Discuss`
- `btn_research` | `🔎 Уточнить в сети` | `🔎 Check the web`

Acks / prompts (`handlers.py`):
- `ack_like` | `Записал ✓` | `Saved ✓`
- `ack_dislike_reason_prompt` | `Почему не интересно?` | `Why not interesting?`
- `ack_reason_saved` | `Записал ✓` | `Saved ✓`  (reuse `ack_like` if you prefer one key — same text)
- `ack_discussion` | `Ок, жду вопрос` | `OK, send your question`
- `msg_ask_question` | `Задайте вопрос по этому разбору одним сообщением.` | `Ask a question about this brief in one message.`
- `msg_question_expired` | `Срок вопроса истёк — нажмите 💬 ещё раз.` | `Question expired — tap 💬 again.`
- `msg_research_expired` | `Запрос устарел, нажмите 💬 заново.` | `Request expired — tap 💬 again.`
- `ack_research_stale` | `Запрос устарел` | `Request expired`
- `ack_research_searching` | `Ищу в сети…` | `Searching the web…`

Research chrome (`research.py`):
- `research_disclaimer` | `🔎 Собрано из открытых источников, точность не гарантируется — перепроверяйте важное.` | `🔎 Gathered from open sources; accuracy not guaranteed — verify anything important.`
- `research_digest_not_found` | `Разбор недоступен, поэтому уточнить в сети не получилось.` | `This brief is unavailable, so the web lookup failed.`
- `research_daily_cap` | `Лимит уточнений в сети на сегодня исчерпан. Попробуйте завтра.` | `Daily web-lookup limit reached. Try again tomorrow.`
- `research_sources_header` | `<b>Источники:</b>` | `<b>Sources:</b>`

Discussion chrome (`discussion.py`):
- `discussion_digest_not_found` | `Не нашёл этот разбор. Возможно, он уже недоступен.` | `Couldn't find this brief. It may no longer be available.`

(EN wordings above are sensible defaults; keep them unless obviously wrong.)

## Wiring

`delivery/keyboards.py` is currently pure (no settings). Keep it pure: give each
`build_*_keyboard(...)` a `lang: str = "ru"` parameter and resolve labels via
`t("btn_...", lang)`. Preserve the existing `✅ ` selected-feedback prefix logic — prepend
`✅ ` to the already-localized label, unchanged.

Thread `lang = settings.ui_language` from every call site (all callers already have
`settings` in scope):
- `delivery/dispatcher.py` → `build_digest_keyboard(..., lang=settings.ui_language)`.
- `delivery/listener/handlers.py` → `build_digest_keyboard`, `build_dislike_reason_keyboard`
  (both the initial and the post-feedback `edit_message_reply_markup` rebuilds must use the
  same lang), and replace every literal ack/prompt with `t(key, settings.ui_language)`.
  `settings` is already a handler parameter.
- `delivery/research.py` → `build_research_keyboard(..., lang=settings.ui_language)` and its
  message literals via `t(...)`.
- `delivery/discussion.py` → not-found message via `t(...)`.

Do NOT read settings inside `keyboards.py` / `strings.py`; pass `lang` down.

If the story-threading `🔄 Обновление по теме:` header exists in `dispatcher.py` by the
time this lands, add a `thread_update_header` key too (`en`: `🔄 <b>Update on:</b> `) and
localize it. If that code isn't present, skip it — do not depend on it.

## Tests (`tests/`, follow existing delivery test style)

1. `build_digest_keyboard(1, lang="en")` yields English button texts; `lang="ru"` (and
   default) yields the current Russian ones. Same for the dislike-reason and research
   keyboards.
2. `t("btn_like", "en") == "👍 Interesting"`; `t("btn_like", "xx")` falls back to the ru
   value.
3. Handler behavior with `settings.ui_language="en"`: a `like` callback answers `Saved ✓`;
   a `dislike` callback answers `Why not interesting?` (assert the fake Telegram client's
   `answer_callback_query` text). A regression case with default `ru` still answers
   `Записал ✓` / `Почему не интересно?`.
4. Callback_data / parsing is unchanged (localization must not touch `callback_data`).

No live Telegram/OpenAI calls.

## Acceptance criteria

- `ui_language` default `"ru"` → byte-for-byte identical behavior to today.
- Setting `ui_language="en"` renders all scope-(1) and scope-(2) strings in English while
  digest bodies, LLM discussion/research answers, and formatter section headers remain in
  the profile's language.
- `callback_data` values and parsing are untouched (no functional change to feedback).
- `make lint` and `make test` pass; add the tests above.

## Note (not for implementation)

Pure demo cosmetics — no effect on selection/dedup logic. Small permanent i18n layer;
worth keeping only because the bot will be shown to a mixed-language audience.
