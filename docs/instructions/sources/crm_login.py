import json, sys
from playwright.sync_api import sync_playwright
L = open('/home/agent/mac/Documents/Доступ помощников к платежам.txt', encoding='utf-8').read().split('\n')
login, pw = L[6].strip(), L[7].strip()
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width': 1440, 'height': 900}, locale='ru-RU')
    pg = ctx.new_page()
    pg.goto('https://app.synergycrm.ru/accounts/sign_in'); pg.wait_for_timeout(1500)
    pg.fill('#login-email', login); pg.fill('#login-password', pw); pg.press('#login-password', 'Enter')
    pg.wait_for_timeout(7000)
    print('url:', pg.url, '| title:', pg.title())
    err = pg.locator('text=Неверный').count()
    print('ошибка входа:', bool(err))
    if not err:
        ctx.storage_state(path='crm/state.json')
        pg.screenshot(path='crm/home.png')
        print('меню:', pg.evaluate("[...document.querySelectorAll('nav a, header a, .menu a, a')].map(a=>a.textContent.trim()).filter(t=>t&&t.length<30).slice(0,60)"))
    b.close()
