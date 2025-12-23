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

def find_repo_root(start: Path) -> Path:
    p = start
    for _ in range(12):
        if (p / ".git").exists():
            return p
        p = p.parent
    ws = os.environ.get("GITHUB_WORKSPACE")
    if ws:
        return Path(ws).resolve()
    return start

ROOT_DIR = find_repo_root(SCRIPT_DIR)

SITEMAP_URL = "https://www.express-servis.cz/sitemap.xml"

# Split output (NEW)
I18N_DIR = ROOT_DIR / "i18n"
PAGES_DIR = I18N_DIR / "pages"
INDEX_JSON = I18N_DIR / "index.json"
GLOBAL_JSON = I18N_DIR / "global.json"

# (Optional) legacy monolith for backward compatibility
LEGACY_DB = I18N_DIR / "i18n_pages_db.json"
WRITE_LEGACY_DB = True

TARGET_LANGS = ["sk", "en", "de"]
DEFAULT_LANG = "cs"

# ✅ testovací adresy (jak chceš)
TEST_URLS = [
    "https://www.express-servis.cz",
    "https://www.express-servis.cz/p/apple-iphone-17-pro-max-256gb-kosmicky-oranzovy",
    "https://www.express-servis.cz/servis-oprava/iphone-15-pro-plzen/cenik",
    "https://www.express-servis.cz/navody-a-clanky/ktere-servisy-jsou-nejdoporucovanejsi-pro-opravu-mobilnich-telefonu-v-ceske-republice",
    "https://www.express-servis.cz/kontakt-es",
]
TEST_ONLY = True

# Z jaké stránky brát GLOBAL (menu/footer) – typicky homepage
GLOBAL_SOURCE_URL = "https://www.express-servis.cz"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,sk;q=0.8,de;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}
REQUEST_TIMEOUT = 30

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

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def normalize_spaces(s: str) -> str:
    # sjednocení whitespace, NBSP ošetříme v JS
    return re.sub(r"\s+", " ", (s or "")).strip()

_units_re = re.compile(r"^\s*[\d\.,]+\s*(gb|mb|tb|mah|w|kw|v|a|mm|cm|m|kg|g|hz|khz|mhz|ghz|°c|dpi|%|x)\s*$", re.I)
_code_like_re = re.compile(r"^[A-Z0-9][A-Z0-9\-_./+ ]{1,}$")
_only_symbols_digits_re = re.compile(r"^[\d\s\W_]+$")
_urlish_re = re.compile(r"https?://|www\.", re.I)

_email_re = re.compile(r"\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b", re.I)
_phone_re = re.compile(r"(\+?\d[\d\s()\-]{7,}\d)")
_postal_re = re.compile(r"\b\d{3}\s?\d{2}\b")
_street_num_re = re.compile(r"\b[^\d,]{3,}\s+\d{1,5}(?:/\d{1,5})?(?:\b|,)", re.U)

def looks_like_contact_or_address(t: str) -> bool:
    t = normalize_spaces(t)
    if not t:
        return False
    if _email_re.search(t):
        return True
    if _phone_re.search(t):
        return True
    if _postal_re.search(t):
        return True
    if _street_num_re.search(t):
        return True
    return False

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
    if looks_like_contact_or_address(t):
        return False
    if _code_like_re.match(t) and not any(ch.islower() for ch in t):
        return False
    return True

def normalize_url(url: str) -> str:
    return (url or "").split("#")[0].strip().rstrip("/")

def page_id(url: str) -> str:
    # krátké, stabilní ID pro soubory
    return sha(normalize_url(url))[:12]


# ----------------------------
# Translation cache (texts)
# ----------------------------
def load_texts_cache() -> Dict[str, Any]:
    # Pro jednoduchost držíme cache v legacy DB souboru, aby se nepřekládalo znovu.
    # (Můžeš to později přesunout do zvláštního texts.json)
    ensure_parent_dir(LEGACY_DB)
    if not LEGACY_DB.exists():
        return {"texts": {}}
    raw = json.loads(LEGACY_DB.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"texts": {}}
    raw.setdefault("texts", {})
    return raw

def save_texts_cache(cache: Dict[str, Any]) -> None:
    # zachováme i pages/global pokud bys chtěl – tady ukládáme jen texts + případně legacy
    ensure_parent_dir(LEGACY_DB)
    LEGACY_DB.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def short_lang_prompt(lang: str) -> str:
    if lang == "sk":
        return (
            "Prelož z češtiny do modernej slovenčiny. "
            "Zachovaj technické termíny, značky, modely, kódy. "
            "Nemeň jednotky, čísla a skratky. "
            "Vráť iba preložený text."
        )
    if lang == "en":
        return (
            "Translate from Czech to natural, professional English for a website. "
            "Keep technical terms, brands, model names, codes. "
            "Do not change numbers, units, abbreviations. "
            "Return only the translated text."
        )
    if lang == "de":
        return (
            "Übersetze aus dem Tschechischen ins natürliche, professionelle Deutsch für eine Website. "
            "Behalte Fachbegriffe, Marken, Modellnamen und Codes unverändert. "
            "Ändere keine Zahlen, Einheiten oder Abkürzungen. "
            "Gib nur den übersetzten Text zurück."
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

def translate_cached(text: str, lang: str, cache: Dict[str, Any]) -> str:
    t = normalize_spaces(text)
    if not t or not is_translatable(t):
        return t

    key = sha(t)
    texts = cache.setdefault("texts", {})
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
# Fetch
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

    # robustní selektor pro odkazy
    if el.name == "a":
        href = (el.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            href = href.replace('"', '\\"')
            return f'a[href="{href}"]'

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
                "textIndex": 0,
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

def extract_textnodes_from_root(root, parent_selector: str = "", parent_id: str = "") -> List[Dict[str, Any]]:
    if not root:
        return []

    skip_parents = {"script", "style", "noscript", "svg", "head", "title", "meta", "link"}

    elements_order: Dict[Tuple[str, str], List[int]] = {}
    element_index_map: Dict[Tuple[str, str, int], int] = {}
    text_index_counter: Dict[Tuple[str, str, int], int] = {}

    nodes: List[Dict[str, Any]] = []
    root_marker = parent_id or parent_selector or "document"

    for node in root.descendants:
        if not isinstance(node, NavigableString):
            continue

        txt = normalize_spaces(str(node))
        if not is_translatable(txt):
            continue

        parent = node.parent
        if not parent or not getattr(parent, "name", None):
            continue

        # nebrat texty z mailto/tel odkazů
        if parent.name == "a":
            href = (parent.get("href") or "").strip().lower()
            if href.startswith("mailto:") or href.startswith("tel:"):
                continue

        if parent.name.lower() in skip_parents:
            continue
        if len(txt) > 900:
            continue

        sel = build_selector(parent)
        if not sel:
            continue

        group_key = (root_marker, sel)
        el_id = id(parent)

        if (root_marker, sel, el_id) not in element_index_map:
            order = elements_order.setdefault(group_key, [])
            element_index_map[(root_marker, sel, el_id)] = len(order)
            order.append(el_id)

        element_index = element_index_map[(root_marker, sel, el_id)]

        ti_key = (root_marker, sel, el_id)
        text_index = text_index_counter.get(ti_key, 0)
        text_index_counter[ti_key] = text_index + 1

        nodes.append({
            "mode": "textnode",
            "attr": "",
            "parent": parent_selector or "",
            "parentId": parent_id or "",
            "selector": sel,
            "index": element_index,
            "textIndex": text_index,
            "source": txt,
        })

    return nodes

def make_node_key(scope_id: str, n: Dict[str, Any]) -> str:
    # scope_id = pageId nebo "global"
    ident = f"{scope_id}|{n.get('mode')}|{n.get('attr')}|{n.get('parentId')}|{n.get('parent')}|{n.get('selector')}|{n.get('index')}|{n.get('textIndex')}"
    ident_h = sha(ident)[:10]
    src_h = sha(n.get("source",""))[:10]
    return f"{scope_id}.{ident_h}.{src_h}"

def build_nodes_with_translations(nodes_raw: List[Dict[str, Any]], cache: Dict[str, Any], scope_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in nodes_raw:
        src = n["source"]
        dst_map: Dict[str, str] = {}
        for lang in TARGET_LANGS:
            if lang != DEFAULT_LANG:
                dst_map[lang] = translate_cached(src, lang, cache)

        out.append({
            "key": make_node_key(scope_id, n),
            "parentId": n.get("parentId") or "",
            "parent": n.get("parent") or "",
            "selector": n.get("selector") or "",
            "index": int(n.get("index") or 0),
            "textIndex": int(n.get("textIndex") or 0),
            "mode": (n.get("mode") or "textnode"),
            "attr": n.get("attr") or "",
            "source": src,
            "dst": dst_map,
        })
    return out


# ----------------------------
# Build GLOBAL and PAGES
# ----------------------------
def extract_global_nodes_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")

    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()

    nodes: List[Dict[str, Any]] = []

    # menu / navigace
    nav_root = soup.select_one(".component--core-navigation")
    nodes += extract_textnodes_from_root(nav_root, parent_selector=".component--core-navigation", parent_id="")

    # submenu
    submenu_root = soup.select_one(".submenu")
    nodes += extract_textnodes_from_root(submenu_root, parent_selector=".submenu", parent_id="")

    # footer
    footer_root = soup.select_one(".component--core-footer")
    nodes += extract_textnodes_from_root(footer_root, parent_selector=".component--core-footer", parent_id="")

    return nodes

def extract_page_nodes_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")

    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()

    nodes: List[Dict[str, Any]] = []

    # title (volitelné – můžeš vyhodit, pokud nechceš)
    nodes += extract_head_nodes(soup)

    # pouze hlavní obsah
    content_root = pick_content_root(soup)
    content_pid = ""
    if content_root is not None:
        content_pid = nearest_parent_id(content_root) or ""
    nodes += extract_textnodes_from_root(content_root, parent_selector="", parent_id=content_pid)

    return nodes


def main():
    if "OPENAI_API_KEY" not in os.environ:
        raise RuntimeError("Chybí OPENAI_API_KEY")

    ensure_dir(I18N_DIR)
    ensure_dir(PAGES_DIR)

    print("ROOT_DIR:", ROOT_DIR.resolve())
    print("I18N_DIR:", I18N_DIR.resolve())

    cache = load_texts_cache()

    # URLs
    if TEST_ONLY:
        urls = [normalize_url(u) for u in TEST_URLS]
    else:
        urls = fetch_sitemap_urls(SITEMAP_URL)
        urls = [normalize_url(u) for u in urls if u]

    # 1) GLOBAL (menu+footer jednou)
    print("Building GLOBAL from:", GLOBAL_SOURCE_URL)
    global_html = fetch_url_with_retry(GLOBAL_SOURCE_URL)
    global_nodes_raw = extract_global_nodes_from_html(global_html)
    global_nodes = build_nodes_with_translations(global_nodes_raw, cache, scope_id="global")

    global_payload = {
        "hash": sha("||".join([f"{n['key']}|{n['selector']}|{n['index']}|{n['textIndex']}|{n['source']}" for n in global_nodes])),
        "updated_at": int(time.time()),
        "nodes": global_nodes
    }
    GLOBAL_JSON.write_text(json.dumps(global_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved GLOBAL: {GLOBAL_JSON} nodes={len(global_nodes)}")

    # 2) PAGES
    index_payload: Dict[str, Any] = {
        "updated_at": int(time.time()),
        "pages": {}  # url -> pageId
    }

    legacy_db: Dict[str, Any] = {"texts": cache.get("texts", {}), "pages": {}, "global": global_payload}

    for url in urls:
        pid = page_id(url)
        print(f"Processing page: {url} -> {pid}")

        html = fetch_url_with_retry(url)
        page_nodes_raw = extract_page_nodes_from_html(html)
        page_nodes = build_nodes_with_translations(page_nodes_raw, cache, scope_id=pid)

        page_payload = {
            "id": pid,
            "url": url,
            "updated_at": int(time.time()),
            "nodes": page_nodes
        }

        # write split file
        page_file = PAGES_DIR / f"{pid}.json"
        page_file.write_text(json.dumps(page_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # index mapping
        index_payload["pages"][url] = pid

        # legacy (optional)
        legacy_db["pages"][url] = {
            "hash": sha("||".join([f"{n['key']}|{n['selector']}|{n['index']}|{n['textIndex']}|{n['source']}" for n in page_nodes])),
            "updated_at": int(time.time()),
            "nodes": page_nodes
        }

        print(f"  saved {len(page_nodes)} nodes -> {page_file}")

        time.sleep(1)

    INDEX_JSON.write_text(json.dumps(index_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved INDEX: {INDEX_JSON} pages={len(index_payload['pages'])}")

    # save cache (texts)
    cache_out = {"texts": cache.get("texts", {})}
    if WRITE_LEGACY_DB:
        # legacy includes texts + global + pages (pro jistotu / kompatibilitu)
        LEGACY_DB.write_text(json.dumps(legacy_db, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved LEGACY DB: {LEGACY_DB}")

    else:
        save_texts_cache(cache_out)

if __name__ == "__main__":
    main()
