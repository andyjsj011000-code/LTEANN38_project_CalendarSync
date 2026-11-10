export async function verifyConnectorInteractions({evaluate,call,sessionId,sleep,capture}) {
  const affordance=await evaluate(`(()=>[...document.querySelectorAll('[data-connector-for]')].map(a=>({link:a.tagName.toLowerCase()==='a',href:a.getAttribute('href'),label:a.getAttribute('aria-label'),width:a.getBoundingClientRect().width,height:a.getBoundingClientRect().height})))()`);
  if(affordance.length!==4||affordance.some(a=>!a.link||!a.href||!a.label||a.width<43||a.height<43))throw new Error('Connectors need named, keyboard-accessible links with 44px targets');
  const maxFont=await evaluate(`(()=>{const panel=document.querySelector('#sync-panel'),plus=[...panel.querySelectorAll('.readable-controls button')].find(b=>b.textContent==='+');for(let i=0;i<6;i++)plus.click();const svg=panel.querySelector('.portrait-diagram');const outside=[...svg.querySelectorAll('[data-portrait-node]')].filter(g=>{const r=g.querySelector('rect').getBBox();return [...g.querySelectorAll('text')].some(t=>{const b=t.getBBox();return b.x<r.x-1||b.y<r.y-1||b.x+b.width>r.x+r.width+1||b.y+b.height>r.y+r.height+1;});}).map(g=>g.dataset.portraitNode);return {outside,maxDisabled:plus.disabled,minFont:Number(panel.querySelector('.diagram-preview').dataset.minimumTextPx),overflow:document.documentElement.scrollWidth>innerWidth};})()`);
  if(maxFont.outside.length||!maxFont.maxDisabled||maxFont.minFont!==28||maxFont.overflow)throw new Error('Maximum font size must remain legible: '+JSON.stringify(maxFont));
  await evaluate("[...document.querySelectorAll('#sync-panel .readable-controls button')].find(b=>b.textContent==='기본 크기').click()");
  async function activate(edge,from,keyboard=false,touch=false){
    const selector=`[data-connector-for="${edge}"][data-connector-node="${from}"]`;
    await evaluate(`(()=>{const a=document.querySelector('${selector}');window.scrollTo({top:a.getBoundingClientRect().top+scrollY-260,behavior:'instant'});a.focus({preventScroll:true});})()`);
    await sleep(150);
    if(keyboard){
      await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13},sessionId);
      await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13},sessionId);
    }else{
      const p=await evaluate(`(()=>{const r=document.querySelector('${selector}').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
      if(touch){
        await call('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:1},sessionId);
        await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[p]},sessionId);await sleep(70);
        await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]},sessionId);
        await call('Emulation.setTouchEmulationEnabled',{enabled:false},sessionId);
      }else{
        await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...p},sessionId);
        await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...p},sessionId);
      }
    }
    await sleep(1050);
  }
  async function check(edge,target,aligned=true){
    const state=await evaluate(`(()=>{const g=document.querySelector('[data-portrait-node="${target}"]'),svg=g.closest('svg'),r=g.getBoundingClientRect(),nav=document.querySelector('.readable-nav');return {focused:document.activeElement===g,top:r.top,bottom:r.bottom,viewport:innerHeight,atBottom:scrollY>=document.documentElement.scrollHeight-innerHeight-2,navBottom:nav.getBoundingClientRect().bottom,edge:svg.dataset.pinnedEdge,related:[...svg.querySelectorAll('.portrait-paths>path.is-related')].map(p=>p.dataset.portraitEdge),clearVisible:!nav.querySelector('[data-clear-selection]').hidden,status:nav.querySelector('[data-selection-status]').textContent};})()`);
    if(!state.focused||state.edge!==edge||state.related.length!==1||state.related[0]!==edge||!state.clearVisible||!state.status||state.top<state.navBottom+8||state.top>state.viewport-100||(aligned&&!state.atBottom&&state.top>state.navBottom+90))throw new Error('Connector arrival failed: '+JSON.stringify(state));
    return state;
  }
  const states=[];
  for(const [edge,source,target] of [['proof-retry','proof','retry'],['enabled-skip','enabled','skip']]){
    await activate(edge,source,false,edge==='enabled-skip');states.push(await check(edge,target));await capture(edge+'-arrival-light');
    if(edge==='proof-retry'){
      await evaluate("document.documentElement.dataset.theme='dark'");await sleep(120);await capture('branch-A-arrival-dark');await evaluate("document.documentElement.dataset.theme='light'");
      for(const width of [1200,1440]){await call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false},sessionId);await sleep(450);await check(edge,target,false);}
      await evaluate("[...document.querySelectorAll('#sync-panel .readable-controls button')].find(b=>b.textContent==='+').click()");await sleep(200);await check(edge,target,false);
      await capture('branch-A-enlarged');
      await evaluate("[...document.querySelectorAll('#sync-panel .readable-controls button')].find(b=>b.textContent==='기본 크기').click()");await sleep(200);await check(edge,target,false);
    }
    await activate(edge,target,true);states.push(await check(edge,source));
    await evaluate("document.querySelector('[data-clear-selection]').click()");
    if(await evaluate("document.querySelectorAll('.portrait-diagram.has-active').length"))throw new Error('Visible clear action failed');
  }
  await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]},sessionId);
  await activate('proof-retry','proof',true);await check('proof-retry','retry');
  await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27},sessionId);
  if(await evaluate("document.querySelectorAll('.portrait-diagram.has-active').length"))throw new Error('Escape failed to clear branch');
  await call('Emulation.setEmulatedMedia',{features:[]},sessionId);
  await evaluate("window.scrollTo({top:0,behavior:'instant'})");
  return {ok:true,affordance,states,maxFont,reducedMotion:true,touch:true,preservedAfterResizeAndZoom:true};
}
