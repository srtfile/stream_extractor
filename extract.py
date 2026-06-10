#!/usr/bin/env python3
"""
Stream URL Extractor — headless CLI version for GitHub Actions.
Usage: python3 extract.py <input_urls.txt> <output_results.json> <run_id>
"""

import os, re, sys, json, ast, codecs, random, string, time, traceback
import html as html_lib
from urllib.parse import urlparse, urljoin
from base64 import b64decode
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

def _session(headers: dict = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if headers:
        s.headers.update(headers)
    return s

def detect_host(url: str) -> str:
    host = urlparse(url).netloc.lower().lstrip("www.")
    for family, patterns in HOST_MAP.items():
        for p in patterns:
            if p in host:
                return family
    return "generic"

def _to_base(n: int, base: int) -> str:
    if n == 0: return "0"
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    while n:
        out.append(chars[n % base])
        n //= base
    return "".join(reversed(out))

def unpack_packer(packed: str) -> str:
    m = re.search(
        r"}\s*\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'((?:[^'\\]|\\.)*)'\s*\.split\(",
        packed, re.DOTALL)
    if not m:
        m = re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{[^}]+\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)\)\)",
            packed, re.DOTALL)
    if not m:
        return packed
    payload = m.group(1).replace("\\'", "'")
    base = int(m.group(2))
    keys = m.group(4).split("|")
    lookup = {_to_base(i, base): w for i, w in enumerate(keys) if w}
    return re.sub(r"\b\w+\b", lambda mo: lookup.get(mo.group(0), mo.group(0)), payload)

def _normalise_media_text(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u002[fF]", "/", text)
    return text

def _normalise_media_url(url: str, base_url: str = "") -> str:
    url = _normalise_media_text(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if base_url and not url.startswith(("http://", "https://")):
        return urljoin(base_url, url)
    return url

def _media_type(url: str) -> str:
    return "m3u8" if ".m3u8" in url.lower() else "mp4"

def find_m3u8(text: str) -> list:
    text = _normalise_media_text(text)
    return list(dict.fromkeys(re.findall(
        r'https?://[^\s"\'\]\[<>]+\.m3u8[^\s"\'\]\[<>]*', text)))

def find_mp4(text: str) -> list:
    text = _normalise_media_text(text)
    return list(dict.fromkeys(re.findall(
        r'https?://[^\s"\'\]\[<>]+\.mp4[^\s"\'\]\[<>]*', text)))

def find_media(text: str, base_url: str = "") -> list:
    text = _normalise_media_text(text)
    found = []
    for url in find_m3u8(text) + find_mp4(text):
        found.append(_normalise_media_url(url, base_url))
    for m in re.finditer(
        r"""(?:\bfile\b|\bsrc\b)\s*(?::|=)\s*['"]([^'"]+\.(?:m3u8|mp4)[^'"]*)['"]""",
        text, re.I):
        found.append(_normalise_media_url(m.group(1), base_url))
    return list(dict.fromkeys(found))

# ── MixDrop ───────────────────────────────────────────────────────────────────
def _mixdrop_unpack(p, a, c, k):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    def base_encode(n):
        rem = n % a
        digit = chr(rem + 29) if rem > 35 else digits[rem]
        return digit if n < a else base_encode(n // a) + digit
    d = {}
    for i in range(c - 1, -1, -1):
        key = base_encode(i)
        d[key] = k[i] if i < len(k) and k[i] else key
    return re.compile(r'\b\w+\b').sub(lambda mo: d.get(mo.group(0), mo.group(0)), p)

def _mixdrop_extract_args(html):
    start = html.find("eval(function(p,a,c,k,e,d)")
    if start == -1: raise RuntimeError("MixDrop: eval(function... not found")
    i = start + len("eval(function(p,a,c,k,e,d)")
    depth = 0
    while i < len(html):
        if html[i] == '{': depth += 1
        elif html[i] == '}':
            depth -= 1
            if depth == 0: i += 1; break
        i += 1
    while i < len(html) and html[i] != '(': i += 1
    if i >= len(html): raise RuntimeError("MixDrop: arg list paren not found")
    i += 1
    arg_start = i
    depth = 1
    while i < len(html) and depth > 0:
        if html[i] == '(': depth += 1
        elif html[i] == ')': depth -= 1
        i += 1
    return html[arg_start:i - 1]

def extract_mixdrop(url):
    url = url.replace('/f/', '/e/')
    host = urlparse(url).scheme + "://" + urlparse(url).netloc
    r = _session({"Referer": host + "/"}).get(url, timeout=20)
    r.raise_for_status()
    raw_args = _mixdrop_extract_args(r.text).replace(".split('|')", "")
    data = ast.literal_eval(f"({raw_args})")
    p, a, c, k = str(data[0]), int(data[1]), int(data[2]), data[3]
    if isinstance(k, str): k = k.split('|')
    decoded = _mixdrop_unpack(p, a, c, k)
    vm = re.search(r'MDCore\.wurl\s*=\s*["\']([^"\']+)["\']', decoded)
    if not vm: raise RuntimeError("MixDrop: MDCore.wurl not found")
    video_url = vm.group(1)
    if video_url.startswith("//"): video_url = "https:" + video_url
    elif not video_url.startswith("http"): video_url = "https:" + video_url
    return {"url": video_url, "type": "mp4", "headers": {"Referer": host + "/"}}

def extract_vidmoly(url):
    r = _session({"Referer": "https://vidmoly.biz"}).get(url, timeout=20)
    r.raise_for_status()
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
    joined = "\n".join(filter(None, scripts))
    m = re.search(r'file\s*:\s*[\'"]([^\'"]+?\.m3u8[^\'"]*)[\'"]', joined)
    if not m: raise RuntimeError("Vidmoly: m3u8 not found")
    return {"url": m.group(1), "type": "m3u8", "headers": {"Referer": "https://vidmoly.biz"}}

def extract_voe(url):
    from bs4 import BeautifulSoup
    host = urlparse(url).scheme + "://" + urlparse(url).netloc + "/"
    r = _session({"Referer": host}).get(url, timeout=20)
    r.raise_for_status()
    html = r.text
    if 'Redirecting...' in html:
        new_url = re.search(r"href\s*=\s*'(.*?)';", html).group(1)
        r = _session({"Referer": host}).get(new_url, timeout=20)
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, 'html.parser')
    script_tag = soup.find('script', attrs={'type': 'application/json'})
    if not script_tag: raise RuntimeError("Voe: JSON script tag not found")
    encoded = re.search(r'\["(.*?)"\]', script_tag.string).group(1)
    data = codecs.decode(encoded, 'rot_13')
    for p in ["@$", "^^", "~@", "%?", "*~", "!!", "#&"]:
        data = re.sub(re.escape(p), "_", data)
    data = data.replace("_", "")
    data = b64decode(data).decode()
    data = ''.join(chr(ord(c) - 3) for c in data)
    data = data[::-1]
    data = b64decode(data).decode()
    parsed = json.loads(data)
    video_url = parsed.get('source') or parsed.get('hls') or parsed.get('url')
    if not video_url: raise RuntimeError("Voe: source URL not found")
    vtype = "m3u8" if ".m3u8" in video_url else "mp4"
    return {"url": video_url, "type": vtype, "headers": {"Referer": host}}

def extract_streamwish(url):
    m = re.search(r'/e/([A-Za-z0-9]+)', url)
    if not m: raise ValueError("StreamWish: cannot parse file code")
    file_code = m.group(1)
    origin = urlparse(url).netloc
    target = f"https://playnixes.com/e/{file_code}"
    r = _session({"Referer": f"https://{origin}/"}).get(target, timeout=20)
    r.raise_for_status()
    packed = re.search(
        r"(eval\(function\(p,a,c,k,e,d\)\{.*?\.split\('\|'\)[^)]*\)\))",
        r.text, re.DOTALL)
    if not packed:
        urls = find_m3u8(r.text)
        if urls: return {"url": urls[0], "type": "m3u8", "extra": urls}
        raise ValueError("StreamWish: packed JS not found")
    decoded = unpack_packer(packed.group(1))
    streams = dict(re.findall(r'"(hls[234])"\s*:\s*"([^"]+)"', decoded))
    extra = find_m3u8(decoded)
    best = streams.get("hls4") or streams.get("hls3") or streams.get("hls2") or (extra[0] if extra else None)
    if not best: raise RuntimeError("StreamWish: no stream URL found")
    return {"url": best, "type": "m3u8", "streams": streams, "extra": extra}

_ST_TERM = re.compile(r"\s*(['\"])((?:\\.|(?!\1).)*)\1\s*")
_ST_PSTR = re.compile(r"\s*\(\s*(['\"])((?:\\.|(?!\1).)*)\1\s*\)\s*")
_ST_SUBS = re.compile(r"\.substring\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\)")
_ST_PLUS = re.compile(r"\s*\+\s*")

def _st_read_term(s, i):
    for pat in (_ST_TERM, _ST_PSTR):
        mo = pat.match(s, i)
        if mo:
            lit = mo.group(2)
            j = mo.end()
            while True:
                sm = _ST_SUBS.match(s, j)
                if not sm: break
                a = int(sm.group(1))
                b = int(sm.group(2)) if sm.group(2) else None
                lit = lit[a:b] if b is not None else lit[a:]
                j = sm.end()
            return lit, j
    return None

def extract_streamta(url):
    r = _session().get(url, timeout=20)
    r.raise_for_status()
    candidates = []
    for mo in re.finditer(
        r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\.innerHTML\s*=\s*([^;]+);",
        r.text):
        stmt = mo.group(2).strip()
        parts, i, n = [], 0, len(stmt)
        ok = True
        while i < n:
            t = _st_read_term(stmt, i)
            if t is None: ok = False; break
            parts.append(t[0]); i = t[1]
            if i >= n: break
            pm = _ST_PLUS.match(stmt, i)
            if not pm: ok = False; break
            i = pm.end()
        if not ok: continue
        res = "".join(parts)
        if "/get_video?id=" not in res or "token=" not in res: continue
        if res.startswith("//"): res = "https:" + res
        elif res.startswith("/"): res = "https://streamta.site" + res
        candidates.append(res)
    if not candidates: raise RuntimeError("StreamTa: no /get_video URL deobfuscated")
    s2 = _session()
    for signed in candidates:
        r2 = s2.get(signed, headers={"Referer": url}, allow_redirects=False, timeout=20)
        if r2.status_code in (301,302,303,307,308) and "Location" in r2.headers:
            return {"url": r2.headers["Location"], "type": "mp4"}
        if r2.status_code == 200:
            return {"url": signed, "type": "mp4"}
    raise RuntimeError("StreamTa: none of the candidates worked")

def extract_streamruby(url):
    try:
        from curl_cffi import requests as cf
        r = cf.get(url, headers={"User-Agent": UA, "Referer": "https://streamruby.com/"},
                   impersonate="chrome120", timeout=30)
        html = r.text
    except Exception:
        r = _session({"Referer": "https://streamruby.com/"}).get(url, timeout=30)
        html = r.text
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    packed = next((s for s in scripts if 'eval(function(p,a,c,k' in s), None)
    if not packed: raise RuntimeError("StreamRuby: no packed JS")
    decoded = unpack_packer(packed)
    urls = find_m3u8(decoded)
    if not urls: raise RuntimeError("StreamRuby: no m3u8 found")
    best = next((u for u in urls if "master.m3u8" in u), urls[0])
    return {"url": best, "type": "m3u8", "extra": urls}

def extract_vids_st(url):
    ID_RE = re.compile(r"/e/(\d+)")
    URL_RE = re.compile(r'const\s+url\s*=\s*"([^"]+\.m3u8[^"]*)"')
    CDN = "https://cdn.vids.st/video{id}/master.m3u8"
    m = ID_RE.search(url)
    if m:
        stream = CDN.format(id=m.group(1))
        try:
            h = {"User-Agent": UA, "Referer": "https://vids.st/", "Accept": "*/*"}
            r2 = requests.get(stream, headers=h, timeout=15, stream=True)
            if r2.status_code == 200 and b"#EXTM3U" in r2.raw.read(64):
                r2.close()
                return {"url": stream, "type": "m3u8", "method": "cdn-direct"}
        except Exception:
            pass
    try:
        from curl_cffi import requests as cf
        r = cf.get(url, impersonate="chrome", timeout=20, headers={"Referer": "https://vids.st/"})
    except Exception:
        r = _session({"Referer": "https://vids.st/"}).get(url, timeout=20)
    html = r.text.replace("\\/", "/")
    mv = URL_RE.search(html)
    if not mv: raise RuntimeError("Vids.st: m3u8 not found in page")
    return {"url": mv.group(1), "type": "m3u8", "method": "page-scrape"}

def extract_savefiles(url):
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        raise RuntimeError("SaveFiles requires cloudscraper")
    m = re.search(r'/e/([a-z0-9]+)', url)
    if not m: raise ValueError("SaveFiles: cannot extract file_code")
    file_code = m.group(1)
    H = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "accept-language": "en-US,en;q=0.9", "origin": "https://savefiles.com",
         "referer": "https://savefiles.com/", "user-agent": UA, "dnt": "1"}
    scraper.get(url, headers=H)
    resp = scraper.post("https://savefiles.com/dl",
                        data=f"op=embed&file_code={file_code}&auto=1&referer=",
                        headers={**H, "content-type": "application/x-www-form-urlencoded",
                                  "referer": url}, allow_redirects=True)
    resp.raise_for_status()
    mv = re.search(r'sources:\s*\[\{file:"([^"]+\.m3u8[^"]+)"', resp.text) or \
         re.search(r'(https://[^\s"\']+\.m3u8[^\s"\']*)', resp.text)
    if not mv: raise RuntimeError("SaveFiles: no m3u8 found")
    return {"url": mv.group(1), "type": "m3u8"}

def extract_bigshare(url):
    try:
        r = _session().get(url, timeout=30)
        html = r.text
        matches = list(dict.fromkeys(re.findall(
            r"""['"](https?://[^'"]+\.(?:mp4|m3u8|mkv|webm)[^'"]*)['"']""", html, re.I)))
        if matches:
            return {"url": matches[0], "type": "m3u8" if ".m3u8" in matches[0] else "mp4", "extra": matches}
    except Exception:
        pass
    try:
        import cloudscraper
        s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        r = s.get(url, timeout=45)
        html = r.text
        matches = list(dict.fromkeys(re.findall(
            r"""['"](https?://[^'"]+\.(?:mp4|m3u8|mkv|webm)[^'"]*)['"']""", html, re.I)))
        if matches:
            return {"url": matches[0], "type": "m3u8" if ".m3u8" in matches[0] else "mp4", "extra": matches}
    except Exception:
        pass
    raise RuntimeError("BigShare: no stream URL found")

DOOD_MIRRORS = [
    "dood.watch","dood.re","dood.so","dood.la","dood.pm","dood.ws","dood.wf",
    "dood.to","dood.cx","dood.sh","dood.li","doods.pro","doods.to",
    "ds2play.com","ds2video.com","d000d.com","d0000d.com","do0od.com",
    "doodstream.com","doodstream.co","dood.yt","d-s.io","vidply.com",
    "playmogo.com",
]

def _dood_session(engine="requests"):
    if engine == "curl_cffi":
        try:
            from curl_cffi import requests as cf
            s = cf.Session(impersonate="chrome120")
            s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            return s
        except Exception:
            pass
    if engine == "cloudscraper":
        try:
            import cloudscraper
            s = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False})
            s.headers.update({"User-Agent": UA})
            return s
        except ImportError:
            pass
    return _session()

def _dood_candidate_mirrors(url):
    mirrors = []
    host = urlparse(url).netloc.lower().lstrip("www.")
    if host:
        mirrors.append(host)
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        mirrors.extend(["dood.so", "dood.la", "dood.to", "playmogo.com"])
        return list(dict.fromkeys(mirrors))
    mirrors.extend(DOOD_MIRRORS)
    return list(dict.fromkeys(mirrors))

def _dood_browser_mirrors(url):
    host = urlparse(url).netloc.lower().lstrip("www.")
    mirrors = [
        host,
        "dood.so",
        "dood.la",
        "dood.to",
        "dood.wf",
        "d0000d.com",
        "do0od.com",
        "doodstream.com",
        "dood.yt",
        "playmogo.com",
    ]
    return [m for m in dict.fromkeys(mirrors) if m]

def _dood_page_marker(html):
    html = html or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title_text = re.sub(r"\s+", " ", title.group(1)).strip() if title else "no-title"
    lowered = html.lower()
    flags = []
    for label, needle in (
        ("cf", "cloudflare"),
        ("moment", "just a moment"),
        ("denied", "access denied"),
        ("captcha", "captcha"),
        ("notfound", "file not found"),
        ("deleted", "file was deleted"),
        ("blocked", "blocked"),
        ("enable-js", "enable javascript"),
    ):
        if needle in lowered:
            flags.append(label)
    body = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html_lib.unescape(re.sub(r"\s+", " ", body)).strip()
    snippet = body[:120].replace(";", ",")
    return f"title={title_text[:60]}; flags={','.join(flags) or 'none'}; body={snippet}"

def _dood_try_mirror(session, mirror, vid, attempts=None, engine="requests"):
    url = f"https://{mirror}/e/{vid}"
    try:
        r = session.get(
            url,
            headers={
                "Referer": f"https://{mirror}/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=25,
            allow_redirects=True,
        )
    except Exception as exc:
        if attempts is not None:
            attempts.append(f"{engine}:{mirror}:ERR:{type(exc).__name__}")
        return None
    ok = r.status_code == 200 and "/pass_md5/" in r.text
    if attempts is not None:
        final_host = urlparse(getattr(r, "url", url)).netloc.lower().lstrip("www.")
        marker = "pass" if ok else "no-pass"
        attempts.append(f"{engine}:{mirror}:{r.status_code}:{final_host}:{marker}")
    if not ok:
        return None
    return r.url, r.text

def _extract_dood_from_page(session, player_url, html):
    parsed = urlparse(player_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    pm = re.search(r"\$\.get\(['\"](/pass_md5/[^'\"]+)['\"]", html)
    if not pm:
        raise RuntimeError("DoodStream: pass_md5 endpoint not in HTML")
    path = pm.group(1)
    token = path.rstrip("/").rsplit("/", 1)[-1]
    r2 = session.get(base + path, headers={"Referer": player_url,
                                             "X-Requested-With": "XMLHttpRequest"}, timeout=25)
    r2.raise_for_status()
    body = r2.text.strip()
    if body == "RELOAD" or not body.startswith("http"):
        raise RuntimeError(f"DoodStream: pass_md5 returned: {body!r}")
    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    direct = body + f"{rnd}?token={token}&expiry={int(time.time()*1000)}"
    return {"url": direct, "type": "mp4", "headers": {"Referer": player_url, "User-Agent": UA}}

def _extract_dood_with_browser(url, vid, mirrors, attempts):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as exc:
        attempts.append(f"browser:unavailable:{type(exc).__name__}")
        raise RuntimeError("Playwright is not installed")

    last_error = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=UA,
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            for mirror in mirrors:
                player_url = f"https://{mirror}/e/{vid}"
                try:
                    page.goto(player_url, wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(5000)
                    html = page.content()
                    if "/pass_md5/" not in html:
                        page.wait_for_timeout(12000)
                        html = page.content()
                    final_url = page.url
                    final_host = urlparse(final_url).netloc.lower().lstrip("www.")
                    ok = "/pass_md5/" in html
                    marker = "pass" if ok else _dood_page_marker(html)
                    attempts.append(f"browser:{mirror}:200:{final_host}:{marker}")
                    if not ok:
                        continue
                    parsed = urlparse(final_url)
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    pm = re.search(r"\$\.get\(['\"](/pass_md5/[^'\"]+)['\"]", html)
                    if not pm:
                        continue
                    path = pm.group(1)
                    token = path.rstrip("/").rsplit("/", 1)[-1]
                    body = page.evaluate(
                        """async ([path]) => {
                            const res = await fetch(path, {
                                headers: {"X-Requested-With": "XMLHttpRequest"},
                                credentials: "include"
                            });
                            return await res.text();
                        }""",
                        [path],
                    ).strip()
                    if body == "RELOAD" or not body.startswith("http"):
                        last_error = f"pass_md5 returned {body!r}"
                        continue
                    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                    direct = body + f"{rnd}?token={token}&expiry={int(time.time()*1000)}"
                    return {
                        "url": direct,
                        "type": "mp4",
                        "headers": {"Referer": final_url, "User-Agent": UA},
                        "method": "playwright",
                    }
                except PlaywrightTimeoutError as exc:
                    last_error = f"{mirror}:timeout"
                    attempts.append(f"browser:{mirror}:ERR:Timeout")
                except Exception as exc:
                    last_error = f"{mirror}:{type(exc).__name__}"
                    attempts.append(f"browser:{mirror}:ERR:{type(exc).__name__}")
        finally:
            browser.close()
    raise RuntimeError(f"browser fallback failed: {last_error or 'no playable page'}")

def extract_dood(url):
    m = re.search(r'/[ed]/([A-Za-z0-9]+)', url.strip())
    vid = m.group(1) if m else url.strip()
    session = None; player_url = None; html = None
    attempts = []
    mirrors = _dood_candidate_mirrors(url)
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        time.sleep(random.uniform(2.5, 6.0))
        engines = ("curl_cffi", "requests")
    else:
        engines = ("curl_cffi", "cloudscraper", "requests")
    for engine in engines:
        sess = _dood_session(engine)
        for mirror in mirrors:
            hit = _dood_try_mirror(sess, mirror, vid, attempts, engine)
            if hit:
                session, player_url, html = sess, hit[0], hit[1]; break
        if html: break
    if html:
        return _extract_dood_from_page(session, player_url, html)
    try:
        return _extract_dood_with_browser(url, vid, _dood_browser_mirrors(url), attempts)
    except Exception as exc:
        summary = "; ".join(attempts[-18:])
        raise RuntimeError(
            f"DoodStream: no working mirror for id={vid!r}; attempts={summary}; "
            f"browser={exc}"
        ) from exc

def extract_luluvdoo(url):
    headers = {
        "Referer": "https://luluvdoo.com/",
        "Origin": "https://luluvdoo.com",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    last_error = None
    r = None
    try:
        from curl_cffi import requests as cf
        r = cf.get(url, headers={**headers, "User-Agent": UA},
                   impersonate="chrome120", timeout=30)
        if r.status_code < 400:
            last_error = None
        else:
            last_error = RuntimeError(f"curl_cffi returned HTTP {r.status_code}")
            r = None
    except Exception as exc:
        last_error = exc
    if r is None:
        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False})
            r = scraper.get(url, headers={**headers, "User-Agent": UA}, timeout=30)
            if r.status_code >= 400:
                last_error = RuntimeError(f"cloudscraper returned HTTP {r.status_code}")
                r = None
        except Exception as exc:
            last_error = exc
    if r is None:
        r = _session(headers).get(url, timeout=20)
    try:
        r.raise_for_status()
    except Exception:
        if last_error:
            raise RuntimeError(f"Luluvdoo: request failed after browser fallbacks: {last_error}") from last_error
        raise
    packed = re.search(r"(eval\(function\(p,a,c,k,e,d\)\{.*?\.split\('\|'\)[^)]*\)\))", r.text, re.DOTALL)
    decoded = unpack_packer(packed.group(1)) if packed else r.text
    urls = find_media(decoded + "\n" + r.text, "https://luluvdoo.com/")
    if not urls: raise RuntimeError("Luluvdoo: m3u8 not found")
    best = next((u for u in urls if ".m3u8" in u.lower()), urls[0])
    return {"url": best, "type": _media_type(best), "extra": urls}

_FN_PACKER = re.compile(
    r"\}\s*\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'((?:[^'\\]|\\.)*)'\.split\('\|'\)",
    re.S)
_FN_LINKS = re.compile(r'(?:var\s+)?(?:links|sources)\s*=\s*(\{.*?\})\s*;?', re.S)
_FN_KV = re.compile(r'["\']?(hls[234])["\']?\s*:\s*["\']([^"\']+)["\']')

def _fn_decode_base(word, base):
    n = 0
    for ch in word:
        if ch.isdigit(): d = int(ch)
        elif ch.islower(): d = ord(ch) - ord('a') + 10
        elif ch.isupper(): d = ord(ch) - ord('A') + 36
        else: return None
        if d >= base: return None
        n = n * base + d
    return n

def _fn_unpack(payload):
    m = _FN_PACKER.search(payload)
    if not m: return payload
    p, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split('|')
    p = p.encode().decode('unicode_escape')
    def repl(mo):
        word = mo.group(0)
        idx = _fn_decode_base(word, a)
        if idx is not None and 0 <= idx < len(k) and k[idx]:
            return k[idx]
        return word
    return re.sub(r"\b\w+\b", repl, p)

def _fn_resolve_txt(u, referer, sess):
    u = _normalise_media_url(u, referer)
    r = sess.get(u, headers={**{"User-Agent": UA}, "Referer": referer},
                 allow_redirects=True, timeout=20)
    final = r.url; body = (r.text or "").strip()
    if final.endswith(".m3u8") or "m3u8" in final: return final
    if body.startswith("http") and ".m3u8" in body.split()[0]: return body.split()[0]
    if body.startswith("#EXTM3U"): return final
    return None

def _fn_file_code(url):
    for pat in (
        r'/(?:e|v|f|d)/([A-Za-z0-9]+)',
        r'embed-([A-Za-z0-9]+)\.html',
    ):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def _fn_fetch_embed_payload(url, origin, sess):
    file_code = _fn_file_code(url)
    if not file_code:
        return None
    headers = {
        "User-Agent": UA,
        "Referer": url,
        "Origin": origin,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    data = {"op": "embed", "file_code": file_code, "auto": "1", "referer": ""}
    r = sess.post(urljoin(origin + "/", "dl"), data=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def extract_filenoons(url):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    sess = _session()
    r = sess.get(url, headers={"Referer": origin + "/"}, timeout=20)
    r.raise_for_status()
    payloads = [r.text]
    if ('action="/dl"' in r.text or "document.forms['F1'].file_code.value" in r.text):
        try:
            posted = _fn_fetch_embed_payload(url, origin, sess)
            if posted:
                payloads.append(posted)
        except Exception:
            pass

    for payload in payloads:
        unpacked = _fn_unpack(payload)
        combined = payload + "\n" + unpacked
        block = _FN_LINKS.search(unpacked) or _FN_LINKS.search(payload)
        if block:
            links = {k: _normalise_media_url(v, origin + "/")
                     for k, v in _FN_KV.findall(block.group(1))}
            m3u8 = links.get("hls2")
            if m3u8 and (m3u8.endswith(".txt") or ".m3u8" not in m3u8.lower()):
                m3u8 = _fn_resolve_txt(m3u8, origin + "/", sess) or m3u8
            if not m3u8:
                for k in ("hls4", "hls3"):
                    if k in links:
                        resolved = _fn_resolve_txt(links[k], origin + "/", sess)
                        if resolved:
                            m3u8 = resolved
                            break
            if m3u8:
                return {"url": m3u8, "type": "m3u8", "streams": links}

        urls = find_media(combined, origin + "/")
        if urls:
            best = next((u for u in urls if ".m3u8" in u.lower()), urls[0])
            return {"url": best, "type": _media_type(best), "extra": urls}

    raise RuntimeError("FileNoons: no media URL found")

def extract_vidoza(url):
    r = _session().get(url, timeout=20)
    r.raise_for_status()
    html = r.text
    if "sourcesCode:" not in html: raise RuntimeError("Vidoza: sourcesCode not found")
    m = re.search(r'src:\s*"([^"]+)"', html)
    if not m: raise RuntimeError("Vidoza: src URL not found")
    return {"url": m.group(1), "type": "mp4"}

def extract_upzur(url):
    sess = _session({"DNT": "1"})
    sess.cookies.update({"lang": "english", "aff": "4881"})
    r = sess.get(url, timeout=15)
    r.raise_for_status()
    html = r.text
    results = []
    fid = re.search(r'embed-([a-z0-9]+)\.html', url)
    if not fid: raise ValueError("Upzur: cannot parse file ID")
    arr = re.search(r'var\s+\w+\s*=\s*(\[(?:"[^"]*",?\s*)+\])', html)
    if arr:
        chars = re.findall(r'"(\\x[0-9a-fA-F]{2}|[^"\\])"', arr.group(1))
        decoded = "".join(bytes.fromhex(c[2:]).decode() if c.startswith("\\x") else c
                          for c in reversed(chars))
        mp4 = re.search(r'src="(https://[^"]+\.mp4)"', decoded)
        if mp4: results.append(mp4.group(1))
    direct = re.findall(r'https://peanut\.upzur\.com/d/[^"\'>\s]+\.mp4', html)
    results.extend(u for u in direct if u not in results)
    if results: return {"url": results[0], "type": "mp4", "extra": results}
    raise RuntimeError("Upzur: no direct media links found")

def extract_vinovo(url):
    r = _session({"Referer": "https://vinovo.to/"}).get(url, timeout=20)
    r.raise_for_status()
    urls = find_m3u8(r.text)
    if not urls:
        packed = re.search(r"(eval\(function\(p,a,c,k,e,d\)\{.*?\.split\('\|'\)[^)]*\)\))", r.text, re.DOTALL)
        if packed:
            decoded = unpack_packer(packed.group(1))
            urls = find_m3u8(decoded)
    if not urls: raise RuntimeError("Vinovo: no m3u8 found")
    return {"url": urls[0], "type": "m3u8", "extra": urls}

def extract_generic(url):
    try:
        from curl_cffi import requests as cf
        r = cf.get(url, impersonate="chrome", timeout=25,
                   headers={"Referer": urlparse(url).scheme + "://" + urlparse(url).netloc + "/"})
        html = r.text
    except Exception:
        r = _session().get(url, timeout=20)
        html = r.text
    html = html.replace("\\/", "/")
    packed = re.search(r"(eval\(function\(p,a,c,k,e,d\)\{.*?\.split\('\|'\)[^)]*\)\))", html, re.DOTALL)
    text = html
    if packed:
        text = html + "\n" + unpack_packer(packed.group(1))
    m3us = find_m3u8(text)
    mp4s = find_mp4(text)
    combined = m3us + [u for u in mp4s if u not in m3us]
    if combined:
        return {"url": combined[0], "type": "m3u8" if combined[0] in m3us else "mp4", "extra": combined}
    raise RuntimeError("Generic: no stream URL found")

def extract_vixsrc(url):
    import base64 as _b64, urllib.parse as _up
    m = re.search(r'vixsrc\.to/(?:movie|tv)/(\d+)', url)
    if not m: raise ValueError("VixSrc: cannot parse movie/tv ID from URL")
    movie_id = m.group(1)
    _HDRS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    sess = requests.Session()
    sess.headers.update(_HDRS)
    sess.get(f"https://vixsrc.to/movie/{movie_id}",
             headers={**_HDRS, "Accept": "text/html,*/*;q=0.8"}, timeout=15)
    def _get_embed():
        r = sess.get(f"https://vixsrc.to/api/movie/{movie_id}",
                     headers={**_HDRS, "Accept": "application/json, */*",
                               "Referer": f"https://vixsrc.to/movie/{movie_id}"}, timeout=15)
        r.raise_for_status()
        raw = r.text.strip()
        try: data = json.loads(raw)
        except Exception:
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(_b64.b64decode(padded).decode())
        src = data.get("src", "")
        if src.startswith("/"): src = "https://vixsrc.to" + src
        return src
    embed_url = _get_embed()
    def _fetch_embed(src):
        return sess.get(src, headers={**_HDRS, "Accept": "text/html,*/*;q=0.8",
                                       "Referer": "https://vixsrc.to/"}, timeout=15)
    r2 = _fetch_embed(embed_url)
    if r2.status_code == 410:
        embed_url = _get_embed()
        r2 = _fetch_embed(embed_url)
    if r2.status_code not in (200, 304):
        raise RuntimeError(f"VixSrc: embed page returned {r2.status_code}")
    html = r2.text
    idx = html.find("window.masterPlaylist")
    if idx == -1: raise RuntimeError("VixSrc: window.masterPlaylist not found")
    start = html.find("{", idx)
    depth = 0; end = start
    for i, ch in enumerate(html[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: end = i; break
    block = html[start:end + 1]
    base_url_m = re.search(r"url\s*:\s*['\"]([^'\"]+)['\"]", block)
    token_m = re.search(r"['\"]token['\"]\s*:\s*['\"]([^'\"]+)['\"]", block)
    expires_m = re.search(r"['\"]expires['\"]\s*:\s*['\"]([^'\"]*)['\"]", block)
    if not base_url_m or not token_m: raise RuntimeError("VixSrc: could not parse playlist params")
    playlist_url = (f"{base_url_m.group(1)}?token={token_m.group(1)}"
                    f"&expires={expires_m.group(1) if expires_m else ''}&h=1&lang=en")
    r3 = sess.get(playlist_url, headers={**_HDRS, "Accept": "*/*",
                                          "Referer": embed_url}, timeout=15)
    r3.raise_for_status()
    if "#EXTM3U" not in r3.text: raise RuntimeError("VixSrc: playlist response is not valid M3U8")
    return {"url": playlist_url, "type": "m3u8", "headers": {"Referer": embed_url}}

def extract_gogoanime(url):
    from bs4 import BeautifulSoup
    import urllib3
    urllib3.disable_warnings()
    _HDRS_PAGE = {"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"}
    _HDRS_API = {"User-Agent": UA, "Accept": "application/json, */*; q=0.01",
                 "X-Requested-With": "XMLHttpRequest"}
    r = requests.get(url, headers=_HDRS_PAGE, timeout=15, verify=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    iframe = soup.find("iframe", src=True)
    if not iframe: raise RuntimeError("GogoAnime: no iframe found")
    stream_url = iframe["src"]
    parsed_mp = urlparse(stream_url)
    base = f"{parsed_mp.scheme}://{parsed_mp.netloc}"
    clean = stream_url.split("?")[0]
    r2 = requests.get(clean, headers={**_HDRS_PAGE, "Referer": url}, timeout=15, verify=False)
    fid = re.search(r'<title>File\s+(\d+)', r2.text, re.IGNORECASE) or \
          re.search(r'getSources\?id=(\d+)', r2.text)
    if not fid: raise RuntimeError("GogoAnime: cannot find file ID")
    file_id = fid.group(1)
    api_url = f"{base}/stream/getSources?id={file_id}&id={file_id}"
    r3 = requests.get(api_url, headers={**_HDRS_API, "Referer": stream_url, "Origin": base},
                      timeout=15, verify=False)
    r3.raise_for_status()
    data = r3.json()
    m3u8_urls = []
    sources = data.get("sources", {})
    if isinstance(sources, dict):
        f = sources.get("file", "")
        if f and ".m3u8" in f: m3u8_urls.append(f)
    elif isinstance(sources, list):
        for s in sources:
            f = s.get("file", "")
            if f and ".m3u8" in f: m3u8_urls.append(f)
    if "file" in data and ".m3u8" in str(data["file"]): m3u8_urls.append(data["file"])
    m3u8_urls = list(dict.fromkeys(m3u8_urls))
    if not m3u8_urls: raise RuntimeError(f"GogoAnime: no m3u8 in getSources response")
    return {"url": m3u8_urls[0], "type": "m3u8", "extra": m3u8_urls,
            "headers": {"Referer": base + "/"}}

def extract_streamimdb(url):
    sess = _session({"Accept": "text/html,*/*;q=0.8"})
    r = sess.get(url, timeout=20)
    r.raise_for_status()
    rcp_m = (re.search(r'(https://cloudnestra\.com/rcp/[^\s"\'<]+)', r.text) or
             re.search(r'["\']?(/rcp/[A-Za-z0-9+/=_\-]+)', r.text))
    if not rcp_m: raise RuntimeError("StreamIMDB: cloudnestra /rcp/ URL not found")
    rcp_url = rcp_m.group(1)
    if rcp_url.startswith("/"): rcp_url = "https://cloudnestra.com" + rcp_url
    r2 = sess.get(rcp_url, headers={"Referer": url}, timeout=20)
    r2.raise_for_status()
    prorcp_m = (re.search(r"""src:\s*['"]?(/prorcp/[A-Za-z0-9+/=_\-]+)""", r2.text) or
                re.search(r"""(/prorcp/[A-Za-z0-9+/=_\-]{20,})""", r2.text))
    if not prorcp_m: raise RuntimeError("StreamIMDB: prorcp path not found")
    prorcp_url = "https://cloudnestra.com" + prorcp_m.group(1)
    r3 = sess.get(prorcp_url, headers={"Referer": "https://cloudnestra.com/"}, timeout=20)
    r3.raise_for_status()
    html3 = r3.text
    m3u8_url = None
    m = re.search(r'file:\s*["\']?(https://app2\.\{v5\}/cdnstr/[^"\'>\s]+\.m3u8)', html3)
    if m: m3u8_url = m.group(1).replace("{v5}", "putgate.org")
    if not m3u8_url:
        m = re.search(r'(https://app2\.putgate\.org/cdnstr/[^"\'>\s]+\.m3u8)', html3)
        if m: m3u8_url = m.group(1)
    if not m3u8_url:
        m = re.search(r'(https?://[^\s"\'<]+\.m3u8)', html3)
        if m: m3u8_url = m.group(1)
    if not m3u8_url: raise RuntimeError("StreamIMDB: m3u8 URL not found in prorcp page")
    return {"url": m3u8_url, "type": "m3u8", "headers": {"Referer": "https://cloudnestra.com/"}}

HOST_MAP = {
    "mixdrop":    ["mixdrop"],
    "vidmoly":    ["vidmoly"],
    "voe":        ["voe.sx", "kellywhatcould", "jilliandescribecompany"],
    "streamwish": ["streamwish", "playnixes"],
    "streamta":   ["streamta.site"],
    "streamruby": ["streamruby.com"],
    "vids_st":    ["vids.st"],
    "savefiles":  ["savefiles.com"],
    "bigshare":   ["bigshare.io"],
    "dood":       ["dood.", "doods.", "ds2play", "ds2video", "d000d", "d-s.io", "vidply", "playmogo"],
    "luluvdoo":   ["luluvdoo.com"],
    "filenoons":  ["filenoons", "earnvideo", "filelions", "vdhide", "callistanise",
                   "vidnest", "bysejikuar", "vidara"],
    "vidoza":     ["vidoza", "videzz"],
    "upzur":      ["upzur.com"],
    "vinovo":     ["vinovo.to"],
    "streamplay": ["streamplay.to"],
    "streamtape": ["streamtape"],
    "vixsrc":     ["vixsrc.to"],
    "gogoanime":  ["gogoanime", "megaplay"],
    "streamimdb": ["streamimdb", "cloudnestra"],
}

EXTRACTOR_MAP = {
    "mixdrop":    extract_mixdrop,
    "vidmoly":    extract_vidmoly,
    "voe":        extract_voe,
    "streamwish": extract_streamwish,
    "streamta":   extract_streamta,
    "streamruby": extract_streamruby,
    "vids_st":    extract_vids_st,
    "savefiles":  extract_savefiles,
    "bigshare":   extract_bigshare,
    "dood":       extract_dood,
    "luluvdoo":   extract_luluvdoo,
    "filenoons":  extract_filenoons,
    "vidoza":     extract_vidoza,
    "upzur":      extract_upzur,
    "vinovo":     extract_vinovo,
    "streamplay": extract_filenoons,
    "streamtape": lambda u: (_ for _ in ()).throw(NotImplementedError("StreamTape not yet implemented")),
    "vixsrc":     extract_vixsrc,
    "gogoanime":  extract_gogoanime,
    "streamimdb": extract_streamimdb,
    "generic":    extract_generic,
}

def extract_stream(url):
    host = detect_host(url)
    fn = EXTRACTOR_MAP.get(host, extract_generic)
    result = fn(url)
    result["host"] = host
    result["input_url"] = url
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: extract.py <input_urls.txt> <output.json> <run_id>")
        sys.exit(1)

    input_file  = sys.argv[1]
    output_file = sys.argv[2]
    run_id      = sys.argv[3]

    with open(input_file, encoding="utf-8-sig") as f:
        urls = [line.strip() for line in f if line.strip().startswith("http")]

    print(f"[extract.py] run_id={run_id}  processing {len(urls)} URL(s)")

    items = []
    for url in urls:
        print(f"  → {url}")
        try:
            result = extract_stream(url)
            # Sanitise non-serialisable values
            result = {k: v for k, v in result.items()
                      if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
            item = {"status": "ok", **result}
        except Exception as e:
            item = {
                "status":    "error",
                "input_url": url,
                "error":     str(e),
                "traceback": traceback.format_exc(),
            }
        items.append(item)
        print(f"     {item['status'].upper()}: {item.get('url') or item.get('error','')[:80]}")

    output = {
        "run_id":    run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total":     len(items),
        "ok":        sum(1 for i in items if i["status"] == "ok"),
        "failed":    sum(1 for i in items if i["status"] == "error"),
        "results":   items,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"[extract.py] wrote {output_file}  ({output['ok']}/{output['total']} ok)")


if __name__ == "__main__":
    main()
