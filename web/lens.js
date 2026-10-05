/* Known-scene optical preview. Samples only our static wallpaper, never the DOM,
   credentials, account text or remote content. No continuous animation loop. */
(() => {
  'use strict';
  const image = new Image();
  const reduced = matchMedia('(prefers-reduced-transparency: reduce)');
  const source = document.createElement('canvas');
  let frame = 0, ready = false;
  const select = '.card,nav,.provider-tabs,[data-use]:not(:disabled),#refresh';
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  function signedDistance(x,y,w,h,r) {
    const qx=Math.abs(x-w/2)-(w/2-r), qy=Math.abs(y-h/2)-(h/2-r);
    return Math.hypot(Math.max(qx,0),Math.max(qy,0))+Math.min(Math.max(qx,qy),0)-r;
  }
  function paint() {
    frame=0;
    if(!ready || document.hidden) return;
    const start=performance.now();
    const strength=Number(document.body.dataset.lensStrength||1.65);
    const layers=[...document.querySelectorAll(select)];
    document.querySelectorAll('.lens-layer').forEach(c=>{if(!c.parentElement.matches(select)||reduced.matches)c.remove();});
    if(reduced.matches)return;
    const W=innerWidth,H=innerHeight;
    source.width=W;source.height=H;
    const context=source.getContext('2d',{willReadFrequently:true});
    const scale=Math.max(W/390,H/844), sw=390*scale,sh=844*scale;
    context.drawImage(image,(W-sw)/2,(H-sh)/2,sw,sh);
    const pixels=context.getImageData(0,0,W,H).data;
    for(const el of layers) {
      const box=el.getBoundingClientRect(),w=box.width,h=box.height;
      if(w<2||h<2)continue;
      let canvas=el.querySelector(':scope > .lens-layer');
      if(!canvas){canvas=document.createElement('canvas');canvas.className='lens-layer';canvas.setAttribute('aria-hidden','true');el.prepend(canvas);}
      // Limit raster work to <=2x CSS resolution; no frame-by-frame screenshot.
      const dpr=Math.min(devicePixelRatio||1,2),cw=Math.ceil(w*dpr),ch=Math.ceil(h*dpr);
      canvas.width=cw;canvas.height=ch;
      const ctx=canvas.getContext('2d'),out=ctx.createImageData(cw,ch),dest=out.data;
      const radius=Math.min(parseFloat(getComputedStyle(el).borderTopLeftRadius)||22,w/2,h/2);
      const isCard=el.classList.contains('card'),rim=isCard?15:Math.min(15,h*.37),gain=isCard?12:16;
      for(let py=0;py<ch;py++)for(let px=0;px<cw;px++){
        const x=(px+.5)/dpr,y=(py+.5)/dpr,d=signedDistance(x,y,w,h,radius),offset=(py*cw+px)*4;
        if(d>0)continue;
        const depth=-d,t=clamp(depth/rim,0,1);
        // Smooth convex rim slope -> Snell refraction at glass index 1.5.
        // Artistic gain only sets perceived thickness; not Apple's renderer.
        const slope=2.6*Math.pow(1-t,2),angle=Math.atan(slope);
        const bend=Math.sin(angle-Math.asin(Math.sin(angle)/1.5))*gain*strength;
        let nx=(signedDistance(x+.5,y,w,h,radius)-signedDistance(x-.5,y,w,h,radius)),
            ny=(signedDistance(x,y+.5,w,h,radius)-signedDistance(x,y-.5,w,h,radius));
        const len=Math.hypot(nx,ny)||1;nx/=len;ny/=len;
        const sx=clamp(box.left+x-nx*bend,0,W-1),sy=clamp(box.top+y-ny*bend,0,H-1);
        const ix=Math.floor(sx),iy=Math.floor(sy),fx=sx-ix,fy=sy-iy;
        const ix1=Math.min(ix+1,W-1),iy1=Math.min(iy+1,H-1);
        const light=(-nx*.55-ny*.83),rimLine=Math.exp(-Math.pow((depth-1.15)/1.0,2));
        const gleam=rimLine*(.12+.32*Math.max(light,0));
        const shade=.065*Math.pow(1-t,3)*Math.max(-light,0);
        for(let c=0;c<3;c++){
          const a=pixels[(iy*W+ix)*4+c]*(1-fx)+pixels[(iy*W+ix1)*4+c]*fx;
          const b=pixels[(iy1*W+ix)*4+c]*(1-fx)+pixels[(iy1*W+ix1)*4+c]*fx;
          const v=(a*(1-fy)+b*fy)*(1-shade);
          dest[offset+c]=v*(1-gleam)+255*gleam;
        }
        // Main panels stay quiet; strongest distortion is on controls/navigation.
        dest[offset+3]=255*clamp(-d*dpr,0,1);
      }
      ctx.putImageData(out,0,0);
    }
    document.documentElement.dataset.lens='ready';
    window.__lensStats={panes:layers.length,lastRenderMs:Math.round(performance.now()-start),source:'local static SVG wallpaper',method:'canvas pixel displacement, curved rim, Snell-inspired'};
  }
  function schedule(){if(!frame)frame=requestAnimationFrame(paint);}
  image.onload=()=>{ready=true;schedule();};
  image.onerror=()=>{document.documentElement.dataset.lens='fallback';};
  image.src='/agy/backdrop.svg';
  new ResizeObserver(schedule).observe(document.querySelector('.shell'));
  const observer=new MutationObserver(mutations=>{if(mutations.some(m=>m.type==='attributes'||[...m.addedNodes,...m.removedNodes].some(n=>n.nodeType===1&&!n.classList?.contains('lens-layer'))))schedule();});
  observer.observe(document.querySelector('#accounts'),{childList:true});
  observer.observe(document.body,{attributes:true,attributeFilter:['data-theme','data-mode','data-lens-strength']});
  addEventListener('resize',schedule);
  // Home is viewport-locked. Scrolling settings/dialog content does not move lenses.
  window.visualViewport?.addEventListener('resize',schedule);
  document.addEventListener('visibilitychange',schedule);reduced.addEventListener('change',schedule);
})();
