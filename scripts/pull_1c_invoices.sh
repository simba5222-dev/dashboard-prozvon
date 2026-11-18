#!/bin/bash
# Подгрузка новых счетов покупателям из 1С в раздел «Счета» Synergy — один проход (таймер dashboard-invoices, раз в 10 минут).
#
# 1. На офисе читаем 1С (только чтение) за последние 3 дня: счёт могут провести задним числом. Нужен туннель WireGuard `pfsense`
#    и ключ ssh пользователя agent (алиас `office`) — поэтому юнит работает под agent.
# 2. От claude (запись в базу дашборда и в Synergy): load_1c_invoices.py — компании по ИНН (недостающие создаются), счёт со строками
#    и назначением платежа «Оплата по счёту № …». Загрузчик идемпотентен: уже загруженные счета пропускает.
# Уже загруженный счёт позже НЕ обновляется (правка суммы в 1С в Synergy не попадёт) — решение на 18.11.2026.
# Любая ошибка — ненулевой код выхода: его видит утренняя диагностика (selfcheck, таймеры).
set -euo pipefail
cd /home/claude/dashboard
D=data/payments
SINCE=$(date -d '3 days ago' +%F)
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 office)
PY=/home/user/ocr_test_vlm/.venv/bin/python

"${SSH[@]}" "cd ~/scan_registry && $PY onec_invoices.py --since $SINCE" > "$D/invoices.auto.tmp"
python3 -c "import json; json.load(open('$D/invoices.auto.tmp'))"   # не принимаем оборванный ответ
mv "$D/invoices.auto.tmp" "$D/invoices_auto.json"

out=$(sudo -n -u claude .venv/bin/python scripts/load_1c_invoices.py --file "$D/invoices_auto.json" --apply 2>&1 | grep -v "HTTP Request") || { echo "$out"; exit 1; }
echo "$out" | grep -E "создан|ВНИМАНИЕ|ОШИБКА|итого" || true
# ФИО менеджера — из комментария счёта в 1С (решение владельца 18.11.2026): список сотрудников из 1С и разбор комментариев
"${SSH[@]}" "cd ~/scan_registry && $PY onec_users.py" > "$D/roster.auto.tmp"
python3 -c "import json; assert len(json.load(open('$D/roster.auto.tmp'))) > 20"
mv "$D/roster.auto.tmp" "$D/roster_1c.json"
sudo -n -u claude .venv/bin/python scripts/invoice_managers.py --file "$D/invoices_auto.json" --apply 2>&1 | grep -E "^счетов|записано" || true
if echo "$out" | grep -q "ОШИБКА"; then echo "ошибка при загрузке счетов"; exit 1; fi
echo "готово $(date -Is)"
