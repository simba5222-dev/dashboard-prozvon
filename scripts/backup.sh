#!/bin/bash
# Резервное копирование: база с разбором, записи разговоров, настройки, конфиги.
#
# Запускается таймером dashboard-backup каждую ночь в 02:00 по Москве.
# Работает под agent: нужен ssh-ключ к боевому серверу в России.
#
# Схема встречная — каждый сервер хранит копию второго:
#   Амстердам → Россия:  база дашборда, записи прозвона, настройки
#   Россия → Амстердам:  записи боевого ASR и его код
#
# Отдельного облака нет намеренно: два сервера в разных странах и у разных
# площадок уже дают географическое разнесение, а третья копия требует ключей
# доступа, которые сами станут дырой.
#
# ВАЖНО: бэкап считается сделанным, только если в логе есть строка «ГОТОВО».
# Скрипт может упасть на середине и оставить файлы, которые выглядят целыми.

set -uo pipefail

DASH=/home/claude/dashboard
ASR=/home/claude/asr-vats-megafon
LOCAL=/var/backups/projects
PROD_HOST=root@91.220.109.49
PROD_KEY=/home/agent/.ssh/ru-proxy_ed25519
SSH="ssh -o ConnectTimeout=15 -o BatchMode=yes -i $PROD_KEY"
KEEP_DAYS=30
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$LOCAL/backup.log"

mkdir -p "$LOCAL"/{db,secrets,configs,prod} || exit 1
exec >> "$LOG" 2>&1
echo "=== $(date -Iseconds) начало"

fail() { echo "ОШИБКА: $*"; exit 1; }

# 1. База дашборда. Именно .backup, а не копирование файла: база живая, рядом
#    лежит журнал WAL, и простой cp даёт битый снимок.
"$DASH/.venv/bin/python" - "$LOCAL/db/dashboard-$STAMP.db" <<'PY' || fail "дамп базы"
import sqlite3, sys
src = sqlite3.connect("file:/home/claude/dashboard/data/dashboard.db?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
gzip -f "$LOCAL/db/dashboard-$STAMP.db" || fail "сжатие базы"
echo "база: $(du -h "$LOCAL/db/dashboard-$STAMP.db.gz" | cut -f1)"

# 2. Настройки и конфиги. Здесь токены ВАТС, Synergy и OpenAI — права строгие.
tar czf "$LOCAL/secrets/env-$STAMP.tgz" \
    -C / home/claude/dashboard/.env home/claude/asr-vats-megafon/.env 2>/dev/null
chmod 600 "$LOCAL/secrets/env-$STAMP.tgz"
tar czf "$LOCAL/configs/system-$STAMP.tgz" \
    -C / etc/systemd/system/dashboard-*.service etc/systemd/system/dashboard-*.timer \
    etc/systemd/system/asr.service etc/systemd/system/mac-mount.* \
    etc/nginx/sites-enabled 2>/dev/null
echo "настройки и конфиги сняты"

# 3. Записи разговоров — зеркалом, без версий: они не меняются, только прибывают.
rsync -a --delete-after "$DASH/data/records/" "$LOCAL/records/" || fail "зеркало записей"
echo "записи: $(du -sh "$LOCAL/records" | cut -f1)"

# 4. Копия на боевой сервер в России.
if $SSH "$PROD_HOST" 'mkdir -p /opt/backups/ams' 2>/dev/null; then
    rsync -a -e "$SSH" "$LOCAL/db/" "$PROD_HOST:/opt/backups/ams/db/" || echo "предупреждение: база не уехала"
    rsync -a -e "$SSH" "$LOCAL/secrets/" "$PROD_HOST:/opt/backups/ams/secrets/" || echo "предупреждение: настройки не уехали"
    rsync -a --delete-after -e "$SSH" "$LOCAL/records/" "$PROD_HOST:/opt/backups/ams/records/" || echo "предупреждение: записи не уехали"
    echo "копия отправлена в Россию"
else
    echo "предупреждение: боевой сервер недоступен, копия осталась только здесь"
fi

# 5. Встречное направление: забираем боевые записи и код себе.
rsync -a -e "$SSH" "$PROD_HOST:/opt/asr/data/" "$LOCAL/prod/data/" || echo "предупреждение: данные боевого не забрались"
$SSH "$PROD_HOST" 'cd /opt/asr && git bundle create /tmp/asr.bundle --all' >/dev/null 2>&1 \
    && rsync -a -e "$SSH" "$PROD_HOST:/tmp/asr.bundle" "$LOCAL/prod/asr-$STAMP.bundle" \
    && echo "код боевого забран" || echo "предупреждение: код боевого не забрался"

# 6. Ротация: дампы и архивы старше месяца не нужны, зеркала живут всегда.
find "$LOCAL/db" "$LOCAL/secrets" "$LOCAL/configs" "$LOCAL/prod" -maxdepth 1 -type f \
     -mtime +$KEEP_DAYS -delete 2>/dev/null
$SSH "$PROD_HOST" "find /opt/backups/ams/db /opt/backups/ams/secrets -maxdepth 1 -type f -mtime +$KEEP_DAYS -delete" 2>/dev/null

echo "свободно на диске: $(df -h / | tail -1 | awk '{print $4}')"
echo "ГОТОВО $(date -Iseconds)"
