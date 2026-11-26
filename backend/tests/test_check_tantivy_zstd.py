import json
from pathlib import Path

import pytest

import check_tantivy_zstd


def test_zstd_check_runs_on_a_throwaway_index():
    # Wheel PyPI (CI) : False ; build `+zstd` local : True. Jamais d'exception.
    assert isinstance(check_tantivy_zstd.zstd_supported(), bool)


def test_zstd_check_reports_unsupported_build(monkeypatch):
    def refuse_zstd(path):
        meta = json.loads((Path(path) / "meta.json").read_text(encoding="utf-8"))
        assert meta["index_settings"]["docstore_compression"].startswith("zstd")
        raise ValueError("unsupported variant `zstd`, please enable Tantivy's `zstd-compression` feature")

    monkeypatch.setattr(check_tantivy_zstd.tantivy.Index, "open", refuse_zstd)

    assert check_tantivy_zstd.zstd_supported() is False
    assert check_tantivy_zstd.main() == 1


def test_zstd_check_propagates_unrelated_errors(monkeypatch):
    def broken(_path):
        raise ValueError("autre erreur")

    monkeypatch.setattr(check_tantivy_zstd.tantivy.Index, "open", broken)

    with pytest.raises(ValueError, match="autre erreur"):
        check_tantivy_zstd.zstd_supported()
