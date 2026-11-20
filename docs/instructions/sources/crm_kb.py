import json
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width':1440,'height':900}, locale='ru-RU', storage_state='crm/state.json')
    pg = ctx.new_page()
    pg.goto('https://app.synergycrm.ru/articles'); pg.wait_for_timeout(4000)
    pg.screenshot(path='crm/kb_home.png')
    print('url', pg.url, pg.title())
    print(pg.inner_text('body')[:2500])
    links = pg.evaluate("[...document.querySelectorAll('a')].filter(a=>a.offsetParent).map(a=>[a.textContent.trim().replace(/\\s+/g,' ').slice(0,60), a.getAttribute('href')])")
    print(len(links)); print(json.dumps([l for l in links if '/articles' in (l[1] or '')], ensure_ascii=False)[:3000])
    b.close()
