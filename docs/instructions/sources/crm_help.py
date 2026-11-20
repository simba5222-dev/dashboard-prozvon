from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width':1440,'height':900}, locale='ru-RU', storage_state='crm/state.json')
    pg = ctx.new_page()
    pg.goto('https://app.synergycrm.ru/indiboards'); pg.wait_for_timeout(4000)
    pg.screenshot(path='crm/home.png')
    # ссылки со словами про знания/помощь
    links = pg.evaluate("""[...document.querySelectorAll('a')].map(a=>[a.textContent.trim().replace(/\\s+/g,' ').slice(0,40), a.href]).filter(x=>/помощ|знани|база|wiki|help|инструк|обуч/i.test(x[0]+x[1]))""")
    for l in links: print(l)
    b.close()
