# powerpuff

Плагин Claude Code для агентной разработки: общий, под любой стек и проект. Человек готовит спеку и проводит аудит, код пишется в автономном прогоне без него.
Правило: **модель предлагает, скрипт решает** — вердикт «готово» дают коды выхода команд, которые запускает драйвер, а не слова модели.
Ядро знает только контракт «команда → код выхода»; всё языковое лежит в профиле проекта `powerpuff.yaml`.

**Режим по умолчанию** (по бенчу, `docs/FINDINGS.md` A22): обычный Claude Code на Opus, одна сессия на срез спеки, с замороженными приёмочными тестами и гейтами-скриптами powerpuff; оркестратор `forge` — только по явному запросу (качество то же, цена ×4–19).

## Цикл
| Этап | Команда | Скрипт | Результат |
|---|---|---|---|
| Подготовка спеки (человек рядом) | `/powerpuff:prepare <спека>` | `prepare_run.py` | `queue.yaml`, вопросы по ходу |
| Автономный прогон | `/powerpuff:queue queue.yaml` | `queue_run.py` | ветка и worktree на единицу, `SUMMARY.md` |
| Одна единица (она же вся спека) | `/powerpuff:forge <спека>` | `forge_run.py` | `.forge/REPORT.md`, `FORGE: done\|failed` |
| Аудит | `audit_cards.py` → `cards.html` → `answers.yaml` → `audit_apply.py` | оба скрипта | `next-queue.yaml` для следующего прогона |

Автономный прогон можно запускать в любое время; режим с человеком рядом — `queue_run.py --attended` и хуки (`docs/attended-mode.md`).

## Компоненты
| Скрипт | Назначение | Коды выхода |
|---|---|---|
| `forge_run.py` | одна спека: аудит → замороженные приёмочные тесты → код → приёмка | 0 done, 1 failed, 3 лимит подписки при `--no-wait` |
| `forge_check.py` | «готово»: покрытие требований, заморозка, гейты | 0 готово, 1 нет |
| `gate_guard.py --base <sha>` | защита от обхода по диффу: пропуск тестов, подавления, защищённые файлы, число тестов | 0, 1 нарушения, 3 ошибка ввода |
| `diff_gates.py <гейт> --base <sha>` | покрытие диффа, дубли, сложность, размер | 0, 1, 3 нельзя оценить (нет отчёта = провал) |
| `spec_lint.py <каталог спеки>` | линтер спеки | 0, 1 ошибки, 3 ошибка ввода |
| `prepare_run.py` | спека → `queue.yaml` | 0, 1 failed, 3 лимит, 4 нужны ответы |
| `queue_run.py` | обход очереди, слияние единиц низкого риска, финальный контур | 0 done, 1 failed, 2 partial, 3 ошибка конфигурации или лимит, 4 пауза `--attended` |
| `audit_cards.py` | `cards.html` из каталога прогона | 0, 3 ошибка ввода |
| `audit_apply.py` | `answers.yaml` → действия и `next-queue.yaml` | 0, 2 действие не удалось, 3 неверный ввод |
| `hook_stop.py`, `hook_protect.py` | хуки Claude Code, opt-in | 0 пропустить, 2 заблокировать |

## Профиль проекта
Гейты выбирает человек: `powerpuff.yaml` в корне репозитория. Без него аудит модели пишет только черновик `.forge/powerpuff.draft.yaml`, отчёт помечается «NOT APPROVED».
```yaml
approved: true
gates:
  iteration: [build, lint, tests]
  unit: [diff-coverage, size]
  final: [mutation-diff]
commands:
  build: "cargo build"
  lint: "cargo clippy -- -D warnings"
  tests: "cargo test"
  diff-coverage: "python scripts/diff_gates.py coverage --base {base} --report lcov.info"
  size: "python scripts/diff_gates.py size --base {base} --max-lines 400"
  mutation-diff: "cargo mutants --in-diff"
format_fix: "cargo fmt"
protected: [powerpuff.yaml, rustfmt.toml]
allow_suppressions: []
hooks:
  stop_gates: true
  protect: true
diff_gates:
  coverage: {report: lcov.info, min: 80}
  size: {max_lines: 400}
```
| Ключ | Смысл |
|---|---|
| `approved` | ставит человек; нет или `false` = черновик, отчёт пишет `profile not approved` |
| `gates` | ярусы: `iteration` и `unit` решают «готово»; `final` драйвер `forge_run` пока не запускает, тяжёлый контур очереди задаётся в `final.commands` файла `queue.yaml` |
| `commands` | команда на каждый id; нет команды или инструмента — гейт провален |
| `protected`, `allow_suppressions` | файлы и исключения для `gate_guard` и `hook_protect` |
| `hooks` | включает хуки режима с присутствием; без секции хуки ничего не делают |
| `diff_gates` | пороги для `diff_gates.py all --base <sha> --config powerpuff.yaml` |

Подробнее: `docs/design-gates.md`, `docs/diff-gates.md`.

## Стеки
Ядро стек не знает. Пресеты команд — стартовые значения, а не код: .NET, TypeScript, Python, Rust, Go и любой другой через ручной профиль; таблица в `docs/design-gates.md` (раздел «Любой стек»), отчёты покрытия и сложности по стекам — `docs/diff-gates.md`. Пресеты Rust и Go составлены по документации инструментов и на реальных прогонах не проверены.

## Состояние
Прогресс по плану: **14 из 43 критериев (33%)**, детали — `docs/ROADMAP.md`. Закрыты этапы основания и большая часть гейтов, линтера спеки и механики очереди.
Проверено на реальном прогоне только `forge` на одной спеке (`FORGE: done`, критерий 1.1). Подготовка спеки, очередь, карточки аудита, хуки и `diff_gates` покрыты тестами с подставным `claude` и ещё не прошли реальный прогон; пилоты на реальных системах и бенч-сравнение с plain не сделаны.

## Требования
Python 3.10+, git, `claude` CLI в PATH, PyYAML необязателен (без него `queue.yaml` пишется как JSON). Пошагово: `docs/quickstart.md`.

## Документы
| Документ | О чём |
|---|---|
| `docs/quickstart.md` | установка и первый цикл |
| `docs/migration-from-bob-pipeline.md` | переход с bob-pipeline |
| `docs/ROADMAP.md`, `docs/FINDINGS.md` | план с критериями, журнал находок |
| `docs/design-system.md`, `docs/design-forge.md`, `docs/design-gates.md` | дизайн |
| `docs/prepare-format.md`, `docs/queue-format.md`, `docs/audit-format.md` | форматы и коды выхода |
| `docs/diff-gates.md`, `docs/attended-mode.md` | гейты по диффу, хуки |
| `docs/from-bob-pipeline.md` | что взято из bob-pipeline и что нет |
| `docs/research/` | исследования с источниками |
