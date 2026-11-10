// Presentation only: the validated native graphs remain in the detail viewer.
export function drawPortrait(preview, original, graph, font, textSize) {
  const ns='http://www.w3.org/2000/svg';
  const previous=preview.querySelector('.portrait-diagram');
  const previousFocus=previous?.contains(document.activeElement)?document.activeElement.id:null;
  const panelId=preview.closest('.panel').id;
  const nodeDomId=id=>panelId+'-step-'+id;
  const width=preview.clientWidth-24;
  const twoColumns=width>=1000;
  const tree=graph.diagram_type==='tree';
  const edges=tree?graph.nodes.filter(n=>n.parent).map(n=>({id:n.parent+'-'+n.id,from:n.parent,to:n.id})):graph.edges;
  // Matching connectors keep these long exception paths out of the main flow.
  const connectorLabels=twoColumns?{'proof-retry':'A','enabled-skip':'B'}:{};
  const connectorEdges=edges.filter(e=>connectorLabels[e.id]);
  const badgeRadius=Math.max(12,textSize*.75);
  const badgeInset=badgeRadius+8,stubEnd=badgeRadius*2+8,stubStart=stubEnd+26;
  const incoming=new Map(graph.nodes.map(n=>[n.id,0]));
  edges.forEach(e=>incoming.set(e.to,incoming.get(e.to)+1));
  const queue=graph.nodes.filter(n=>!incoming.get(n.id));
  const ordered=[];
  while(queue.length){const n=queue.shift();ordered.push(n);for(const e of edges.filter(e=>e.from===n.id)){incoming.set(e.to,incoming.get(e.to)-1);if(!incoming.get(e.to))queue.push(graph.nodes.find(n=>n.id===e.to));}}
  if(ordered.length!==graph.nodes.length)throw new Error('Portrait layout requires an acyclic graph');
  // Keep each entry path together instead of interleaving unrelated roots.
  const visit=(id,seen,result)=>{if(seen.has(id))return;seen.add(id);result.push(graph.nodes.find(n=>n.id===id));for(const e of edges.filter(e=>e.from===id))visit(e.to,seen,result);};
  const nodes=[];const seen=new Set();graph.nodes.filter(n=>!edges.some(e=>e.to===n.id)).forEach(n=>visit(n.id,seen,nodes));
  const numbers=new Map(nodes.map((n,i)=>[n.id,String(i+1).padStart(2,'0')]));
  const svg=document.createElementNS(ns,'svg');
  svg.classList.add('portrait-diagram');svg.setAttribute('role','img');
  svg.dataset.columns=twoColumns?'2':'1';
  svg.setAttribute('aria-label',graph.meta.title+(twoColumns?' · 번호순으로 지그재그로 읽는 2열 그림':' · 위에서 아래로 읽는 1열 그림'));
  const add=(tag,attrs,parent=svg)=>{const el=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))el.setAttribute(k,String(v));parent.append(el);return el;};
  const title=add('title',{});title.textContent=graph.meta.title;
  const defs=add('defs',{}),markerPrefix='portrait-arrow-'+preview.closest('.panel').id;
  const colors={main:'var(--accent)',branch:'var(--flow-branch)',error:'var(--flow-error)'};
  for(const [kind,color] of Object.entries(colors)){
    const marker=add('marker',{id:markerPrefix+'-'+kind,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'},defs);
    add('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:color},marker);
  }
  const canvas=document.createElement('canvas').getContext('2d');
  function lines(value,size,maxWidth,weight){
    canvas.font=weight+' '+size+'px '+font;const result=[];let line='';
    for(const ch of String(value||'')){if(ch==='\n'||canvas.measureText(line+ch).width>maxWidth){if(line)result.push(line);line=ch==='\n'?'':ch;}else line+=ch;}
    if(line)result.push(line);return result;
  }
  function text(value,x,y,size,maxWidth,parent,weight=400){
    const rows=lines(value,size,maxWidth,weight);
    const el=add('text',{x,y,'font-size':size,'font-weight':weight,fill:'var(--ink)'},parent);
    rows.forEach((line,i)=>{const span=add('tspan',{x,dy:i?size*1.5:0},el);span.textContent=line;});
    return Math.max(1,rows.length)*size*1.5;
  }
  const gutter=tree?34:Math.min(150,Math.max(66,width*.14));
  const left=twoColumns?44:tree?30:8;
  const middleGap=100;
  const cardWidth=twoColumns?(width-2*left-middleGap)/2:width-left-gutter-6;
  const layout=new Map();
  const paths=add('g',{'class':'portrait-paths'}),cards=add('g',{'class':'portrait-cards'}),connectorLayer=add('g',{'class':'connector-links'});
  nodes.forEach((n,index)=>{
    // A tree root spans both columns; workflow rows alternate reading direction.
    const root=tree&&index===0;
    const row=tree&&twoColumns?root?0:1+Math.floor((index-1)/2):twoColumns?Math.floor(index/2):index;
    const col=twoColumns&&!root?(tree?(index-1)%2:row%2===0?index%2:1-index%2):0;
    const x=left+col*(cardWidth+middleGap);
    const nodeWidth=twoColumns&&root?width-2*left:cardWidth;
    const hasConnector=connectorEdges.some(e=>e.from===n.id||e.to===n.id);
    const contentWidth=nodeWidth-(hasConnector?Math.max(88,badgeRadius*2+60):36);
    const outgoing=edges.filter(e=>e.from===n.id);
    const g=add('g',{id:nodeDomId(n.id),'data-portrait-node':n.id,tabindex:0,role:'button','aria-pressed':'false','aria-label':numbers.get(n.id)+' '+n.label+' · 연결 강조'},cards);
    const rect=add('rect',{x:0,y:0,width:nodeWidth,height:100,rx:12,fill:'var(--panel)',stroke:'var(--rule-strong)'},g);
    let cursor=Math.max(22,textSize*1.1+4);
    const lane=graph.lanes?.find(l=>l.id===n.lane);
    cursor+=text(numbers.get(n.id)+(lane?' · '+lane.label:''),18,cursor,textSize,contentWidth,g,600)+Math.max(9,textSize*.4);
    cursor+=text(n.label,18,cursor,textSize*1.5,contentWidth,g,700);
    if(n.sublabel)cursor+=text(n.sublabel,18,cursor,textSize*1.125,contentWidth,g);
    const ports=new Map();
    if(!tree&&outgoing.length){
      add('line',{x1:18,x2:nodeWidth-18,y1:cursor-9,y2:cursor-9,stroke:'var(--rule)'},g);
      for(const e of outgoing){
        const target=graph.nodes.find(n=>n.id===e.to);
        const h=text((connectorLabels[e.id]?connectorLabels[e.id]+' · ':'')+'→ '+(e.label?e.label+' · ':'')+numbers.get(e.to)+' '+target.label,18,cursor+9,textSize,contentWidth,g);
        ports.set(e.id,cursor+4);cursor+=h+3;
      }
    }
    const height=cursor-6;rect.setAttribute('height',height);
    layout.set(n.id,{x,y:0,width:nodeWidth,height,ports,row,col,g});
  });
  let y=8;
  const rowCount=Math.max(...[...layout.values()].map(n=>n.row))+1;
  for(let row=0;row<rowCount;row++){
    const members=[...layout.values()].filter(n=>n.row===row);
    for(const n of members){n.y=y;n.g.setAttribute('transform',`translate(${n.x} ${n.y})`);}
    y+=Math.max(...members.map(n=>n.height))+(twoColumns?28:14);
  }
  const nodeIndex=new Map(nodes.map((n,i)=>[n.id,i]));
  const isPrimary=e=>!e.role&&nodeIndex.get(e.to)===nodeIndex.get(e.from)+1;
  const routePlans=new Map();
  const routedSegments=[];
  let routeBottom=y;
  function segments(points){return points.slice(1).map((p,i)=>({a:points[i],b:p}));}
  function routeCost(points){
    let cost=0;
    for(const s of segments(points)){
      const horizontal=s.a[1]===s.b[1];
      cost+=(Math.abs(s.a[0]-s.b[0])+Math.abs(s.a[1]-s.b[1]))*.01;
      for(const t of routedSegments){
        const otherHorizontal=t.a[1]===t.b[1];
        if(horizontal===otherHorizontal){
          const axis=horizontal?0:1,other=1-axis;
          if(Math.abs(s.a[other]-t.a[other])<.5){
            const overlap=Math.min(Math.max(s.a[axis],s.b[axis]),Math.max(t.a[axis],t.b[axis]))-Math.max(Math.min(s.a[axis],s.b[axis]),Math.min(t.a[axis],t.b[axis]));
            if(overlap>1)cost+=overlap*10000;
          }
        }else{
          const h=horizontal?s:t,v=horizontal?t:s;
          const inside=(n,a,b)=>n>Math.min(a,b)+.5&&n<Math.max(a,b)-.5;
          if(inside(v.a[0],h.a[0],h.b[0])&&inside(h.a[1],v.a[1],v.b[1]))cost+=10000;
        }
      }
    }
    return cost;
  }
  const incomingPorts=new Map();
  for(const n of nodes){
    const incoming=edges.filter(e=>e.to===n.id).sort((a,b)=>Number(isPrimary(b))-Number(isPrimary(a)));
    incoming.forEach((e,i)=>incomingPorts.set(e.id,28+i*18));
  }
  // Reserve short main-flow routes first. Secondary routes use clear center or
  // outer tracks; each incoming arrow gets its own port instead of merging lines.
  const routingOrder=[...edges].sort((a,b)=>Number(isPrimary(b))-Number(isPrimary(a))||Math.abs(layout.get(a.from).row-layout.get(a.to).row)-Math.abs(layout.get(b.from).row-layout.get(b.to).row));
  for(const e of routingOrder){
    if(tree||connectorLabels[e.id])continue;
    const a=layout.get(e.from),b=layout.get(e.to);
    const targetY=b.y+Math.min(b.height-14,incomingPorts.get(e.id));
    const sourceY=a.y+a.ports.get(e.id);
    const candidates=[];
    if(twoColumns&&isPrimary(e)&&a.row===b.row){
      candidates.push([[a.col===0?a.x+a.width:a.x,a.y+28],[b.col===0?b.x+b.width:b.x,b.y+28]]);
    }else if(twoColumns&&isPrimary(e)&&a.col===b.col&&b.row===a.row+1){
      candidates.push([[a.x+a.width/2,a.y+a.height],[b.x+b.width/2,b.y]]);
    }else if(twoColumns){
      const cross=a.col!==b.col;
      const fromX=cross?(a.col===0?a.x+a.width:a.x):(a.col===0?a.x:a.x+a.width);
      const toX=cross?(b.col===0?b.x+b.width:b.x):(b.col===0?b.x:b.x+b.width);
      const channels=cross?Array.from({length:10},(_,i)=>left+cardWidth+12+i*8):[14,22,30,38].map(offset=>fromX+(a.col===0?-offset:offset));
      for(const channel of channels)candidates.push([[fromX,sourceY],[channel,sourceY],[channel,targetY],[toX,targetY]]);
      if(cross){
        const outerA=a.col===0?a.x:a.x+a.width,outerB=b.col===0?b.x:b.x+b.width;
        for(const offsetA of [14,22,30,38])for(const offsetB of [14,22,30,38])for(let bottom=0;bottom<4;bottom++){
          const trackA=outerA+(a.col===0?-offsetA:offsetA),trackB=outerB+(b.col===0?-offsetB:offsetB),bottomY=y+4+bottom*8;
          candidates.push([[outerA,sourceY],[trackA,sourceY],[trackA,bottomY],[trackB,bottomY],[trackB,targetY],[outerB,targetY]]);
        }
      }
    }else{
      const x=left+cardWidth;
      for(let channel=x+14;channel<width-7;channel+=8)candidates.push([[x,sourceY],[channel,sourceY],[channel,targetY],[x,targetY]]);
    }
    const points=candidates.reduce((best,candidate)=>routeCost(candidate)<routeCost(best)?candidate:best);
    routedSegments.push(...segments(points));
    routeBottom=Math.max(routeBottom,...points.map(p=>p[1]+10));
    routePlans.set(e.id,points);
  }
  edges.forEach((e)=>{
    const a=layout.get(e.from),b=layout.get(e.to);
    let d;
    if(connectorLabels[e.id]){
      const sourceY=a.y+a.ports.get(e.id),targetY=b.y+28;
      d=`M ${a.x+a.width-stubStart} ${sourceY} H ${a.x+a.width-stubEnd} M ${b.x+b.width-stubEnd} ${targetY} H ${b.x+b.width-stubStart}`;
      for(const [node,localY] of [[a,a.ports.get(e.id)],[b,28]]){
        const id=node.g.dataset.portraitNode,peer=id===e.from?e.to:e.from;
        const action=numbers.get(peer)+' '+graph.nodes.find(n=>n.id===peer).label+(id===e.from?'로 이동':'로 돌아가기');
        const badge=add('a',{id:panelId+'-link-'+e.id+'-'+id,href:'#'+nodeDomId(peer),tabindex:0,role:'link','class':'connector-link','aria-label':connectorLabels[e.id]+' 연결 · '+(e.label?e.label+' · ':'')+action,'data-connector-for':e.id,'data-connector-node':id,transform:`translate(${node.x} ${node.y})`},connectorLayer);
        const hitSize=Math.max(44,badgeRadius*2+12),centerX=node.width-badgeInset;
        add('rect',{x:centerX-hitSize/2,y:localY-hitSize/2,width:hitSize,height:hitSize,fill:'transparent','pointer-events':'all'},badge);
        add('circle',{cx:centerX,cy:localY,r:badgeRadius,fill:'var(--panel)',stroke:colors[e.role==='error'?'error':'branch'],'stroke-width':2},badge);
        const label=add('text',{x:centerX,y:localY+textSize*.32,'text-anchor':'middle','font-size':textSize,'font-weight':700},badge);
        label.textContent=connectorLabels[e.id];
        const title=add('title',{},badge);title.textContent=connectorLabels[e.id]+' · '+action;
        badge.addEventListener('click',ev=>{ev.preventDefault();ev.stopPropagation();jumpToPeer(e,peer);});
        badge.addEventListener('keydown',ev=>{if(ev.key===' '){ev.preventDefault();jumpToPeer(e,peer);}else if(ev.key==='Escape'){ev.preventDefault();clearSelection();}});
      }
    }
    else if(tree&&twoColumns){
      const trunk=width/2,target=b.col===0?b.x+b.width:b.x;
      d=`M ${trunk} ${a.y+a.height} V ${b.y+28} H ${target}`;
    }
    else if(tree){const x=left-16;d=`M ${left} ${a.y+32} H ${x} V ${b.y+32} H ${left}`;}
    else{
      const points=routePlans.get(e.id);
      d=`M ${points[0][0]} ${points[0][1]}`;
      for(let i=1;i<points.length;i++)d+=points[i][1]===points[i-1][1]?` H ${points[i][0]}`:` V ${points[i][1]}`;
    }
    const kind=e.role==='error'?'error':e.role==='branch'||e.role==='async'?'branch':'main';
    const primary=tree||isPrimary(e);
    const path=add('path',{d,fill:'none',stroke:colors[kind],'stroke-width':primary?2.8:1.8,'stroke-opacity':primary?1:.85,'marker-end':`url(#${markerPrefix}-${kind})`,'data-portrait-edge':e.id,'data-from':e.from,'data-to':e.to,'data-primary':primary},paths);
    if(!primary||e.variant==='dashed')path.setAttribute('stroke-dasharray','6 5');
    const tip=add('title',{},path);tip.textContent=graph.nodes.find(n=>n.id===e.from).label+' → '+(e.label?e.label+' → ':'')+graph.nodes.find(n=>n.id===e.to).label;
  });
  // Connector stubs sit in each card's reserved margin, above its background.
  if(connectorEdges.length)svg.append(paths,connectorLayer);
  let pinned=previous?.dataset.pinnedNode||null;
  let pinnedEdge=previous?.dataset.pinnedEdge||null;
  function highlight(id,edgeId=pinnedEdge){
    svg.classList.toggle('has-active',!!id);
    svg.dataset.pinnedNode=pinned||'';
    svg.dataset.pinnedEdge=pinnedEdge||'';
    const edge=edges.find(e=>e.id===edgeId);
    const related=new Set([id]);
    if(edge){related.add(edge.from);related.add(edge.to);}
    else for(const e of edges)if(e.from===id||e.to===id){related.add(e.from);related.add(e.to);}
    svg.style.setProperty('--selection-color',edge?.role==='error'?colors.error:edge?colors.branch:colors.main);
    for(const n of layout.values()){
      const current=n.g.dataset.portraitNode;
      n.g.classList.toggle('is-unrelated',!!id&&!related.has(current));
      n.g.classList.toggle('is-active',current===id);
      n.g.classList.toggle('is-endpoint',!!edge&&related.has(current));
      n.g.setAttribute('aria-pressed',String(current===pinned));
    }
    paths.querySelectorAll('path').forEach(p=>p.classList.toggle('is-related',edge?p.dataset.portraitEdge===edgeId:p.dataset.from===id||p.dataset.to===id));
    connectorLayer.querySelectorAll('a').forEach(a=>{const selected=edge?a.dataset.connectorFor===edgeId:related.has(a.dataset.connectorNode);a.classList.toggle('is-related',!!id&&selected);a.classList.toggle('is-unrelated',!!id&&!selected);});
  }
  function publishSelection(){
    const edge=edges.find(e=>e.id===pinnedEdge);
    const text=edge?connectorLabels[edge.id]+' 분기 · '+numbers.get(edge.from)+' → '+numbers.get(edge.to):pinned?numbers.get(pinned)+' '+graph.nodes.find(n=>n.id===pinned).label:'';
    svg.dispatchEvent(new CustomEvent('portrait-selection',{bubbles:true,detail:{active:!!pinned,text}}));
  }
  function clearSelection(silent=false){
    pinned=null;pinnedEdge=null;highlight(null,null);
    if(!silent)publishSelection();
  }
  function jumpToPeer(edge,peer){
    pinned=peer;pinnedEdge=edge.id;highlight(peer,edge.id);publishSelection();
    const destination=layout.get(peer).g;
    destination.focus({preventScroll:true});
    const navHeight=document.querySelector('.readable-nav')?.getBoundingClientRect().height||0;
    const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({top:Math.max(0,scrollY+destination.getBoundingClientRect().top-navHeight-24),behavior:reduced?'instant':'smooth'});
  }
  svg.addEventListener('portrait-clear',ev=>clearSelection(!!ev.detail?.silent));
  for(const [id,n] of layout){
    n.g.addEventListener('pointerenter',()=>highlight(pinned||id));
    n.g.addEventListener('pointerleave',()=>highlight(pinned));
    n.g.addEventListener('focus',()=>highlight(pinned||id));
    n.g.addEventListener('blur',()=>highlight(pinned));
    const toggle=()=>{pinned=pinned===id?null:id;pinnedEdge=null;highlight(pinned,null);publishSelection();};
    n.g.addEventListener('click',toggle);
    n.g.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();toggle();}else if(ev.key==='Escape'){clearSelection();}});
  }
  svg.setAttribute('viewBox',`0 0 ${width} ${routeBottom}`);svg.setAttribute('width',width);svg.setAttribute('height',routeBottom);
  preview.querySelector('.portrait-diagram')?.remove();
  original.style.setProperty('display','none','important');preview.append(svg);
  highlight(pinned,pinnedEdge);
  if(previousFocus){
    svg.querySelector('#'+CSS.escape(previousFocus))?.focus({preventScroll:true});
    if(pinned)requestAnimationFrame(()=>{
      const destination=layout.get(pinned).g,top=destination.getBoundingClientRect().top;
      const navHeight=document.querySelector('.readable-nav')?.getBoundingClientRect().height||0;
      if(top<navHeight+8||top>innerHeight-100)window.scrollTo({top:Math.max(0,scrollY+top-navHeight-24),behavior:'instant'});
    });
  }
  preview.dataset.minimumTextPx=String(textSize);
}
