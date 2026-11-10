import fs from 'node:fs';
import {drawPortrait} from './portrait-layout.mjs';
const font = '"Malgun Gothic","맑은 고딕","Noto Sans KR","Apple SD Gothic Neo",system-ui,sans-serif';

export function makeReadable(html) {
  const css = `
:root{--sans:${font};--mono:${font};--flow-branch:#7c3aed;--flow-error:#c7680d}[data-theme=dark]{--flow-branch:#b39bff;--flow-error:#ffc06a}
html{scroll-behavior:smooth;scroll-padding-top:88px}body{font-family:var(--sans)!important;font-size:18px;line-height:1.7;word-break:keep-all;text-rendering:optimizeLegibility}
.shell{max-width:1600px!important;width:calc(100% - 48px)!important;margin:auto!important}
.tools{flex-wrap:wrap;max-width:100%}.diagram figcaption{flex-wrap:wrap;gap:12px}.walk-start{display:none!important}.panel-body,.panel-head,.diagram,.diagram-title{min-width:0;overflow-wrap:anywhere}.answer-id{display:none!important}.verdict{box-shadow:none!important}.board{display:block!important}.panel{width:100%!important;height:auto!important;margin:18px 0!important;break-inside:avoid}
.panel-head h2{font:700 25px/1.45 var(--sans)!important}.panel p,.panel li{font-size:18px!important;line-height:1.75!important}
h1{font-family:var(--sans)!important;font-size:clamp(30px,3vw,42px)!important;letter-spacing:-.03em!important}
.readable-nav{position:sticky;top:0;z-index:15;display:flex;gap:12px;flex-wrap:wrap;padding:16px 0;background:var(--paper);border-bottom:1px solid var(--rule)}
.readable-nav a{font:650 17px/1.4 var(--sans);padding:10px 16px;background:var(--panel);color:var(--ink);border:1px solid var(--rule);border-radius:12px;text-decoration:none}
.readable-help{padding:14px 18px;border:1px solid var(--rule);border-radius:12px;background:var(--sunk);font-size:17px;color:var(--secondary);margin:20px 0}
.readable-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 8px;position:relative;z-index:2}
.readable-controls button{min-height:44px;padding:8px 14px;border:1px solid var(--rule-strong);border-radius:10px;background:var(--panel);color:var(--ink);font:650 16px/1.4 var(--sans);cursor:pointer}
.readable-controls output{font:600 16px/1.4 var(--sans);min-width:110px;color:var(--secondary)}
.board .diagram-preview{display:block!important;overflow:visible!important;max-height:none!important;width:100%!important;cursor:default;border:1px solid var(--rule);border-radius:14px;padding:12px!important;background:var(--panel);scrollbar-width:auto;box-sizing:border-box}
.portrait-cards>g{cursor:pointer;outline:none}.portrait-cards>g>rect{transition:stroke .15s,stroke-width .15s}.portrait-cards>g:focus-visible>rect,.portrait-cards>g.is-active>rect{stroke:var(--accent);stroke-width:3}.portrait-diagram.has-active .portrait-paths>path{opacity:.1}.portrait-diagram.has-active .portrait-paths>path.is-related{opacity:1;stroke-width:3}.portrait-cards>g.is-unrelated,.connector-link.is-unrelated{opacity:.3}.portrait-cards>g.is-endpoint>rect{stroke:var(--selection-color);stroke-width:2}.portrait-cards>g.is-active>rect{stroke:var(--selection-color);stroke-width:3}.flow-legend{display:flex;flex-wrap:wrap;gap:8px 18px;font:500 16px/1.5 var(--sans);margin:0 0 8px;color:var(--secondary)}.flow-legend span{display:inline-flex;align-items:center;gap:7px}.flow-legend span::before{content:"";width:18px;height:3px;background:var(--accent)}.flow-legend .branch::before{background:none;border-top:2px dashed var(--flow-branch)}.flow-legend .error::before{background:none;border-top:2px dashed var(--flow-error)}
.connector-link{cursor:pointer;outline:none}.connector-link circle{transition:fill .15s,stroke-width .15s}.connector-link:hover circle,.connector-link:focus-visible circle,.connector-link.is-related circle{fill:var(--accent-soft);stroke-width:3}.connector-link text{text-decoration:underline;text-underline-offset:2px;pointer-events:none}
.tools button{font-size:16px}.theme-controls{height:44px}.theme-controls button{height:38px}
.readable-nav button{min-height:44px;border:1px solid var(--rule-strong);border-radius:10px;padding:8px 12px;font:600 16px/1.4 var(--sans);background:var(--panel);color:var(--ink);cursor:pointer}.readable-nav [hidden]{display:none!important}.selection-status{font:600 16px/1.4 var(--sans);color:var(--ink);align-self:center;overflow-wrap:anywhere}
.readable-controls button:disabled{opacity:.4;cursor:default}.readable-controls button:focus-visible,.readable-nav a:focus-visible,.readable-nav button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.sources h2,.sources ol,.sources li::marker,.sources code{font-size:16px!important;line-height:1.6!important;overflow-wrap:anywhere}.sources code{font-family:var(--sans)!important}.sources a{ text-underline-offset:3px}::selection{background:var(--accent-soft);color:var(--ink)}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.connector-link circle,.portrait-cards>g>rect{transition:none}}
.board .diagram-preview>svg.portrait-diagram{width:100%!important;min-width:0!important;max-width:100%!important;height:auto!important;display:block!important;margin:0!important;flex:none!important}.board .diagram-preview>svg:not(.portrait-diagram){display:none!important}
.board .diagram-preview text{font-family:var(--sans)!important;letter-spacing:normal!important;text-rendering:optimizeLegibility!important;fill:var(--ink)!important;opacity:1!important}
.diagram-preview [data-detail="context"],.diagram-preview [data-detail="full"]{visibility:visible!important;opacity:1!important}
.diagram-semantics{margin-top:16px}.diagram-semantics code{font-family:var(--sans)!important}
.explore-button,.tool-button{font-family:var(--sans)!important;font-size:16px!important;min-height:44px}
.diagram-fit-status{display:none!important}.diagram-title{font:700 20px/1.5 var(--sans)!important}
@media(max-width:700px){.shell{width:calc(100% - 24px)!important}.readable-nav{gap:6px}.readable-nav a{font-size:15px;padding:8px}.panel-head h2{font-size:22px!important}}
@media print{.readable-controls,.readable-nav,.readable-help{display:none!important}.board .diagram-preview>svg{width:100%!important;min-width:0!important}.board .diagram-preview{overflow:visible!important}}
`;
  html = html.replace('</head>', `<style id="readable-korean-layout">${css}</style></head>`);
  const graphs=Object.fromEntries(['structure','requests','sync','notifications'].map(id=>[id,JSON.parse(fs.readFileSync(new URL('../source/'+id+'.json',import.meta.url),'utf8'))]));
  const script = `const drawPortrait=${drawPortrait.toString()};(${installReadable.toString()})(${JSON.stringify(font)},${JSON.stringify(graphs)});`;
  const end = html.lastIndexOf('</body>');
  if (end < 0) throw new Error('Missing explainer body');
  return (html.slice(0,end) + `<script id="readable-korean-controls">${script}</script>` + html.slice(end)).replace(/^[ \t]+$/gm,'');
}

function installReadable(font, graphs) {
  const init = () => {
    const board = document.querySelector('.board');
    if (!board) return;
    document.querySelector('#answer h2')?.replaceChildren('핵심 구조');
    for (const b of document.querySelectorAll('.theme-controls button')) { const labels={System:'자동',Light:'밝게',Dark:'어둡게'}; if(labels[b.textContent.trim()]) b.textContent=labels[b.textContent.trim()]; }
    const nav = document.createElement('nav');
    nav.className = 'readable-nav';
    nav.setAttribute('aria-label','네 그림 바로가기');
    for (const [id,label] of [['structure-panel','01 코드 구조'],['requests-panel','02 로그인·일정 요청'],['sync-panel','03 동기화'],['notifications-panel','04 알림 발송']]) {
      const a=document.createElement('a');a.href='#'+id;a.textContent=label;nav.append(a);
    }
    const selectionStatus=document.createElement('span');selectionStatus.className='selection-status';selectionStatus.dataset.selectionStatus='';selectionStatus.setAttribute('role','status');selectionStatus.setAttribute('aria-live','polite');selectionStatus.hidden=true;
    const clear=document.createElement('button');clear.type='button';clear.dataset.clearSelection='';clear.textContent='강조 해제';clear.hidden=true;clear.title='강조 해제 (Esc)';
    nav.append(selectionStatus,clear);
    const resetSelection=()=>{document.querySelectorAll('.portrait-diagram').forEach(svg=>svg.dispatchEvent(new CustomEvent('portrait-clear',{detail:{silent:true}})));selectionStatus.textContent='';selectionStatus.hidden=true;clear.hidden=true;};
    clear.addEventListener('click',resetSelection);
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!clear.hidden)resetSelection();});
    board.addEventListener('portrait-selection',event=>{
      if(event.detail.active)document.querySelectorAll('.portrait-diagram').forEach(svg=>{if(svg!==event.target)svg.dispatchEvent(new CustomEvent('portrait-clear',{detail:{silent:true}}));});
      selectionStatus.textContent=event.detail.text;selectionStatus.hidden=!event.detail.active;clear.hidden=!event.detail.active;
    });
    document.querySelector('.sources h2')?.replaceChildren('코드 근거');
    board.before(nav);
    const help=document.createElement('p');help.className='readable-help';
    help.textContent='넓은 화면은 2열, 좁은 화면은 1열입니다. 단계 번호는 지그재그 순서이며, 굵은 실선은 기본 진행, 점선은 분기·합류입니다. A/B를 클릭하거나 Enter를 누르면 대응 단계로 이동해 해당 분기를 강조합니다. 도착지의 같은 표식으로 돌아올 수 있으며, 상단 강조 해제 또는 Esc로 해제합니다.';
    nav.after(help);
    for (const figure of document.querySelectorAll('figure.diagram')) {
      const preview=figure.querySelector('.diagram-preview'),svg=preview?.querySelector('svg');
      if(!preview||!svg) continue;
      const id=figure.closest('.panel')?.id.replace('-panel','');
      const graph=graphs[id];
      if(!graph)throw new Error('Missing diagram source: '+id);
      const baseSize=graph.diagram_type==='tree'?18:16;
      let target=baseSize;
      const controls=document.createElement('div');controls.className='readable-controls';
      controls.setAttribute('aria-label','그림 글씨 크기');
      const out=document.createElement('output');out.setAttribute('aria-live','polite');
      function apply() {
        drawPortrait(preview,svg,graph,font,target);
        out.textContent='최소 글씨 '+target+'px';
        for(const button of controls.querySelectorAll('button'))button.disabled=button.textContent==='−'?target<=baseSize:button.textContent==='+'?target>=28:target===baseSize;
      }
      for(const [text,action] of [['−',()=>{target=Math.max(baseSize,target-2);apply();}],['+',()=>{target=Math.min(28,target+2);apply();}],['기본 크기',()=>{target=baseSize;apply();}]]) {
        const b=document.createElement('button');b.type='button';b.textContent=text;
        b.setAttribute('aria-label',text==='−'?'글씨 줄이기':text==='+'?'글씨 키우기':text);
        b.addEventListener('click',action);controls.append(b);
      }
      controls.append(out);preview.before(controls);
      if(graph.diagram_type==='workflow'){
        const legend=document.createElement('div');legend.className='flow-legend';legend.setAttribute('aria-label','연결선 범례');
        for(const [kind,label] of [['main','기본 흐름 · 실선'],['branch','분기·비동기 · 점선'],['error','실패·거부 · 점선']]){const item=document.createElement('span');item.className=kind;item.textContent=label;legend.append(item);}
        preview.before(legend);
      }
      let lastWidth=0;
      new ResizeObserver(()=>{if(preview.clientWidth!==lastWidth){lastWidth=preview.clientWidth;apply();}}).observe(preview);
      figure.querySelector('.explore-button span')?.replaceChildren('자세히 보기');
      figure.querySelector('.diagram-semantics')?.removeAttribute('open');
      preview.tabIndex=0;preview.setAttribute('aria-label',(preview.getAttribute('aria-label')||'그림')+' · 화면 너비에 따라 1열 또는 2열');
      apply();
    }
    // The full native Viewer is a sandboxed local srcdoc frame. Add only font
    // styling when it opens; the frozen graph and source evidence stay intact.
    const styleNative = frame => {
      if(!(frame instanceof HTMLIFrameElement)||!frame.srcdoc||frame.dataset.readableFont)return;
      frame.dataset.readableFont='true';
      const css='<style id="readable-font">body,svg text,button,input{font-family:'+font+'!important;letter-spacing:normal!important;text-rendering:optimizeLegibility!important}</style>';
      frame.srcdoc=frame.srcdoc.replace('</head>',css+'</head>');
    };
    new MutationObserver(entries=>{for(const entry of entries){if(entry.type==='attributes')styleNative(entry.target);for(const el of entry.addedNodes){if(el instanceof Element){styleNative(el);el.querySelectorAll?.('iframe').forEach(styleNative);}}}}).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['srcdoc']});
    document.querySelectorAll('iframe').forEach(styleNative);
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>requestAnimationFrame(init),{once:true});else requestAnimationFrame(init);
}
