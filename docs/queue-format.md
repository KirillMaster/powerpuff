# queue.yaml — очередь единиц и `queue_run.py`

`queue_run.py` — внешний скрипт-оркестратор автономного прогона: сам моделей не вызывает, каждую единицу отдаёт `forge_run.py`. Провал или блокировка одной единицы прогон не останавливают.

```
python scripts/queue_run.py queue.yaml [--repo .] [--engine plain|forge] [--no-wait] [--only U1,U2] [--dry-run] [--attended]
```

## Пример
```yaml
run_id: 2026-11-17-a
base: main
integration_branch: powerpuff/run-2026-11-17-a
spec: specs/001-feature
budget: {max_usd: 60, max_hours: 8, fix_reserve: 0.33}
model: opus
writer: sonnet
engine: plain          # plain (по умолчанию) | forge; CLI --engine перекрывает
auto_merge: [low]
final:
  timeout: 1800
  commands:
    unit_all: "pytest -q"
    smoke: "python scripts/smoke.py"
units:
  - id: U1
    title: "Парсер заголовков"
    deps: []
    risk: low
    requirements: [FR-1, AS-2]
    files: [src/parser.py]
    spec: |
      Что построить: ...
  - id: U2
    title: "Эндпоинт /items"
    deps: [U1]
    risk: high
    files: [src/parser.py, src/api.py]
    spec_file: units/u2.md
```

## Поля
| Поле | По умолчанию | Смысл |
|---|---|---|
| `run_id` | дата изменения файла + 6 символов sha1 | имя прогона; для продолжения через сутки или после правки файла задавайте явно |
| `base` | `HEAD` | ветка или коммит, от которых стартует прогон |
| `integration_branch` | `powerpuff/run-<run_id>` | ветка, куда сливаются единицы; не может совпадать с `powerpuff/<run_id>` |
| `spec` | нет | общая спека (каталог или файл), добавляется в спеку каждой единицы как контекст |
| `budget.max_usd` / `max_hours` | 60 / 8 | лимиты на весь прогон |
| `budget.fix_reserve` | 0.33 | доля бюджета вне первого прохода (0 ≤ x < 1) |
| `model` / `writer` | `opus` / `sonnet` | сильная модель и писатель для `forge_run.py` |
| `auto_merge` | `[]` | уровни риска, которые сливаются без человека |
| `assume_defaults` | `false` | `true` → `--assume-defaults` в `forge_run.py`: неотвеченные `blocking` вопросы решаются дефолтами аудитора; не bool = ошибка конфигурации |
| `final.commands` | нет | `{имя: команда}`, тяжёлый контур в интеграционном worktree после очереди; если в репозитории есть `powerpuff.yaml`, контур = гейты его уровня `final` + эти команды (одноимённые из очереди переопределяют). Подстановки `{base}` (`base_sha` прогона) и `{root}`. Источник — `final_gates_source` в `summary.json`, неутверждённый профиль помечен. Нет инструмента = провал |
| `final.timeout` | 1800 | секунд на команду |
| `units[].id` | обязателен | уникальный, `[A-Za-z0-9_.-]+` |
| `units[].title` | `id` | заголовок |
| `units[].deps` | `[]` | id единиц, которые должны быть слиты в интеграционную ветку |
| `units[].risk` | `high` | `low` или `high` |
| `units[].requirements` | `[]` | id требований; информативно и попадают в спеку единицы |
| `units[].files` | `[]` | файлы единицы; общий файл без зависимости между единицами = ошибка конфигурации |
| `units[].spec` / `spec_file` | обязателен один из двух | текст единицы; `spec_file` — путь рядом с `queue.yaml` |

Без PyYAML файл читается как JSON.

## Проверка перед стартом (выход 3, все проблемы списком, ничего не создаётся)
Повторяющийся id, неизвестная зависимость, цикл, неверный `risk`, нет текста спеки, пересечение файлов, грязное дерево (изменения отслеживаемых файлов), нет `base`, занятая интеграционная ветка.

## Ход прогона
| Шаг | Что происходит |
|---|---|
| Выбор | первая `pending` единица, все `deps` которой `merged`; по одной |
| Ветка | `powerpuff/<run_id>/<id>` от текущей вершины интеграционной ветки, worktree `.powerpuff/runs/<run_id>/wt/<id>` |
| Запуск | `forge_run.py <wt>/.forge-unit-spec.md --repo <wt> ... --no-wait --stream`, вывод в `<run>/<id>.log` |
| Код 0 | `done`; риск из `auto_merge` → `git merge --no-ff` в интеграционном worktree → `merged`; конфликт → `failed` («merge conflict»), ветка остаётся |
| Код 0, риск не в `auto_merge` | остаётся `done` до аудита; зависимые → `blocked` («awaits audit of Ux») |
| Код 1 | `failed`, причина из первой строки `REPORT.md` |
| Код 1, блокирующие вопросы | `needs-answer`, причина `<причина forge>; answer in <wt>/.forge/questions.json and rerun`; зависимые → `blocked` («dep Ux needs-answer») |
| Код 3 | ожидание сброса лимита (время из вывода, не дольше остатка часов) и повтор; с `--no-wait` прогон завершается с кодом 3, состояние сохранено |
| Код 3, `FORGE: paused (API unreachable)` | не провал и не сбой: пауза с нарастающим ожиданием 30 с … 15 мин и повтор той же единицы (всего не дольше `--max-outage-min`, 120, и остатка часов); с `--no-wait` или после предела прогон завершается с кодом 3, единица остаётся `running` (причина `API unreachable`), повторный запуск продолжает с того же этапа |
| Иной код | одна повторная попытка (forge продолжит с места), затем `failed` |
| Зависимость `failed`/`blocked`/`skipped-budget`/`done` | единица `blocked`, независимые продолжаются |

Рабочая копия пользователя не затрагивается: слияния идут в отдельном worktree `wt/_integration`, HEAD не двигается. Worktree слитых единиц удаляются (ветки и копия `.forge` остаются), остальные ждут аудита.

## Бюджет
- Лимит единицы: `min(остаток, max_usd * (1 - fix_reserve) / число_единиц * 2)`.
- Остаток меньше лимита или часы вышли → оставшиеся `skipped-budget`.
- Стоимость берётся из `.forge/state.json` каждой единицы.
- Повторный запуск возвращает `needs-answer`, `blocked` и `skipped-budget` в `pending`; `needs-answer` единица продолжается в том же worktree (аудит не повторяется) после заполнения `answer` в `.forge/questions.json`; `done` единица, уже слитая вручную, помечается `merged`.

## Состояние и результат
| Файл (`<repo>/.powerpuff/runs/<run_id>/`) | Содержимое |
|---|---|
| `state.json` | на единицу: `status` (pending, running, done, merged, failed, needs-answer, blocked, skipped-budget), `branch`, `worktree`, `forge_status`, `cost`, `started`, `finished`, `reason`; итоги: стоимость по моделям, время |
| `<id>.log` | вывод `forge_run.py` |
| `forge/<id>/` | копия `.forge` единицы (отчёт, требования, состояние, проверка) |
| `SUMMARY.md` | таблица единиц, итоги, ожидающие аудита, заблокированные, тяжёлый контур |
| `summary.json` | то же машиночитаемо; ключ `needs_answer`: `[{id, reason}]` (в `SUMMARY.md` — раздел `## Needs answer`); на единицу: путь к `REPORT.md`, строки `D`/`Q`, открытые пробелы, результат guard |

`.powerpuff/`, `.forge/` и `.forge-unit-spec.md` добавляются в `.git/info/exclude`.

## Коды выхода и последняя строка
| Код | Строка | Условие |
|---|---|---|
| 0 | `QUEUE: done` | все единицы `merged` или `done`, тяжёлый контур зелёный |
| 2 | `QUEUE: partial` | есть `merged`/`done`, но не все, или упал тяжёлый контур |
| 1 | `QUEUE: failed` | ни одной `merged`/`done` |
| 3 | `QUEUE: config error` / нет строки | ошибка конфигурации; лимит с `--no-wait`; API недоступен (`--no-wait` или `--max-outage-min` исчерпан) |
| 4 | `QUEUE: paused after <id>` | `--attended`, остались единицы |

Тяжёлый контур запускается один раз для данной вершины интеграционной ветки и только если слита хотя бы одна единица; провал статусы единиц не меняет.
