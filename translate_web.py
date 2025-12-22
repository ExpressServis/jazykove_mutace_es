import os, re, json, time, hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup, NavigableString
from openai import OpenAI

client = OpenAI()

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

SITEMAP_URL = "https://www.express-servis.cz/sitemap.xml"
TRANSLATION_DB = ROOT_DIR / "i18n_pages_db.json"

TARGET_LANG = "sk"   # pak můžeš rozšířit na ["sk","en"]
DEFAULT_LANG = "cs"

def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def load_db() -> Dict[str, Any]:
    if TRANSLATION_DB.exists():
        db = json.loads(TRANSLATION_DB.read_text(encoding="utf-8"))
        db.setdefault("texts", {})
        db.setdefault("pages", {})
        return db
    return {"texts": {}, "pages": {}}

def save_db(db: Dict[str, Any]) -> None:
    TRANSLATION_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

_units_re = re.compile(r"^\s*[\d\.,]+\s*(gb|mb|tb|mah|w|kw|v|a|mm|cm|m|kg|g|hz|khz|mhz|ghz|°c|dpi|%|x)\s*$", re.I)
_code_like_re = re.compile(r"^[A-Z0-9][A-Z0-9\-_./+ ]{1,}$")
_only_symbols_digits_re = re.compile(r"^[\d\s\W_]+$")

def is_translatable(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _only_symbols_digits_re.match(t):
        return False
    if _units_re.match(t):
        return False
    if len(t) <= 2:
        return False
    if _code_like_re.match(t) and not any(ch.islower() for ch in t):
        return False
    return True

def translate_text(text: str, lang: str, max_retries: int = 3) -> str:
    text = (text or "").strip()
    if not text:
        return text

    if lang == "sk":
        prompt = (
            "Prelož nasledujúci text z češtiny do modernej slovenčiny. "
            "Zachovaj technické termíny, značky, modely a kódy. "
            "Nemeň jednotky, čísla a skratky. "
            "Vráť iba preložený text:\n\n" + text
        )
    else:
        prompt = f"Translate Czech to {lang}. Keep brands/models/codes, keep units/numbers. Return only translation:\n\n{text}"

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=800,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Translate failed: {last_err}")

def translate_cached(text: str, lang: str, db: Dict[str, Any]) -> str:
    t = (text or "").strip()
    if not t or not is_translatable(t):
        return t

    key = sha(t)
    texts = db.setdefault("texts", {})
    entry = texts.get(key)

    if entry and entry.get("src") == t and entry.get("dst", {}).get(lang):
        return entry["dst"][lang]

    dst = translate_text(t, lang)
    if not entry:
        entry = {"src": t, "dst": {}}
    entry["src"] = t
    entry.setdefault("dst", {})[lang] = dst
    texts[key] = entry
    return dst

def fetch_sitemap_urls(sitemap_url: str) -> List[str]:
    xml = requests.get(sitemap_url, timeout=30).text
    soup = BeautifulSoup(xml, "xml")
    return [loc.text.strip() for loc in soup.find_all("loc") if loc.text]

def normalize_url(url: str) -> str:
    # vyhoď # a typicky i UTM parametry (dle potřeby)
    return url.split("#")[0].strip()

def build_selector(el) -> str:
    """
    Jednoduchý, relativně stabilní selektor:
    - preferuje id
    - jinak tag + 1-2 class (bez generických)
    """
    if not el or not getattr(el, "name", None):
        return ""
    if el.get("id"):
        return f"#{el.get('id')}"
    tag = el.name
    classes = [c for c in (el.get("class") or []) if c and not c.startswith("js-")]
    classes = classes[:2]
    if classes:
        return tag + "." + ".".join(classes)
    return tag

def extract_nodes(html: str) -> List[Tuple[str, str, int, str]]:
    """
    Vrací list: (parent_selector, selector, index, sourceText)
    parent_selector zatím prázdné = globální dokument; můžeš doplnit heuristiky.
    """
    soup = BeautifulSoup(html, "lxml")

    # vyhoď script/style
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()

    nodes_out: List[Tuple[str, str, int, str]] = []

    # Ber jen elementy, které typicky obsahují text
    candidates = soup.find_all(["h1","h2","h3","p","a","button","label","span","li"])

    # Skupiny podle selectoru → kvůli indexům (slider, listy, …)
    buckets: Dict[str, List[Tuple[Any, str]]] = {}

    for el in candidates:
        text = el.get_text(" ", strip=True)
        if not is_translatable(text):
            continue

        sel = build_selector(el)
        if not sel:
            continue

        buckets.setdefault(sel, []).append((el, text))

    # Vygeneruj indexy v rámci stejného selektoru
    for sel, arr in buckets.items():
        for idx, (el, text) in enumerate(arr):
            parent_sel = ""  # můžeš později doplnit (např. nearest section)
            nodes_out.append((parent_sel, sel, idx, text))

    return nodes_out

def page_hash(nodes: List[Tuple[str,str,int,str]]) -> str:
    payload = "||".join([f"{p}|{s}|{i}|{t}" for (p,s,i,t) in nodes])
    return sha(payload)

def main():
    if "OPENAI_API_KEY" not in os.environ:
        raise RuntimeError("Chybí OPENAI_API_KEY")

    db = load_db()

    urls = fetch_sitemap_urls(SITEMAP_URL)

    # TEST jen 1 stránka:
    test_only_first = True
    if test_only_first:
        urls = urls[:1]

    for url in urls:
        url = normalize_url(url)
        print(f"Processing {url}")

        r = requests.get(url, timeout=30, headers={"User-Agent":"i18n-bot/1.0"})
        html = r.text

        nodes = extract_nodes(html)
        h = page_hash(nodes)

        pages = db.setdefault("pages", {})
        prev = pages.get(url)

        if prev and prev.get("hash") == h:
            print("  unchanged, skip")
            continue

        out_nodes = []
        for parent_sel, sel, idx, src in nodes:
            dst = translate_cached(src, TARGET_LANG, db)

            key = f"p.{sha(url)[:8]}.{sha(parent_sel+'|'+sel+'|'+str(idx)+'|'+src)[:10]}"

            out_nodes.append({
                "key": key,
                "parent": parent_sel,
                "selector": sel,
                "index": idx,
                "source": src,
                "dst": {TARGET_LANG: dst}
            })

        pages[url] = {"hash": h, "nodes": out_nodes}
        save_db(db)
        print(f"  saved {len(out_nodes)} nodes")

if __name__ == "__main__":
    main()
