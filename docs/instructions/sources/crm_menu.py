from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width':1440,'height':900}, locale='ru-RU', storage_state='crm/state.json')
    pg = ctx.new_page()
    pg.goto('https://app.synergycrm.ru/indiboards'); pg.wait_for_timeout(3500)
    pg.mouse.click(646,25); pg.wait_for_timeout(1000); pg.screenshot(path='crm/menu_more.png')
    print('more:', pg.evaluate("[...document.querySelectorAll('a')].filter(a=>a.offsetParent).map(a=>a.textContent.trim().replace(/\\s+/g,' ').slice(0,30)+' -> '+a.getAttribute('href')).slice(0,60)"))
    pg.keyboard.press('Escape')
    pg.mouse.click(1361,25); pg.wait_for_timeout(1200); pg.screenshot(path='crm/menu_burger.png')
    print('burger:', pg.evaluate("[...document.querySelectorAll('a')].filter(a=>a.offsetParent).map(a=>a.textContent.trim().replace(/\\s+/g,' ').slice(0,30)+' -> '+a.getAttribute('href')).slice(0,80)"))
    b.close()
