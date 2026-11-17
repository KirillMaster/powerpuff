---
description: Автономный прогон — запустить queue_run.py по queue.yaml в фоне; повтор продолжает с места
argument-hint: "<queue.yaml> [--no-wait] [--only U1,U2] [--dry-run] [--attended]"
---

# /powerpuff:queue — автономный прогон очереди

Оркестратор — скрипт `${CLAUDE_PLUGIN_ROOT}/scripts/queue_run.py`, не вы. Ничего не реализуйте, вопросов не задавайте. Формат: `docs/queue-format.md` в плагине.

1. `<root>` = `git rev-parse --show-toplevel`.
2. `--dry-run`: выполнить в переднем плане и показать порядок единиц. Иначе запустить отдельным процессом из `<root>`, `$ARGUMENTS` без изменений:
   - POSIX: `PYTHONUNBUFFERED=1 nohup python3 ${CLAUDE_PLUGIN_ROOT}/scripts/queue_run.py $ARGUMENTS --repo <root> > .queue-run.log 2>&1 &`
   - Windows: `powershell.exe -NoProfile -Command "Start-Process cmd -WindowStyle Hidden -ArgumentList '/c set PYTHONUNBUFFERED=1&& python ${CLAUDE_PLUGIN_ROOT}/scripts/queue_run.py $ARGUMENTS --repo <root> > .queue-run.log 2>&1'"`
3. Сказать человеку тремя строками: прогон запущен отдельно; лог — `<root>/.queue-run.log` (последняя строка `QUEUE: done|partial|failed|config error`) и `<root>/.powerpuff/runs/<run_id>/<id>.log` на единицу; состояние — `<root>/.powerpuff/runs/<run_id>/state.json`, итог — `SUMMARY.md` и `summary.json` рядом.
4. Повтор той же команды продолжает прогон: `needs-answer`, `blocked` и `skipped-budget` возвращаются в `pending`; ответы на блокирующие вопросы пишутся в `.forge/questions.json` единицы (путь указан в причине). Ошибка конфигурации (выход 3) печатает все проблемы списком, ничего не создаётся.
5. Выход 3 с `--no-wait` = лимит подписки; повтор после сброса продолжит.
6. Единицы с `risk`, не входящим в `auto_merge`, остаются `done` до аудита.
