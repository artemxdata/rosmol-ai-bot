# Аудит точек роста конверсии

**Дата:** 15 июля 2026

**Режим:** read-only аудит; код, routing, prompts, thresholds и KB не изменялись

**Базовый runtime:** code RC `eea1972`, server handoff `e56894e`, `knowledge_base = 2186`

**Решение release gate:** `LIMITED GO` для ограниченного операторского теста

> **Статус после аудита.** Пользователь явно снял прежний freeze и разрешил срочный
> pre-operator correction cycle. Этот документ остаётся снимком исходных findings; фактический
> статус нового кандидата фиксируется в `docs/CURRENT_STATE.md`. В новом цикле закрыты
> подтверждённые cross-event/link defects, lifecycle/guard/cache ошибки, ticket telemetry,
> HDE ordering/dedup, Docker privacy boundary, HMAC и retention fail-safe. Старый `LIMITED GO`
> больше не действует до нового server-local gate.

## Короткий вывод

Конверсию можно заметно улучшить без переписывания проекта. Рабочая planning hypothesis первого
контролируемого цикла после операторского теста — прибавить `5–10` процентных пунктов к
first-turn closure на сложных answerable информационных запросах. Это не прогноз: сначала слабые
категории нужно разделить на KB/routing defects, персональные статусы, реальные технические сбои
и другие justified escalations. Текущий проект ещё не считает закрытие полного HDE-тикета и не
имеет свежего sealed holdout для текущего RC.

Самый большой резерв находится в четырёх местах:

1. успешное завершение диалогов после уточнения;
2. подтверждённый контент по ФГАИС, навигации и техподдержке;
3. правильная привязка события, темы и актуальности источника;
4. надёжность HDE-контекста, доставки и измерения результата тикета.

Нельзя повышать метрику за счёт неподтверждённых ответов. Целевые ограничения остаются:
unsupported claims — `0`, safety-regression — `0`, justified escalation не считается ошибкой.

## Что проверено

Read-only аудит охватил:

- все `2186` записей `data/knowledge_base_seed.json` и их metadata;
- Yonote/XLSX/DOCX происхождение, taxonomy, aliases, ссылки, даты, дубли и условную логику;
- FastAPI/HDE path, LangGraph nodes, retrieval, rerank, generation, verify/respond, cache и
  session memory;
- миграции и схемы PostgreSQL, Redis session path, Qdrant KB/cache path;
- versioned unit/eval-наборы, приватные агрегированные отчёты и release gate;
- корневые книги `Тесты бота Росмола.xlsx`, `Сложные_запросы_июнь.xlsx`,
  `NLU - бот Росмолодёжь. Общий документ.xlsx`, `Новый бот Росмол .xlsx`,
  `June2026_depersonalized.xlsx`, `2026_Full_depersonalized.xlsx`;
- все 48 страниц `Ошибки бота.pdf`.

Сырые сообщения и персональные данные не включались в этот документ. PostgreSQL оценивался по
локальной схеме/БД, приватным обезличенным артефактам и предоставленным server-агрегатам; прямой
SSH-аудит server PostgreSQL в этом цикле не выполнялся. Полный текст production-обращений
намеренно не выгружался.

Текущий код проходит технические проверки:

- `ruff check .` — успешно;
- `pytest` — `1029 passed`;
- `scripts/index_kb.py --validate-only` — `2186` валидных записей.

Последний пункт означает только структурную валидность seed. Текущий validator не обнаруживает
смысловую ошибку события, просроченный факт, конфликтный дубль или нерабочую ссылку.

## Честная базовая линия

Сейчас нет одной доказанной цифры «текущей production-конверсии». Наборы имеют разные срезы и
были прогнаны на разных итерациях:

Воспроизводимые агрегаты зафиксированы в `docs/complex_request_quality_baseline.md`,
`docs/operator_golden_calibration.md`, `reports/june_complex_full_conversion_summary.md` и
ignored/private `data/private/operator_qa/analysis/operator_full_june_final_audit.md`. Прогоны
478/3295 датированы 9–13 июля и предшествуют RC/обновлению Yonote от 14 июля.

| Набор | Прямой ответ | Уточнение | Оператор | Ограничение |
| --- | ---: | ---: | ---: | --- |
| 478 сложных июньских запросов | 46,7% | 29,3% | 24,1% | строки, не полные тикеты; прогон до текущего RC |
| полный июньский массив, 3295 текстовых строк | 45,1% | 41,9% | 12,7% | почти все строки изолированы; прогон до текущего RC |
| Calibration 200 | 68,5% | 27,5% | 4,0% | использовался для настройки |
| pre-pilot suite | release gate зелёный | — | — | regression/calibration, не независимая конверсия |

В полном июньском массиве direct rate по категориям:

- форумы — `74,7%`;
- гранты — `75,1%`;
- техподдержка — `43,7%`;
- ФГАИС — `35,7%`;
- прочее — `25,5%`;
- навигация — `22,2%`.

Это показывает, где искать ближайший резерв, но не доказывает его размер. Низкий direct rate
может объясняться не только KB/routing gaps, но и высокой долей персональных статусов, реальных
сбоев и других justified escalations. Диапазон `5–10` п.п. остаётся planning hypothesis до
ручной классификации и проверки на свежих тикетах.

Текущий `Holdout 100` больше нельзя считать sealed-проверкой: все 100 его формулировок уже
участвовали в полном прогоне 3295 строк, а 22 вошли в synthetic follow-up. В нём только четыре
темы, нет техподдержки, expected chunks не размечены, записи остаются candidate. Для следующего
решения нужен новый свежий holdout операторов, который не запускается до фиксации RC.

Сценарии, где после уточнения закрывается `30%`, `50%` или `70%` соответствующих обращений,
дают row-level surrogate примерно `57,6%`, `66,0%` и `74,4%`. Это иллюстрация чувствительности,
не прогноз и не ticket-level conversion.

### Качество содержания ответа

Малое число `cited_sources` само по себе нормально: бот должен использовать минимальный набор
точных источников, а не перечислять всю KB. Риск в другом — текущие исторические оценки часто
проверяют наличие citation, но не полноту ответа и не правильный source ID:

- golden subset дал `394/577` (`68,3%`) direct answers с citation, но expected chunks в нём не
  размечены;
- среди 213 исторических direct answers с reference facts средняя эвристическая полнота фактов
  составила `40,4%`, а порог 60% прошли `79/213`;
- в историческом full run 25 direct answers не имели citation;
- synthetic follow-up `297/297` проверяет механизм памяти на искусственно удобном втором ходе,
  но не доказывает реальную multi-turn conversion.

Эти результаты получены до текущего RC и используются как направление аудита, а не как оценка
его фактической точности.

Не являются доказательством общей конверсии: smoke `16/16`, зелёный pre-pilot regression gate,
containment `87,3%` с незавершёнными уточнениями, synthetic follow-up `297/297`, одно наличие
citation `394/577`, настроенный Calibration 200, скомпрометированный Holdout 100, структурная
валидация 2186 записей, смешанные local eval traces и ручной smoke из нескольких сообщений.

## Корневые материалы с ошибками

### `Тесты бота Росмола.xlsx`

- 117 первичных вопросов и 29 продолжений, всего 146 ходов;
- 144 ручные оценки, средняя оценка `3,22/5`;
- `44/144` (`30,6%`) получили 1–2;
- 28 формулировок имеют точное текстовое совпадение с versioned adversarial cases: это `28/117`
  primary или `28/146` (`19,2%`) всех ходов;
- formal manifest `строка -> case ID -> expectation -> split` отсутствует для всех 146 ходов, а
  у 118 нет даже точного текстового совпадения.

Часть формулировок покрыта похожими тестами, но без manifest это нельзя считать доказанным
покрытием исходной книги.

### `Сложные_запросы_июнь.xlsx`

- 478 сложных обращений;
- прямой grounded-ответ `223/478` (`46,7%`);
- уточнение `140/478` (`29,3%`);
- оператор `115/478` (`24,1%`);
- versioned June regression содержит 11 связанных парафраз, точных совпадений — 0;
- ни один исходный вопрос не входит в Holdout 100.

### `NLU - бот Росмолодёжь. Общий документ.xlsx`

- 91 строка результатов, 88 описаний проблем, только 37 описанных действий;
- колонка повторной проверки не заполнена;
- 67 строк backlog, но статус есть только у 11;
- четыре листа содержат кандидаты новой семантики по грантам, форумам, техподдержке и прочему;
- точных совпадений 91 строки с versioned eval JSON не найдено.

Есть отдельные производные regression-кейсы, отмеченные как закрытые, но отсутствует единый
row-level manifest. Поэтому книга остаётся backlog, а не доказательством текущего качества.

### `Ошибки бота.pdf`

На 48 страницах собраны примерно 55 сценариев старого бота. Повторяющиеся классы:

- неответ на основной вопрос или общий нерелевантный текст;
- потеря события и контекста следующего сообщения;
- одинаковый ответ на разные вопросы;
- смешение профиля, заявки, кеша/браузера, документов, отчёта и грантов;
- неполный multi-intent ответ;
- устаревшие даты и условия;
- длинные или сломанные ссылки, плохое форматирование;
- большая задержка или отсутствие ответа;
- ложная передача оператору на коротких/разговорных формулировках.

Многие классы уже имеют regression-защиту, но полного provenance PDF-caption -> case ID нет.

### `Новый бот Росмол .xlsx`

Это legacy-источник части XLSX seed, а не актуальная истина выше Yonote. В опубликованном seed
обнаружены пять подтверждённых cross-event ошибок. Четыре однотипные записи под событиями
«Арктика. Лёд тронулся», «Добрино», «ШУМ» и «ГосСтарт» содержат текст про изменение заявки на
«Ростов»:

- `xlsx_category_r0496_vnesti_izmeneniya_v_zayavku`;
- `xlsx_category_r0527_vnesti_izmeneniya_v_zayavku`;
- `xlsx_category_r0556_vnesti_izmeneniya_v_zayavku`;
- `xlsx_category_r0587_vnesti_izmeneniya_v_zayavku`.

Отдельно `xlsx_category_r0570_usloviya_i_sroki_uchastiya_granty` имеет metadata «ШУМ», но текст
и ссылка относятся к «Добрино». Это не доказывает, что найден весь cross-event backlog.

Это известный P0 correctness risk. Он не исправляется сейчас только из-за прямого условия freeze
«до получения результатов тестов код, routing и KB не менять». `LIMITED GO` поэтому относится
только к контролируемому внутреннему тестированию операторами, а для широкого трафика эти дефекты
являются блокерами. После снятия freeze каждый дефект исправляется отдельно с regression-тестом;
пользователь может отдельно разрешить срочный pre-test correction cycle.

### Полные июньские/годовые выгрузки

`June2026_depersonalized.xlsx` содержит 6095 строк данных, `2026_Full_depersonalized.xlsx` —
9315. Несмотря на название, в книгах остаются прямые идентификаторы и персоноподобные поля.
Файлы игнорируются Git, но их размещение в корне противоречит проектному правилу
`data/private/`. После freeze их нужно перенести в private-boundary и повторно проверить
санитизацию. До этого их нельзя публиковать или передавать как обезличенный датасет.

## Аудит 2186 чанков

### Состав

| Показатель | Значение |
| --- | ---: |
| Всего published | 2186 |
| Yonote | 1436 (65,7%) |
| XLSX | 714 (32,7%) |
| DOCX | 36 (1,6%) |
| Форумные чанки | 1824 (83,4%) |
| Гранты | 247 (11,3%) |
| ФГАИС | 57 (2,6%) |
| General + tech + navigation | 58 (2,7%) |

Исторический июньский поток значительно сильнее смещён к ФГАИС, прочему, навигации и
техподдержке, чем KB. Поэтому массовое добавление ещё большего числа форумных чанков не является
первым рычагом роста.

### Подтверждённые дефекты и сигналы для review

- 85 групп одинакового нормализованного текста, 234 записи, 149 повторных записей сверх первой;
  это не означает, что все можно удалить — разные forum metadata могут быть нужны фильтрации;
- 62 duplicate-группы имеют разные форумы, 14 пересекают разные source types;
- 1004 topic-slugs, из них 829 (`82,6%`) встречаются один раз; singleton сам по себе допустим,
  но такое распределение является сигналом фрагментации taxonomy;
- 563 чанка помечены эвристикой `has_conditional_logic=true`, но `conditions_summary` не заполнен
  ни у одного;
- `valid_from`/`valid_to` не заполнены ни у одного чанка;
- по состоянию на 15 июля 2026 года из 35 извлечённых registration deadlines 24 уже прошли;
- ещё 34 эвристических кандидата `до/по + дата` не получили structured deadline; не все эти
  даты обязательно являются сроком регистрации;
- у 750 записей отсутствует `source_url`;
- 150 Yonote-чанков короче 80 символов, у 96 после первого заголовка меньше 20 символов тела;
  длина сама по себе не доказывает плохое качество;
- 304 Yonote-чанка попали под эвристику общих/generated topics: «Описание», «Регистрация»,
  «Документы» и подобные. Это очередь на review, а не утверждение, что все они бесполезны.

### Потеря структуры Yonote

В 48 группах одного документа повторяется одинаковый topic-заголовок. При чанкинге часто
теряется parent section: роль участника/волонтёра, название смены, тип заявки или стадия проекта.
Retrieval видит одинаковое `registraciya`, хотя условия и ссылки разные. Исправление требует
сохранения `parent_section`, роли, смены и типа заявки и отдельной контролируемой переиндексации.

### Taxonomy и aliases

- `TOPIC_EQUIVALENCE_GROUPS` независимо продублирован в
  `src/graph/nodes/rerank.py:58` и `src/graph/nodes/generate.py:1277` и расходится; это drift risk,
  а не автоматически дефект, потому что relevance и coverage могут иметь разные правила;
- aliases `грант`/`гранты` слишком широко ведут к «Грантам для физических лиц»;
- `Гранты 1 сезон` не канонизируется в актуальное Yonote-название;
- registry-label `Полюс` не связывается с Yonote-label `молодых учёных «Полюс»`; считать их одним
  событием можно только после продуктовой проверки;
- после текущей канонизации 71 distinct output отсутствует среди 36 curated registry names и
  требует ручной классификации, а не массового auto-merge.

### Ссылки

Подтверждены:

- опечатка `events.myrosmol.rru` в
  `xlsx_category_r0275_podacha_zayavki_na_proekt`;
- malformed Markdown в тексте и top-level `links` записи
  `xlsx_category_r0008_zapolnit_shablon_proekta`;
- три ссылки на staging storage:
  `yonote_api_golwpprpwc_s0006_polozhenie`,
  `yonote_api_bhjm352dd6_s0009_polozhenie_o_molodezhnom_dne_pmef`,
  `yonote_api_4tojorksoq_s0011_polozhenie`;
- 13 незаполненных placeholders `VK TG`;
- 29 значений top-level `links` без `http(s)` scheme в 24 чанках.

Остальные ссылки не объявляются рабочими или нерабочими без отдельного rate-limited link check.

## Архитектурные точки роста

### P0. Не измеряется закрытие полного тикета

`request_traces` и ops-report считают отдельные сообщения и эскалации. В них нет итогового
статуса HDE-тикета, подключения оператора, факта доставки и оценки ответа. Partial response может
остаться non-escalated, поэтому `1 - escalation_rate` не является конверсией.

Трассы также не содержат обязательные `eval_run_id`, git commit, KB fingerprint и split. В одной
PostgreSQL смешаны разные локальные eval-прогоны, поэтому агрегат по всем строкам не является
production-метрикой. Набор `EXPECTED_ESCALATION_REASONS` в `src/ops/reports.py` не совпадает со
всеми фактическими причинами, из-за чего dashboard дополнительно искажает `quality_issue_rate`.

Нужны first-class outcomes:

- `closed_first_turn`;
- `closed_multi_turn`;
- `clarification_open`;
- `correct_escalation`;
- `false_containment`;
- `wrong_answer_or_source`;
- `not_delivered`.

### P1. HDE не гарантирует порядок, идемпотентность и доставку

`src/main.py:462` отвечает webhook-у до завершения in-process `BackgroundTasks`. Upstream event
ID не сохраняется, inbox/outbox и retry отсутствуют. `SessionManager` делает Redis
read/modify/write без CAS или per-ticket lock. Два быстрых сообщения одного тикета могут
прочитать старую сессию и отправить ответы не по порядку. Повтор webhook может создать повторный
ответ; 429 может оставить успешный trace без фактической доставки.

Минимальный post-freeze пакет: upstream message ID, inbox uniqueness, per-ticket Redis lock,
outbox/retry и delivery telemetry.

### P1. Forum fast path может обходить cross-encoder

`src/graph/nodes/rerank.py` разрешает source-only fast path до реального reranker, когда найден
чанк нужного форума. Правильный форум ещё не означает правильную тему. Перед изменением runtime
нужен shadow cross-encoder experiment на свежем holdout и сравнение expected chunk hit, полноты
ответа и p95.

### P1. Rule engine фрагментирован

Deterministic analyzer обычно завершает классификацию до LLM fallback. Большое число substring
rules и дублированных `_asks_*` predicates в analyze/rerank/generate увеличивает риск, что новая
разговорная формулировка будет классифицирована уверенно, но неверно. Нужен shadow structured
analyzer и metamorphic paraphrase tests, а не новый `if` под каждую строку.

### P1. Ответ может измениться после verifier

Последовательность `generate -> verify -> respond` позволяет foreign/event/temporal guards в
`respond` заменить уже проверенный текст. Citation при этом может относиться к предыдущему
ответу. Guard нужно выполнять до verifier или повторно верифицировать финальный текст вместе с
реальным source ID.

Отдельный blind spot находится в `src/graph/nodes/verify.py:1022`: при высокой reranker
confidence и official citation LLM judge может быть пропущен. Reranker score доказывает
релевантность источника, но не entailment каждого числа, URL или даты в сгенерированном ответе.
Нужен regression, где официальный source процитирован, но добавленная неверная дата или ссылка
обязательно отклоняется.

### P1. Semantic cache хранит только строку ответа

Cache payload не сохраняет citations, analysis, topics и disposition. Cache hit обходит graph,
а partial/clarification сейчас могут кэшироваться как обычный успех. Miss и hit должны возвращать
одинаковый structured envelope либо partial/clarification нельзя кэшировать.

### P1. Multi-turn policy не покрывает важные переходы

Нужны regressions:

- `техпроблема -> шаги -> «не помогло»` приводит к repeated support failure;
- после перечисления нескольких событий местоименный follow-up уточняет, а не выбирает событие
  из собственного ответа бота;
- смена темы использует последний явный user anchor;
- уточнение завершается ответом и считается одним закрытым тикетом.

### P1. Yonote исключён из keyword rescue

Основной hybrid retrieval видит Yonote, но keyword rescue ограничен другими source types. При
редком имени, ссылке или точной формулировке rescue не может вернуть 65,7% KB. Сначала нужно
добавить Yonote только в shadow candidates и измерить recall/latency.

### P1. Latency — существенный UX/SLO-риск

На историческом полном июньском прогоне:

- p50 — `304 ms`;
- p95 — `23,9 s`;
- p99 — `48,9 s`;
- 574 ответа дольше 10 секунд;
- 123 дольше 30 секунд;
- 15 дольше минуты.

Calibration 200 имел p95 `51,63 s`; в ручном HDE smoke были grounded-ответы по 17–18 секунд.
Причинная связь с уходом пользователя пока не измеряется. Это сильная гипотеза, которую нужно
проверить корреляцией latency с повторным сообщением, незакрытием тикета и подключением оператора.
Оптимизацию проводить по stage timings и отдельно для source-only, reranker и Max, не снижая
groundedness.

### P2. Остальной долг

- multi-forum decomposition может декартово размножить аспекты между событиями;
- deterministic source response иногда возвращает почти весь длинный чанк;
- retriever учитывает `valid_to`, но не проверяет `valid_from <= today`; lifecycle-пакет должен
  исправить и metadata, и runtime filter с regression на будущий, ещё не действующий чанк;
- HDE rate-limit settings из `.env.example` не проброшены в ML compose;
- `docs/architecture.md` содержит исторические fixed turn limit, старые thresholds и React/CI
  планы, расходящиеся с кодом и `DECISIONS.md`;
- `eval/check_regression.py` молча завершается успешно без `eval/metrics.json`, а
  `data/golden_set.json` пуст: текущий GitHub eval workflow фактически не является quality gate.

## Privacy и упаковка Docker

Это отдельный P0 до публикации или передачи image:

- `.dockerignore` не исключает `reports/`, `outputs/`, `tmp/`, `.pytest-tmp-*`, `.idea/` и
  `*.pdf`;
- `.env` уже исключён, но 19 tracked reports гарантированно попадают в clean-clone image при
  текущем `COPY . .` и требуют отдельного privacy review;
- `Dockerfile` выполняет `COPY . .`;
- в текущем локальном image read-only проверка обнаружила сотни local reports и корневой PDF;
- чистый server clone может не иметь ignored-файлов, но локально собранный image уже не имеет
  гарантированной privacy boundary;
- Compose монтирует весь `./data` в app-контейнер, поэтому runtime видит и `data/private`, хотя
  приватные файлы не входят в image. Нужность такого широкого доступа должна быть проверена по
  принципу наименьших привилегий.

До исправления нельзя публиковать или экспортировать локально собранный image. Post-freeze
нужно исключить local/private artifacts по умолчанию, проверить image manifest и явно копировать
или монтировать только необходимые runtime-артефакты. В частности, сначала нужно учесть
`ADMIN_QUALITY_REPORT_PATH`: админке может требоваться один проверенный агрегированный report, но
не весь каталог. `data/private` не должен становиться частью image, а его runtime mount нужно
сузить, если приложению не требуются эти файлы.

Дополнительный privacy/retention backlog: `src/session/memory.py` использует unsalted SHA-256 от
`channel:user_id`, поэтому предсказуемые внешние ID потенциально перебираемы. Скрипт очистки
памяти есть, но production cron/retention для `conversation_turns` и `request_traces` в аудите не
подтверждён. После freeze нужны keyed HMAC/pseudonymization policy и проверяемый retention job.

## Что уже хорошо

- ответ без published source запрещён;
- safety и explicit operator request обрабатываются до LLM;
- Yonote подключён read-only и имеет приоритет над legacy sources;
- forum/category/topic metadata и fail-closed routing снижают смешение событий;
- trace хранит retrieval, rerank, citations, latency и LLM cost;
- persistent masked memory поддерживает длинные диалоги без fixed turn limit;
- текущий release gate широк по известным safety/routing regressions и зелёный на них, но не
  доказывает полноту фактов или обобщение на новые обращения;
- операторские ответы не индексируются автоматически как факты.

Проект не нужно переписывать. Нужно укрепить measurement, channel reliability и source
governance, затем точечно улучшать retrieval/answering по частотным кластерам.

## План действий

### Во время операторского теста — без изменений runtime

1. Оставить замороженными код, routing, prompts, thresholds и KB.
2. Не использовать в админке `Save`, `Reindex` и `Apply to KB`.
3. Не публиковать и не экспортировать локально собранный Docker image.
4. Для каждого полного тикета фиксировать outcome из P0-списка, cited sources, unsupported claim,
   latency и delivery.
5. Собирать ошибки пакетно, не исправлять их по одной.
6. До просмотра кейсов определить cohort/denominator, размер выборки и детерминированный hash
   assignment в calibration/holdout; ограничить доступ к holdout и не открывать его при настройке.

### Первый post-freeze цикл

1. До quality-правок ввести ticket outcomes, delivery telemetry, `eval_run_id`, commit, KB
   fingerprint и новый provenance manifest; зафиксировать frozen baseline.
2. Отдельным security-пакетом исправить Docker build context и private file placement.
3. Отдельным срочным correctness-пакетом исправить пять известных cross-event чанков, ссылки и
   точечные aliases; на каждый дефект добавить regression.
4. По частоте операторских ошибок подготовить source-approved Yonote/KB gaps для ФГАИС,
   навигации и техподдержки.
5. Реализовать ticket ordering/idempotency/delivery telemetry.
6. Запустить shadow experiments reranker/analyzer/Yonote keyword rescue; не включать победителя
   без измеримого выигрыша.

Изменения KB, routing, cache и prompts не смешивать в одном эксперименте.

### Acceptance следующего RC

- `ruff`, полный `pytest`, структурная KB validation и воспроизводимый semantic audit: event/text
  conflicts, lifecycle, ссылки, duplicates, weak-chunk review и taxonomy warnings;
- server-local calibration с тем же frozen split;
- first-turn delta измеряется paired-сравнением на одном frozen adjudicated calibration-наборе;
  продуктовая цель первого цикла — не менее `5` п.п., `10` п.п. — stretch, а не обещание;
- ticket-level baseline сначала фиксируется операторским тестом, затем новый RC сравнивается с
  ним на сопоставимом потоке или paired replay; до baseline обязательного `+5` п.п. для этой
  метрики нет;
- размер efficacy holdout заранее рассчитывается под минимальный detectable effect и confidence
  interval; `N=100` около 50% имеет примерно `±9,8` п.п. 95% CI и годится скорее для
  safety/non-inferiority, чем для доказательства uplift `5` п.п.;
- новый holdout открывается один раз после фиксации RC;
- `0` наблюдаемых unsupported claims на полностью проверенном calibration/holdout наборе;
- safety/off-topic/profanity — без regressions;
- source coverage и expected/equivalent chunk hit не ухудшаются;
- p95 не ухудшается, отдельно контролируется доля ответов >10 и >30 секунд;
- короткий HDE smoke проверяет порядок, dedup и delivery.

## Админка для команды

Стандартный безопасный доступ до появления TLS:

```bash
ssh -N -L 18088:127.0.0.1:80 root@<server-ip>
```

После открытия tunnel:

```text
http://127.0.0.1:18088/admin/kb
```

Каждый сотрудник открывает собственный SSH tunnel. `127.0.0.1` — локальный адрес его
компьютера, а не публичная общая ссылка. Порт `18088` выбран, чтобы не пересекаться с локальным
Compose на `8080`. Admin token нельзя вводить через публичный HTTP.
