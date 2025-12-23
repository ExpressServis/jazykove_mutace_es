# clean_i18n_db.py
# Spusť po translate_web.py
# Čistí vadné překlady ("Es tut mir leid..." apod.) ve split i18n formátu:
# - i18n/global.json
# - i18n/index.json (nemění – jen kontrola existence)
# - i18n/pages/*.json
# + volitelně i legacy cache: i18n/i18n_pages_db.json (pokud existuje)
#
# Zároveň může vynutit, že některé brandy zůstanou beze změny.

import json
import re
from pathlib import Path
from typing import Dict, Any, Tuple, List


ROOT_DIR = Path(__file__).resolve().parent
I18N_DIR = ROOT_DIR / "i18n"
PAGES_DIR = I18N_DIR / "pages"
GLOBAL_JSON = I18N_DIR / "global.json"
INDEX_JSON = I18N_DIR / "index.json"
LEGACY_DB = I18N_DIR / "i18n_pages_db.json"  # volitelné (cache + kompatibilita)

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


def clean_dst_map(src: str, dst_map: Dict[str, Any]) -> Tuple[int, int]:
    """
    Vrací: (removed_entries, forced_resets)
    """
    removed = 0
    resets = 0

    if not isinstance(dst_map, dict):
        return (0, 0)

    src_n = norm(src)

    for lang in list(dst_map.keys()):
        dst = norm(dst_map.get(lang, ""))

        # Force no-translate brands
        if src_n in NEVER_TRANSLATE_EXACT and dst and dst != src_n:
            dst_map[lang] = src_n
            resets += 1
            continue

        # Remove bad
        if looks_bad(dst, src_n):
            del dst_map[lang]
            removed += 1

    return (removed, resets)


def clean_nodes_payload(payload: Dict[str, Any]) -> Tuple[int, int]:
    """
    Čistí payload se strukturou { ..., "nodes": [ { "source": "...", "dst": {...}} ] }
    Vrací: (removed_node_entries, forced_resets)
    """
    removed_nodes = 0
    resets = 0

    nodes = payload.get("nodes", []) or []
    if not isinstance(nodes, list):
        return (0, 0)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        src = node.get("source", "")
        dst_map = node.get("dst", {}) or {}

        r, rr = clean_dst_map(src, dst_map)
        removed_nodes += r
        resets += rr

        node["dst"] = dst_map

    return (removed_nodes, resets)


def clean_legacy_db(db: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Legacy: { "texts": {...}, "pages": {...}, "global": {...} }
    Vrací: (removed_text_entries, removed_page_node_entries, forced_resets)
    """
    removed_text_entries = 0
    removed_page_node_entries = 0
    forced_resets = 0

    texts = db.get("texts", {}) or {}
    pages = db.get("pages", {}) or {}

    # --- 1) vyčistit cache v texts ---
    for key, entry in list(texts.items()):
        if not isinstance(entry, dict):
            continue
        src = entry.get("src", "")
        dst_map = entry.get("dst", {}) or {}

        r, rr = clean_dst_map(src, dst_map)
        removed_text_entries += r
        forced_resets += rr

        entry["dst"] = dst_map if isinstance(dst_map, dict) else {}

    # --- 2) vyčistit pages nodes (dst uvnitř nodes) ---
    for _, pdata in pages.items():
        if not isinstance(pdata, dict):
            continue
        r_nodes, rr = clean_nodes_payload(pdata)
        removed_page_node_entries += r_nodes
        forced_resets += rr

    # --- 3) vyčistit global (pokud existuje v legacy) ---
    if isinstance(db.get("global"), dict):
        r_nodes, rr = clean_nodes_payload(db["global"])
        removed_page_node_entries += r_nodes
        forced_resets += rr

    return removed_text_entries, removed_page_node_entries, forced_resets


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_page_files() -> List[Path]:
    if not PAGES_DIR.exists():
        return []
    return sorted([p for p in PAGES_DIR.glob("*.json") if p.is_file()])


def main():
    if not I18N_DIR.exists():
        raise SystemExit(f"i18n dir not found: {I18N_DIR}")

    if not GLOBAL_JSON.exists():
        print(f"⚠️ global.json not found: {GLOBAL_JSON} (skipping)")
    if not INDEX_JSON.exists():
        print(f"⚠️ index.json not found: {INDEX_JSON} (skipping)")

    removed_global = 0
    removed_pages = 0
    resets_total = 0

    # --- 1) GLOBAL ---
    if GLOBAL_JSON.exists():
        global_payload = read_json(GLOBAL_JSON)
        if not isinstance(global_payload, dict):
            raise SystemExit("global.json format is not a dict")

        r_nodes, rr = clean_nodes_payload(global_payload)
        removed_global += r_nodes
        resets_total += rr

        write_json(GLOBAL_JSON, global_payload)
        print(f"✅ cleaned global.json (removed: {r_nodes}, resets: {rr})")

    # --- 2) PAGES split ---
    page_files = list_page_files()
    if not page_files:
        print(f"⚠️ no page files found in: {PAGES_DIR}")

    for pf in page_files:
        payload = read_json(pf)
        if not isinstance(payload, dict):
            print(f"⚠️ skipping (not a dict): {pf}")
            continue

        r_nodes, rr = clean_nodes_payload(payload)
        if r_nodes or rr:
            write_json(pf, payload)

        removed_pages += r_nodes
        resets_total += rr

    print(f"✅ cleaned pages/*.json files={len(page_files)} (removed: {removed_pages}, resets: {resets_total})")

    # --- 3) LEGACY DB (optional) ---
    removed_texts = 0
    removed_legacy_nodes = 0
    legacy_resets = 0

    if LEGACY_DB.exists():
        legacy = read_json(LEGACY_DB)
        if isinstance(legacy, dict):
            r_texts, r_nodes, rr = clean_legacy_db(legacy)
            removed_texts += r_texts
            removed_legacy_nodes += r_nodes
            legacy_resets += rr
            write_json(LEGACY_DB, legacy)
            print(f"✅ cleaned legacy i18n_pages_db.json (texts removed: {r_texts}, nodes removed: {r_nodes}, resets: {rr})")
        else:
            print("⚠️ legacy DB format is not a dict (skipping)")

    # --- Summary ---
    print("\n✅ i18n cleanup done")
    print(f"- global.json removed bad dst entries: {removed_global}")
    print(f"- pages/*.json removed bad dst entries: {removed_pages}")
    if LEGACY_DB.exists():
        print(f"- legacy texts removed bad entries: {removed_texts}")
        print(f"- legacy nodes removed bad dst entries: {removed_legacy_nodes}")
    print(f"- forced brand resets total: {resets_total + legacy_resets}")


if __name__ == "__main__":
    main()
