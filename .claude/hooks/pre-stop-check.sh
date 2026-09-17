#!/bin/bash
# Stop-хук: не дать закончить ответ, оставив в дереве сломанный код.
#
# Здесь это не формальность. Таймеры dashboard-collect, dashboard-records и
# dashboard-leads запускаются по расписанию и берут файлы с диска как есть —
# сломанный .py подхватится ближайшим запуском, а узнаем мы об этом из журнала
# через час.
#
# Exit 0 — разрешить, exit 2 — заблокировать (stderr уходит Claude).

set -uo pipefail
cd "${CLAUDE_PROJECT_DIR:-/home/claude/dashboard}" || exit 0

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

ERRORS=""

# 1. Синтаксис изменённых .py — и отслеживаемых, и новых (git add -N не нужен:
#    берём ещё и untracked, иначе новый файл проверку минует).
PY_FILES=$(
    {
        git diff --name-only HEAD -- '*.py' 2>/dev/null
        git diff --cached --name-only -- '*.py' 2>/dev/null
        git ls-files --others --exclude-standard -- '*.py' 2>/dev/null
    } | sort -u
)

if [ -n "$PY_FILES" ]; then
    while IFS= read -r file; do
        [ -f "$file" ] || continue
        # compile() вместо py_compile: та же проверка синтаксиса, но без записи __pycache__.
        if ! err=$("$PY" -c "import sys; src=open(sys.argv[1], encoding='utf-8').read(); compile(src, sys.argv[1], 'exec')" "$file" 2>&1); then
            ERRORS="${ERRORS}  Синтаксическая ошибка в ${file}: $(echo "$err" | tail -1)
"
        fi
    done <<< "$PY_FILES"
fi

# 2. Отладочный мусор в добавленных строках.
DIFF=$(git diff HEAD 2>/dev/null; git diff --cached 2>/dev/null)
if [ -n "$DIFF" ]; then
    DEBUG=$(echo "$DIFF" | grep '^+' | grep -E '(import pdb|pdb\.set_trace|breakpoint\()' | head -5)
    [ -n "$DEBUG" ] && ERRORS="${ERRORS}  Отладочный код:
${DEBUG}
"
fi

if [ -n "$ERRORS" ]; then
    printf 'Задача не закончена — в рабочем дереве есть проблемы:\n%s\nПочините и повторите. Таймеры берут файлы с диска как есть.\n' "$ERRORS" >&2
    exit 2
fi

# 3. Мягкое напоминание: маркер задачи остался, хотя коммиты были.
if [ -f .claude/current-task.md ]; then
    COMMITS=$(git log --oneline --since='3 hours ago' 2>/dev/null | wc -l)
    if [ "$COMMITS" -gt 0 ]; then
        echo "Напоминание: .claude/current-task.md ещё на месте. Если задача закрыта — удалите маркер и обновите NEXT_SESSION.md." >&2
    fi
fi

exit 0
