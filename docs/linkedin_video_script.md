# Видео для LinkedIn — newsAnalyzer (~128 сек / ~2 мин)

Черновик сценария для видео, которым я делюсь с сообществом: показываю проект,
который построил и использую сам. Это НЕ поиск работы и НЕ питч — просто рассказ
о своей работе. Аудитория: инженеры + любопытствующие, разный тех. уровень.
Тон: инженер спокойно показывает реальную работу. Без пафоса, без buzzwords,
ничего выдуманного. Никакого «наймите меня», никаких ссылок «дайте мне работу».

Честная рамка: **система, которую я построил и использую сам — не продукт с пользователями.**

Правило языка в этом файле: **английский — только моя речь в кадре (voiceover)
и текст, который реально виден на слайдах.** Всё остальное — по-русски.

---

## ⛔ Что НЕЛЬЗЯ показывать в кадре (безопасность)

Локальный `.env` содержит **живые** секреты (реальные значения, не плейсхолдеры):
`OPENAI_API_KEY` (sk-proj…), `TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (пароль Neon),
`POSTGRES_PASSWORD`, `TELEGRAM_CHAT_ID`.

1. Никогда не открывай `.env` / `cat .env` / `printenv` / `env`. Закрой вкладку до записи.
2. Следи за трейсбэками — упавшая стадия может напечатать `DATABASE_URL`. Вырежь такой кадр.
3. Хочешь показать конфиг — показывай `.env.example` (плейсхолдеры), не `.env`.
4. `config/profiles/volodymyr.yaml` = личные данные (не секрет) — показывай осознанно.
5. Проверь `alembic.ini` на захардкоженный `sqlalchemy.url` перед показом дерева файлов.
6. `.session`-файлы (MTProto) = катастрофа при утечке (MTProto сейчас выключен).
7. Если `.env` мелькнул в кадре → ротатни ключ OpenAI и токен бота до публикации.

## 2 поправки на точность (чтобы не поймали на камеру)

- **Английский вывод:** в `volodymyr.yaml` стоит `output_language: ru`. Для англ. демо
  временно поставь `output_language: en` (или отдельный `en.yaml`) — промт прошит
  через `{{ profile.output_language }}`. Иначе бот ответит по-русски.
- **Кросс-язычная кластеризация:** заложена (`text-embedding-3-large`, эмбеддинг по
  заголовку+лиду, online nearest-centroid по косинусу 0.82 — НЕ k-means). Но точную
  долю слияния одного события на UA/RU/PL ты **ещё не мерил** (это в roadmap).
  Говори «designed to group across languages»; если спросят «насколько точно?» →
  «точную долю слияния ещё не мерил, это в списке».
- Система **пока не само-адаптивна по отбору** — taste-вектор переранжирует, но не
  отсекает. Говори «I'm building the feedback loop», не «it already adapts».

---

## Сценарий (~128 сек, ~310 слов)

Voiceover: короткие фразы, B1–B2, легко произносить.

| Тайминг | Voiceover (English, речь в кадре) | Что показывать |
|---|---|---|
| **0:00–0:05** Хук | "I get hundreds of news articles a day. Almost none of them matter to me." | Слайд 1: стена мультиязычных заголовков, текст хука поверх. |
| **0:05–0:22** Что это | "So I built a system for myself. It reads news in Polish, Russian and English — and sends me only what I actually need, in one language." | Появляется Слайд 2 (архитектура), стрелки зажигаются слева направо. |
| **0:22–0:38** Архитектура | "It works as a pipeline. It groups articles about the same event — even across languages — then merges duplicates, so one event becomes one post, not ten." | Держим схему; подсвечиваем Cluster → Consolidate. |
| **0:38–0:58** Как решает | "Then it decides what matters to me. The reference point is a profile I wrote — where I live, my citizenship, what I follow. First a cheap keyword pass drops the obvious noise. Then a small model scores each event against that profile: is it about Ukraine, Polish rules for foreigners, the border, my work? And for war news — is it a real development, or just routine shelling? Only what survives gets the expensive check and the summary." | Слайд 2b «How it decides»: слева кусок `profile.yaml` (interests + keep/drop правила, **личные поля замазаны**), справа воронка cheap → expensive. |
| **0:58–1:18** Живое демо | "Let me run it live. One command. You can see each stage — fetch, cluster, consolidate, score, verify, summarize — with the exact tokens and cost. A whole day costs a few cents, because the expensive model only ever sees what passed the filter." | Консоль: `uv run python -m engine run …`, таблица из 9 стадий с tokens + cost, `Total cost` в кадре. Строка `consolidate` видна. (.env закрыт! вывод `delivery send` в кадр НЕ давать — палит токен бота.) |
| **1:18–1:30** Результат + мультиязычность | "And here is the result in Telegram. A short factual summary, why it matters for me, a confidence level, and links to the original sources. The sources were Polish. The output is English." | Telegram: пост бота с 👍/👎/💬, ссылки на PL-источники. Подсветить «PL in → EN out». Кнопки английские (`UI_LANGUAGE=en`). |
| **1:30–1:38** Тред-обновления | "And when a story keeps developing, the updates come as a quiet reply under the first post — so I get the new details without another alert." | Telegram: раскрыть тред — под исходным постом про удар подшит реплай «🔄 Update on…» с обновлёнными деталями. Показать: это один тред, а не пять постов. |
| **1:38–1:51** Ограничения (козырь) | "It is not perfect, and I know where. My own feedback data shows the real bottleneck is selection — deciding what's worth showing — not the ranking. That's the part I'm still improving." | Слайд 3 / overlay: «Known trade-offs → next steps». |
| **1:51–2:08** Закрытие | "So that's the problem it solves: hundreds of articles a day, in three languages — and it hands me back only the few that actually affect my life. I built it for myself, and I use it every day. Thanks for watching — if you're curious about any part of it, ask me in the comments." | Слайд 4: тихий титр. Callback к хуку (проблема → решение) + благодарность. Никаких «open to roles» и ссылок на репозиторий. |

Заметки по демо:
- Живой прогон бьёт по реальному OpenAI и занимает время. Лучше записать заранее и
  смонтировать (или заранее прогнать пайплайн, а на камеру сделать `--skip fetch` и
  только доставку, чтобы пост пришёл быстро). «One command» в озвучке остаётся честным.
- Тред-обновление для демо: нужен хотя бы один многодневный сюжет с апдейтом (напр. удар
  + обновлённые потери назавтра), чтобы в телеге реально был реплай-тред. Прогони пайплайн
  два дня подряд на демо-БД заранее — иначе показывать нечего.
- Хронометраж вырос до ~100с. Хочешь в 90 — самый жирный кандидат на подрезку: озвучка
  архитектуры 0:22–0:42 (сократи перечисление проверок relevant/credible/important).

---

## Слайды

Демо-heavy + 3–4 лёгкие карточки. Слайды только там, где экран не самоочевиден.
Ниже: описание слайда — по-русски; текст, который реально виден на слайде — по-английски.

**Слайд 1 — Хук.** Тёмный фон, размытая стена заголовков PL/RU/EN, крупный текст:
> "Hundreds of articles a day. Almost none matter."

Внизу мелко — название проекта.

**Слайд 2 — Архитектура (главный).** Горизонтальный конвейер, боксы + стрелки.
Текст боксов (на слайде):

```
[ 8 RSS sources ]  [ Embed ]     [ Cluster ]     [ Consolidate ]   [ Filter ]   [ Relevance ]  [ Verify ]   [ Summarize ]    [ Telegram ]
  PL · RU · EN  →  3-large   →  same event,  →  merge dup     → profile/ →  LLM: is it →  credible? → grounded facts →  👍 👎 💬
  (multilingual)   embeddings    cross-lang       stories (LLM)     keyword     for me?       hype?        + analysis,
                                 (cosine 0.82)                      rules                                  your language
```

- 3 цветовые группы: вход (мультиязычные источники) / LLM-стадии (Consolidate, Relevance,
  Verify, Summarize — общий лейбл на слайде: "OpenAI · structured outputs") / выход (Telegram).
- Пунктирная стрелка обратной связи из Telegram (👍/👎) назад **к ранжированию на доставке**
  (не к Relevance — фидбек пока меняет только порядок, не отсекает), подпись: "feedback re-ranks delivery".
- Под Consolidate: "merge duplicate stories". Под Embed: "cross-language clustering".
  Под Summarize: "PL/RU/EN in → your language out".

**Слайд 2b — Как решает (How it decides).** Снимает ощущение абстрактности: показывает
«точку отсчёта» (профиль) и воронку отбора «дёшево → дорого». Слева на слайде — кусок
`profile.yaml` (текст как в файле): `interests`, `keyword_rules: keep_if / drop_if`.
**Личные поля `name` / `location` / `citizenship` — замазать или обрезать** (не секрет, но
незачем). Справа — воронка (текст на слайде):

```
my profile  (written by hand)
     │
 keyword rules            ← cheap, drops the obvious noise
     │
 LLM relevance vs profile ← gpt-4o-mini, keep / drop
   • Ukraine  • Poland-for-foreigners  • UA–PL  • my work  • EU status
   • war news: real development?  or routine shelling?
     │
 verify + summarize       ← expensive model, only on survivors
```

Подпись на слайде: "cheap filter first — the expensive model only sees what survives".
Честный сильный факт для комментов/собеса (не в кадр): промты `relevance_v1 → v2 → v3`
лежат в репо — рубрикатор отбора переписан **трижды** по реальным промахам, а не угадан.

**Слайд 3 — Trade-offs → Next (опциональный overlay).** Две колонки, заголовки на слайде:
"What works well" / "Where it's going". Пункты (на слайде):
- Cheap by design: cents a day — the expensive model only runs on what passes the filter
- The real bottleneck is selection, not ranking → example-based relevance gate (next)

**Слайд 4 — Закрытие.** Спокойный титр в духе «делюсь работой», без призыва и без
репозитория. Голос закольцовывает на проблему (callback к хуку) и благодарит. Текст на
слайде (по-английски):
> Hundreds of articles a day → only the few that matter.
>
> A system I built and use myself.
> Thanks for watching — questions welcome in the comments.

Внизу мелко — имя / короткое название проекта. Ничего про работу/наём и никаких ссылок на код.

---

## Ограничения — подготовка к собесу / комментам (НЕ в видео)

Подавай каждое как осознанный trade-off, который ты нашёл сам, а не как баг.

1. **Отбор — узкое место, не ранжирование.** Фидбек (n=41): taste-вектор поднял AUC
   до 0.669, но dislike-rate стоит на ~58% — переранжирование меняет порядок, не отсекает.
   → example-based relevance-gate на чистых лейблах (few-shot реальными примерами, без
   LLM-синтеза профиля — он галлюцинирует и молча ломает отбор).
2. **COGS — измерено, дёшево.** По `decisions` за 21 день: **~$0.11/день ≈ $2–3/мес** на мой
   профиль (моя старая оценка «$10–30» ошибочна — она предполагала gpt-4o на каждом событии).
   ~80% стоимости — `relevance` (gpt-4o-mini на всех ~95 событиях, $0.044/прогон) + `summarize`
   (gpt-4o, но только на прошедших отбор — единицы, $0.043/прогон); `verify` $0.013. Дорогой
   gpt-4o почти не работает, потому что дешёвый relevance-фильтр отсекает почти всё до него.
   Масштаб зависит **не от числа юзеров, а от того, сколько событий проходит отбор** (широкие
   интересы → больше summarize → дороже; $2–3 — для строгого профиля). `embed`/`cluster`/
   `consolidate` общие между юзерами и амортизируются. Рычаги для роста: сегмент-шеринг, cap на
   summarize/день.
3. **Кросс-язычная кластеризация не измерена.** 0.82 + 3-large выбраны под кросс-язычность,
   но долю слияния одного события на UA/RU/PL ещё не валидировал. Возможна фрагментация
   near-duplicate за день. → ручная проверка N событий, порог задан заранее.
4. **Один профиль, нет онбординга.** Профиль — рукописный YAML (мой). Мульти-юзер и
   онбординг-интервью впереди. Не заявляй «self-adaptive per user» как готовое.
5. **Дедуп событий — отгружен.** `consolidate`-стадия схлопывает фрагменты одного события
   внутри прогона (LLM-адъюдикация gpt-4o-mini), а на доставке многодневные апдейты
   подшиваются реплаем-тредом под исходный пост (тихо, без пуша). Остаток: тематическая
   группировка через дни (разные события одной темы) и отсутствие жёсткого cap на объём пачки.
6. **Батч, не realtime.** Последовательный прогон по cron, 9 стадий; не стриминг. Демо вручную.
7. **Эмбеддинг усечён** до ~600 симв (заголовок + лид) ради стоимости/плотности кластеров —
   теряется сигнал из тела статьи. Осознанный trade-off.
8. **Тесты (119) в основном юнит**, mypy покрывает только `engine` + `delivery` (audit исключён).
9. **Обработка ошибок стадий:** прогон продолжается при падении стадии (собирает ошибку);
   `stop_on_error` опционален — сознательный выбор «частичный дайджест лучше, чем ноль».
