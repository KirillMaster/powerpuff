# Attended mode: хуки Claude Code

Два хука плагина для сессий с человеком. Оба opt-in: без `powerpuff.yaml` или без секции `hooks:` это тихий no-op (exit 0).

| Хук | Событие | Что делает |
|---|---|---|
| `scripts/hook_stop.py` | Stop | Не даёт завершить ход при красных gates tier `iteration` |
| `scripts/hook_protect.py` | PreToolUse (`Edit\|Write\|MultiEdit\|NotebookEdit`) | Блокирует правку protected-файлов |

Регистрация: `hooks/hooks.json`. Корень проекта = ближайший предок `cwd` с `powerpuff.yaml` (поиск останавливается на корне git).

## Включение

```yaml
hooks:
  stop_gates: true
  protect: true
  stop_max_blocks: 3
  stop_timeout_s: 300
```

| Ключ | По умолчанию | Смысл |
|---|---|---|
| `stop_gates` | false | Включает Stop-хук |
| `protect` | false | Включает protect-хук |
| `stop_max_blocks` | 3 | Подряд блокировок на сессию до отпускания |
| `stop_timeout_s` | 300 | Таймаут каждого gate и `format_fix` |

## Stop

| Ситуация | Результат |
|---|---|
| Дерево чистое (нет изменений к HEAD, нет untracked) | exit 0, gates не запускаются |
| Дерево совпадает с последним зелёным (HEAD + status + mtime) | exit 0, кэш |
| Gates зелёные | exit 0, счётчик сброшен |
| Красный gate или отсутствует инструмент | exit 2: имена упавших gates + хвост вывода (до 40 строк, всего до 6000 символов), в конце «Fix the cause; do not disable or skip the check.» |
| `POWERPUFF_DRIVER=1` | exit 0 (решает драйвер) |

Порядок: `format_fix` (если задан), затем gates `iteration` через `forge_check.check_gates`. Отсутствующий инструмент = красный gate.

Loop guard: счётчик подряд идущих блокировок по `session_id` в `.powerpuff/hook-state.json`. После `stop_max_blocks` хук отпускает ход (exit 0) и пишет в stderr `gates still red after N attempts — stopping for the human`; счётчик сбрасывается при зелёном и после отпускания.

## Protect

| Цель правки | Результат |
|---|---|
| Совпадает с glob из `protected` (то же сопоставление, что в `gate_guard`) | exit 2: файл, glob, «ask the human to change it; do not work around it» |
| Сам `powerpuff.yaml` | exit 2 |
| Остальное, пути вне корня проекта | exit 0 |

Путь нормализуется относительно корня, слеши прямые; на Windows регистр не важен. Действует и внутри сессий драйвера.

## Связь с автономным драйвером

| Аспект | Драйвер (`forge_run`, `prepare_run`) | Attended |
|---|---|---|
| Кто решает про gates | Скрипт | Stop-хук |
| Stop-хук | Отключён (`POWERPUFF_DRIVER=1` в окружении `claude`) | Активен |
| Protect-хук | Активен | Активен |
| Очередь `--attended` | Пауза после каждой единицы (см. `docs/queue-format.md`) | Не связано с хуками |

## Осознанный обход

| Способ | Действие |
|---|---|
| Разовая правка protected-файла | Человек запускает сессию с `POWERPUFF_ALLOW_PROTECTED=1` |
| Выключить хук | `stop_gates: false` / `protect: false` в `powerpuff.yaml` (правит человек) |
| Сбросить кэш и счётчики | Удалить `.powerpuff/hook-state.json` |

Модель не должна обходить хуки: ни переименованием файла, ни записью через shell, ни отключением проверки.

Внутренняя ошибка хука: exit 0 и строка `powerpuff hook error: ...` в stderr; сессия не падает.
