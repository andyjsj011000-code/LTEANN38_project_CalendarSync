import {verifyConnectorInteractions} from './connector-interactions.mjs';
import {measureRoutes} from './measure-routes.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const artifact=path.join(root,'oracle-guide.html');
const reviewName='browser-'+new Date().toISOString().replace(/[:.]/g,'-');
const out=process.env.ARCHIFY_REVIEW_DIR?path.resolve(process.env.ARCHIFY_REVIEW_DIR):path.join(root,'.checks',reviewName);await fs.mkdir(out,{recursive:true});
const profile=await fs.mkdtemp(path.join(out,'qa-profile-'));
const candidates=[process.env.ARCHIFY_BROWSER,'C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','/usr/bin/google-chrome','/usr/bin/chromium','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].filter(Boolean);
let browser;
for(const candidate of candidates){try{await fs.access(candidate);browser=candidate;break;}catch{}}
if(!browser)throw new Error('Set ARCHIFY_BROWSER to a Chrome or Edge executable path');
const chrome=spawn(browser,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:['ignore','ignore','pipe']});
let socket;
const pending=new Map();let nextId=1;const browserErrors=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const receipt={artifact,sha256:createHash('sha256').update(await fs.readFile(artifact)).digest('hex'),viewports:[],screenshots:[]};
try {
 const endpoint=await new Promise((resolve,reject)=>{let buf='';const timeout=setTimeout(()=>reject(new Error('Chrome debug startup timed out')),15000);chrome.stderr.on('data',chunk=>{buf+=chunk;const match=buf.match(/DevTools listening on (ws:\/\/[^\s]+)/);if(match){clearTimeout(timeout);resolve(match[1]);}});chrome.on('exit',code=>reject(new Error('Chrome exited '+code)));});
 socket=new WebSocket(endpoint);await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
 socket.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.method==='Runtime.exceptionThrown')browserErrors.push(data.params.exceptionDetails.exception?.description||data.params.exceptionDetails.text);if(data.id&&pending.has(data.id)){const p=pending.get(data.id);pending.delete(data.id);data.error?p.reject(new Error(JSON.stringify(data.error))):p.resolve(data.result);}});
 const call=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=nextId++;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));setTimeout(()=>{if(pending.delete(id))reject(new Error('CDP timed out: '+method));},15000).unref();});
 const target=await call('Target.createTarget',{url:'about:blank'});
 const {sessionId}=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true});
 const evaluate=async(expression)=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},sessionId);if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 await call('Page.enable',{},sessionId);
 await call('Runtime.enable',{},sessionId);
 await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false},sessionId);
 await call('Page.navigate',{url:pathToFileURL(artifact).href},sessionId);
 for(let i=0;i<80;i++){if(await evaluate("document.querySelectorAll('.readable-controls').length===4"))break;await sleep(100);}
 await evaluate('document.fonts.ready.then(()=>true)');await sleep(600);
 const metrics=await evaluate(`(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,nav:[...document.querySelectorAll('.readable-nav a')].map(a=>({id:a.hash,exists:!!document.querySelector(a.hash)})),diagrams:[...document.querySelectorAll('.diagram-preview')].map(p=>{const svg=p.querySelector('svg.portrait-diagram'),scale=svg.getScreenCTM().a;const texts=[...svg.querySelectorAll('text')].filter(t=>t.getClientRects().length&&getComputedStyle(t).display!=='none');return {title:p.getAttribute('aria-label'),minFont:Math.min(...texts.map(t=>parseFloat(getComputedStyle(t).fontSize)*scale)),font:getComputedStyle(texts[0]).fontFamily,height:svg.getBoundingClientRect().height,fontSizes:[...new Set(texts.map(t=>parseFloat(getComputedStyle(t).fontSize)))],width:p.clientWidth,contentWidth:p.scrollWidth,broken:texts.some(t=>t.textContent.includes('�'))};})}))()`);
 if(metrics.scrollWidth>metrics.width+1||metrics.diagrams.length!==4||metrics.nav.some(x=>!x.exists)||metrics.diagrams.some(x=>x.minFont<15.8||x.contentWidth>x.width+1||x.broken||!x.font.includes('Malgun')))throw new Error('Readability failed: '+JSON.stringify(metrics));
 receipt.viewports.push(metrics);console.log(JSON.stringify({readability:metrics}));
 const columns=await evaluate(`(()=>[...document.querySelectorAll('.portrait-diagram')].map(svg=>{const boxes=[...svg.querySelectorAll('[data-portrait-node] rect')].map(n=>n.getBoundingClientRect());return {columns:new Set(boxes.map(b=>Math.round(b.left))).size,overlap:boxes.some((a,i)=>boxes.some((b,j)=>i<j&&a.left<b.right-1&&a.right>b.left+1&&a.top<b.bottom-1&&a.bottom>b.top+1))};}))()`);
 if(columns.some(d=>d.columns!==2||d.overlap))throw new Error('Expected two columns without overlapping cards: '+JSON.stringify(columns));receipt.columns=columns;
 const routes=await evaluate(`(()=>[...document.querySelectorAll('.portrait-diagram')].map(svg=>{const cards=[...svg.querySelectorAll('[data-portrait-node]')].map(g=>({id:g.dataset.portraitNode,box:g.querySelector('rect').getBoundingClientRect()}));const matrix=svg.getScreenCTM();return {crossesCard:[...svg.querySelectorAll('[data-portrait-edge]')].some(path=>{const len=path.getTotalLength();for(let at=0;at<=len;at+=3){const p=path.getPointAtLength(at).matrixTransform(matrix);if(cards.some(c=>c.id!==path.dataset.from&&c.id!==path.dataset.to&&p.x>c.box.left+1&&p.x<c.box.right-1&&p.y>c.box.top+1&&p.y<c.box.bottom-1))return true;}return false;}),textOutsideCard:[...svg.querySelectorAll('[data-portrait-node]')].some(g=>{const b=g.querySelector('rect').getBoundingClientRect();return [...g.querySelectorAll('text')].some(t=>{const r=t.getBoundingClientRect();return r.left<b.left-1||r.right>b.right+1||r.top<b.top-1||r.bottom>b.bottom+1;});})};}))()`);
 if(routes.some(d=>d.crossesCard||d.textOutsideCard))throw new Error('Route geometry failed: '+JSON.stringify(routes));receipt.routes=routes;
 receipt.routing=await evaluate('('+measureRoutes.toString()+')()');
 console.log(JSON.stringify({routing:receipt.routing.map(({geometry,...r})=>r)}));
 if(receipt.routing.some(r=>r.overlappingPairs.length||r.crossings))throw new Error('Workflow arrows still overlap or cross');
 const connectors=await evaluate(`(()=>[...document.querySelectorAll('[data-connector-for]')].map(g=>({edge:g.dataset.connectorFor,node:g.dataset.connectorNode,label:g.querySelector('text').textContent})))()`);
 for(const [edge,source,target,label] of [['proof-retry','proof','retry','A'],['enabled-skip','enabled','skip','B']]){const pair=connectors.filter(c=>c.edge===edge);if(pair.length!==2||!pair.some(c=>c.node===source)||!pair.some(c=>c.node===target)||pair.some(c=>c.label!==label))throw new Error('Missing paired connector: '+edge);}receipt.connectors=connectors;
 const focus=await evaluate(`(()=>{const svg=document.querySelector('#requests-panel .portrait-diagram'),card=svg.querySelector('[data-portrait-node="claims"]');card.dispatchEvent(new MouseEvent('click',{bubbles:true}));const selected=svg.classList.contains('has-active')&&card.getAttribute('aria-pressed')==='true';const edges=[...svg.querySelectorAll('.portrait-paths>path.is-related')].map(e=>e.dataset.portraitEdge);card.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));const cleared=!svg.classList.contains('has-active');card.focus();card.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));const keyboard=card.getAttribute('aria-pressed')==='true';card.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));card.blur();return {selected,edges,cleared,keyboard};})()`);
 if(!focus.selected||!focus.cleared||!focus.keyboard||focus.edges.length!==3)throw new Error('Connection focus failed: '+JSON.stringify(focus));receipt.focus=focus;
 async function capture(name){const r=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},sessionId);const file=path.join(out,name+'.png');await fs.writeFile(file,Buffer.from(r.data,'base64'));receipt.screenshots.push(file);}
 receipt.connectorNavigation=await verifyConnectorInteractions({evaluate,call,sessionId,sleep,capture});
 await capture('overview-1440-light');
 const navTest=await evaluate("(()=>{document.querySelector('.readable-nav a[href=\"#requests-panel\"]').click();return true;})()");await sleep(1300);const navTop=await evaluate("document.querySelector('#requests-panel').getBoundingClientRect().top");if(navTop < 0 || navTop > 180)throw new Error('Section navigation failed: '+navTop);receipt.navigation={ok:true,requestsTop:navTop};
 for(const id of ['structure-panel','requests-panel','sync-panel','notifications-panel']){
  await evaluate(`(()=>{window.scrollTo({top:document.querySelector('#${id}').getBoundingClientRect().top+scrollY-95,behavior:'instant'});})()`);await sleep(450);await capture(id+'-1440-light');
 }
 for(const id of ['requests-panel','sync-panel','notifications-panel']){
  await evaluate(`(()=>{const p=document.querySelector('#${id} .diagram-preview');window.scrollTo({top:p.getBoundingClientRect().top+scrollY+p.clientHeight*.53-90,behavior:'instant'});})()`);await sleep(300);await capture(id+'-middle-1440-light');
 }
 const controls=await evaluate(`(()=>{const panel=document.querySelector('#requests-panel');const output=panel.querySelector('.readable-controls output');const buttons=[...panel.querySelectorAll('.readable-controls button')];buttons.find(b=>b.textContent==='+').click();const bigger=output.textContent;buttons.find(b=>b.textContent==='기본 크기').click();return {bigger,reset:output.textContent};})()`);
 if(!controls.bigger.includes('18px')||!controls.reset.includes('16px'))throw new Error('Reader controls failed');receipt.controls=controls;
 await evaluate("document.documentElement.dataset.theme='dark';window.scrollTo({top:document.querySelector('#requests-panel').getBoundingClientRect().top+scrollY-95,behavior:'instant'})");await sleep(450);await capture('requests-1440-dark');
 await call('Emulation.setDeviceMetricsOverride',{width:2048,height:1320,deviceScaleFactor:1,mobile:false},sessionId);await sleep(400);
 const wide=await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,diagrams:document.querySelectorAll('.readable-controls').length})");if(wide.scrollWidth>wide.width+1||wide.diagrams!==4)throw new Error('Wide viewport overflow');receipt.viewports.push(wide);
 await evaluate("document.documentElement.dataset.theme='light';window.scrollTo({top:0,behavior:'instant'})");await sleep(300);await capture('overview-2048-light');
 await call('Emulation.setDeviceMetricsOverride',{width:760,height:1000,deviceScaleFactor:1,mobile:false},sessionId);await sleep(400);
 for(const width of [2048,1440,1200,1100,1024,760,390]){
  await call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false},sessionId);await sleep(450);
  const narrow=await evaluate(`(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,diagrams:[...document.querySelectorAll('.diagram-preview')].map(p=>{const svg=p.querySelector('.portrait-diagram');return {columns:Number(svg.dataset.columns),width:p.clientWidth,contentWidth:p.scrollWidth,minFont:Math.min(...[...svg.querySelectorAll('text')].map(t=>parseFloat(getComputedStyle(t).fontSize)*svg.getScreenCTM().a)),height:svg.getBoundingClientRect().height,nodes:svg.querySelectorAll('[data-portrait-node]').length,edges:svg.querySelectorAll('[data-portrait-edge]').length,clipped:[...svg.querySelectorAll('text')].some(t=>{const b=t.getBBox();return b.x<0||b.x+b.width>svg.viewBox.baseVal.width+1;})};})}))()`);
  if(narrow.scrollWidth>narrow.width+1||narrow.diagrams.length!==4||narrow.diagrams.some(d=>d.columns!==(width>=1200?2:1)||d.contentWidth>d.width+1||d.minFont<15.8||d.clipped)){console.log(await evaluate("[...document.querySelectorAll('body *')].filter(e=>getComputedStyle(e).display!=='none'&&e.getBoundingClientRect().right>innerWidth+1&&e.getBoundingClientRect().width>0).slice(0,15).map(e=>({tag:e.tagName,cls:e.className?.baseVal||e.className,right:e.getBoundingClientRect().right,text:e.textContent?.slice(0,70)}))"));throw new Error('Narrow viewport overflow: '+JSON.stringify(narrow));};receipt.viewports.push(narrow);
  if(width>=1200){const routing=await evaluate('('+measureRoutes.toString()+')()');if(routing.some(r=>r.crossings||r.overlappingPairs.length))throw new Error('Crossing at width '+width+': '+JSON.stringify(routing.map(({geometry,...r})=>r)));(receipt.routingByWidth??=[]).push({width,routing:routing.map(({geometry,...r})=>r)});}
  await evaluate("window.scrollTo({top:document.querySelector('#requests-panel').getBoundingClientRect().top+scrollY-95,behavior:'instant'})");await sleep(200);await capture('requests-'+width+'-light');
 }
 const topology=await evaluate(`(()=>[...document.querySelectorAll('figure.diagram')].map(f=>({id:f.closest('.panel').id,nodes:[...f.querySelectorAll('[data-portrait-node]')].map(n=>n.dataset.portraitNode),edges:[...f.querySelectorAll('[data-portrait-edge]')].map(n=>n.dataset.portraitEdge)})))()`);
 for(const diagram of topology){const id=diagram.id.replace('-panel',''),graph=JSON.parse(await fs.readFile(path.join(root,'source',id+'.json'),'utf8'));if(diagram.nodes.length!==graph.nodes.length||graph.nodes.some(n=>!diagram.nodes.includes(n.id)))throw new Error('Missing node '+id);const edges=graph.edges||graph.nodes.filter(n=>n.parent).map(n=>({id:n.parent+'-'+n.id}));if(diagram.edges.length!==edges.length||edges.some(e=>!diagram.edges.includes(e.id)))throw new Error('Missing edge '+id);}receipt.topology={ok:true,diagrams:topology};
 const spacing=await evaluate(`(()=>[...document.querySelectorAll('.portrait-diagram')].map(svg=>({overlap:[...svg.querySelectorAll('[data-portrait-node]')].some(g=>{const boxes=[...g.querySelectorAll('text')].map(t=>t.getBBox());return boxes.some((a,i)=>boxes.some((b,j)=>i<j&&a.x<b.x+b.width-1&&a.x+a.width>b.x+1&&a.y<b.y+b.height-1&&a.y+a.height>b.y+1));}),outsideCard:[...svg.querySelectorAll('[data-portrait-node]')].some(g=>{const r=g.querySelector('rect').getBBox();return [...g.querySelectorAll('text')].some(t=>{const b=t.getBBox();return b.y<r.y-1||b.y+b.height>r.y+r.height+1;});})})))()`);
 if(spacing.some(d=>d.overlap||d.outsideCard)){console.log(JSON.stringify(await evaluate("[...document.querySelectorAll('[data-portrait-node]')].slice(0,15).map(g=>({node:g.dataset.portraitNode,texts:[...g.querySelectorAll('text')].map(t=>({text:t.textContent,y:t.getBBox().y,h:t.getBBox().height,font:getComputedStyle(t).fontSize}))}))")));throw new Error('Compact spacing failed: '+JSON.stringify(spacing));}receipt.spacing=spacing;
 const expectedFonts=[[18,27,20.25],[16,24,18],[16,24,18],[16,24,18]];
 if(receipt.viewports[0].diagrams.some((d,i)=>JSON.stringify(d.fontSizes)!==JSON.stringify(expectedFonts[i])))throw new Error('Diagram font sizes changed');
 if(browserErrors.length)throw new Error('Browser runtime errors: '+JSON.stringify(browserErrors));receipt.browserErrors=browserErrors;
 receipt.ok=true;await fs.writeFile(path.join(out,'readability-review.json'),JSON.stringify(receipt,null,2));console.log(JSON.stringify({ok:true,controls,screenshots:receipt.screenshots}));
 await call('Browser.close');
} catch(error) {receipt.ok=false;receipt.error=String(error);await fs.writeFile(path.join(out,'readability-review.json'),JSON.stringify(receipt,null,2));throw error;}
finally {socket?.close();if(chrome.exitCode===null){chrome.kill();}await sleep(250);const resolved=path.resolve(profile);if(resolved.startsWith(path.resolve(out)+path.sep)&&path.basename(resolved).startsWith('qa-profile-')){await fs.rm(resolved,{recursive:true,force:true,maxRetries:3,retryDelay:200}).catch(()=>{});}}
