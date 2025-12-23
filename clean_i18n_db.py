# clean_i18n_db.py
# Spusť po translate_web.py
# Projde i18n_pages_db.json a smaže vadné překlady (typicky "Es tut mir leid..." apod.)
# + volitelně vynutí, že některé brandy zůstanou beze změny.

import json
import re
from pathlib import Path
from typing import Dict, Any, Tuple

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "i18n" / "i18n_pages_db.json"

# Texty, které nikdy nechceme překlápět (vrátit src)
NEVER_TRANSLATE_EXACT = {
    "Facebook", "YouTube", "Instagram", "TikTok", "Spotify",
    "E-shop", "Servis",
}

# Příznaky "ujetých" odpovědí
BAD_PHRASES = [
    "es tut mir leid",
    "ich benötige den spezifischen text",
    "bitte geben sie den text an",
    "i'm sorry",
    "i am sorry",
    "please provide the text",
    "as an ai",
]

# pokud je src krátké a dst dlouhé -> podezřelé (např. TikTok -> dlouhá omluva)
MAX_SRC_LEN_FOR_SANITY = 20
MIN_DST_LEN_SUSPICIOUS = 60


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def looks_bad(dst: str, src: str) -> bool:
    d = (dst or "")
    s = (src or "")
    if not d:
        return False
    dl = d.lower()

    if any(p in dl for p in BAD_PHRASES):
        return True

    if len(norm(s)) <= MAX_SRC_LEN_FOR_SANITY and len(norm(d)) >= MIN_DST_LEN_SUSPICIOUS:
        return True

    return False


def clean_db(db: Dict[str, Any]) -> Tuple[int, int, int]:
    removed_text_entries = 0
    removed_page_node_entries = 0
    forced_brand_resets = 0

    texts = db.get("texts", {}) or {}
    pages = db.get("pages", {}) or {}

    # --- 1) vyčistit cache v texts ---
    for key, entry in list(texts.items()):
        if not isinstance(entry, dict):
            continue
        src = norm(entry.get("src", ""))
        dst_map = entry.get("dst", {}) or {}
        if not isinstance(dst_map, dict):
            continue

        for lang in list(dst_map.keys()):
            dst = norm(dst_map.get(lang, ""))

            # Force no-translate brands
            if src in NEVER_TRANSLATE_EXACT and dst and dst != src:
                dst_map[lang] = src
                forced_brand_resets += 1
                continue

            # Remove bad
            if looks_bad(dst, src):
                del dst_map[lang]
                removed_text_entries += 1

        if not dst_map:
            entry["dst"] = {}

    # --- 2) vyčistit pages nodes (dst uvnitř nodes) ---
    for url, pdata in pages.items():
        if not isinstance(pdata, dict):
            continue
        nodes = pdata.get("nodes", []) or []
        if not isinstance(nodes, list):
            continue

        for node in nodes:
            if not isinstance(node, dict):
                continue
            src = norm(node.get("source", ""))
            dst_map = node.get("dst", {}) or {}
            if not isinstance(dst_map, dict):
                continue

            for lang in list(dst_map.keys()):
                dst = norm(dst_map.get(lang, ""))

                # Force no-translate brands
                if src in NEVER_TRANSLATE_EXACT and dst and dst != src:
                    dst_map[lang] = src
                    forced_brand_resets += 1
                    continue

                # Remove bad
                if looks_bad(dst, src):
                    del dst_map[lang]
                    removed_page_node_entries += 1

            node["dst"] = dst_map

    return removed_text_entries, removed_page_node_entries, forced_brand_resets


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")

    db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    if not isinstance(db, dict):
        raise SystemExit("DB format is not a dict")

    r_texts, r_nodes, resets = clean_db(db)

    DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

    print("✅ i18n DB cleaned")
    print(f"- removed bad entries in texts: {r_texts}")
    print(f"- removed bad entries in page nodes: {r_nodes}")
    print(f"- forced brand resets: {resets}")
    print(f"Saved: {DB_PATH}")


if __name__ == "__main__":
    main()
