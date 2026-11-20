import json, re, os, time
from playwright.sync_api import sync_playwright
os.makedirs('crm/kb/img', exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width':1440,'height':900}, locale='ru-RU', storage_state='crm/state.json')
    pg = ctx.new_page()
    ids = {}
    cats = [None,336,337,338,339,340,341,2,3,4,5,329,335]
    for c in cats:
        for page in range(1, 12):
            url = 'https://app.synergycrm.ru/articles/list' + ('?' if c else '') + (f'by_article_category_id={c}&' if c else '?') + f'page={page}'
            pg.goto(url); pg.wait_for_timeout(1500)
            found = pg.evaluate("[...document.querySelectorAll('a')].map(a=>a.getAttribute('href')).filter(h=>/^\\/articles\\/\\d+$/.test(h||''))")
            new = [f for f in found if f not in ids]
            for f in found: ids.setdefault(f, set()).add(c)
            if not new: break
    print('статей найдено:', len(ids), flush=True)
    out = []
    for h in sorted(ids, key=lambda x:int(x.split('/')[-1])):
        pg.goto('https://app.synergycrm.ru'+h); pg.wait_for_timeout(1500)
        title = pg.evaluate("(document.querySelector('h1')||document.querySelector('.article-title')||{}).innerText||document.title")
        body = pg.evaluate("""(()=>{const e=document.querySelector('.article-content, .article_body, .article-body, .ql-editor, article, .content')||document.body; return e.innerText})()""")
        imgs = pg.evaluate("[...document.querySelectorAll('img')].map(i=>i.src).filter(s=>/synergy|amazonaws|storage|uploads|attach|active_storage|rails/i.test(s) && !/avatar|logo|icon/i.test(s))")
        saved = []
        for i, u in enumerate(dict.fromkeys(imgs)):
            try:
                r = ctx.request.get(u)
                if r.ok and len(r.body()) > 3000:
                    fn = f"crm/kb/img/{h.split('/')[-1]}_{i}.png"; open(fn, 'wb').write(r.body()); saved.append(fn)
            except Exception: pass
        out.append({'id': h.split('/')[-1], 'cats': sorted(c for c in ids[h] if c), 'title': title.strip(), 'text': body.strip(), 'images': saved})
        print(h, title.strip()[:50], len(body), len(saved), flush=True)
    json.dump(out, open('crm/kb/articles.json', 'w'), ensure_ascii=False, indent=1)
    print('DONE', flush=True)
    b.close()
