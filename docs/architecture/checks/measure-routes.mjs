// Measure visible workflow geometry; a tree's shared containment trunk is intentional.
export function measureRoutes() {
  return [...document.querySelectorAll('.portrait-diagram')].slice(1).map(svg=>{
    function segments(path) {
      const tokens=path.getAttribute('d').match(/[MHV]|-?\d+(?:\.\d+)?/g);
      const result=[];let x=0,y=0,index=0;
      while(index<tokens.length){
        const command=tokens[index++];
        if(command==='M'){x=Number(tokens[index++]);y=Number(tokens[index++]);continue;}
        const next=Number(tokens[index++]);
        const end=command==='H'?{x:next,y}:{x,y:next};
        if(Math.abs(x-end.x)+Math.abs(y-end.y)>.1)result.push({x1:x,y1:y,x2:end.x,y2:end.y});
        x=end.x;y=end.y;
      }
      return result;
    }
    const paths=[...svg.querySelectorAll('[data-portrait-edge]')].map(p=>({id:p.dataset.portraitEdge,segments:segments(p)}));
    let crossings=0,overlapLength=0;const overlappingPairs=[],crossingPairs=[];
    const inside=(n,a,b)=>n>Math.min(a,b)+.5&&n<Math.max(a,b)-.5;
    for(let i=0;i<paths.length;i++)for(let j=i+1;j<paths.length;j++){
      let overlap=0;
      for(const a of paths[i].segments)for(const b of paths[j].segments){
        const ah=a.y1===a.y2,bh=b.y1===b.y2;
        if(ah===bh){
          const same=ah?Math.abs(a.y1-b.y1)<.5:Math.abs(a.x1-b.x1)<.5;
          if(same){const minA=ah?Math.min(a.x1,a.x2):Math.min(a.y1,a.y2),maxA=ah?Math.max(a.x1,a.x2):Math.max(a.y1,a.y2),minB=ah?Math.min(b.x1,b.x2):Math.min(b.y1,b.y2),maxB=ah?Math.max(b.x1,b.x2):Math.max(b.y1,b.y2);overlap+=Math.max(0,Math.min(maxA,maxB)-Math.max(minA,minB));}
        }else{
          const h=ah?a:b,v=ah?b:a;
          if(inside(v.x1,h.x1,h.x2)&&inside(h.y1,v.y1,v.y2)){
            crossings++;
            crossingPairs.push({edges:[paths[i].id,paths[j].id],x:v.x1,y:h.y1});
          }
        }
      }
      if(overlap>1){overlappingPairs.push([paths[i].id,paths[j].id]);overlapLength+=overlap;}
    }
    const nodes=[...svg.querySelectorAll('[data-portrait-node]')].map(g=>{
      const matrix=g.transform.baseVal.consolidate().matrix,r=g.querySelector('rect');
      return {id:g.dataset.portraitNode,x:matrix.e,y:matrix.f,width:Number(r.getAttribute('width')),height:Number(r.getAttribute('height')),ports:[...g.querySelectorAll('text')].filter(t=>t.textContent.startsWith('→')).map(t=>Number(t.getAttribute('y'))-5)};
    });
    return {panel:svg.closest('.panel').id,crossings,overlapLength,overlappingPairs,crossingPairs,geometry:{width:svg.viewBox.baseVal.width,nodes}};
  });
}
