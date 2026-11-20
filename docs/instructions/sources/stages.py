from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b=p.chromium.launch(); ctx=b.new_context(viewport={'width':1440,'height':900},locale='ru-RU',storage_state='crm/state.json'); pg=ctx.new_page()
    pg.goto('https://app.synergycrm.ru/orders'); pg.wait_for_timeout(4500)
    pg.locator('td:has-text("110210")').first.click(); pg.wait_for_timeout(8000)
    pg.locator('text=Этап: 1 из 11').first.click(); pg.wait_for_timeout(2500)
    print(pg.evaluate("""(()=>{const seen=new Set();const out=[];
      [...document.querySelectorAll('*')].forEach(e=>{const t=(e.textContent||'').trim(); if(e.children.length===0&&t&&t.length<40&&/Новый|Общая заявка|Подбор|передачу|Согласовани|Пропущенный|Выставлен|Сделка/.test(t)&&!seen.has(t)){seen.add(t);out.push(t)}});
      return out})()"""))
    b.close()
