"""Verifie que le tantivy installe sait lire un doc store compresse en zstd.

Le wheel PyPI n'active pas la feature Rust `zstd-compression` : il refuse
alors d'ouvrir les index convertis par `indexer compress`. Le test construit
un index jetable dans un dossier temporaire, declare zstd dans ses reglages
et tente de le rouvrir. Aucun index reel n'est lu.

Code de sortie : 0 si zstd est pris en charge, 1 sinon.
"""

import json
import sys
import tempfile
from pathlib import Path

import tantivy


def zstd_supported() -> bool:
    with tempfile.TemporaryDirectory(prefix="oeil-zstd-check-") as tmp:
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("body", stored=True)
        index = tantivy.Index(builder.build(), path=tmp)
        writer = index.writer(heap_size=15_000_000, num_threads=1)
        writer.add_document(tantivy.Document(body="test"))
        writer.commit()
        writer.wait_merging_threads()
        del writer, index

        meta_path = Path(tmp) / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.setdefault("index_settings", {})["docstore_compression"] = "zstd(compression_level=3)"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        try:
            tantivy.Index.open(tmp)
        except ValueError as exc:
            if "zstd" in str(exc):
                return False
            raise
        return True


def main() -> int:
    if zstd_supported():
        print("OK : ce tantivy lit les index compresses en zstd.")
        return 0
    print(
        "Ce tantivy ne lit pas le zstd. Backend arrete, lancez : .\\build_tantivy_zstd.ps1 -Install",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
