#!/usr/bin/env bash
# SessionEnd: зеркалить живую память Claude в репозиторий и застейджить.
#
# Живая память лежит в ~/.claude/projects/<slug>/memory и существует только на
# диске этой машины. Если сервер потеряется, уроки уйдут вместе с ним, а код
# останется — он в git. Хук закрывает этот разрыв на каждом завершении сессии.
#
# Зеркалим и стейджим, но НЕ коммитим: коммит — действие по явной просьбе
# владельца. Застейженное подхватит ближайший обычный коммит.
#
# Всегда exit 0: помешать завершению сессии этот хук не должен.

set -uo pipefail

PROJ="${CLAUDE_PROJECT_DIR:-/home/claude/dashboard}"
REPO="$PROJ/memory"

SLUG=$(printf '%s' "$PROJ" | tr '/' '-')
LIVE="${HOME}/.claude/projects/${SLUG}/memory"

[ -d "$LIVE" ] || exit 0
[ -d "$REPO" ] || exit 0

# -u: не затирать файл в репозитории, если он новее живого (кто-то правил прямо там).
rsync -au --exclude='.consolidate-lock' "$LIVE/" "$REPO/" 2>/dev/null || exit 0

# Секрет-скан ПЕРЕД стейджем. Паттерны узкие: ложное срабатывание здесь дороже
# пропуска, потому что хук отказывает молча, а разбираться будет следующая сессия.
# Токены ВАТС и Synergy, ключи OpenAI, приватные ключи.
SUSPECT=$(grep -rlE -e '-----BEGIN [A-Z ]*PRIVATE KEY-----' \
                    -e 'sk-[A-Za-z0-9_-]{20,}' \
                    -e 'DASH_[A-Z_]*(TOKEN|KEY)[[:space:]]*=[[:space:]]*[A-Za-z0-9]' \
                    -e 'X-API-KEY:[[:space:]]*[A-Za-z0-9]' \
                    "$REPO" 2>/dev/null)

if [ -n "$SUSPECT" ]; then
    echo "memory-sync: в памяти похоже на секреты, в индекс не добавлено:" >&2
    echo "$SUSPECT" >&2
    echo "Файлы всё равно скопированы в $REPO (в git они не попадут, пока не уберёте секрет)." >&2
    exit 0
fi

git -C "$PROJ" add -- memory 2>/dev/null || true
exit 0
