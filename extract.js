const fs = require('fs');
const path = require('path');
const cheerio = require('cheerio');

const profiles = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'profiles.json'), 'utf8'));
const VERSION = 'v1.04';

function norm(v) { return String(v ?? '').replace(/\s+/g, ' ').trim(); }
function arr(v) { return Array.isArray(v) ? v.flat(Infinity).map(norm).filter(Boolean) : (norm(v) ? [norm(v)] : []); }
function unique(xs) { return [...new Set(xs.map(norm).filter(Boolean))]; }
function splitVals(xs, sep) {
  if (!sep) return unique(xs);
  const re = new RegExp(sep);
  return unique(xs.flatMap(x => String(x).split(re).map(norm)));
}
function hostname(url) { try { return new URL(url).hostname.toLowerCase(); } catch { return ''; } }
function matchesDomain(host, domains=[]) {
  return domains.some(d => {
    d = String(d).toLowerCase().replace(/^\*\./, '');
    return d === '*' || host === d || host.endsWith('.' + d);
  });
}
function chooseProfile(url, customProfiles=[]) {
  const host = hostname(url);
  for (const p of customProfiles || []) { const ds=p.domains||p.match?.domains||[]; if (matchesDomain(host, ds)) return p; }
  for (const p of profiles.profiles || []) if (matchesDomain(host, p.domains)) return p;
  return { id:'generic', use_generic:true, fields:{} };
}

function jsonLd($) {
  const out=[];
  $('script[type="application/ld+json"]').each((_,el)=>{
    const raw=$(el).text();
    try { out.push(JSON.parse(raw)); } catch {}
  });
  return out;
}
function pathGet(obj, pathStr) {
  let cur=obj;
  for (const part of String(pathStr||'').replace(/^\./,'').split('.').filter(Boolean)) {
    if (cur == null) return [];
    if (/^\d+$/.test(part) && Array.isArray(cur)) cur=cur[Number(part)];
    else if (Array.isArray(cur)) cur=cur.flatMap(x=>x && typeof x==='object' ? x[part] : undefined);
    else cur=cur[part];
  }
  return arr(cur);
}
function jsonLdResolve($, rule) {
  const wanted = rule.jsonType || rule.typeName;
  const all=jsonLd($), out=[];
  function walk(x) {
    if (Array.isArray(x)) { x.forEach(walk); return; }
    if (!x || typeof x !== 'object') return;
    if (wanted) {
      const ts=Array.isArray(x['@type'])?x['@type']:[x['@type']];
      if (!ts.filter(Boolean).some(t=>String(t).toLowerCase()===String(wanted).toLowerCase())) return;
    }
    if (rule.path) out.push(...pathGet(x,rule.path));
    else out.push(...Object.values(x).filter(v=>typeof v==='string'));
    if (rule.deep !== false) Object.values(x).forEach(v=>{ if (v && typeof v==='object') walk(v); });
  }
  all.forEach(walk);
  return unique(out);
}
function resolveRule($, url, rule) {
  if (!rule) return [];
  const type=rule.type || 'text';
  let out=[];
  if (type==='url') out=[url];
  else if (type==='title') out=[$('title').first().text()];
  else if (type==='canonical') {
    const v=$('link[rel="canonical"]').first().attr('href');
    if (v) out=[new URL(v,url).href];
  } else if (type==='meta') {
    const key=String(rule.key||'');
    const attr=rule.attr || 'property';
    let el=$(`meta[${attr}="${key.replace(/"/g,'\\"')}"]`).first();
    if (!el.length && attr==='property') el=$(`meta[name="${key.replace(/"/g,'\\"')}"]`).first();
    const v=el.attr('content'); if (v) out=[v];
  } else if (type==='css' || type==='text') {
    $(rule.selector||'').each((_,el)=>out.push(rule.attr ? $(el).attr(rule.attr) : $(el).text()));
  } else if (type==='attribute') {
    $(rule.selector||'').each((_,el)=>{ const v=$(el).attr(rule.attribute||'href'); if(v)out.push(v); });
  } else if (type==='html') {
    $(rule.selector||'').each((_,el)=>out.push($.html(el)));
  } else if (type==='regex') {
    const src=resolveRule($,url,rule.source||{type:'html',selector:'html'}).join('\n');
    try { const re=new RegExp(rule.pattern||'',rule.flags||'gi'); let m; while((m=re.exec(src))){ out.push(m[1] ?? m[0]); if(!re.global)break; } } catch {}
  } else if (type==='jsonld' || type==='jsonld_each') out=jsonLdResolve($,{...rule,deep:type!=='jsonld_each'?rule.deep:false});
  if (rule.absolute_url) out=out.map(v=>{try{return new URL(v,url).href}catch{return v}});
  if (rule.split) out=splitVals(out,rule.split);
  return unique(out);
}
function applyRules($, url, rules) {
  if (!Array.isArray(rules)) rules=[rules];
  let out=[];
  for(const r of rules||[]) out.push(...resolveRule($,url,r));
  return unique(out);
}
function genericExtract($, finalUrl) {
  const meta=(key,attr='property')=>applyRules($,finalUrl,{type:'meta',key,attr})[0]||'';
  return {
    title: meta('og:title') || norm($('title').first().text()),
    url: applyRules($,finalUrl,[{type:'canonical'},{type:'url'}])[0] || finalUrl,
    description: meta('og:description') || meta('description','name'),
    image: meta('og:image') || meta('twitter:image','name'),
    siteName: meta('og:site_name'),
    author: meta('author','name'),
    published: meta('article:published_time'),
    modified: meta('article:modified_time'),
    keywords: splitVals(applyRules($,finalUrl,{type:'meta',key:'keywords',attr:'name'}), ',|;'),
    language: $('html').attr('lang') || '',
    jsonld: jsonLd($)
  };
}
function dedupeObjectValues(o){
  for(const k of Object.keys(o)) if(Array.isArray(o[k])) o[k]=unique(o[k]);
  return o;
}
async function fetchOne(url, timeoutMs=20000){
  const ac=new AbortController(); const t=setTimeout(()=>ac.abort(),timeoutMs);
  try {
    const res=await fetch(url,{redirect:'follow',signal:ac.signal,headers:{'user-agent':'Mozilla/5.0 (compatible; TagmarkExtractor/1.01; +https://tagmark.example)','accept':'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8','accept-language':'ko,en;q=0.8'}});
    const text=await res.text();
    return {ok:res.ok,status:res.status,finalUrl:res.url||url,html:text,contentType:res.headers.get('content-type')||''};
  } finally { clearTimeout(t); }
}
async function extract(url, originalTitle, customProfiles=[], profileOverride=null){
  const started=Date.now();
  let u=String(url||'').trim();
  if(!/^https?:\/\//i.test(u)) return {url:u,originalTitle,status:'error',error:'HTTP/HTTPS URL이 아닙니다.'};
  const f=await fetchOne(u);
  if(!f.contentType.includes('html') && !/^<!doctype|<html/i.test(f.html.trim())) return {url:u,originalTitle,status:'error',error:`HTML 문서가 아닙니다 (${f.contentType||'unknown'}).`,httpStatus:f.status};
  const $=cheerio.load(f.html,{decodeEntities:true});
  const profile=profileOverride || chooseProfile(f.finalUrl, customProfiles);
  const data=genericExtract($,f.finalUrl);
  if(profile.fields) {
    for(const [key,rules] of Object.entries(profile.fields)) {
      const vals=applyRules($,f.finalUrl,rules);
      if(vals.length) data[key]=vals.length===1?vals[0]:vals;
    }
  }
  const tags=[];
  for(const r of profile.tagRules||[]) tags.push(...applyRules($,f.finalUrl,r.rules||r.rule).flatMap(v=>r.split?String(v).split(new RegExp(r.split)):v));
  const categories=[];
  for(const r of profile.categoryRules||[]) categories.push(...applyRules($,f.finalUrl,r.rules||r.rule));
  const profilesOut=[];
  for(const r of profile.profileRules||[]) profilesOut.push(...applyRules($,f.finalUrl,r.rules||r.rule));
  data.tags=unique(tags); data.categories=unique(categories); data.profiles=unique(profilesOut);
  data.title=data.title||originalTitle||u; data.url=data.url||f.finalUrl;
  return {url:u,originalTitle:originalTitle||'',status:'success',httpStatus:f.status,finalUrl:f.finalUrl,profile:profile.id||'generic',data:dedupeObjectValues(data),durationMs:Date.now()-started};
}

module.exports = async function handler(req,res){
  if(req.method!=='POST') return res.status(405).json({error:'POST만 지원합니다.'});
  try {
    const body=typeof req.body==='string'?JSON.parse(req.body):req.body||{};
    const items=Array.isArray(body.items)?body.items:[];
    const customProfiles=Array.isArray(body.customProfiles)?body.customProfiles:[];
    const profileOverride=body.profileOverride && typeof body.profileOverride==='object' ? body.profileOverride : null;
    if(!items.length) return res.status(400).json({error:'items가 없습니다.'});
    if(items.length>40) return res.status(400).json({error:'한 요청에는 최대 40개 URL만 넣을 수 있습니다.'});
    const results=new Array(items.length);
    const limit=5;
    let cursor=0;
    async function worker(){
      while(true){
        const i=cursor++;
        if(i>=items.length)return;
        const item=items[i];
        try { results[i]=await extract(item.url,item.title,customProfiles,profileOverride); }
        catch(e){ results[i]={url:item.url,title:item.title||'',status:'error',error:e?.name==='AbortError'?'시간 초과':String(e?.message||e)}; }
      }
    }
    await Promise.all(Array.from({length:Math.min(limit,items.length)},worker));
    return res.status(200).json({version:VERSION,results});
  } catch(e) { return res.status(500).json({error:String(e?.message||e)}); }
};
