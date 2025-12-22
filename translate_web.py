import os, re, json, time, hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString
from openai import OpenAI

client = OpenAI()

# ----------------------------
# Paths / config
# ----------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

SITEMAP_URL = "https://www.express-servis.cz/sitemap.xml"

# ✅ ukládat do i18n/i18n_pages_db.json
TRANSLATION_DB = ROOT_DIR / "i18n" / "i18n_pages_db.json"

TARGET_LANGS = ["sk"]
DEFAULT_LANG = "cs"

# ✅ test zatím jen výkup
TEST_ONLY_URL = "https://www.express-servis.cz/vykup-zarizeni"
TEST_ONLY = True

# ✅ upravené hlavičky – jako běžný prohlížeč
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,sk;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 30

# Úspora tokenů / kvalita
MAX_TEXT_LEN_TO_TRANSLATE = 320
SKIP_IF_CONTAINS_URL = True
DEBUG = True


# ----------------------------
# Helpers
# ----------------------------
def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def load_db() -> Dict[str, Any]:
    ensure_parent_dir(TRANSLATION_DB)
    if TRANSLATION_DB.exists():
        db = json.loads(TRANSLATION_DB.read_text(encoding="utf-8"))
        db.setdefault("texts", {})
        db.setdefault("pages", {})
        return db
    return {"texts": {}, "pages": {}}

def save_db(db: Dict[str, Any]) -> None:
    ensure_parent_dir(TRANSLATION_DB)
    TRANSLATION_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

_units_re = re.compile(r"^\s*[\d\.,]+\s*(gb|mb|tb|mah|w|kw|v|a|mm|cm|m|kg|g|hz|khz|mhz|ghz|°c|dpi|%|x)\s*$", re.I)
_code_like_re = re.compile(r"^[A-Z0-9][A-Z0-9\-_./+ ]{1,}$")
_only_symbols_digits_re = re.compile(r"^[\d\s\W_]+$")
_urlish_re = re.compile(r"https?://|www\.", re.I)

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def is_translatable(text: str) -> bool:
    t = normalize_spaces(text)
    if not t:
        return False
    if len(t) <= 2:
        return False
    if _only_symbols_digits_re.match(t):
        return False
    if _units_re.match(t):
        return False
    if SKIP_IF_CONTAINS_URL and _urlish_re.search(t):
        return False
    if _code_like_re.match(t) and not any(ch.islower() for ch in t):
        return False
    return True

def short_lang_prompt(lang: str) -> str:
    if lang == "sk":
        return (
            "Prelož z češtiny do modernej slovenčiny. "
            "Zachovaj technické termíny, značky, modely, kódy. "
            "Nemeň jednotky, čísla a skratky. "
            "Vráť iba preložený text."
        )
    return f"Translate Czech to {lang}. Keep brands/models/codes, keep units/numbers. Return only translation."

def translate_text(text: str, lang: str, max_retries: int = 3) -> str:
    text = normalize_spaces(text)
    if not text:
        return text

    if len(text) > MAX_TEXT_LEN_TO_TRANSLATE:
        return text

    prompt = short_lang_prompt(lang) + "\n\n" + text

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=900,
            )
            return normalize_spaces(resp.choices[0].message.content or "")
        except Exception as e:
            last_err = e
            time.sleep(1.2 * attempt)
    raise RuntimeError(f"Translate failed: {last_err}")

def translate_cached(text: str, lang: str, db: Dict[str, Any]) -> str:
    t = normalize_spaces(text)
    if not t or not is_translatable(t):
        return t

    key = sha(t)
    texts = db.setdefault("texts", {})
    entry = texts.get(key)

    if entry and entry.get("src") == t and entry.get("dst", {}).get(lang):
        return entry["dst"][lang]

    dst = translate_text(t, lang)

    if not entry:
        entry = {"src": t, "dst": {}, "meta": {}}
    entry["src"] = t
    entry.setdefault("dst", {})[lang] = dst
    entry.setdefault("meta", {})["updated_at"] = int(time.time())

    texts[key] = entry
    return dst


# ----------------------------
# Fetch with retry (429 fix)
# ----------------------------
def fetch_url_with_retry(url: str, retries: int = 5, delay: float = 5.0) -> str:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            if r.status_code == 200:
                return r.text
            elif r.status_code == 429:
                print(f"  429 Too Many Requests, retry {i+1}/{retries} after {delay*(i+1)}s")
                time.sleep(delay * (i + 1))
            else:
                r.raise_for_status()
        except Exception as e:
            last_err = e
            print(f"  fetch error {i+1}/{retries}: {e}")
            time.sleep(delay * (i + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")

def fetch_sitemap_urls(sitemap_url: str) -> List[str]:
    xml = requests.get(sitemap_url, timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS).text
    soup = BeautifulSoup(xml, "xml")
    return [loc.text.strip() for loc in soup.find_all("loc") if loc.text]

def normalize_url(url: str) -> str:
    return (url or "").split("#")[0].strip().rstrip("/")


# ----------------------------
# Selector building
# ----------------------------
_GENERIC_CLASSES = {
    "container","row","col","text","text-center","text-left","text-right",
    "btn","button","link","nav","menu","item","active","clearfix"
}

def build_selector(el) -> str:
    if not el or not getattr(el, "name", None):
        return ""
    if el.get("id"):
        return f"{el.name}#{el.get('id')}"
    tag = el.name
    classes = [c for c in (el.get("class") or []) if c and not c.startswith("js-")]
    classes = [c for c in classes if c not in _GENERIC_CLASSES][:2]
    if classes:
        return tag + "." + ".".join(classes)
    return tag

def nearest_parent_id(el) -> Optional[str]:
    cur = el
    while cur is not None and getattr(cur, "name", None):
        if cur.get("id") == "snippet--content":
            return "snippet--content"
        cur = cur.parent
    cur = el
    while cur is not None and getattr(cur, "name", None):
        if cur.get("id"):
            return cur.get("id")
        cur = cur.parent
    return None


# ----------------------------
# Extraction
# ----------------------------
def strip_global_layout(soup: BeautifulSoup) -> None:
    for bad in soup.select("header, footer, nav, aside"):
        bad.decompose()

def extract_head_nodes(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    title_el = soup.select_one("title#snippet--title") or soup.select_one("head > title")
    if title_el:
        src = normalize_spaces(title_el.get_text(" ", strip=True))
        if is_translatable(src):
            nodes.append({
                "mode": "text",
                "attr": "",
                "parent": "head",
                "parentId": "",
                "selector": "title#snippet--title" if title_el.get("id") == "snippet--title" else "head > title",
                "index": 0,
                "source": src,
            })
    return nodes

def pick_content_root(soup: BeautifulSoup):
    return (
        soup.select_one("#snippet--content")
        or soup.select_one("main")
        or soup.select_one("article")
        or soup.body
        or soup
    )

def extract_body_text_nodes(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()

    strip_global_layout(soup)
    content_root = pick_content_root(soup)

    buckets: Dict[Tuple[str, str], List[str]] = {}
    skip_parents = {"script", "style", "noscript", "svg", "head", "title", "meta", "link"}

    for node in content_root.descendants:
        if not isinstance(node, NavigableString):
            continue

        txt = normalize_spaces(str(node))
        if not is_translatable(txt):
            continue

        parent = node.parent
        if not parent or not getattr(parent, "name", None):
            continue
        if parent.name.lower() in skip_parents:
            continue

        if len(txt) > 900:
            continue

        sel = build_selector(parent)
        if not sel:
            continue

        pid = nearest_parent_id(parent) or ""
        buckets.setdefault((pid, sel), []).append(txt)

    nodes: List[Dict[str, Any]] = []
    for (pid, sel), texts in buckets.items():
        for idx, txt in enumerate(texts):
            nodes.append({
                "mode": "text",
                "attr": "",
                "parent": "",
                "parentId": pid,
                "selector": sel,
                "index": idx,
                "source": txt,
            })
    return nodes

def extract_nodes_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    return extract_head_nodes(soup) + extract_body_text_nodes(soup)


def page_hash(nodes: List[Dict[str, Any]]) -> str:
    payload = "||".join([
        f"{n.get('mode')}|{n.get('attr')}|{n.get('parentId')}|{n.get('parent')}|"
        f"{n.get('selector')}|{n.get('index')}|{n.get('source')}"
        for n in nodes
    ])
    return sha(payload)

def make_node_key(url: str, n: Dict[str, Any]) -> str:
    u = sha(normalize_url(url))[:8]
    ident = f"{n.get('mode')}|{n.get('attr')}|{n.get('parentId')}|{n.get('parent')}|{n.get('selector')}|{n.get('index')}"
    ident_h = sha(ident)[:8]
    src_h = sha(n.get("source",""))[:10]
    return f"p.{u}.{ident_h}.{src_h}"


# ----------------------------
# Main
# ----------------------------
def main():
    if "OPENAI_API_KEY" not in os.environ:
        raise RuntimeError("Chybí OPENAI_API_KEY")

    db = load_db()
    urls = fetch_sitemap_urls(SITEMAP_URL)
    urls = [normalize_url(u) for u in urls if u]

    if TEST_ONLY:
        urls = [u for u in urls if normalize_url(u) == normalize_url(TEST_ONLY_URL)]
        if not urls:
            urls = [normalize_url(TEST_ONLY_URL)]

    pages = db.setdefault("pages", {})

    for url in urls:
        print(f"Processing {url}")

        html = fetch_url_with_retry(url)

        nodes_raw = extract_nodes_from_html(html)
        h = page_hash(nodes_raw)

        prev = pages.get(url)
        if prev and prev.get("hash") == h:
            print("  unchanged, skip")
            continue

        out_nodes: List[Dict[str, Any]] = []
        for n in nodes_raw:
            src = n["source"]
            dst_map: Dict[str, str] = {}
            for lang in TARGET_LANGS:
                if lang != DEFAULT_LANG:
                    dst_map[lang] = translate_cached(src, lang, db)

            out_nodes.append({
                "key": make_node_key(url, n),
                "parentId": n.get("parentId") or "",
                "parent": n.get("parent") or "",
                "selector": n.get("selector") or "",
                "index": int(n.get("index") or 0),
                "mode": n.get("mode") or "text",
                "attr": n.get("attr") or "",
                "source": src,
                "dst": dst_map,
            })

        pages[url] = {
            "hash": h,
            "updated_at": int(time.time()),
            "nodes": out_nodes
        }

        save_db(db)
        print(f"  saved {len(out_nodes)} nodes -> {TRANSLATION_DB}")
        print("DB path:", TRANSLATION_DB.resolve())
        print("Nodes saved for page:", len(out_nodes))
        print("Texts dictionary size:", len(db.get("texts", {})))

        time.sleep(3)

if __name__ == "__main__":
    main()
