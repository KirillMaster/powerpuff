# audit — карточки, `answers.yaml` и `audit_apply.py`

Аудит после автономного прогона: человек разбирает `cards.html` (10–20 минут), экспортирует `answers.yaml`, скрипт превращает ответы в действия и очередь следующего прогона. Моделей не вызывает.

```
python scripts/audit_cards.py <run dir | summary.json> [--repo .] [--out <run dir>/cards.html] [--max-diff-lines 400]
python scripts/audit_apply.py answers.yaml --run <run dir> [--repo .] [--queue queue.yaml] [--out next-queue.yaml] [--dry-run]
```

## Карточки (`cards.html`)
Один файл: CSS и JS внутри, внешних запросов нет, открывается с `file://`, светлая и тёмная тема по `prefers-color-scheme`. Данные лежат одним JSON-блоком, текст из файлов выводится через `textContent`.

| Вид | Что попадает | Id | Варианты |
|---|---|---|---|
| `question` | открытый вопрос любой единицы; сначала `blocking`, затем `non-blocking`; `info` — в свёрнутом блоке карточки `summary` | `question:<unit>:<Qn>` | `accept-default`, `answer` |
| `high-risk` | единица `done`, ждёт аудита: решения `D`, дифф-стат, свёрнутый патч, гейты, guard | `unit:<unit>` | `merge`, `reject`, `rework` |
| `failure` | единица `failed`: причина, начало `REPORT.md`, вывод упавших проверок, конец лога | `unit:<unit>` | `retry`, `rework`, `drop` |
| `blocked` | `blocked`, `skipped-budget`, `needs-answer` | `unit:<unit>` | `requeue`, `drop` |
| `final` | упавшая команда финального контура с хвостом вывода | `final:<имя команды>` | `fix`, `ignore` |
| `summary` | единицы по статусам, стоимость по моделям, время, ветка | `summary` | нет решения |

Дифф берётся как `git diff <integration_branch>...<branch>`; нет репозитория, ветки или снимка `.forge` — раздел пропускается с пометкой, генерация не падает.

Клавиши: `j`/`k` — следующая/предыдущая карточка, `1`–`9` — вариант, `e` — экспорт. Прогресс «decided n / m»; решения хранятся в `localStorage` по `run_id`. «Export» скачивает `answers.yaml` и показывает его в поле для копирования.

## `answers.yaml`
| Поле | Смысл |
|---|---|
| `run_id` | должен совпасть с `run_id` прогона |
| `decided_at` | время экспорта, ISO 8601 |
| `answers[].card` | id карточки из таблицы выше |
| `answers[].decision` | `accept-default`, `answer`, `merge`, `reject`, `rework`, `retry`, `drop`, `requeue`, `fix`, `ignore` |
| `answers[].text` | комментарий или ответ; обязателен для `answer` и `rework` |

Нерешённые карточки в файл не попадают. Допустимость решения зависит от вида карточки (таблица выше); `accept-default` требует непустого значения по умолчанию.

```yaml
run_id: "2026-11-17-a"
decided_at: "2026-11-17T10:00:00.000Z"
answers:
  - card: "question:U6:Q1"
    decision: answer
    text: "oauth2"
  - card: "unit:U2"
    decision: rework
    text: "use cursor pagination"
  - card: "unit:U3"
    decision: retry
    text: ""
  - card: "final:lint"
    decision: fix
    text: "only src/a.py"
```

Скаляры в двойных кавычках (JSON-экранирование). Без PyYAML `audit_apply.py` читает этот формат и JSON.

## Что делает `audit_apply.py`
| Решение | Действие |
|---|---|
| `accept-default` / `answer` | поле `answer` в `.forge/questions.json` рабочей копии единицы (если она ещё есть) и в её снимке `<run dir>/forge/<unit>/`; запись в `decisions.md` |
| `merge` | единица сливается в интеграционную ветку во временном worktree (`worktree add --detach`, `merge --no-ff`, `update-ref` с проверкой старого значения); рабочая копия пользователя не затрагивается; ветка занята в другом worktree или конфликт — действие `FAILED`, код выхода 2; уже слито — пропуск |
| `rework`, `retry`, `requeue` | единица попадает в следующую очередь |
| `fix` | новая единица `FIX-<имя>`: упавшая команда, хвост вывода, комментарий |
| `reject`, `drop`, `ignore` | только запись в `audit-actions.md` |

Следующая очередь (`--out`, по умолчанию `<run dir>/next-queue.yaml`):
| Поле | Значение |
|---|---|
| `run_id` | `<run_id>-r2`, `-r3`, … |
| `base` | интеграционная ветка прогона |
| `spec`, `budget`, `model`, `writer`, `auto_merge`, `assume_defaults`, `final` | копия из исходной очереди (`--queue`, иначе `queue_file` из `state.json`) |
| `units` | единицы исходной очереди в прежнем порядке, затем `FIX-*`; `spec_file` встраивается в `spec` |
| `units[].deps` | только присутствующие в новой очереди; зависимость от отсутствующей единицы заменяется её собственными зависимостями (слитые и отброшенные не требуются) |
| `units[].spec` | исходный текст + `Human answers:` (ответы на вопросы этой единицы) + `Human feedback:` (комментарий) |

Файлы в `<run dir>`: `decisions.md` (ответы на вопросы), `audit-actions.md` (git-команды слияний и остальные решения), оба дополняются при каждом применении.

## Коды выхода
| Код | Условие |
|---|---|
| 0 | всё выполнено |
| 2 | действие не удалось (конфликт слияния, запись ответа) |
| 3 | неверный ввод: чужой `run_id`, неизвестная карточка, недопустимое решение, нет текста, нет исходной очереди; перечислены все проблемы, ничего не изменено |

`--dry-run` проверяет ввод и печатает план: файлы не пишутся, ветки не двигаются. Повторное применение тех же ответов на вопросы даёт код 3: закрытые вопросы карточками больше не считаются.
