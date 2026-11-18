#!/bin/bash
# Подгрузка новых входящих платежей из 1С в «Платежи» Synergy — один проход (запускает таймер dashboard-payments, раз в 30 минут).
#
# 1. На офисе читаем 1С (только чтение) за последние 3 дня с запасом: документ в 1С могут провести задним числом.
#    Нужен туннель WireGuard `pfsense` и ключ ssh пользователя agent (алиас `office`) — поэтому юнит работает под agent.
# 2. Дальше от claude (запись в базу дашборда): наши организации и счета, компании-плательщики и их счета,
#    платежи, привязка реквизитов к платежам. Каждый шаг идемпотентен: повторный проход ничего не дублирует.
# Правила отбора (в load_1c_payments.py и sync_requisites.py): только оплаты покупателей, без платежей между своими
# организациями и без МСК Техноресурс; статус «не проведён». Решения владельца 17.11.2026.
# Любая ошибка — ненулевой код выхода: его видит утренняя диагностика (selfcheck, таймеры).
set -euo pipefail
cd /home/claude/dashboard
D=data/payments
SINCE=$(date -d '3 days ago' +%F)
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 office)
PY=/home/user/ocr_test_vlm/.venv/bin/python

for what in incoming requisites; do
    "${SSH[@]}" "cd ~/scan_registry && $PY onec_$what.py --since $SINCE" > "$D/$what.auto.tmp"
    python3 -c "import json,sys; json.load(open('$D/$what.auto.tmp'))"   # не принимаем оборванный ответ
done
mv "$D/incoming.auto.tmp" "$D/incoming_auto.json"
mv "$D/requisites.auto.tmp" "$D/requisites_auto.json"

run() {   # шаги пишут «ОШИБКА» в вывод, но возвращают 0 — ловим сами
    local out
    out=$(sudo -n -u claude env PAY_TAG=auto .venv/bin/python "$@" 2>&1 | grep -v "HTTP Request") || { echo "$out"; exit 1; }
    echo "$out" | grep -E "создан|создано|ОШИБКА|итого|обработано" || true
    if echo "$out" | grep -q "ОШИБКА"; then echo "ошибка на шаге: $*"; exit 1; fi
}
run scripts/sync_requisites.py orgs --apply
run scripts/sync_requisites.py accounts --apply
run scripts/sync_requisites.py payers --apply
run scripts/load_1c_payments.py --file "$D/incoming_auto.json" --apply
run scripts/link_payment_requisites.py --apply
echo "готово $(date -Is)"
