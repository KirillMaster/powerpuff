# Профиль проекта `powerpuff.yaml`, пресеты, `quality-report.json`

Профиль лежит в репозитории проекта, утверждается человеком. Ядро знает только контракт «идентификатор гейта → команда → код выхода»; стек задаётся профилем. Дизайн: `docs/design-gates.md`, гейты по диффу: `docs/diff-gates.md`.

## Ключи профиля
| Ключ | Тип | Смысл | Кто читает |
|---|---|---|---|
| `approved` | bool | человек проверил и утвердил профиль; `false` = отчёт помечается «гейты не утверждены» | `forge_run`, `queue_run`, `quality_report` |
| `stack` | строка | информационно (`dotnet`, `mixed`, ...) | никто не ветвится |
| `gates` | map уровень → список id | `iteration` (секунды–минуты, на каждую итерацию), `unit` (перед «готово»), `final` (конец прогона, тяжёлые) | `forge_run` (iteration + unit = проверка «готово», `final` по `--final`), `queue_run` (`final`), `hook_stop` (`iteration`), `quality_report` (любой) |
| `commands` | map id → команда | команда гейта, код выхода 0 = пройден; нет инструмента = провал | те же |
| `format_fix` | строка | форматтер в режиме правки; писатель запускает перед гейтами | `forge_run` |
| `protected` | список glob | защищённые файлы (линтеры, анализаторы, CI): правка = нарушение; добавляется к списку по умолчанию | `gate_guard`, `hook_protect` |
| `unprotected` | список glob | убрать glob из списка по умолчанию | `gate_guard` |
| `allow_suppressions` | список `{rule, file, pattern}` | разрешённые подавления предупреждений | `gate_guard` |
| `languages` | map | своя строка таблицы языков (ext, test_file, skip, suppression, test_decl); без PyYAML только в `powerpuff.gates.json` | `gate_guard` |
| `test_gates` | список id | какие гейты проверяют «красный на базе»; без ключа — id с `test` | `forge_run` |
| `test_infra` | список glob | манифесты, которые писатель тестов может менять | `forge_run` |
| `diff_gates` | map | пороги для `diff_gates.py all` | `diff_gates` |
| `hooks` | — | не ключ: хуки плагина (`hooks/hooks.json`) берут `gates.iteration` и `protected` из профиля | `hook_stop`, `hook_protect` |

Без PyYAML профиль читает встроенный разбор `gate_guard.parse_simple_yaml`: плоские ключи, списки `[a, b]`, одноуровневые map. Команды в профиле пишите в одинарных кавычках.

## Подстановки в командах
| Что | Кто подставляет | Значение |
|---|---|---|
| `{base}` | драйвер при запуске гейта | коммит, с которого стартовала единица (`--base` у `quality_report`, по умолчанию HEAD = пустой дифф) |
| `{root}` | драйвер | корень репозитория, прямые слэши |
| `{plugin}` | драйвер | корень плагина (каталог с `scripts/`), прямые слэши; в профиле остаётся как есть |
| `{pm}`, `{pmx}` | только в пресете TypeScript; `profile_init` | `npm run` / `pnpm run` / `yarn run` и `npx --no-install` / `pnpm exec` / `yarn` по lock-файлу |

Те же значения экспортируются в окружение гейта: `POWERPUFF_BASE`, `POWERPUFF_ROOT`, `POWERPUFF_PLUGIN`. Профиль переносим: `profile_init` оставляет `{plugin}` и его можно коммитить; `--plugin DIR` подставляет путь явно (профиль перестаёт быть переносимым).

## Пресеты (`presets/<стек>.yaml`)
Пресет — стартовые значения профиля плюс служебные ключи: `detect` (признаки стека), `needs` (условие, при котором гейт попадает в профиль), `global` (гейты на весь репозиторий, один раз).

| Стек | Признаки (`detect`) | Обязательные инструменты | Остальные (гейт попадает в профиль, только если есть) |
|---|---|---|---|
| dotnet | `*.sln`, `*.slnx`, `*.csproj` | `dotnet` SDK | coverlet.collector (покрытие), `lizard`, `qg` (`KirillMaster.QualityGates.Cli`; `qg pr`, `qg mutation-diff`, коды 0/1/2/3 — ненулевой = провал) |
| typescript | `tsconfig.json`, `package.json` с `typescript` | `node`, пакетный менеджер | typescript, eslint, prettier, vitest / jest / c8, dependency-cruiser, Stryker, jscpd, `lizard` |
| python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `Pipfile` | `python` | pytest, ruff, pyright или mypy, coverage, import-linter, mutmut, `lizard` |
| rust | `Cargo.toml` | `cargo` | rustfmt, clippy, cargo-llvm-cov, cargo-deny, cargo-mutants, `lizard` |
| go | `go.mod` | `go`, `gofmt` | golangci-lint, go-arch-lint, gremlins, `lizard` |

Rust и Go составлены по документации инструментов, на реальных репозиториях не проверены.
Везде есть гейты без внешних инструментов: `duplication`, `size` (`diff_gates.py`, нужен только git), у Python ещё `syntax` (`compileall`).
Тесты по умолчанию идут один раз (`tests`) и второй раз с покрытием (`coverage`): цена — двойной прогон.

### Условия `needs`
Значение — термы через `+` (нужны все); терм — `вид:вариант|вариант` (достаточно одного). Вариант может сменить вид: `tool:ruff|has:pyproject.toml~tool.ruff`.
| Вид | Выполнено, если |
|---|---|
| `tool` | исполняемый файл в PATH (или `node_modules/.bin` проекта) |
| `file` | в проекте (глубина до 6, без `node_modules`, `bin`, `obj`, `target`, `vendor`, `.venv`) есть файл по glob |
| `has` | `glob~текст`: файл по glob содержит текст |
| `dep` | пакет объявлен в `package.json` или есть в `node_modules/.bin` |
| `script` | в `package.json` есть скрипт (заглушка `no test specified` не считается) |
| `tests` | есть файл тестов по glob |
| `gate` / `no-gate` | другой гейт этого проекта оставлен / не оставлен (альтернативы: eslint-скрипт или прямой вызов) |

## `scripts/profile_init.py`: черновик профиля без модели
`python scripts/profile_init.py [--repo .] [--write] [--stack name,...] [--plugin DIR]`
| Поведение | Как |
|---|---|
| Определение стека | `detect` в корне и подкаталогах глубиной до 2; несколько стеков или каталогов = монорепозиторий |
| Монорепозиторий | id с префиксом (`web-lint`, `dotnet-build`); команды подкаталога `cd <dir> && ...`; у `diff_gates` добавляются `--root {root}`, у покрытия `--missing-file ignore`; `duplication` и `size` один раз на репозиторий |
| Отбрасывание | оставляются гейты, чей инструмент или конфигурация есть в репозитории; остальные — комментарием «рекомендуется добавить» с причиной |
| Вывод | черновик в stdout, `approved: false`, шапка «человек обязан проверить и поставить `approved: true`» |
| `--write` | пишет `powerpuff.yaml` только если нет ни `powerpuff.yaml`, ни `powerpuff.gates.json`; никогда не перезаписывает |
| Стек не найден | заготовка с `commands: {}`, сообщение «нет пресета — профиль пишется вручную», код 2 |

Коды выхода: `0` черновик получен; `1` `--write` отказал (профиль уже есть); `2` нет пресета.
Ограничения: на монорепозитории с несколькими стеками проверяйте префиксы и пути вручную; инструмент «установлен» = есть в PATH на машине, где запущена генерация.

## `scripts/quality_report.py`: гейты уровня и `quality-report.json`
`python scripts/quality_report.py --repo . [--tier iteration|unit|final|all] [--base <sha>] [--out quality-report.json] [--timeout 1800]`
Профиль читает загрузчик `forge_run.load_profile`; каждый гейт запускается отдельно через `forge_check.check_gates` с контекстом `{base}`/`{root}`/`{plugin}`; `exit_code` и `seconds` берутся из результата `check_gates`.
`forge_run` в конце прогона (`done` или `failed`) пишет `.forge/quality-report.json` той же схемы (последний результат каждого гейта, плюс `guard`) и ссылку с таблицей в `REPORT.md`; построитель один: `forge_check.quality_report`.
Коды выхода: `0` всё зелёное; `1` есть красный гейт; `3` нет профиля, профиль неверен, в уровне нет гейтов или не найден инструмент (приоритетнее `1`).

### Схема `quality-report.json`, версия 1
| Поле | Тип | Смысл |
|---|---|---|
| `schema` | int | 1 |
| `tool` | строка | `powerpuff.quality_report`; у отчёта `forge_run` — `powerpuff.forge_run` (tier `all`, дополнительно поле `guard`) |
| `generated_at` | строка | UTC, ISO 8601 |
| `head` | строка или null | sha HEAD репозитория |
| `base` | строка или null | коммит `{base}` |
| `profile_approved` | bool | `approved` профиля |
| `tier` | строка | `iteration`, `unit`, `final` или `all` |
| `gates[]` | массив | по порядку профиля: `name`, `tier`, `ok`, `exit_code`, `seconds`, `missing_tool`, `output_tail` |
| `ok` | bool | все гейты зелёные |
`exit_code`: настоящий код возврата, `null` при отсутствии инструмента или таймауте; `seconds` — замер `check_gates` (0,1 с). `output_tail` — последние 2000 символов вывода красного гейта (у `forge_run` — 600). Формат минимальный и не совпадает с отчётом `qg` (`schema`, `tool`, `gates[{id, status, blocking, value, threshold}]`): поля `status`/`blocking` не нужны, пока все гейты профиля обязательные.
Файлы `quality-report.json`, `powerpuff.yaml` и `.powerpuff-*` (отчёты lizard, дифф для мутаций) остаются в рабочем дереве как неотслеживаемые: добавьте их в `.gitignore`; гейт `size` их исключает.

## Стек без пресета
1. `python scripts/profile_init.py --repo .` — код 2 и заготовка; `--write` сохраняет её как `powerpuff.yaml`.
2. Заполнить `gates` и `commands`: по гейту на проверку, команда с кодом выхода, запускаемая из корня; стек-независимые гейты (`diff_gates.py duplication|size|complexity`) берутся как есть.
3. Для `complexity` и `coverage` отчёт делает инструмент стека (`docs/diff-gates.md`, таблица «Команды, которые делают отчёты»); lizard знает больше 20 языков.
4. Перечислить в `protected` конфиги линтера, сборки, CI.
5. Прогнать `quality_report.py --tier iteration`, затем `--tier unit`; поправить; поставить `approved: true`.
6. Для языка вне таблицы `gate_guard` защита от обхода слепа к пропускам тестов и подавлениям; её `languages` дополняется в `powerpuff.gates.json`.

Пример (репозиторий C: Kotlin + Gradle, подпроект Android-приложения; пресета нет). Ключи `format_fix` и `test_gates` не заданы: форматтера и тестов в проекте нет.
```yaml
approved: false
stack: kotlin-gradle
gates:
  iteration: [gradle-help]
  unit: [duplication, size, complexity]
  final: [assemble]
commands:
  gradle-help: '.\gradlew.bat --no-daemon -q help'
  duplication: 'python "{plugin}/scripts/diff_gates.py" duplication --base {base} --max-new 0'
  size: 'python "{plugin}/scripts/diff_gates.py" size --base {base} --max-lines 400 --exclude "quality-report*.json" --exclude powerpuff.yaml --exclude ".powerpuff-*"'
  complexity: 'lizard -l kotlin --csv -x "*/build/*" . > "{root}/.powerpuff-lizard.csv" && python "{plugin}/scripts/diff_gates.py" complexity --base {base} --report "{root}/.powerpuff-lizard.csv" --max 15'
  assemble: '.\gradlew.bat --no-daemon assembleDebug'
protected: ['build.gradle.kts', '*/build.gradle.kts', 'settings.gradle.kts', 'gradle.properties', 'gradle/**', '.editorconfig', '.github/workflows/**']
```
`{plugin}` раскрывает драйвер. На Windows `cmd.exe` не ищет программу в текущем каталоге, поэтому `.\gradlew.bat`, а не `gradlew.bat`; на POSIX — `./gradlew`.

## Проверка на реальных репозиториях (критерий 2.4)
Три локальных репозитория трёх стеков; работа только в локальных клонах (`git clone --local --no-hardlinks`), клоны удалены. Гейты без модели, `{base}` = HEAD (пустой дифф), `--timeout 900`.

| Репо | Стек | Уровень | Гейт: результат, секунды |
|---|---|---|---|
| A | .NET (sln, веб-приложение, 1 тестовый проект) | iteration | build: pass 28.1; format: pass 39.0 |
| A | | unit | tests: pass 12.6; coverage: pass 6.5; diff-coverage: pass 0.4; duplication: pass 0.4; size: pass 0.4; complexity: pass 0.6 |
| B | Python (pyproject, pytest) | iteration | syntax: pass 0.1 |
| B | | unit | tests: pass 5.8; coverage: pass 6.6; diff-coverage: pass 0.6; duplication: pass 0.5; size: pass 0.5; complexity: pass 0.8 |
| C | Kotlin + Gradle, пресета нет (профиль вручную) | iteration | gradle-help: pass 67.6 |
| C | | unit | duplication: pass 0.6; size: pass 0.6; complexity: pass 1.3 |

Не запускались: уровень `final` (A: `qg` не установлен; B: `mutmut` долго; C: `assemble` нужен Android SDK), `diff-coverage` на непустом диффе. Пустой дифф даёт «пройден: изменённых строк нет» — гейты по диффу здесь проверены на запуск и разбор отчётов, а не на ловлю нарушений (это делают `tests/test_diff_gates.py`).

Что пришлось исправить после первого прогона:
| Репо | Что случилось | Исправление |
|---|---|---|
| A | сгенерированный `size` красный (796 строк): неотслеживаемый отчёт покрытия в `TestResults/` и файлы самого плагина посчитаны как изменённый код | в команду `size` всех пресетов добавлены `--exclude` для `quality-report*.json`, `powerpuff.yaml`, `.powerpuff-*` и артефактов покрытия стека |
| B | уровень `iteration` пуст: в репозитории нет конфигурации ruff / pyright / mypy | в пресет Python добавлен гейт `syntax` (`python -m compileall`); остальное — в «рекомендуется добавить» |
| C | `profile_init` на подкаталоге: код 2, заготовка (как задумано) | профиль написан вручную (пример выше) |
| C | `gradlew.bat`: «not recognized» (код 9009, `missing_tool`) | `.\gradlew.bat` |
| C | отчёт lizard в `{root}/.git/...`: каталога `.git` нет в подпроекте монорепозитория | отчёт в `{root}/.powerpuff-lizard.csv`; так же исправлены все пресеты |
В корне монорепозитория C `profile_init` находит Python и .NET и не видит Kotlin и shell: подпроекты без пресета приходится вести вручную.

Пробелы: `forge_run.py` сам `quality-report.json` не пишет (отчёт делает `quality_report.py`); `exit_code` красного гейта не сохраняется; Rust и Go не проверены.
