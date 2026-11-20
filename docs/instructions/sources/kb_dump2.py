import json, os
from playwright.sync_api import sync_playwright
os.makedirs('crm/kb/img', exist_ok=True)
bycat=json.load(open('crm/kb/ids_by_cat.json'))
ids={}
for cat,v in bycat.items():
    for h in v: ids.setdefault(h,[]).append(cat) if cat!='Все статьи' else ids.setdefault(h,[])
out=[]
with sync_playwright() as p:
    b=p.chromium.launch(); ctx=b.new_context(viewport={'width':1440,'height':900},locale='ru-RU',storage_state='crm/state.json'); pg=ctx.new_page()
    for h in sorted(ids,key=lambda x:int(x.split('/')[-1]),reverse=True):
        aid=h.split('/')[-1]
        pg.goto('https://app.synergycrm.ru'+h); pg.wait_for_timeout(2500)
        data=pg.evaluate("""(()=>{
          const imgs=[...document.querySelectorAll('img')].filter(i=>i.naturalWidth>180&&i.offsetParent).map(i=>i.src);
          const cands=[...document.querySelectorAll('div')].filter(d=>d.innerText&&d.innerText.length>50);
          const h1=document.querySelector('h1,h2');
          return {title:document.title.replace(' - Synergy - CRM для разумных продаж',''), imgs, text:document.body.innerText};
        })()""")
        saved=[]
        for i,u in enumerate(dict.fromkeys(data['imgs'])):
            try:
                r=ctx.request.get(u)
                if r.ok and len(r.body())>3000:
                    ext='png' if 'png' in (r.headers.get('content-type','')) else 'jpg'
                    fn=f'crm/kb/img/{aid}_{i}.{ext}'; open(fn,'wb').write(r.body()); saved.append(fn)
            except Exception as e: pass
        pg.screenshot(path=f'crm/kb/page_{aid}.png', full_page=True)
        out.append({'id':aid,'cats':ids[h],'title':data['title'],'text':data['text'],'images':saved})
        print(aid, data['title'][:50], len(data['text']), len(saved), flush=True)
    json.dump(out,open('crm/kb/articles.json','w'),ensure_ascii=False,indent=1)
    b.close()
print('DONE')
