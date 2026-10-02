#!/bin/bash
# Замена доступов Яндекса сразу во всех трёх точках: дашборд, здешний ASR, боевой ASR.
# Нужен, когда меняется облако (новый сервисный аккаунт и новый ключ).
# Если облако то же, а меняется только платёжный аккаунт — этот скрипт НЕ нужен.
#
#   ./swap_yandex.sh КЛЮЧ ПАПКА [СЕРВИСНЫЙ_АККАУНТ]
#
# Ассистента и поисковый индекс скрипт не трогает: они живут внутри папки
# и при переезде в другое облако собираются заново (scripts/build_memory.py).
set -euo pipefail

KEY="${1:?нужен API-ключ (AQVN...)}"
FOLDER="${2:?нужен id каталога (b1g...)}"
SA="${3:-}"
RU_KEY=/home/agent/.ssh/ru-proxy_ed25519
RU=root@91.220.109.49
STAMP=$(date +%Y%m%d-%H%M%S)

patch_env() {  # файл, префикс
  local file=$1 pref=$2
  sudo cp "$file" "$file.bak-$STAMP"
  sudo sed -i -E "s|^${pref}_YANDEX_API_KEY=.*|${pref}_YANDEX_API_KEY=${KEY}|" "$file"
  sudo sed -i -E "s|^${pref}_YANDEX_FOLDER=.*|${pref}_YANDEX_FOLDER=${FOLDER}|" "$file"
  [ -n "$SA" ] && sudo sed -i -E "s|^${pref}_YANDEX_SA_ID=.*|${pref}_YANDEX_SA_ID=${SA}|" "$file"
  echo "  правлено: $файл (копия $file.bak-$STAMP)"
}

echo "=== 1. проверяю ключ до всякой правки"
code=$(curl -s -m 25 -o /dev/null -w '%{http_code}' \
  -H "Authorization: Api-Key $KEY" -H 'Content-Type: application/json' \
  -d "{\"modelUri\":\"gpt://${FOLDER}/yandexgpt-lite/latest\",\"text\":\"проверка\"}" \
  https://llm.api.cloud.yandex.net/foundationModels/v1/tokenize)
if [ "$code" != "200" ]; then
  echo "  ключ не работает с этой папкой (ответ $code) — ничего не меняю"; exit 1
fi
echo "  ключ рабочий"

echo "=== 2. дашборд"
patch_env /home/claude/dashboard/.env DASH
echo "=== 3. здешний ASR"
patch_env /home/claude/asr-vats-megafon/.env ASR
echo "=== 4. боевой ASR"
ssh -i "$RU_KEY" "$RU" "cp /opt/asr/.env /opt/asr/.env.bak-$STAMP \
  && sed -i -E 's|^ASR_YANDEX_API_KEY=.*|ASR_YANDEX_API_KEY=${KEY}|' /opt/asr/.env \
  && sed -i -E 's|^ASR_YANDEX_FOLDER=.*|ASR_YANDEX_FOLDER=${FOLDER}|' /opt/asr/.env \
  && echo '  правлено: /opt/asr/.env'"

echo "=== 5. перезапуск по очереди"
sudo systemctl restart dashboard && echo "  dashboard поднят"
sudo systemctl restart asr       && echo "  asr (Амстердам) поднят"
ssh -i "$RU_KEY" "$RU" 'systemctl restart asr && echo "  asr (боевой) поднят"'

echo
echo "Осталось руками: собрать базу знаний в новой папке —"
echo "  cd /home/claude/dashboard && .venv/bin/python scripts/build_memory.py"
echo "и записать новые ASSISTANT_ID и SEARCH_INDEX в dashboard/.env"
