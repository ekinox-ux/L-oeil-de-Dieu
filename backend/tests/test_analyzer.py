import json
import os
import sys
import time
from collections import Counter
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import analyzer


@pytest.fixture(autouse=True)
def clean_locks(tmp_path, monkeypatch):
    """Isole chaque test dans un CACHE_DIR temporaire propre."""
    monkeypatch.setattr(analyzer, 'CACHE_DIR', tmp_path)
    yield
    # Nettoyage best-effort des locks restants
    for f in tmp_path.glob("analyzer_*.lock"):
        f.unlink(missing_ok=True)


# ---- Tests unitaires existants (inchanges) ----


def test_trim_counter_keeps_top_entries():
    counter = Counter({'a@example.com': 5, 'b@example.com': 3, 'c@example.com': 1})
    analyzer._trim_counter(counter, 2)
    assert counter == Counter({'a@example.com': 5, 'b@example.com': 3})


def test_trim_counter_disabled_when_limit_zero():
    counter = Counter({'a': 3, 'b': 2, 'c': 1})
    analyzer._trim_counter(counter, 0)
    assert len(counter) == 3


def test_build_result_computes_counts_and_limits():
    result = analyzer._build_result(
        source_dir='D:/source',
        jsonl_files=['one.jsonl', 'two.jsonl'],
        email_counter=Counter({'alice@gmail.com': 3, 'bob@corp.fr': 2}),
        domain_counter=Counter({'gmail.com': 3, 'corp.fr': 2}),
        phone_counter=Counter({'0612345678': 4, '+33611111111': 1}),
        total_lines=20,
        total_emails_found=5,
        total_phones_found=5,
        unique_emails_seen=2,
        unique_phones_seen=2,
        files_by_folder=Counter({'source_a': 2}),
        elapsed=1.24,
        done_files=2,
    )

    assert result['complete'] is True
    assert result['emails']['unique_count'] == 2
    assert result['emails']['perso_count'] == 3
    assert result['emails']['pro_count'] == 2
    assert result['emails']['top_domains'][0] == {'domain': 'gmail.com', 'count': 3}
    assert result['emails']['tld_distribution'][0]['tld'] == '.com'
    assert result['phones']['top_phones'][0]['phone'] == '0612345678'
    assert result['limits']['email_counter_limit'] == analyzer.ANALYZER_MAX_EMAIL_ENTRIES
    assert result['limits']['phone_counter_limit'] == analyzer.ANALYZER_MAX_PHONE_ENTRIES


def test_load_progress_accepts_legacy_list_format():
    path = analyzer._get_legacy_progress_json_path('D:/source')
    path.write_text('["a.jsonl", "b.jsonl"]', encoding='utf-8')

    loaded = analyzer._load_progress('D:/source')

    assert loaded == {'processed_files': ['a.jsonl', 'b.jsonl']}


def test_artifact_paths_do_not_collide_for_similar_sources():
    first = analyzer.get_cache_path('D:/a_b/c')
    second = analyzer.get_cache_path('D:/a/b_c')

    assert first != second
    assert first.name.startswith('analysis_')
    assert second.name.startswith('analysis_')


def test_restore_seen_values_uses_persisted_sets_when_available():
    progress = {
        'seen': {
            'emails': ['a@example.com', 'b@example.com'],
            'phones': ['0612345678'],
        }
    }
    counters = {
        'emails': {'a@example.com': 5},
        'phones': {'0612345678': 2},
    }

    seen_emails, seen_phones = analyzer._restore_seen_values(progress, counters)

    assert seen_emails == {'a@example.com', 'b@example.com'}
    assert seen_phones == {'0612345678'}


def test_restore_seen_values_falls_back_to_counter_keys_for_legacy_progress():
    seen_emails, seen_phones = analyzer._restore_seen_values(
        {},
        {
            'emails': {'a@example.com': 5, 'b@example.com': 1},
            'phones': {'0612345678': 2},
        },
    )

    assert seen_emails == {'a@example.com', 'b@example.com'}
    assert seen_phones == {'0612345678'}


# ---- Tests lockfile ----


def test_acquire_and_release_lock():
    """Le lockfile est cree puis supprime correctement."""
    source = "D:/test_source"

    assert analyzer._acquire_lock(source) is True
    lock_path = analyzer._get_lock_path(source)
    assert lock_path.exists()

    # Contenu = JSON avec source_dir
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["source_dir"] == source

    # Double acquisition echoue
    assert analyzer._acquire_lock(source) is False

    analyzer._release_lock(source)
    assert not lock_path.exists()


def test_lockfile_contains_source_dir():
    """Le lockfile persiste le source_dir pour reconstruction apres restart."""
    source = "X:/partage/data"
    analyzer._acquire_lock(source)

    lock_path = analyzer._get_lock_path(source)
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["source_dir"] == source

    analyzer._release_lock(source)


# ---- Tests run_analysis ----


def test_run_analysis_acquires_lock_before_popen():
    """Le lock est pris cote parent, avant Popen."""
    source = "D:/test_source"

    with patch('analyzer.subprocess.Popen') as mock_popen:
        result = analyzer.run_analysis(source, resume=True)

    assert result is True
    # Lock existe (pris par parent, pas encore libere par worker)
    assert analyzer._get_lock_path(source).exists()
    assert mock_popen.call_count == 1

    args = mock_popen.call_args[0][0]
    assert args[0] == sys.executable
    assert "analyzer.py" in args[1]
    assert args[2] == source
    assert "--no-resume" not in args

    analyzer._release_lock(source)


def test_run_analysis_no_resume_flag():
    """run_analysis passe --no-resume dans les args Popen."""
    source = "D:/test_nr"

    with patch('analyzer.subprocess.Popen') as mock_popen:
        analyzer.run_analysis(source, resume=False)

    args = mock_popen.call_args[0][0]
    assert "--no-resume" in args
    analyzer._release_lock(source)


def test_double_run_analysis_only_one_launch():
    """Deux appels rapproches a run_analysis: seul le premier lance un worker."""
    source = "D:/test_source"
    import threading

    # Le mock .wait() doit bloquer pour que le watchdog ne libere pas le lock
    # avant le 2e appel a run_analysis.
    block = threading.Event()

    with patch('analyzer.subprocess.Popen') as mock_popen:
        mock_popen.return_value.wait.side_effect = lambda: block.wait()
        result1 = analyzer.run_analysis(source)
        result2 = analyzer.run_analysis(source)

    assert result1 is True
    assert result2 is False
    assert mock_popen.call_count == 1

    block.set()  # debloquer le watchdog
    analyzer._release_lock(source)


def test_run_analysis_releases_lock_on_popen_failure():
    """Si Popen echoue, le lock est libere et run_analysis retourne False."""
    source = "D:/test_source"

    with patch('analyzer.subprocess.Popen', side_effect=OSError("spawn failed")):
        result = analyzer.run_analysis(source)

    assert result is False
    # Lock doit etre libere
    assert not analyzer._get_lock_path(source).exists()


def test_run_analysis_after_popen_failure_can_retry():
    """Apres un echec Popen (lock libere), un nouvel appel peut relancer."""
    source = "D:/test_source"

    with patch('analyzer.subprocess.Popen', side_effect=OSError("spawn failed")):
        result1 = analyzer.run_analysis(source)
    assert result1 is False

    with patch('analyzer.subprocess.Popen'):
        result2 = analyzer.run_analysis(source)
    assert result2 is True

    analyzer._release_lock(source)


# ---- Tests get_status persistant ----


def test_get_status_finds_running_via_lockfile_scan():
    """get_status detecte une analyse en cours via scan des lockfiles."""
    source = "D:/test_source"
    analyzer._acquire_lock(source)

    status = analyzer.get_status()
    assert status["running"] is True

    analyzer._release_lock(source)


def test_get_status_not_running_without_lockfile():
    """get_status retourne running=False quand aucun lockfile n'existe."""
    status = analyzer.get_status()
    assert status["running"] is False


def test_get_status_survives_backend_restart(tmp_path):
    """Simule un restart backend: la variable memoire est perdue mais
    le lockfile persiste. get_status doit quand meme trouver l'analyse."""
    source = "D:/test_source"

    # Le parent acquiert le lock (simule avant restart)
    analyzer._acquire_lock(source)

    # Ecrire un fichier progress (ecrit par le worker) via legacy JSON
    progress_path = analyzer._get_legacy_progress_json_path(source)
    progress_path.write_text(json.dumps({
        "processed_files": ["a.jsonl", "b.jsonl", "c.jsonl"],
        "total_emails_found": 99,
        "total_phones_found": 12,
    }), encoding="utf-8")

    # Simuler restart: aucune variable memoire, seulement les fichiers
    # (le fixture clean_locks a deja mis CACHE_DIR = tmp_path)
    status = analyzer.get_status()
    assert status["running"] is True
    assert status["progress"] == 3
    assert status["emails_found"] == 99
    assert status["phones_found"] == 12

    analyzer._release_lock(source)


def test_get_status_reads_cache_when_not_running():
    """Quand aucune analyse ne tourne, get_status retourne les infos du dernier cache."""
    source = "D:/test_source"

    # Pas de lock, mais un cache existe
    cache_path = analyzer.get_cache_path(source)
    cache_path.write_text(json.dumps({
        "source_dir": source,
        "scan_date": "2026-04-03 12:00:00",
        "total_files": 50,
        "files_processed": 50,
        "emails": {"total_occurrences": 200},
        "phones": {"total_occurrences": 30},
    }), encoding="utf-8")

    status = analyzer.get_status()
    assert status["running"] is False
    assert status["last_run"] == "2026-04-03 12:00:00"
    assert status["total_files"] == 50
    assert status["progress"] == 50
    assert status["emails_found"] == 200
    assert status["phones_found"] == 30


def test_find_latest_cached_source_returns_latest_cache():
    """Le cache le plus recent est utilise quand aucune analyse n'est active."""
    old_source = "D:/old"
    new_source = "D:/new"

    analyzer.get_cache_path(old_source).write_text(json.dumps({
        "source_dir": old_source,
        "scan_date": "2026-04-03 10:00:00",
    }), encoding="utf-8")
    analyzer.get_cache_path(new_source).write_text(json.dumps({
        "source_dir": new_source,
        "scan_date": "2026-04-03 12:00:00",
    }), encoding="utf-8")

    os.utime(analyzer.get_cache_path(old_source), (1, 1))
    os.utime(analyzer.get_cache_path(new_source), None)

    assert analyzer._find_latest_cached_source() == new_source


def test_get_status_reads_progress_during_analysis():
    """get_status lit le progress quand une analyse est en cours."""
    source = "D:/test_source"
    analyzer._acquire_lock(source)

    progress_path = analyzer._get_legacy_progress_json_path(source)
    progress_path.write_text(json.dumps({
        "processed_files": ["a.jsonl", "b.jsonl"],
        "total_emails_found": 42,
        "total_phones_found": 7,
    }), encoding="utf-8")

    status = analyzer.get_status()
    assert status["running"] is True
    assert status["progress"] == 2
    assert status["emails_found"] == 42
    assert status["phones_found"] == 7

    analyzer._release_lock(source)


def test_find_active_lock_returns_none_when_empty():
    """_find_active_lock retourne None quand aucun lockfile n'existe."""
    assert analyzer._find_active_lock() is None


def test_find_active_lock_ignores_corrupt_lockfile(tmp_path):
    """Un lockfile corrompu ne produit pas de faux positif running=True."""
    lock_path = tmp_path / "analyzer_corrupt.lock"
    lock_path.write_text("not valid json {{{", encoding="utf-8")

    assert analyzer._find_active_lock() is None

    status = analyzer.get_status()
    assert status["running"] is False


def test_find_active_lock_returns_source_dir():
    """_find_active_lock lit le source_dir depuis le lockfile."""
    source = "D:/data/sources"
    analyzer._acquire_lock(source)

    result = analyzer._find_active_lock()
    assert result == source

    analyzer._release_lock(source)


# ---- Tests TTL lockfile orphelin ----


def test_recent_lock_is_kept():
    """Un lock recent (< 24h) est conserve."""
    source = "D:/recent"
    analyzer._acquire_lock(source)

    assert analyzer._find_active_lock() == source
    assert analyzer._get_lock_path(source).exists()

    analyzer._release_lock(source)


def test_old_lock_without_activity_is_cleaned(tmp_path):
    """Un lock ancien (> 24h) sans progress recent est supprime."""
    source = "D:/old_no_progress"
    analyzer._acquire_lock(source)

    # Vieillir le lockfile de 25h
    lock_path = analyzer._get_lock_path(source)
    old_mtime = time.time() - (25 * 3600)
    os.utime(lock_path, (old_mtime, old_mtime))

    result = analyzer._find_active_lock()
    assert result is None
    # Lock supprime
    assert not lock_path.exists()


def test_old_lock_with_recent_progress_is_kept(tmp_path):
    """Un lock ancien mais avec progress recent est conserve (analyse toujours active)."""
    source = "D:/old_active"
    analyzer._acquire_lock(source)

    # Vieillir le lockfile de 25h
    lock_path = analyzer._get_lock_path(source)
    old_mtime = time.time() - (25 * 3600)
    os.utime(lock_path, (old_mtime, old_mtime))

    # Mais le progress est recent (touche maintenant)
    progress_path = analyzer._get_legacy_progress_json_path(source)
    progress_path.write_text(json.dumps({
        "processed_files": ["a.jsonl"],
        "total_emails_found": 10,
        "total_phones_found": 2,
    }), encoding="utf-8")

    result = analyzer._find_active_lock()
    assert result == source
    # Lock conserve
    assert lock_path.exists()

    analyzer._release_lock(source)


def test_old_lock_with_old_progress_is_cleaned(tmp_path):
    """Un lock ancien avec progress ancien aussi est supprime."""
    source = "D:/old_dead"
    analyzer._acquire_lock(source)

    lock_path = analyzer._get_lock_path(source)
    old_mtime = time.time() - (25 * 3600)
    os.utime(lock_path, (old_mtime, old_mtime))

    progress_path = analyzer._get_legacy_progress_json_path(source)
    progress_path.write_text(json.dumps({
        "processed_files": ["a.jsonl"],
    }), encoding="utf-8")
    os.utime(progress_path, (old_mtime, old_mtime))

    result = analyzer._find_active_lock()
    assert result is None
    assert not lock_path.exists()


def test_cleanup_stale_analyzer_locks_removes_only_orphans(tmp_path):
    old_source = "D:/old_dead"
    active_source = "D:/active"

    analyzer._acquire_lock(old_source)
    analyzer._acquire_lock(active_source)

    old_lock = analyzer._get_lock_path(old_source)
    active_lock = analyzer._get_lock_path(active_source)
    old_mtime = time.time() - (25 * 3600)
    os.utime(old_lock, (old_mtime, old_mtime))

    result = analyzer.cleanup_stale_analyzer_locks()

    assert old_source in result["removed"]
    assert active_source in result["kept"]
    assert not old_lock.exists()
    assert active_lock.exists()

    analyzer._release_lock(active_source)


def test_do_analysis_keeps_exact_unique_counts_when_counters_are_trimmed(tmp_path, monkeypatch):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "sample.jsonl").write_text(
        "\n".join([
            '{"email":"a@example.com","phone":"0611111111"}',
            '{"email":"b@example.com","phone":"0622222222"}',
            '{"email":"c@example.com","phone":"0633333333"}',
            '{"email":"a@example.com","phone":"0611111111"}',
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(analyzer, 'ANALYZER_MAX_EMAIL_ENTRIES', 1)
    monkeypatch.setattr(analyzer, 'ANALYZER_MAX_PHONE_ENTRIES', 1)
    monkeypatch.setattr(analyzer, 'ANALYZER_SAVE_EVERY', 1)

    analyzer._do_analysis(str(source_dir), resume=False)
    result = analyzer.load_cache(str(source_dir))

    assert result['emails']['unique_count'] == 3
    assert result['phones']['unique_count'] == 3
    assert len(result['emails']['top_emails']) == 1
    assert len(result['phones']['top_phones']) == 1


def test_resume_preserves_exact_unique_counts_after_trim(tmp_path, monkeypatch):
    source_dir = tmp_path / "src_resume"
    source_dir.mkdir()
    first = source_dir / "part1.jsonl"
    second = source_dir / "part2.jsonl"
    first.write_text(
        "\n".join([
            '{"email":"a@example.com","phone":"0611111111"}',
            '{"email":"b@example.com","phone":"0622222222"}',
        ]),
        encoding="utf-8",
    )
    second.write_text(
        "\n".join([
            '{"email":"a@example.com","phone":"0611111111"}',
            '{"email":"c@example.com","phone":"0633333333"}',
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(analyzer, 'ANALYZER_MAX_EMAIL_ENTRIES', 1)
    monkeypatch.setattr(analyzer, 'ANALYZER_MAX_PHONE_ENTRIES', 1)
    monkeypatch.setattr(analyzer, 'ANALYZER_SAVE_EVERY', 1)

    analyzer._save_progress(
        str(source_dir),
        [str(first)],
        Counter({'a@example.com': 1}),
        Counter({'example.com': 2}),
        Counter({'0611111111': 1}),
        {'a@example.com', 'b@example.com'},
        {'0611111111', '0622222222'},
        Counter({source_dir.name: 1}),
        total_lines=2,
        total_emails_found=2,
        total_phones_found=2,
    )

    analyzer._do_analysis(str(source_dir), resume=True)
    result = analyzer.load_cache(str(source_dir))

    assert result['emails']['unique_count'] == 3
    assert result['phones']['unique_count'] == 3


# ---- Tests persistance SQLite ----


def test_save_progress_writes_sqlite_file():
    """_save_progress ecrit bien un fichier .sqlite valide."""
    source = "D:/test_sqlite"
    analyzer._save_progress(
        source,
        ["a.jsonl", "b.jsonl"],
        Counter({"alice@example.com": 3}),
        Counter({"example.com": 3}),
        Counter({"0611111111": 2}),
        {"alice@example.com"},
        {"0611111111"},
        Counter({"folder1": 2}),
        total_lines=10,
        total_emails_found=3,
        total_phones_found=2,
    )

    sqlite_path = analyzer._get_progress_path(source)
    assert sqlite_path.exists()
    assert sqlite_path.suffix == ".sqlite"
    # Pas de JSON legacy cree par _save_progress
    assert not analyzer._get_legacy_progress_json_path(source).exists()


def test_load_progress_roundtrip_sqlite():
    """Save puis load: toutes les donnees sont preservees."""
    source = "D:/roundtrip"
    processed = ["fileA.jsonl", "fileB.jsonl"]
    email_c = Counter({"a@x.com": 5, "b@x.com": 1})
    domain_c = Counter({"x.com": 6})
    phone_c = Counter({"0611111111": 4, "0622222222": 1})
    seen_emails = {"a@x.com", "b@x.com", "c@x.com"}  # delta vs email_c
    seen_phones = {"0611111111", "0622222222", "0633333333"}
    folders = Counter({"dir1": 2})

    analyzer._save_progress(
        source, processed, email_c, domain_c, phone_c,
        seen_emails, seen_phones, folders,
        total_lines=42, total_emails_found=6, total_phones_found=5,
    )
    loaded = analyzer._load_progress(source)

    assert loaded is not None
    assert set(loaded["processed_files"]) == set(processed)
    assert Counter(loaded["counters"]["emails"]) == email_c
    assert Counter(loaded["counters"]["domains"]) == domain_c
    assert Counter(loaded["counters"]["phones"]) == phone_c
    assert Counter(loaded["counters"]["folders"]) == folders
    assert set(loaded["seen"]["emails"]) == seen_emails
    assert set(loaded["seen"]["phones"]) == seen_phones
    assert loaded["total_lines"] == 42
    assert loaded["total_emails_found"] == 6
    assert loaded["total_phones_found"] == 5
    assert loaded["unique_emails_seen"] == 3
    assert loaded["unique_phones_seen"] == 3


def test_load_progress_migrates_legacy_json_to_sqlite():
    """Un progress JSON structure est migre en SQLite au premier load."""
    source = "D:/migration"
    legacy_path = analyzer._get_legacy_progress_json_path(source)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({
        "processed_files": ["old1.jsonl", "old2.jsonl"],
        "counters": {
            "emails": {"legacy@x.com": 7},
            "domains": {"x.com": 7},
            "phones": {"0611111111": 3},
            "folders": {"dirX": 2},
        },
        "seen": {
            "emails": ["legacy@x.com", "untrimmed@x.com"],
            "phones": ["0611111111", "0699999999"],
        },
        "unique_emails_seen": 2,
        "unique_phones_seen": 2,
        "total_lines": 100,
        "total_emails_found": 7,
        "total_phones_found": 3,
    }), encoding="utf-8")

    loaded = analyzer._load_progress(source)

    # Donnees correctement migrees
    assert loaded is not None
    assert set(loaded["processed_files"]) == {"old1.jsonl", "old2.jsonl"}
    assert loaded["counters"]["emails"] == {"legacy@x.com": 7}
    assert set(loaded["seen"]["emails"]) == {"legacy@x.com", "untrimmed@x.com"}
    assert loaded["total_lines"] == 100

    # SQLite cree, JSON archive
    assert analyzer._get_progress_path(source).exists()
    assert not legacy_path.exists()
    assert legacy_path.with_suffix(".json.migrated").exists()


def test_load_progress_prefers_sqlite_over_legacy_json():
    """Si les deux fichiers existent, le SQLite l'emporte (migration deja faite)."""
    source = "D:/prefers_sqlite"
    # Legacy present mais doit etre ignore
    legacy_path = analyzer._get_legacy_progress_json_path(source)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({
        "processed_files": ["legacy.jsonl"],
    }), encoding="utf-8")

    # SQLite cree via _save_progress avec des donnees differentes
    analyzer._save_progress(
        source,
        ["sqlite_current.jsonl"],
        Counter(), Counter(), Counter(),
        set(), set(), Counter(),
        total_lines=0, total_emails_found=0, total_phones_found=0,
    )

    loaded = analyzer._load_progress(source)
    assert loaded["processed_files"] == ["sqlite_current.jsonl"]


def test_load_progress_handles_corrupt_sqlite(tmp_path):
    """Un SQLite corrompu ne plante pas, fallback sur JSON legacy si present."""
    source = "D:/corrupt_sqlite"
    # Ecrire n'importe quoi dans le fichier SQLite
    sqlite_path = analyzer._get_progress_path(source)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.write_bytes(b"this is not a sqlite file")

    # Aucun JSON legacy => None
    result = analyzer._load_progress(source)
    assert result is None

    # Avec un JSON legacy => lecture du legacy
    legacy_path = analyzer._get_legacy_progress_json_path(source)
    legacy_path.write_text(json.dumps({
        "processed_files": ["fallback.jsonl"],
    }), encoding="utf-8")

    result = analyzer._load_progress(source)
    assert result is not None
    assert result["processed_files"] == ["fallback.jsonl"]


def test_do_analysis_cleans_both_formats_on_complete(tmp_path):
    """A la fin de l'analyse, SQLite ET JSON legacy sont supprimes."""
    source_dir = tmp_path / "src_cleanup"
    source_dir.mkdir()
    (source_dir / "tiny.jsonl").write_text(
        '{"email":"x@y.com"}', encoding="utf-8",
    )

    # Creer un legacy JSON pour qu'il soit present avant l'analyse
    legacy_path = analyzer._get_legacy_progress_json_path(str(source_dir))
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({"processed_files": []}), encoding="utf-8")

    analyzer._do_analysis(str(source_dir), resume=False)

    assert not analyzer._get_progress_path(str(source_dir)).exists()
    assert not legacy_path.exists()


def test_failed_file_is_not_marked_processed_and_progress_is_kept(tmp_path, monkeypatch):
    source_dir = tmp_path / "src_failure"
    source_dir.mkdir()
    good = source_dir / "good.jsonl"
    broken = source_dir / "broken.jsonl"
    good.write_text('{"email":"ok@example.com"}\n', encoding="utf-8")
    broken.write_text('{"email":"retry@example.com"}\n', encoding="utf-8")

    original_reader = analyzer._iter_bounded_text_lines

    def failing_reader(filepath):
        if filepath == str(broken):
            raise OSError("simulated read failure")
        yield from original_reader(filepath)

    monkeypatch.setattr(analyzer, "_iter_bounded_text_lines", failing_reader)
    analyzer._do_analysis(str(source_dir), resume=False)

    result = analyzer.load_cache(str(source_dir))
    progress = analyzer._load_progress(str(source_dir))
    assert result["complete"] is False
    assert result["files_processed"] == 1
    assert result["failed_files"][0]["path"] == str(broken)
    assert progress is not None
    assert progress["processed_files"] == [str(good)]
    assert str(broken) in progress["failed_files"]


def test_analysis_rejects_oversized_line_without_unbounded_read(tmp_path, monkeypatch):
    source_dir = tmp_path / "src_large_line"
    source_dir.mkdir()
    source_file = source_dir / "large.jsonl"
    source_file.write_text('{"value":"' + ('x' * 200) + '"}\n', encoding="utf-8")
    monkeypatch.setattr(analyzer, "ANALYZER_MAX_LINE_BYTES", 64)

    analyzer._do_analysis(str(source_dir), resume=False)

    result = analyzer.load_cache(str(source_dir))
    assert result["complete"] is False
    assert result["files_processed"] == 0
    assert "superieure" in result["failed_files"][0]["error"]


def test_most_recent_progress_mtime_prefers_newest():
    """Le helper mtime retourne bien le plus recent des deux fichiers."""
    source = "D:/mtime_both"
    sqlite_path = analyzer._get_progress_path(source)
    legacy_path = analyzer._get_legacy_progress_json_path(source)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.write_bytes(b"x")
    legacy_path.write_bytes(b"y")

    old = time.time() - 1000
    recent = time.time() - 10
    os.utime(sqlite_path, (old, old))
    os.utime(legacy_path, (recent, recent))

    mtime = analyzer._most_recent_progress_mtime(source)
    assert mtime is not None
    assert abs(mtime - recent) < 1.0


def test_json_root_is_dict_detection(tmp_path):
    """Detecte correctement un dict vs une list a la racine d'un JSON."""
    dict_path = tmp_path / "dict.json"
    list_path = tmp_path / "list.json"
    empty_path = tmp_path / "empty.json"

    dict_path.write_text('  \n  { "a": 1 }', encoding="utf-8")
    list_path.write_text('\t[1, 2]', encoding="utf-8")
    empty_path.write_text('   ', encoding="utf-8")

    assert analyzer._json_root_is_dict(dict_path) is True
    assert analyzer._json_root_is_dict(list_path) is False
    assert analyzer._json_root_is_dict(empty_path) is False
    assert analyzer._json_root_is_dict(tmp_path / "missing.json") is False


# ---- Corrections de robustesse ----


def test_connect_progress_releases_handle_on_corrupt_file(tmp_path):
    """Une base illisible ne doit laisser aucun handle ouvert (bloquant sous Windows)."""
    import sqlite3

    corrupt = tmp_path / "progress_corrupt.sqlite"
    corrupt.write_bytes(b"this is not a sqlite file" * 100)

    with pytest.raises(sqlite3.DatabaseError):
        analyzer._connect_progress(corrupt)

    renamed = corrupt.with_name("renamed.sqlite")
    corrupt.replace(renamed)  # echouerait sous Windows si un handle restait ouvert
    renamed.unlink()


def test_do_analysis_quarantines_corrupt_progress_and_completes(tmp_path):
    source_dir = tmp_path / "src_corrupt_progress"
    source_dir.mkdir()
    (source_dir / "data.jsonl").write_text('{"email":"ok@example.com"}\n', encoding="utf-8")
    progress_path = analyzer._get_progress_path(str(source_dir))
    progress_path.write_bytes(b"corrupted progress" * 100)

    analyzer._do_analysis(str(source_dir), resume=True)

    result = analyzer.load_cache(str(source_dir))
    assert result["complete"] is True
    assert result["emails"]["unique_count"] == 1
    assert progress_path.with_name(progress_path.name + ".corrupt").exists()


def test_snapshot_estimates_perso_pro_for_progress_without_totals(tmp_path):
    """Une progression ancienne sans totaux perso/pro garde une estimation non nulle."""
    progress_path = tmp_path / "legacy_progress.sqlite"
    conn = analyzer._connect_progress(progress_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO counter_domains(key, count) VALUES(?, ?)",
                [("gmail.com", 3), ("corp.example", 2)],
            )
        result = analyzer._analysis_snapshot(conn, "D:/legacy", [], 0.0)
    finally:
        conn.close()

    assert result["emails"]["perso_count"] == 3
    assert result["emails"]["pro_count"] == 2


def test_worker_logging_is_not_configured_on_import():
    """Importer l'analyseur (API, tests) ne doit ouvrir aucun fichier de log."""
    assert not any(getattr(handler, "_oeil_worker", False) for handler in analyzer._logger.handlers)


def test_do_analysis_keeps_configured_source_as_artifact_key(tmp_path, monkeypatch):
    """Les artefacts restent indexes par le chemin configure, variables comprises."""
    source_dir = tmp_path / "src_env"
    source_dir.mkdir()
    (source_dir / "data.jsonl").write_text('{"email":"env@example.com"}\n', encoding="utf-8")
    monkeypatch.setenv("OEIL_TEST_SOURCE_ROOT", str(tmp_path))
    configured = os.path.join("${OEIL_TEST_SOURCE_ROOT}", "src_env")

    analyzer._do_analysis(configured, resume=False)

    result = analyzer.load_cache(configured)
    assert result is not None
    assert result["source_dir"] == configured
    assert result["files_processed"] == 1
