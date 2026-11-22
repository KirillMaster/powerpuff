# Handoff: рефакторинг powerpuff (расход + скорость), 2026-11-22

## Цель следующей сессии
1. Отрефакторить плагин powerpuff (`C:/pet/powerpuff`, GitHub KirillMaster/powerpuff, v0.2.0) по списку ниже.
2. Обновить интеграции: `C:/pet/vetal_project` (ppar), `C:/pet/dasha_b_rabota_bot`, `C:/pet/tictactoe` (у всех есть `powerpuff.yaml`).
3. Перезапустить vetal_project с места остановки.

## Данные (ночь 21→22.11, vetal_project)
- Полный отчёт: `C:/Work/notes/claude-usage-vetal-powerpuff-2026-11-22.md`. Langfuse: http://localhost:3000/project/claude-code (поднят).
- 12 ч: 3 640 вызовов Opus, 618M токенов чтения из кэша, ≈$544 по API (~34% недельного лимита). 81 запуск, 64 юнита в `queue.yaml`.
- Статус ~11:08: 39 merged, 4 running (U33, U39, U42, U53), 1 failed (U01), 20 не начаты (U10b U13b U36 U40 U41 U43–U47 U49 U51 U52 U55–U60 U62).
- U07 ждал ответа на Q1 (`.forge/questions.json`), потом смержен.
- Время (28.6 юнит-часа): plain-build 27%, **пауза по rate limit 21% (6 ч, 98 пауз)**, plain-tests 15%, setup (`dotnet restore`) 9%, check-гейты 6%. Окна простоя в ppar-main.log: 02:48→04:52 (124 мин), 09:12→10:32 (79 мин) и ещё 6 по ~20–30 мин.
- Деньги: plain-tests 44%, plain-build 40%, cover 10%, fix 6%; не дошло до merge 41% ($176).

## Проблема → решение
| # | Проблема | Решение | Где | Эффект |
|---|---|---|---|---|
| 1 | В промпт каждого юнита вшита вся спека, ~211K символов (~100K токенов): spec.yaml 37K, tasks.yaml 60K, spec.md, tasks.md, contracts, data-model; спецификация юнита — 2K. Каждый вызов перечитывает ~170K | В промпт — только unit spec + его T-задачи + срезы contracts/data-model по `requirements`/`files` юнита; остальное дать путями для Read. Цель — ≤15K | `forge_run.py` (сборка промпта для plain-tests), `queue_run.py` (unit spec) | −40–45% $ и быстрее каждый вызов |
| 2 | Rate limit `five_hour rejected` → `FORGE: paused` → юнит уходит в pending → ppar запускает его заново с нуля (25 брошенных запусков, $42) | Пауза = ждать `resetsAt` из `rate_limit_event` и продолжать ту же сессию/стадию; не создавать новый run | `forge_run.py:391 session()` (RateLimited), `ppar.py` (обработка pending) | −10%, без потери работы |
| 3 | Неограниченные повторы: U05 ×6, U07 ×5, U04 ×4; $68 — повторы юнитов, которые потом смержились | Не больше 2 запусков на юнит, потом `needs-human`; при повторе передавать `.forge/disputes.md` и причину прошлой неудачи | `ppar.py` / `queue_run.py` | −10–15% |
| 4 | Fix round ничего не меняет в дереве (19 раз), но юнит всё равно перезапускается | «no change to the tree» → сразу `needs-answer` с вопросом из disputes, без повтора | `forge_run.py` (fix loop) | меньше пустых прогонов |
| 5 | Всё на Opus, включая build/fix (46% $) | Отдельный параметр `model_build` / `model_fix` (Sonnet), Opus оставить для plain-tests | `queue_run.py:426`, `forge_run.py --model` | −15–18% (следить за числом fix rounds) |
| 6 | `--resume` протаскивает контекст через tests → cover → build → fix (120K → 245K) | build/fix — новая сессия с короткой передачей дел: diff + красный гейт + хвост ошибки | `forge_run.py` | −5–10% |
| 7 | `dotnet restore` на каждый worktree — 2.5 ч из 28.6 | Общий NuGet-кэш (`NUGET_PACKAGES`) / restore один раз в int-worktree и hardlink `obj` | `ppar.py` / `setup` в `powerpuff.yaml` | −9% времени |
| 8 | Параллельность 4 упирается в 5-часовой лимит: эффективно ~2.5 юнита одновременно | Сначала п.1–6 (меньше токенов → лимит дольше держится); затем авто-throttle: снижать MAXPAR при `allowed_warning` | `ppar.py` | стабильный темп |
| 9 | ppar.py — самописная обёртка в `vetal_project/.powerpuff/`, не в плагине | Перенести параллельные волны (int-branch, merge, MAXPAR) в плагин (`queue_run.py --parallel N`) | плагин | одна интеграция на все проекты |
| 10 | Headless-сессии наследуют всё пользовательское окружение (11 плагинов, 3 MCP, hooks mem0/telemetry) — системная часть всего ~12K, но лишние хуки и `ToolSearch` | `--strict-mcp-config` без MCP, `POWERPUFF_DRIVER=1` → глушить mem0/telemetry-хуки | `forge_run.py:397` | мелочь, чистота |

Ожидаемо по сумме: ≈ −60% токенов. Скорость — минус 6 ч пауз и ~2.5 ч restore, плюс быстрее каждый вызов.

## Почему медленно (кратко)
- Упор в 5-часовой лимит: 21% юнит-времени — паузы, два окна по 1–2 ч без прогресса вообще.
- Брошенные и повторные запуски: 42 лишних запуска сверх 39 смерженных юнитов.
- Каждая сессия тянет ~100K лишнего контекста → вызовы медленнее.
- Тяжёлые .NET-гейты: restore, build `-warnaserror`, тесты с coverage на каждый юнит.
- Темп: 39 юнитов за 11.5 ч ≈ 3.4 юнита в час; осталось 25.

## Перезапуск vetal_project после рефакторинга
- Остановить текущий ppar (PID 28396 `ppar.py queue.yaml 4` и дочерние `queue_run.py`) — или дождаться, пока он сам закончит.
- База — `ppar/integration` (int-worktree `.powerpuff/ppar/int`); merged юниты пропускать по `ppar-main.log` / state.
- Сначала добить running (U33, U39, U42, U53) и U01, потом 20 не начатых.
- Проверить, что `powerpuff.yaml` совместим с новой версией плагина (gates, `test_gates`, protected).
- Обновить кэш плагина: `C:/Users/suhor/.claude/plugins/cache/powerpuff/powerpuff/<ver>` (ppar.py захардкодил `0.2.0` в PLUGIN).

## Скрипты анализа: `C:/Work/notes/powerpuff-usage-scripts/`
`agg.py` (расход по транскриптам), `stages.py`/`stages2.py` (по стадиям из логов), `time.py` (время по фазам) — логику при необходимости перенести в `powerpuff/scripts/run_report.py`.

## Итог (0.3.0, 2026-11-22)
| # | Статус |
|---|---|
| 1 | сделано: `scripts/spec_slice.py`, на vetal 211K → ~14K символов на юнит (среднее по 64 юнитам); проверка покрытия просит только id юнита |
| 2 | сделано; причина оказалась другой: сообщение «You've hit your 5-hour limit» классифицировалось как «API unreachable» (24 паузы `FORGE: paused (API unreachable)`); теперь `rate_limit_event` `rejected` → ожидание `resetsAt` и та же стадия |
| 3 | сделано: `max_attempts` (2), повтор с причиной и `disputes.md`; повтор и при merge-конфликте |
| 4 | сделано: `stuck:` → `needs-answer`, продолжение с `plain-build` |
| 5 | сделано: `model_build` / `model_fix` (queue, профиль, CLI); последний фикс-раунд эскалируется на `model` |
| 6 | сделано: build — новая сессия; fix продолжает build, при смене модели — новая сессия с короткой передачей дел |
| 7 | заменено: restore занимал ~4 с на юнит (10 мин за ночь), 9% «setup» — это base check всех гейтов; теперь кеш зелёных деревьев (`verified_trees`), setup выполняется всё равно; в vetal убран дублирующий гейт `tests` (его покрывает `coverage`) |
| 8 | сделано: слоты по загрузке 5-часового окна (`<run>/rate.json`) |
| 9 | сделано: `queue_run.py --parallel N` / `parallel`, `already_merged`, `budget.unit_usd`; `ppar.py` больше не нужен |
| 10 | частично: `--strict-mcp-config`; mem0/telemetry-хуки пользователя не тронуты (их нужно глушить по `POWERPUFF_DRIVER=1` в самих хуках) |
