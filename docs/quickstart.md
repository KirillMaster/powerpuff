# Быстрый старт

Цель: установить плагин, пройти одну спеку через `forge` (движок `plain` по умолчанию), затем цикл подготовка → прогон → аудит. Замер шага 3 — в разделе «Что проверено».

## Требования
| Что | Условие |
|---|---|
| Python | 3.10+ (в скриптах аннотации `X \| None`) |
| git | репозиторий проекта, чистое отслеживаемое дерево перед `queue_run.py` |
| `claude` CLI | в PATH, вход выполнен; драйверы запускают `claude -p` |
| PyYAML | необязателен; без него `queue.yaml` пишется как JSON, `powerpuff.yaml` читается встроенным разбором (два уровня, списки `[a, b]`) |
| Инструменты проекта | всё, что вызывают команды профиля (линтер, тесты); нет инструмента = гейт провален |

## 1. Установка
В Claude Code:
```
/plugin marketplace add KirillMaster/powerpuff
/plugin install powerpuff@powerpuff
```
Команды: `/powerpuff:prepare`, `/powerpuff:queue`, `/powerpuff:forge`. Хуки плагина зарегистрированы, но ничего не делают, пока в `powerpuff.yaml` нет секции `hooks:`.

## 2. Профиль маленького репозитория
`powerpuff.yaml` в корне; пример для Python-проекта:
```yaml
approved: true
gates:
  iteration: [lint, tests]
commands:
  lint: "ruff check ."
  tests: "pytest -q"
format_fix: "ruff format ."
protected: [powerpuff.yaml, pyproject.toml]
```
| Правило | Следствие |
|---|---|
| Гейт из `gates` без команды в `commands` | `FORGE: failed` до первой сессии |
| Тестовые гейты — ключ `test_gates`, иначе id с `test` в имени | на них проверяется «приёмочные тесты падают до реализации» |
| `approved: true` не поставлен | профиль работает, отчёт пишет `profile not approved` |
| Профиля нет | гейты берутся из пресета стека, черновик — `.forge/powerpuff.draft.yaml`; гейты, красные на нетронутой базе (например, нет инструмента покрытия), исключаются и перечисляются в отчёте; утвердить = сохранить как `powerpuff.yaml` с `approved: true` |

Закоммитьте профиль: перед запуском все гейты один раз проходят на нетронутой базе; с утверждённым профилем красная база = отказ (`--allow-red-base` обходит осознанно).

## 3. Одна спека через forge
Спека — каталог с `spec.md` (или `spec.yaml`, `plan.md`, `tasks.md`); требования удобно нумеровать `R1`, `R2`…
```
/powerpuff:forge specs/001-feature
```
Или напрямую:
```
python <плагин>/scripts/forge_run.py specs/001-feature --repo . --max-usd 20
```
Ход движка `plain`: Opus задаёт вопросы по неоднозначностям и пишет приёмочные тесты → скрипт их замораживает и проверяет, что они красные на базе → та же сессия пишет код → скрипт гоняет гейты, красное возвращает в ту же сессию (до 4 раундов). Подробно — `docs/engine-plain.md`; прежний многосессионный конвейер — `--engine forge`.
| Что | Где |
|---|---|
| Прогресс | `.forge/progress.md` |
| Требования, решения, вопросы | `.forge/requirements.md`, `.forge/questions.json` |
| Результат | `.forge/REPORT.md`, последняя строка вывода `FORGE: done` или `FORGE: failed` |
| Состояние | `.forge/state.json`; повторный запуск продолжает с места |

Блокирующие вопросы без ответа останавливают прогон (`FORGE: failed (blocking questions: …)`, код 1): ответьте в поле `answer` в `.forge/questions.json` и перезапустите — сессия продолжится; `--assume-defaults` берёт ответы по умолчанию сразу.

| Код | Действие |
|---|---|
| 0 | `done`: прочитать `.forge/REPORT.md`, раздел решений `D`, строки `gates:` и `guard:` |
| 1 | причина в `REPORT.md` и последней строке: `base is red: <гейты>` — починить базу; `blocking questions: Q1,Q4` — ответить в `questions.json` и повторить, либо `--assume-defaults`; `cost budget exhausted` / `wall-clock budget exhausted` — поднять `--max-usd` / `--max-hours` и повторить; иное — правка спеки |
| 3 | лимит подписки при `--no-wait`: повторить после сброса, прогон продолжится |

## 4. Подготовка спеки
```
/powerpuff:prepare specs/001-feature --out queue.yaml
```
Стадии: `spec_lint` → гейты на базе → вопросы → нарезка на единицы → `queue.yaml`. Состояние — `.powerpuff/prepare/<slug>/` (`lint.json`, `questions.json`, `decisions.md`, `units.yaml`, `progress.md`). Последняя строка: `PREPARE: ready queue.yaml`.

| Код | Строка | Действие |
|---|---|---|
| 0 | `PREPARE: ready <path>` | проверить шапку `queue.yaml` (число единиц, покрытие требований), запустить очередь |
| 1 | `PREPARE: failed (<причина>)` | ошибки линтера — править спеку (`--accept-lint` только осознанно); красная база — чинить; невалидная нарезка — список проблем в выводе, черновик остаётся |
| 3 | `PREPARE: rate limited` | повторить ту же команду после сброса |
| 4 | `PREPARE: needs answers <questions.json>` | заполнить `answer` блокирующих вопросов (или `--answers файл`, или `--assume-defaults`), повторить команду |

Проверка без затрат: `--dry-run` выполняет только линтер и гейты базы.

## 5. Автономный прогон
```
/powerpuff:queue queue.yaml --dry-run
/powerpuff:queue queue.yaml
```
`--dry-run` печатает порядок единиц и бюджет. Прогон идёт отдельным процессом; лог — `.queue-run.log`, на единицу — `.powerpuff/runs/<run_id>/<id>.log`.

| Файл в `.powerpuff/runs/<run_id>/` | Содержимое |
|---|---|
| `state.json` | статус, ветка, стоимость каждой единицы |
| `SUMMARY.md`, `summary.json` | итог, ожидающие аудита, заблокированные, финальный контур |
| `forge/<id>/` | копия `.forge` единицы |

| Код | Последняя строка | Действие |
|---|---|---|
| 0 | `QUEUE: done` | перейти к аудиту |
| 1 | `QUEUE: failed` | ни одной готовой единицы; причины в `SUMMARY.md` и логах единиц |
| 2 | `QUEUE: partial` | часть сделана; провалы и блокировки идут на аудит |
| 3 | `QUEUE: config error` | список проблем в выводе, ничего не создано; либо лимит с `--no-wait` — повторить после сброса |
| 4 | `QUEUE: paused after <id>` | только `--attended`; повторить команду для следующей единицы |

Единицы с `risk`, не входящим в `auto_merge`, остаются `done` до аудита. Единица `needs-answer` продолжается после ответа в `.forge/questions.json` (путь в причине) и повторного запуска.

## 6. Аудит
```
python <плагин>/scripts/audit_cards.py .powerpuff/runs/<run_id>
```
Результат — `.powerpuff/runs/<run_id>/cards.html` (один файл, открывается с `file://`). Решения кнопкой «Export» скачиваются как `answers.yaml`.
```
python <плагин>/scripts/audit_apply.py answers.yaml --run .powerpuff/runs/<run_id> --dry-run
python <плагин>/scripts/audit_apply.py answers.yaml --run .powerpuff/runs/<run_id>
```
Результат: `next-queue.yaml` в каталоге прогона, `decisions.md` и `audit-actions.md`. Следующий прогон:
```
/powerpuff:queue .powerpuff/runs/<run_id>/next-queue.yaml
```
| Код `audit_cards.py` | Действие |
|---|---|
| 0 | `AUDIT: N cards (M to decide) -> <путь>` |
| 3 | нет или нечитаем `summary.json` — сначала завершить прогон |

| Код `audit_apply.py` | Действие |
|---|---|
| 0 | всё выполнено |
| 2 | действие не удалось (конфликт слияния, запись ответа): смотреть `FAILED` в выводе, решить вручную |
| 3 | неверный ввод (чужой `run_id`, неизвестная карточка, недопустимое решение); ничего не изменено; повторное применение тех же ответов на вопросы тоже даёт 3 |

## Что проверено
Реальным прогоном подтверждён только шаг 3 на одной спеке. Шаги 4–6 покрыты тестами с подставным `claude`; реальные прогоны очереди (критерии 4.1, 5.1, 5.3) открыты. Состояние — `docs/ROADMAP.md`.

## Дальше
`docs/prepare-format.md`, `docs/queue-format.md`, `docs/audit-format.md` — полные таблицы флагов; `docs/attended-mode.md` — хуки для сессий с человеком.
