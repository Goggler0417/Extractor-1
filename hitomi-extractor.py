#!/usr/bin/env python3
"""Hitomi metadata workbench. Python 3.9+; standard library only."""
import argparse
import base64
import binascii
import csv
import html
import io
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = 'https://hitomi.la'
HOSTS = {'hitomi.la', 'www.hitomi.la', 'ltn.hitomi.la'}
ID = re.compile(r'[1-9][0-9]{0,11}\Z')
PATH = re.compile(r'/(?:reader|galleries)/([0-9]{1,12})(?:\.html)?/?\Z|/[^/]+/[^/]+-([0-9]{1,12})\.html\Z', re.I)
URL = re.compile(r"(?<![\w./@-])(?:https?://|//)?(?:www\.)?hitomi\.la/[^\s<>\"'\\]+", re.I)
MAX_FILE = 12 * 1024 * 1024
MAX_REMOTE = 5 * 1024 * 1024
MAX_LINKS = 3000
GATE = threading.BoundedSemaphore(4)
VIDEO = {'mp4', 'webm', 'mov', 'm4v', 'mkv', 'avi', 'gifv'}
IMAGE = {'jpg', 'jpeg', 'png', 'webp', 'avif', 'gif', 'bmp', 'jxl'}

def gallery_url(raw):
    raw = html.unescape(raw.strip())
    if raw.startswith('//'):
        raw = 'https:' + raw
    if not re.match(r'^https?://', raw, re.I):
        raw = 'https://' + raw
    try:
        u = urllib.parse.urlsplit(raw)
        if u.netloc.lower() not in ('hitomi.la', 'www.hitomi.la'):
            return None
        m = PATH.fullmatch(urllib.parse.unquote(u.path))
    except ValueError:
        return None
    if not m:
        return None
    ident = (m[1] or m[2]).lstrip('0')
    if not ID.fullmatch(ident):
        return None
    path = urllib.parse.quote(urllib.parse.unquote(u.path), safe='/!$&()*+,-.:;=@_~')
    return ident, SITE + path

def links(parts):
    found = {}
    for part in parts:
        for m in URL.finditer(html.unescape(part)):
            result = gallery_url(m[0].rstrip('.,);]}、。'))
            if result:
                ident, url = result
                if ident not in found and len(found) >= MAX_LINKS:
                    raise ValueError('한 번에 작품 3,000개까지 추가할 수 있습니다.')
                found.setdefault(ident, {'id': ident, 'url': url})
    return list(found.values())

class SourceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_starttag(self, tag, attrs):
        self.parts.extend(v for k, v in attrs if k in ('href', 'data-href', 'data-url') and v)
    def handle_data(self, data):
        self.parts.append(data)

def json_strings(obj):
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for k, v in reversed(list(node.items())):
                stack.extend((v, k))
        elif isinstance(node, list):
            stack.extend(reversed(node))

def docx_parts(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [i for i in z.infolist() if re.fullmatch(r'word/(?:_rels/[^/]+\.xml\.rels|[^/]+\.xml)', i.filename)]
        if sum(i.file_size for i in names) > 30 * 1024 * 1024:
            raise ValueError('DOCX 내부 문서가 너무 큽니다.')
        for item in names:
            content = z.read(item)
            if re.search(rb'<!DOCTYPE|<!ENTITY', content.replace(b'\x00', b''), re.I):
                raise ValueError('지원하지 않는 DOCX XML입니다.')
            root = ET.fromstring(content)
            if item.filename.endswith('.rels'):
                for element in root.iter():
                    if element.tag.rsplit('}', 1)[-1] == 'Relationship':
                        yield element.get('Target', '')
            else:
                for element in root.iter():
                    if element.tag.rsplit('}', 1)[-1] == 'p':
                        yield ''.join(t.text or '' for t in element.iter() if t.tag.rsplit('}', 1)[-1] in ('t', 'instrText'))
                    for key, value in element.attrib.items():
                        if key.rsplit('}', 1)[-1] == 'instr':
                            yield value

def read_file(name, blob):
    ext = name.rsplit('.', 1)[-1].lower()
    if ext == 'docx':
        return links(docx_parts(blob))
    if ext not in {'html','htm','json','txt','csv','md','url'}:
        raise ValueError('DOCX, HTML, JSON, TXT, CSV, MD 파일을 선택하세요.')
    if blob.startswith((b'\xff\xfe', b'\xfe\xff')):
        value = blob.decode('utf-16')
    else:
        try:
            value = blob.decode('utf-8-sig')
        except UnicodeDecodeError:
            value = blob.decode('cp949')
    if ext == 'json':
        return links(json_strings(json.loads(value)))
    if ext in ('html', 'htm'):
        parser = SourceHTML()
        parser.feed(value)
        return links([value, *parser.parts])
    return links([value])

class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != 'https' or u.netloc.lower() not in HOSTS:
            raise ValueError('히토미 외부로 연결되는 주소입니다.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

OPENER = urllib.request.build_opener(RestrictedRedirect())

def get(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != 'https' or u.netloc.lower() not in HOSTS:
        raise ValueError('허용되지 않는 메타데이터 주소입니다.')
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36', 'Referer':SITE + '/', 'Accept':'*/*'})
    with GATE, OPENER.open(req, timeout=9) as res:
        data = res.read(MAX_REMOTE + 1)
        if len(data) > MAX_REMOTE:
            raise ValueError('메타데이터 응답이 너무 큽니다.')
        return data.decode('utf-8-sig', 'replace')

def gallery_json(raw):
    m = re.match(r'^\s*(?:(?:var|let|const)\s+)?(?:window\.)?galleryinfo\s*=\s*', raw)
    value, end = json.JSONDecoder().raw_decode(raw[m.end():] if m else raw.lstrip())
    if not isinstance(value, dict):
        raise ValueError('지원하지 않는 메타데이터 응답입니다.')
    return value

def clean(v):
    return html.unescape(v).strip() if isinstance(v, str) else ''

def names(raw, *keys):
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            item = next((item[k] for k in (*keys, 'name') if clean(item.get(k))), '')
        item = clean(item)
        if item and item not in out:
            out.append(item)
    return out

def duration(raw):
    if isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, str) and ':' in raw:
            parts = raw.split(':')
            if len(parts) not in (2,3) or not all(re.fullmatch(r'\d+', s) for s in parts) or any(int(s)>=60 for s in parts[1:]):
                return None
            raw = sum(int(s)*60**i for i,s in enumerate(reversed(parts)))
        elif isinstance(raw,str) and raw.startswith('PT'):
            m = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', raw)
            if not m or not any(m.groups()):
                return None
            raw = 3600*int(m[1] or 0)+60*int(m[2] or 0)+float(m[3] or 0)
        val = float(raw)
        return round(val) if math.isfinite(val) and 0 <= val <= 10**9 else None
    except (TypeError, ValueError, OverflowError):
        return None

def normalize(data):
    files = data.get('files')
    pages = None
    seconds = next((v for k in ('durationSeconds','duration_seconds','duration','runtime') if (v:=duration(data.get(k))) is not None), None)
    if isinstance(files, list) and files:
        exts = [str(f.get('name','')).lower().rsplit('.',1)[-1] if isinstance(f,dict) else '' for f in files]
        if any(e in VIDEO for e in exts):
            lengths = [duration(f.get('duration_seconds', f.get('duration'))) if isinstance(f,dict) else None for f,e in zip(files,exts) if e in VIDEO]
            if seconds is None and all(v is not None for v in lengths):
                seconds = sum(lengths)
            if any(e in IMAGE for e in exts):
                pages = sum(e in IMAGE for e in exts)
        elif all(e in IMAGE or (isinstance(f,dict) and f.get('hash') and not f.get('name')) for f,e in zip(files,exts)):
            pages = len(files)
    if pages is None:
        n = data.get('pages',data.get('page_count'))
        if re.fullmatch(r'[1-9]\d{0,6}',str(n)):
            pages = int(n)
    return dict(title=clean(data.get('title')) or clean(data.get('japanese_title')),
                artists=names(data.get('artists') or data.get('artist'),'artist'),
                groups=names(data.get('groups') or data.get('group'),'group','circle'),
                series=names(data.get('parodys') or data.get('parodies') or data.get('series'),'parody','series'),
                type=clean(data.get('type')), language=clean(data.get('language')), pages=pages, durationSeconds=seconds)

class GalleryHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.rows = {}
        self.cells = None
        self.cell = None
        self.scripts = []
        self.artist = []
        self.h2 = False
        self.h2link = None
        self.anchor = False
        self.in_h1 = False
        self.h1 = ""
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            self.meta[a.get('property') or a.get('name') or ''] = a.get('content','')
        if tag == 'script' and a.get('src'):
            self.scripts.append(a['src'])
        if tag == 'tr':
            self.cells = []
        if tag in ('td','th') and self.cells is not None:
            self.cell = ['']
        if tag == 'a' and self.cell is not None:
            self.cell.append('')
            self.anchor = True
        if tag == 'h1':
            self.in_h1 = True
        if tag == 'h2':
            self.h2 = True
        if tag == 'a' and self.h2 and '/artist/' in a.get('href',''):
            self.h2link = ''
    def handle_data(self, data):
        if self.cell is not None:
            self.cell[0] += data
            if self.anchor:
                self.cell[-1] += data
        if self.in_h1:
            self.h1 += data
        if self.h2link is not None:
            self.h2link += data
    def handle_endtag(self, tag):
        if tag == 'a':
            self.anchor = False
        if tag == 'h1':
            self.in_h1 = False
        if tag == 'a' and self.h2link is not None:
            self.artist.append(self.h2link)
            self.h2link = None
        if tag == 'h2':
            self.h2 = False
        if tag in ('td','th') and self.cell is not None and self.cells is not None:
            self.cells.append(self.cell)
            self.cell = None
        if tag == 'tr' and self.cells is not None:
            if len(self.cells)>1:
                self.rows[self.cells[0][0].strip().rstrip(':').lower()] = self.cells[1]
            self.cells = self.cell = None
    def fields(self):
        def row(*keys):
            for k in keys:
                if k in self.rows:
                    vals = self.rows[k]
                    return names(vals[1:]) or names(vals[0])
            return []
        return normalize(dict(title=self.meta.get('og:title') or self.meta.get('twitter:title') or (self.h1 if self.rows else ''),
            artists=row('artist','artists') or self.artist, groups=row('group','groups','circle'),
            series=row('series','parody'), type=next(iter(row('type','format')),''),
            language=next(iter(row('language')),''), pages=next(iter(row('pages','page count')),None),
            duration=self.meta.get('video:duration') or next(iter(row('duration','runtime')),None)))

def extract(ident, url, fetch=get):
    endpoints = [f'https://ltn.hitomi.la/galleries/{ident}.js']
    errors = []
    result = {}
    page = None
    try:
        result = normalize(gallery_json(fetch(endpoints[0])))
        if not any(v is not None and v != '' and v != [] for v in result.values()):
            raise ValueError('갤러리 정보를 찾지 못했습니다.')
    except (ValueError, OSError, urllib.error.URLError) as e:
        errors.append(e)
    if not result.get('title') or any(not result.get(k) for k in ('type','artists','groups','series')) or (result.get('pages') is None and result.get('durationSeconds') is None):
        detail = url if not urllib.parse.urlsplit(url).path.startswith('/reader/') else SITE + f'/galleries/{ident}.html'
        try:
            page = GalleryHTML()
            page.feed(fetch(detail))
            if not result.get('title'):
                for src in page.scripts:
                    candidate = urllib.parse.urljoin(detail, src)
                    u = urllib.parse.urlsplit(candidate)
                    if u.netloc.lower() in HOSTS and u.scheme=='https' and u.path==f'/galleries/{ident}.js' and candidate not in endpoints:
                        try:
                            result.update(normalize(gallery_json(fetch(candidate))))
                            break
                        except (OSError, ValueError) as e:
                            errors.append(e)
            if page.meta.get('og:title') or page.rows:
                for key, value in page.fields().items():
                    if not result.get(key) and value not in (None,'',[]):
                        result[key] = value
        except (OSError, ValueError) as e:
            errors.append(e)
    if not result.get('title'):
        if any(isinstance(e,urllib.error.HTTPError) and e.code in (403,429) for e in errors):
            raise ValueError('사이트가 요청을 제한했습니다. 잠시 후 다시 시도하세요.')
        if any(isinstance(e,urllib.error.HTTPError) and e.code==404 for e in errors):
            raise ValueError('작품 정보를 찾을 수 없습니다. 링크를 확인하세요.')
        if any(isinstance(e,OSError) for e in errors):
            raise ValueError('히토미에 연결할 수 없습니다. 네트워크를 확인한 뒤 다시 시도하세요.')
        raise ValueError('사이트 응답에서 제목을 확인하지 못했습니다.')
    return result

def csv_text(rows):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    keys = ('id','title','url','pages','durationSeconds','artists','groups','type','series','language','note','status')
    writer.writerow(keys)
    for row in rows:
        values = []
        for key in keys:
            val = row.get(key)
            if isinstance(val,list):
                val = ' | '.join(val)
            if isinstance(val,str) and (val.startswith(('\t','\r','\n')) or val.lstrip().startswith(('=','+','-','@'))):
                val = "'" + val
            values.append(val)
        writer.writerow(values)
    return '\ufeff' + out.getvalue()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def send(self, payload, status=200, kind='application/json; charset=utf-8'):
        b = payload.encode('utf-8') if isinstance(payload,str) else json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(b)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(b)
    def expected(self):
        return f'127.0.0.1:{self.server.server_port}'
    def do_GET(self):
        if self.headers.get('Host')!=self.expected():
            self.send({'error':'잘못된 주소입니다.'},403)
        elif self.path == '/':
            self.send(PAGE,kind='text/html; charset=utf-8')
        else:
            self.send({},404)
    def do_POST(self):
        if self.headers.get('Host')!=self.expected() or self.headers.get('Origin')!=f'http://{self.expected()}' or self.headers.get_content_type()!='application/json':
            return self.send({'error':'로컬 앱에서 요청하세요.'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 18*1024*1024:
                raise ValueError('입력 크기가 너무 큽니다.')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError('잘못된 요청입니다.')
            if self.path=='/api/links':
                rows=links([data.get('text','')])
            elif self.path=='/api/file':
                blob=base64.b64decode(data['data'],validate=True)
                if len(blob)>MAX_FILE:
                    raise ValueError('파일은 12MB까지 올릴 수 있습니다.')
                rows=read_file(data['name'],blob)
            elif self.path=='/api/extract':
                v=gallery_url(data.get('url',''))
                if not v:
                    raise ValueError('지원하는 히토미 작품 링크를 입력하세요.')
                return self.send(extract(*v))
            elif self.path=='/api/csv':
                return self.send(csv_text(data['rows']),kind='text/csv; charset=utf-8')
            else:
                return self.send({},404)
            self.send({'rows':rows})
        except (KeyError, TypeError, AttributeError, ValueError, OSError, ET.ParseError, zipfile.BadZipFile, binascii.Error, RecursionError) as e:
            self.send({'error':str(e) if isinstance(e,ValueError) and type(e) is ValueError else '파일이나 입력 형식을 읽을 수 없습니다.'},400)

PAGE = r'''<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hitomi 추출기</title>
<style>
:root{color-scheme:light;--ink:#183235;--muted:#526968;--line:#dbe6e2;--accent:#176457;--paper:#fff}
[hidden]{display:none!important}*{box-sizing:border-box}body{margin:0;background:#f2f7f4;color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1580px;margin:auto;padding:34px clamp(16px,4vw,56px) 45px}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;margin-bottom:22px}h1{font-size:clamp(28px,4vw,38px);margin:0;letter-spacing:-.04em}h2{font-size:17px;margin:0}.eyebrow{font-size:12px;font-weight:750;letter-spacing:.12em;color:var(--accent)}p{margin:5px 0;color:var(--muted)}.panel{border:1px solid var(--line);border-radius:15px;background:var(--paper);padding:17px 20px;margin-bottom:17px;box-shadow:0 1px 4px #173b3210}
textarea,input,select,button{font:inherit;color:inherit}textarea,input,select{background:#fff;border:1px solid #ceddd7;border-radius:7px;padding:7px 9px;min-width:0}textarea{width:100%;margin-top:7px;resize:vertical;min-height:69px}button,.file{background:white;border:1px solid #c9dad3;border-radius:8px;padding:8px 12px;white-space:nowrap;cursor:pointer;display:inline-block;font:inherit}button:hover,.file:hover{background:#edf6f0}button:disabled{opacity:.45;cursor:default}.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:650}.primary:not(:disabled):hover{background:#0f5144}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{outline:2px solid #218375;outline-offset:2px}.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.between{justify-content:space-between}.hint{font-size:12px;color:var(--muted)}.status{font-weight:650;color:var(--accent)}#feedback{min-height:21px;margin-top:7px}.error{color:#a23232!important}.wide{padding:0;overflow:hidden}.tablebar{padding:14px 17px;border-bottom:1px solid var(--line)}.scroll{overflow:auto;max-height:65vh}table{border-collapse:separate;border-spacing:0;width:100%;min-width:1380px}th,td{text-align:left;padding:8px 7px;border-bottom:1px solid var(--line);vertical-align:top}th{position:sticky;top:0;background:#e8f2ed;z-index:1;font-size:12px;white-space:nowrap}tbody tr:nth-child(even){background:#f8fbf9}td input{width:100%;font-size:13px}td a{font-size:12px;color:var(--accent);text-decoration:none}td:first-child,th:first-child{padding-left:14px}td:last-child,th:last-child{padding-right:14px}.badge{white-space:nowrap;font-size:11px;background:#edf1ef;padding:3px 6px;border-radius:6px}.badge.ok{background:#def3e7;color:#155d48}.badge.fail{background:#fce9e6;color:#a23232}.badge.loading{background:#e7f1fc;color:#225487}.rowtools{display:flex;gap:5px;align-items:center;margin-top:5px}.rowtools button{padding:3px 6px;font-size:11px}.reason{font-size:11px;color:#9d3434;max-width:160px;overflow-wrap:anywhere}.empty{padding:38px 14px;text-align:center;color:var(--muted)}.foot{padding:10px 16px;display:flex;justify-content:space-between;gap:8px;align-items:center}.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}@media(max-width:700px){main{padding-top:21px}.panel{padding:13px}.wide{padding:0}header{align-items:flex-start;flex-direction:column}.scroll{max-height:65vh}}
</style>
<main>
<header><div><div class="eyebrow">BOOKMARK TOOLS / 01</div><h1>Hitomi 추출기</h1><p>작품 링크를 모아 메타데이터를 채우고, 목록에서 바로 수정하세요.</p></div><span class="hint">로컬 작업 공간 · v1.0</span></header>
<section class="panel" aria-label="링크와 파일 입력">
<label for="links"><strong>작품 링크</strong></label><textarea id="links" placeholder="https://hitomi.la/reader/123456.html&#10;여러 링크를 줄바꿈으로 붙여넣을 수 있어요."></textarea>
<div class="bar"><button id="add" class="primary">링크 추가</button><button id="files">파일 추가</button><input class="sr" type="file" id="upload" multiple accept=".docx,.html,.htm,.json,.txt,.csv,.md,.url" tabindex="-1" aria-label="파일 선택"><span class="hint">DOCX · HTML · JSON · TXT · CSV · MD</span></div>
<div id="feedback" aria-live="polite" class="hint"></div>
</section>
<section class="panel wide" aria-label="결과 편집">
<div class="tablebar">
<div class="bar between"><h2>편집 목록 <span id="counts" class="hint"></span></h2><div class="bar"><button id="start" class="primary">정보 수집</button><button id="stop" disabled>수집 중지</button><button id="json" disabled>JSON 내보내기</button><button id="csv" disabled>CSV 내보내기</button></div></div>
<div class="bar between" style="margin-top:8px"><span class="hint">셀을 클릭해 바로 수정 · 작가, 그룹, 시리즈는 | 로 구분 · 재생시간은 초 또는 mm:ss</span><span id="summary" class="status" aria-live="polite"></span></div>
</div>
<div class="empty" id="empty">링크나 파일을 추가하면 여기에 작품 목록이 표시됩니다.</div>
<div class="scroll" id="grid" hidden><table><colgroup><col style="width:130px"><col style="width:260px"><col style="width:218px"><col style="width:70px"><col style="width:94px"><col style="width:132px"><col style="width:132px"><col style="width:108px"><col style="width:132px"><col style="width:88px"><col style="width:140px"></colgroup><thead><tr><th>상태 / 작품 번호</th><th>제목</th><th>링크</th><th>페이지</th><th>재생시간</th><th>작가</th><th>그룹</th><th>형식</th><th>시리즈</th><th>언어</th><th>메모</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="foot" id="foot" hidden><span class="hint" id="range"></span><div class="bar"><button id="prev">이전</button><button id="next">다음</button></div></div>
</section><p class="hint">작품 이미지와 영상은 내려받지 않습니다. 현재 목록을 남기려면 JSON 또는 CSV로 내보내세요.</p>
<datalist id="types"><option value="doujinshi" label="동인지"><option value="manga" label="망가"><option value="artistcg" label="작가 CG"><option value="gamecg" label="게임 CG"><option value="anime" label="애니메이션"></datalist>
</main>
<script>
'use strict';
const $=id=>document.getElementById(id), fields=['title','url','pages','durationSeconds','artists','groups','type','series','language','note'];
const labels=['제목','링크','페이지','재생시간','작가','그룹','형식','시리즈','언어','메모'];
const multis=new Set(['artists','groups','series']);
let rows=[], current=0, running=false, stopped=false;
const pageSize=50, statusNames={pending:'대기',loading:'수집 중',ok:'완료',fail:'실패'};
function note(msg,bad=false){$('feedback').textContent=msg;$('feedback').className=bad?'hint error':'hint'}
async function api(path,data,asText=false){
 const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
 if(!r.ok){const d=await r.json();throw new Error(d.error||'요청을 처리하지 못했습니다.')}
 return asText?r.text():r.json();
}
function add(found){
 let added=0, dup=0, excess=0;const ids=new Set(rows.map(r=>r.id));
 for(const item of found){if(ids.has(item.id)){dup++;continue}if(rows.length>=3000){excess++;continue}
  ids.add(item.id);rows.push({...item,title:'',pages:'',durationSeconds:'',artists:'',groups:'',type:'',series:'',language:'',note:'',status:'pending',error:'',edited:new Set()});added++;
 }
 render();
 return added+'개 추가'+(dup?' · 중복 '+dup+'개 제외':'')+(excess?' · 한도 초과 '+excess+'개':'');
}
$('add').onclick=async()=>{try{const d=await api('links',{text:$('links').value});if(!d.rows.length)return note('지원하는 히토미 작품 링크를 찾지 못했습니다.',true);note(add(d.rows));$('links').value=''}catch(e){note(e.message,true)}};
function encodeFile(buffer){const bytes=new Uint8Array(buffer);let out='';for(let i=0;i<bytes.length;i+=32768)out+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(out)}
$('files').onclick=()=>$('upload').click();
$('upload').onchange=async ev=>{
 const files=[...ev.target.files];if(!files.length)return;
 $('files').disabled=true;const errors=[],found=[];
 try{for(const f of files){try{if(f.size>12*1024*1024)throw Error('12MB를 초과했습니다.');const d=await api('file',{name:f.name,data:encodeFile(await f.arrayBuffer())});if(!d.rows.length)errors.push(f.name+': 작품 링크 없음');found.push(...d.rows)}catch(e){errors.push(f.name+': '+e.message)}}
 note(add(found)+(errors.length?' · '+errors.join(' / '):''),!!errors.length);
 }finally{ev.target.value='';$('files').disabled=false}
};
function linkId(value){
 try{const u=new URL(value.trim().replace(/^(?=hitomi\.la\/|www\.hitomi\.la\/)/i,'https://').replace(/^\/\//,'https://'));
 if(!/^https?:$/.test(u.protocol)||!['hitomi.la','www.hitomi.la'].includes(u.host.toLowerCase())||u.username||u.password)return '';
 const m=decodeURIComponent(u.pathname).match(/^\/(?:reader|galleries)\/(\d{1,12})(?:\.html)?\/?$|^\/[^/]+\/[^/]+-(\d{1,12})\.html$/i);
 return (m?.[1]||m?.[2]||'').replace(/^0+/,'');
 }catch{return ''}
}
function update(){
 const ok=rows.filter(r=>r.status==='ok').length, fail=rows.filter(r=>r.status==='fail').length, waiting=rows.length-ok-fail;
 $('counts').textContent=rows.length+'개';$('summary').textContent=rows.length?'완료 '+ok+' · 실패 '+fail+' · 대기/수집 '+waiting:'';
 $('start').disabled=running||!rows.some(r=>r.status==='pending'||r.status==='fail');$('stop').disabled=!running||stopped;
 $('json').disabled=$('csv').disabled=!rows.length;
}
function cell(parent,tag,value){const e=document.createElement(tag);if(value!=null)e.textContent=value;parent.append(e);return e}
function paint(tr,row){
 const b=tr.querySelector('.badge');b.className='badge '+row.status;b.textContent=statusNames[row.status];
 tr.querySelector('.reason').textContent=row.error;
 tr.querySelector('.retry').disabled=running||row.status==='loading';
 const open=tr.querySelector('a');open.href=linkId(row.url)?'https://hitomi.la/reader/'+linkId(row.url)+'.html':'#';open.hidden=!linkId(row.url);
 for(const input of tr.querySelectorAll('input'))if(input!==document.activeElement)input.value=row[input.dataset.field]??'';
}
function render(){
 const tbody=$('rows');tbody.replaceChildren();current=Math.max(0,Math.min(current,Math.ceil(rows.length/pageSize)-1));
 $('empty').hidden=!!rows.length;$('grid').hidden=!rows.length;$('foot').hidden=!rows.length;
 const a=current*pageSize,b=Math.min(a+pageSize,rows.length);
 $('range').textContent=(a+1)+'–'+b+' / '+rows.length;
 $('prev').disabled=current===0;$('next').disabled=b===rows.length;
 for(const row of rows.slice(a,b)){
  const tr=cell(tbody,'tr');tr.dataset.id=row.id;const first=cell(tr,'td');
  cell(first,'span').className='badge';const tools=cell(first,'div');tools.className='rowtools';
  const retry=cell(tools,'button','재수집');retry.className='retry';retry.onclick=()=>collect([row]);
  const del=cell(tools,'button','제거');del.onclick=()=>{if(row.status==='loading')return note('수집 중인 행은 완료 후 제거할 수 있습니다.',true);rows=rows.filter(r=>r!==row);render()};
  const why=cell(first,'div');why.className='reason';const open=cell(first,'a',row.id+' ↗');open.target='_blank';open.rel='noopener noreferrer';
  fields.forEach((f,i)=>{const td=cell(tr,'td'), el=cell(td,'input');el.dataset.field=f;el.setAttribute('aria-label',row.id+' '+labels[i]);el.autocomplete='off';if(f==='type')el.setAttribute('list','types');if(f==='pages')el.inputMode='numeric';
   el.oninput=()=>{row[f]=el.value;row.edited.add(f)};
   if(f==='url')el.onchange=()=>{const id=linkId(row.url);if(!id||id!==row.id){row.status='fail';row.error='이 행과 같은 작품 번호의 링크를 입력하세요. 다른 작품은 새로 추가하세요.'}else if(row.status==='fail'&&row.error.startsWith('이 행')){row.status='pending';row.error=''}paint(tr,row);update()};
  });paint(tr,row);
 }
 update();
}
$('prev').onclick=()=>{current--;render();$('grid').scrollTop=0};$('next').onclick=()=>{current++;render();$('grid').scrollTop=0};
function refresh(row){const tr=$('rows').querySelector('tr[data-id="'+row.id+'"]');if(tr)paint(tr,row);update()}
async function collect(queue){
 if(running||!queue.length)return;running=true;stopped=false;update();$('rows').querySelectorAll('.retry').forEach(el=>el.disabled=true);
 let index=0, attempts=0;
 async function worker(){
  while(!stopped&&index<queue.length){
   const row=queue[index++];if(!rows.includes(row))continue;
   row.status='loading';row.error='';refresh(row);
   try{
    if(linkId(row.url)!==row.id)throw Error('이 행과 같은 작품 번호의 링크를 입력하세요. 다른 작품은 새로 추가하세요.');
    const metadata=await api('extract',{url:row.url});
    for(const f of fields)if(f!=='url'&&f in metadata&&!row.edited.has(f)){
     const v=metadata[f];row[f]=Array.isArray(v)?v.join(' | '):(v??'').toString();
    }
    row.status='ok';
   }catch(e){row.status='fail';row.error=e.message}
   attempts++;refresh(row);await new Promise(r=>setTimeout(r,350));
  }
 }
 try{await Promise.all(Array.from({length:Math.min(3,queue.length)},worker));note((stopped?'수집 중지 · ':'수집 완료 · ')+attempts+'개 처리');}
 finally{running=false;for(const row of rows.slice(current*pageSize,(current+1)*pageSize))refresh(row);update()}
}
$('start').onclick=()=>collect(rows.filter(r=>r.status==='pending'||r.status==='fail'));
$('stop').onclick=()=>{stopped=true;update();note('진행 중인 요청이 끝나면 중지합니다.')};
function parseSeconds(s){
 if(!s)return null;
 if(/^\d+$/.test(s)&&Number(s)<=1e9)return Number(s);
 if(/^\d+(?::[0-5]\d){1,2}$/.test(s)){const n=s.split(':').reduce((acc,val)=>acc*60+Number(val),0);if(n<=1e9)return n}
 throw Error('재생시간은 초, mm:ss 또는 hh:mm:ss로 입력하세요.');
}
function exportRows(){
 return rows.map(r=>{
  const out={id:r.id};
  for(const f of fields){
   const v=r[f].trim();
   if(multis.has(f))out[f]=[...new Set(v.split(/\s*[|;\n]\s*/).filter(Boolean))];
   else if(f==='pages'){if(v&&!/^[1-9]\d{0,6}$/.test(v))throw Error(r.id+'번: 페이지는 양의 정수로 입력하세요.');out[f]=v?Number(v):null}
   else if(f==='durationSeconds'){try{out[f]=parseSeconds(v)}catch(e){throw Error(r.id+'번: '+e.message)}}
   else if(f==='url'){if(linkId(v)!==r.id)throw Error(r.id+'번: 작품 링크를 확인하세요.');out[f]=v}
   else out[f]=v;
  }
  out.status=r.status==='loading'?'pending':r.status;if(r.error)out.error=r.error;
  return out;
 });
}
function download(value,ext){
 const url=URL.createObjectURL(new Blob([value],{type:ext==='json'?'application/json;charset=utf-8':'text/csv;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;a.download='hitomi-'+new Date().toISOString().slice(0,10)+'.'+ext;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('json').onclick=()=>{try{const items=exportRows();download(JSON.stringify({format:'hitomi-metadata',version:1,exportedAt:new Date().toISOString(),items},null,2),'json');note(items.length+'개 작품을 JSON으로 내보냈습니다.')}catch(e){note(e.message,true)}};
$('csv').onclick=async()=>{try{const items=exportRows();download(await api('csv',{rows:items},true),'csv');note(items.length+'개 작품을 CSV로 내보냈습니다.')}catch(e){note(e.message,true)}};
render();
</script></html>'''

def main():
    args=argparse.ArgumentParser(description='Hitomi 메타데이터 추출기')
    args.add_argument('--port',type=int,default=0,help='로컬 포트 (기본: 빈 포트 자동 선택)')
    args.add_argument('--no-browser',action='store_true')
    options=args.parse_args()
    with ThreadingHTTPServer(('127.0.0.1',options.port), Handler) as server:
        url=f'http://127.0.0.1:{server.server_port}/'
        print(f'Hitomi 추출기: {url}\n종료: Ctrl+C',flush=True)
        if not options.no_browser:
            threading.Timer(.25,webbrowser.open,args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    main()
