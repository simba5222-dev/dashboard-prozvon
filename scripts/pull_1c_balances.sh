#!/bin/bash
# Раз в неделю: баланс расчётов с покупателями из 1С → поля компании в Synergy «Баланс по СК» (custom-30791) и «Баланс по АЛ» (custom-30792).
# 1. На офисе читаем 1С (только чтение): onec_balances.py → JSON. Нужен туннель WireGuard `pfsense` и ключ ssh пользователя agent (алиас `office`).
# 2. От claude (запись в базу и в Synergy): scripts/sync_balances.py --apply. Пишет только долги перед нами; у кого долг погашен — обнуляет.
# Ошибка на любом шаге — ненулевой код: её видит утренняя диагностика, а при поломке уходит сообщение в Telegram «Сторож звонков».
set -euo pipefail
cd /home/claude/dashboard
OUT=data/call_plan
ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 office \
    'cd ~/scan_registry && /home/user/ocr_test_vlm/.venv/bin/python onec_balances.py' > "$OUT/balances_1c.auto.tmp"
python3 -c "import json; d=json.load(open('$OUT/balances_1c.auto.tmp')); assert d['orgs']['СК'] and 'own_inns' in d"   # не принимаем оборванный ответ
mv "$OUT/balances_1c.auto.tmp" "$OUT/balances_1c.json"
out=$(sudo -n -u claude .venv/bin/python scripts/sync_balances.py --apply 2>&1 | grep -v "HTTP Request\|Synergy ответила") || { echo "$out"; exit 1; }
echo "$out" | tail -6
echo "$out" | grep -q "ошибок 0" || { echo "балансы записаны с ошибками"; exit 1; }
echo "готово $(date -Is)"
