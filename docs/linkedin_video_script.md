# Видео для LinkedIn — newsAnalyzer (~85 сек)

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

## Сценарий (~85 сек, ~210 слов)

Voiceover: короткие фразы, B1–B2, легко произносить.

| Тайминг | Voiceover (English, речь в кадре) | Что показывать |
|---|---|---|
| **0:00–0:05** Хук | "I get hundreds of news articles a day. Almost none of them matter to me." | Слайд 1: стена мультиязычных заголовков, текст хука поверх. |
| **0:05–0:22** Что это | "So I built a system for myself. It reads news in Polish, Russian and English — and sends me only what I actually need, in one language." | Появляется Слайд 2 (архитектура), стрелки зажигаются слева направо. |
| **0:22–0:40** Архитектура | "It works as a pipeline. First it groups articles about the same event together, using embeddings — even across different languages. Then a language model checks each event: is it relevant to my profile, is it credible, and how important is it." | Держим схему; по очереди подсвечиваем Cluster → Relevance → Verify. |
| **0:40–1:00** Живое демо | "Let me run it live. One command. You can see each stage — fetch, cluster, score, verify, summarize — and the exact tokens and cost for the run. Just a few cents." | Консоль: `uv run python -m engine run …`, таблица стадий с tokens + cost. (.env закрыт!) |
| **1:00–1:15** Результат + мультиязычность | "And here is the result in Telegram. A short factual summary, why it matters for me, a confidence level, and links to the original sources. The sources were Polish. The output is English." | Telegram: пост бота с 👍/👎/💬, ссылки на PL-источники. Подсветить «PL in → EN out». |
| **1:15–1:28** Ограничения (козырь) | "It is not perfect, and I know where. My own feedback data shows the real bottleneck is selection, not ranking. And running the strong model per user is expensive — so the next step is sharing work across similar profiles." | Слайд 3 / overlay: «Known trade-offs → next steps». |
| **1:28–1:35** Закрытие | "That's the system — I built it for myself, and I use it every day. If you're curious about any part of it, ask me in the comments." | Слайд 4: спокойный титр. Никаких «open to roles» и ссылок на репозиторий. Только сдержанное закрытие + приглашение к разговору. |

Заметки по демо:
- Живой прогон бьёт по реальному OpenAI и занимает время. Лучше записать заранее и
  смонтировать (или заранее прогнать пайплайн, а на камеру сделать `--skip fetch` и
  только доставку, чтобы пост пришёл быстро). «One command» в озвучке остаётся честным.

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
[ 8 RSS sources ]   [ Embed ]        [ Cluster ]       [ Filter ]     [ Relevance ]   [ Verify ]     [ Summarize ]      [ Telegram ]
  PL · RU · EN   →  3-large     →  same event,    → profile/    →  LLM: is it  →  credible? →  grounded facts →   👍 👎 💬
  (multilingual)    embeddings     cross-lang         keyword       for me?        hype?        + analysis,
                                   (cosine 0.82)      rules                                     your language
```

- 3 цветовые группы: вход (мультиязычные источники) / LLM-стадии (Relevance, Verify,
  Summarize — общий лейбл на слайде: "OpenAI · structured outputs") / выход (Telegram).
- Пунктирная стрелка обратной связи из Telegram (👍/👎) назад к Relevance/Ranking,
  подпись на слайде: "learns from your feedback".
- Под Embed: "cross-language clustering". Под Summarize: "PL/RU/EN in → your language out".

**Слайд 3 — Trade-offs → Next (опциональный overlay).** Две колонки, заголовки на слайде:
"What I simplified on purpose" / "Where it's going". Пункты (на слайде):
- Selection > ranking is the real bottleneck → example-based relevance gate
- gpt-4o per user is costly → share work across similar profiles

**Слайд 4 — Закрытие.** Спокойный титр в духе «делюсь работой», без призыва и без
репозитория. Голос закрывает на работе, слайд — тихая точка. Текст на слайде:
имя, "A system I built and use myself" (или короткое имя проекта),
"Questions welcome in the comments". Ничего про работу/наём и никаких ссылок на код.

---

## Ограничения — подготовка к собесу / комментам (НЕ в видео)

Подавай каждое как осознанный trade-off, который ты нашёл сам, а не как баг.

1. **Отбор — узкое место, не ранжирование.** Фидбек (n=41): taste-вектор поднял AUC
   до 0.669, но dislike-rate стоит на ~58% — переранжирование меняет порядок, не отсекает.
   → example-based relevance-gate на чистых лейблах (few-shot реальными примерами, без
   LLM-синтеза профиля — он галлюцинирует и молча ломает отбор).
2. **COGS.** `verify` + `summarize` крутят gpt-4o на каждое событие — наивно ~$10–30/юзер/мес.
   → фильтрация на уровне сегмента (стоимость масштабируется числом сегментов, не юзеров),
   тарифные лимиты, narrate-стадия для схлопывания дублей.
3. **Кросс-язычная кластеризация не измерена.** 0.82 + 3-large выбраны под кросс-язычность,
   но долю слияния одного события на UA/RU/PL ещё не валидировал. Возможна фрагментация
   near-duplicate за день. → ручная проверка N событий, порог задан заранее.
4. **Один профиль, нет онбординга.** Профиль — рукописный YAML (мой). Мульти-юзер и
   онбординг-интервью впереди. Не заявляй «self-adaptive per user» как готовое.
5. **Доставка без cap.** `dispatcher` шлёт все pending-дайджесты хронологически;
   ранжирование переупорядочивает, но не ограничивает объём. Дубли одной темы в утренней
   пачке — известная проблема (narrate-стадия не построена).
6. **Батч, не realtime.** Последовательный прогон по cron, 8 стадий; не стриминг. Демо вручную.
7. **Эмбеддинг усечён** до ~600 симв (заголовок + лид) ради стоимости/плотности кластеров —
   теряется сигнал из тела статьи. Осознанный trade-off.
8. **Тесты (~101) в основном юнит**, mypy покрывает только `engine` + `delivery` (audit исключён).
9. **Обработка ошибок стадий:** прогон продолжается при падении стадии (собирает ошибку);
   `stop_on_error` опционален — сознательный выбор «частичный дайджест лучше, чем ноль».
