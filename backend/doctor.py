"""Diagnostic local hors API pour l'exploitation locale."""

import argparse
import json
from pathlib import Path

from analyzer import CACHE_DIR, get_cache_path, _get_progress_path, _get_lock_path
from settings import INDEXES_CONFIG, resolve_runtime_path


def _load_indexes_config() -> list[dict]:
    if not INDEXES_CONFIG.is_file():
        return []
    with open(INDEXES_CONFIG, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def _lockfiles_for_index(index_path: Path) -> list[dict]:
    items = []
    for name in (".tantivy-meta.lock", ".tantivy-writer.lock"):
        lock_path = index_path / name
        if not lock_path.exists():
            continue
        stat = lock_path.stat()
        items.append({
            "name": name,
            "path": str(lock_path),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return items


def build_diagnostics() -> dict:
    payload = {
        "indexes_config": str(INDEXES_CONFIG),
        "indexes_config_exists": INDEXES_CONFIG.is_file(),
        "cache_dir": str(CACHE_DIR),
        "cache_dir_exists": CACHE_DIR.is_dir(),
        "indexes": [],
    }

    for cfg in _load_indexes_config():
        idx_id = cfg.get("id", "")
        idx_name = cfg.get("name", idx_id)
        index_path = resolve_runtime_path(cfg.get("path", ""))
        source_raw = cfg.get("source", "")
        source_path = resolve_runtime_path(source_raw) if source_raw else None
        analysis_cache = get_cache_path(source_raw) if source_raw else None
        progress_path = _get_progress_path(source_raw) if source_raw else None
        analyzer_lock = _get_lock_path(source_raw) if source_raw else None

        payload["indexes"].append({
            "id": idx_id,
            "name": idx_name,
            "path": str(index_path),
            "path_exists": index_path.is_dir(),
            "source": source_raw,
            "source_exists": source_path.is_dir() if source_path else False,
            "tantivy_lockfiles": _lockfiles_for_index(index_path),
            "analysis_cache": str(analysis_cache) if analysis_cache else "",
            "analysis_cache_exists": analysis_cache.exists() if analysis_cache else False,
            "progress_file": str(progress_path) if progress_path else "",
            "progress_file_exists": progress_path.exists() if progress_path else False,
            "analyzer_lock": str(analyzer_lock) if analyzer_lock else "",
            "analyzer_lock_exists": analyzer_lock.exists() if analyzer_lock else False,
        })

    return payload


def print_human(payload: dict):
    print("L'Oeil de Dieu - diagnostic local")
    print(f"- indexes_config: {payload['indexes_config']} ({'ok' if payload['indexes_config_exists'] else 'absent'})")
    print(f"- cache_dir: {payload['cache_dir']} ({'ok' if payload['cache_dir_exists'] else 'absent'})")
    if not payload["indexes"]:
        print("- aucun index configure")
        return

    for item in payload["indexes"]:
        print("")
        print(f"[{item['id']}] {item['name']}")
        print(f"  path: {item['path']} ({'ok' if item['path_exists'] else 'absent'})")
        if item["source"]:
            print(f"  source: {item['source']} ({'ok' if item['source_exists'] else 'absent'})")
        else:
            print("  source: non configure")
        print(f"  analysis_cache: {'ok' if item['analysis_cache_exists'] else 'absent'}")
        print(f"  progress_file: {'ok' if item['progress_file_exists'] else 'absent'}")
        print(f"  analyzer_lock: {'present' if item['analyzer_lock_exists'] else 'absent'}")
        if item["tantivy_lockfiles"]:
            print("  tantivy_lockfiles:")
            for lock in item["tantivy_lockfiles"]:
                print(f"    - {lock['name']}: {lock['path']}")
        else:
            print("  tantivy_lockfiles: aucun")


def main():
    parser = argparse.ArgumentParser(description="Diagnostic local L'Oeil de Dieu")
    parser.add_argument("--json", action="store_true", help="Sortie JSON")
    args = parser.parse_args()

    payload = build_diagnostics()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_human(payload)


if __name__ == "__main__":
    main()
