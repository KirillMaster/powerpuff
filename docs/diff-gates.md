# diff_gates: гейты на дифф единицы работы (любой стек)

Скрипт `scripts/diff_gates.py` (stdlib). Ядро знает форматы отчётов, а не стеки: отчёт делает инструмент проекта, скрипт считает только по строкам диффа от `--base` (рабочее дерево, включая неотслеживаемые файлы). Вызывается как обычная команда профиля: решает код выхода.

Коды выхода: `0` пройден, `1` провален, `3` оценить нельзя (нет или не разобран отчёт, не git-репозиторий, неизвестный формат, неоднозначное соответствие путей). Нет отчёта = провал, тихого пропуска нет.

`python scripts/diff_gates.py <gate> --base <sha> [--root .] [--config powerpuff.yaml] [--json]`. С `--json` печатается `{gate, ok, value, threshold, details:[...]}`; при коде 3 добавляется `error`.

## Гейты
| Гейт | Что считает | Вход | Ключи |
|---|---|---|---|
| `coverage` | покрытие добавленных и изменённых исполняемых строк (тестовые файлы не считаются) | `--report` путь или glob; cobertura, lcov, go coverprofile, coverage.py json, jacoco | `--format auto`, `--min 80`, `--min-lines 1`, `--missing-file fail\|ignore`, `--exclude` (повторяемый) |
| `duplication` | новый копипаст: окно из `--min-lines` строк, целиком добавленных в диффе, встречается в другом месте отслеживаемого кода (другой файл или другое место того же файла) | только git | `--min-lines 6`, `--max-new 0`, `--exclude` |
| `complexity` | функции, пересекающие изменённые строки, с цикломатической сложностью выше порога | `--report`; lizard csv/xml, eslint json, radon json, gocyclo, sarif | `--format auto`, `--max 15`, `--base-report`, `--exclude` |
| `size` | бюджет единицы: изменённые строки (добавлено + удалено) и файлы без тестов, lock- и сгенерированных файлов | только git | `--max-lines 400`, `--max-files 25`, `--exclude` |
| `all` | запускает гейты из секции `diff_gates` профиля | `--config` | `--out` фрагмент `quality-report.json` |

## Правила
| Тема | Поведение |
|---|---|
| Строка не в отчёте, файл в отчёте | не исполняемая, игнорируется |
| Изменённый кодовый файл не в отчётах | все его добавленные строки кода считаются непокрытыми и перечисляются; `--missing-file ignore` отключает; расширения вне таблицы языков `gate_guard` не считаются |
| Ноль изменённых исполняемых строк (или меньше `--min-lines`) | пройден с пометкой |
| Пути отчёта и репозитория | абсолютные, обратные слеши, `file://`, `sources` cobertura, префикс модуля из `go.mod`, пути относительно подпроекта (совпадение по суффиксу); неоднозначность, затрагивающая изменённый файл, даёт код 3 со списком |
| Дубли: что не считается | окна из тривиальных строк (скобки, импорты, `end`); пустые и комментарные строки (префиксы по расширению, иначе `#`, `//`); код, перенесённый из другого места (там он удалён); окна с перекрытием внутри файла |
| Дубли: что индексируется | файлы с известным расширением кода до 1 МБ; исключены `node_modules`, `bin`, `obj`, `dist`, `build`, `target`, `vendor`, `.git`, сгенерированные и минифицированные файлы |
| Сложность и базовый отчёт | провал: сложность выше `--max` и (функции нет в базовом отчёте или она стала сложнее, чем на базе); сложность не выше базовой проходит как `pre-existing`; без базового отчёта проваливает каждая |
| Сложность: нет диапазона строк | gocyclo даёт только начальную строку: пересечение проверяется по ней, в `details` есть пометка |
| SARIF | правила, в id или имени которых есть `complex`; число берётся из текста сообщения |

## Команды, которые делают отчёты
| Стек | Покрытие | Сложность |
|---|---|---|
| .NET | `dotnet test --collect:"XPlat Code Coverage"` (coverlet, cobertura в `TestResults/**/coverage.cobertura.xml`) | lizard |
| JS/TS | `c8 --reporter=lcov npm test` или `jest --coverage --coverageReporters=lcov` (`coverage/lcov.info`) | `eslint -f json -o eslint.json --rule '{"complexity":["warn",1]}' src` (порог 1: в отчёте все функции, порог гейта задаёт `--max`) |
| Python | `coverage run -m pytest && coverage json` (`coverage.json`) или `coverage xml` (cobertura) | `radon cc -j -s src > radon.json` |
| Rust | `cargo llvm-cov --lcov --output-path lcov.info` | `lizard -l rust --csv src > lizard.csv` |
| Go | `go test -coverprofile=cover.out ./...` | `gocyclo . > gocyclo.txt` |
| JVM | `mvn verify` с jacoco (`target/site/jacoco/jacoco.xml`) | `lizard -l java --csv src > lizard.csv` или `--xml` |
| Любой язык | cobertura или lcov, что умеет инструмент | `lizard --csv .` (формат `lizard-csv`), `lizard --xml .` (формат `lizard-xml`) |

## Профиль
```yaml
gates:
  unit: [diff-coverage, duplication, complexity, size]
commands:
  diff-coverage: python scripts/diff_gates.py coverage --base {base} --report coverage/lcov.info --min 80
  duplication:   python scripts/diff_gates.py duplication --base {base} --max-new 0
  complexity:    python scripts/diff_gates.py complexity --base {base} --report eslint.json --max 15
  size:          python scripts/diff_gates.py size --base {base} --max-lines 400
diff_gates:              # для `diff_gates.py all --base {base} --config powerpuff.yaml --out diff-gates.json`
  coverage:   {report: coverage/lcov.info, min: 80, missing_file: fail}
  duplication: {min_lines: 6, max_new: 0}
  complexity:
    report: eslint.json
    max: 15
    base_report: base/eslint.json
  size:       {max_lines: 400, max_files: 25}
```
### Подстановки в командах
Скрипт-драйвер подставляет в строки команд профиля (`commands`, `format_fix`, `final.commands` очереди) до запуска, без раскрытия переменных оболочки: `{base}` — коммит, с которого стартовал юнит, `{root}` — корень репозитория, `{plugin}` — корень плагина (прямые слэши). Неизвестный `{...}` остаётся как есть. Те же значения экспортируются в окружение гейта: `POWERPUFF_BASE`, `POWERPUFF_ROOT`, `POWERPUFF_PLUGIN`. Вызов скрипта плагина в профиле: `python "{plugin}/scripts/diff_gates.py" size --base {base} --max-lines 400` (профиль переносим между машинами).

| Где | `{base}` |
|---|---|
| `forge_run.py` | `start_sha` из `.forge/state.json` (HEAD на старте прогона); в проверке чистой базы — текущий HEAD (пустой дифф: гейты по дифф проходят по правилу «изменённых строк нет») |
| `queue_run.py`, финальный контур | `base_sha` прогона |
| `hook_stop.py` | `HEAD` (дифф = незакоммиченные правки) |
| `prepare_run.py`, проверка базы | текущий HEAD |

Без PyYAML секция `diff_gates` читается встроенным разбором: два уровня, списки только в виде `[a, b]`. `all` пишет фрагмент `{"schema": 1, "tool": "diff_gates", "gates": [...]}` с id `diff-coverage`, `duplication`, `complexity`, `size`; не оценённый гейт получает `status: failed` и поле `error`.

## Ограничения
| Что | Почему |
|---|---|
| Покрытие и сложность работают на языках, для которых есть отчёт; сам скрипт отчёты не строит | ядро не знает стеков |
| Дубли ищутся по нормализованным строкам, не по синтаксису: переименование переменных скрывает копию | цена простоты; семантические клоны ловит jscpd |
| Для `size` переименование без правок не считается; бинарные файлы пропускаются | `git diff --numstat -M` |
| Файлы-тесты определяются таблицей языков `gate_guard` (в том числе подстрокой `test` в пути) | единый источник правды |
