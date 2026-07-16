# Готовность к презентации

Статус: **STALE / NO GO / SECURITY HOLD**.

> Отчёт сгенерирован 7 июля и сохраняется только как исторический quality artifact. Старая VM и
> админка выключены после P0-инцидента 15 июля. Не открывать старый URL, не проводить live demo и
> не регенерировать этот отчёт до infrastructure parameterization, clean rebuild, correction cycle
> по feedback Наты и нового финального gate.

Это короткий контрольный лист перед демонстрацией руководству. Он не заменяет большой отчёт качества, а собирает главный вердикт из уже прогнанных gate-ов.

## Главные цифры

- Final acceptance: `True`.
- Presentation quality: `178/178` (100.0%).
- Pre-demo smoke: `12/12` (100.0%), trace required: `True`.
- Оценочная стоимость LLM в большом presentation quality report: `171.964022 RUB`.
- Оценочная стоимость LLM в быстром smoke: `0.000000 RUB`.

## Gates

- OK `final_acceptance_passed`: Полный локальный acceptance gate должен быть зелёным.
- OK `presentation_quality_100_percent`: Pass rate презентационного отчёта качества должен быть >= 100%.
- OK `pre_demo_smoke_100_percent`: Быстрый pre-demo smoke должен иметь pass rate >= 100%.
- OK `trace_required_and_available`: Pre-demo smoke должен проверять trace, источники, эскалации и PII masking.

## Что показывать

- Админ-панель: недоступна; новый HTTPS URL появится в `docs/CURRENT_STATE.md` после handoff.
- Runbook показа: `docs/presentation_demo_runbook.md`.
- Пакет для руководства: `docs/leadership_demo_pack.md`.
- Большой отчёт качества: `reports/presentation_quality/presentation_quality_report.md`.
- Пакет живых примеров: `reports/presentation_quality/demo_pack.md`.
- Быстрый smoke: `reports/presentation_quality/pre_demo_smoke_latest/pre_demo_smoke.md`.

## Границы демо

Входит в демонстрацию:
- RAG-бот на опубликованной базе знаний и read-only Yonote-источниках.
- Админ-панель для поиска, правки чанков, reindex, validation, quality и ops-отчётов.
- FastAPI /ask API, trace, cited sources, PII masking и safety escalation.
- Тестовый HDE/VK-контур только для коротких smoke-проверок.
- Процесс еженедельной продуктовой калибровки по деперсонализированным тикетам.

Не обещаем как готовое к понедельнику:
- Автономная запись, удаление или редактирование документов внутри Yonote.
- Массовое production-включение HDE. У HDE общий лимит 300 RPM на систему.
- Рассылки и маркетинговые кампании. Это отдельный модуль consent/opt-out/audit.
- Обещание 100% закрытия всех будущих новых вопросов без операторов.

## Команды перед показом

```powershell
.venv\Scripts\python.exe scripts\run_pre_demo_smoke.py --target http://localhost:8001/ask --output-dir reports/presentation_quality/pre_demo_smoke_latest --fail-under 1.0
.venv\Scripts\python.exe scripts\build_presentation_readiness.py
```

Если нужно полностью перепроверить релиз локально:

```powershell
.venv\Scripts\python.exe scripts\run_acceptance.py --target http://localhost:8001/ask --max-llm-cost-rub 80
```
