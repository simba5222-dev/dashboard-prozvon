BLUR = r"""(()=>{
 const re=/(\+?\d[\d\s\-()]{8,}\d)|ООО|ИП |АО |ЗАО|ПАО|@/;
 const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
 const hit=[];
 while(w.nextNode()){const n=w.currentNode; if(re.test(n.textContent)&&n.parentElement) hit.push(n.parentElement)}
 hit.forEach(e=>e.style.filter='blur(5px)');
})()"""
