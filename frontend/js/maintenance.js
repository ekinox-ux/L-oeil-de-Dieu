(function () {
    'use strict';

    var AppUtils = window.AppUtils;
    var API_BASE = AppUtils.apiBase;
    var apiFetch = AppUtils.apiFetch;

    function showToast(message, level) {
        level = level || 'info';
        var existing = document.getElementById('app-toast');
        if (existing) {
            existing.remove();
        }
        var toast = document.createElement('div');
        toast.id = 'app-toast';
        toast.className = 'app-toast app-toast-' + level;
        toast.textContent = message;
        document.body.appendChild(toast);
        requestAnimationFrame(function () {
            toast.classList.add('visible');
        });
        setTimeout(function () {
            toast.classList.remove('visible');
            setTimeout(function () {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 220);
        }, 2800);
    }

    function formatTime(ts) {
        if (!ts) return '-';
        try {
            return new Date(ts * 1000).toLocaleString('fr-FR');
        } catch (_err) {
            return '-';
        }
    }

    function formatRetry(ts) {
        if (!ts) return 'aucun';
        var seconds = Math.max(0, Math.round(ts - Date.now() / 1000));
        return seconds > 0 ? 'dans ' + seconds + 's' : 'immediat';
    }

    function fetchJson(url) {
        return apiFetch(url).then(function (res) {
            return res.json().then(function (data) {
                if (!res.ok) {
                    throw new Error(data.detail || 'Erreur backend');
                }
                return data;
            });
        });
    }

    function fetchSection(name, url) {
        return fetchJson(url).then(function (data) {
            return { name: name, ok: true, data: data };
        }).catch(function (err) {
            return { name: name, ok: false, error: err };
        });
    }

    function createFlag(label, state) {
        var span = document.createElement('span');
        span.className = 'maintenance-flag maintenance-flag-' + state;
        span.textContent = label;
        return span;
    }

    function createMeta(label, value) {
        var row = document.createElement('div');
        row.className = 'maintenance-meta-row';

        var labelSpan = document.createElement('span');
        labelSpan.className = 'maintenance-meta-label';
        labelSpan.textContent = label;

        var valueSpan = document.createElement('span');
        valueSpan.className = 'maintenance-meta-value';
        valueSpan.textContent = value;

        row.appendChild(labelSpan);
        row.appendChild(valueSpan);
        return row;
    }

    function analyzeIndex(indexId, button) {
        button.disabled = true;
        button.textContent = 'Lancement...';

        apiFetch(API_BASE + '/api/analyze?index=' + encodeURIComponent(indexId), { method: 'POST' })
            .then(function (res) {
                return res.json().then(function (data) {
                    if (!res.ok) {
                        throw new Error(data.detail || 'Erreur lancement analyse');
                    }
                    return data;
                });
            })
            .then(function (data) {
                showToast(data.status === 'already_running' ? 'Analyse deja en cours' : 'Analyse lancee', 'info');
                loadMaintenance();
            })
            .catch(function (err) {
                showToast(err.message || 'Erreur lancement analyse', 'error');
            })
            .finally(function () {
                button.disabled = false;
                button.textContent = 'Lancer analyse';
            });
    }

    function retryIndex(indexId, button) {
        button.disabled = true;
        button.textContent = 'Retry...';

        apiFetch(API_BASE + '/api/diagnostics/indexes/retry?id=' + encodeURIComponent(indexId), { method: 'POST' })
            .then(function (res) {
                return res.json().then(function (data) {
                    if (!res.ok) {
                        throw new Error(data.detail || 'Erreur retry index');
                    }
                    return data;
                });
            })
            .then(function () {
                showToast('Retry manuel demande pour ' + indexId, 'info');
                loadMaintenance();
            })
            .catch(function (err) {
                showToast(err.message || 'Erreur retry index', 'error');
            })
            .finally(function () {
                button.disabled = false;
                button.textContent = 'Retry index';
            });
    }

    function cleanupAnalyzerLocks(button) {
        button.disabled = true;
        button.textContent = 'Nettoyage...';

        apiFetch(API_BASE + '/api/diagnostics/analyzer-locks/cleanup', { method: 'POST' })
            .then(function (res) {
                return res.json().then(function (data) {
                    if (!res.ok) {
                        throw new Error(data.detail || 'Erreur nettoyage locks');
                    }
                    return data;
                });
            })
            .then(function (data) {
                var msg = data.removed_count > 0
                    ? (data.removed_count + ' lock(s) orphelin(s) supprime(s)')
                    : 'Aucun lock orphelin a supprimer';
                showToast(msg, 'info');
                loadMaintenance();
            })
            .catch(function (err) {
                showToast(err.message || 'Erreur nettoyage locks', 'error');
            })
            .finally(function () {
                button.disabled = false;
                button.textContent = 'Nettoyer locks analyseur';
            });
    }

    function renderIndexes(indexesPayload, diagnosticsPayload) {
        var container = document.getElementById('maintenance-indexes');
        container.innerHTML = '';

        if (!indexesPayload.length) {
            var empty = document.createElement('p');
            empty.className = 'maintenance-empty';
            empty.textContent = 'Aucun index configure';
            container.appendChild(empty);
            return;
        }

        indexesPayload.forEach(function (idx) {
            var diag = diagnosticsPayload.indexes.filter(function (item) { return item.id === idx.id; })[0] || {};
            var card = document.createElement('div');
            card.className = 'maintenance-index-card';

            var head = document.createElement('div');
            head.className = 'maintenance-index-head';

            var titleWrap = document.createElement('div');
            var title = document.createElement('h3');
            title.className = 'maintenance-index-title';
            title.textContent = idx.name || idx.id;
            var subtitle = document.createElement('div');
            subtitle.className = 'maintenance-index-subtitle';
            subtitle.textContent = idx.id;
            titleWrap.appendChild(title);
            titleWrap.appendChild(subtitle);

            var flags = document.createElement('div');
            flags.className = 'maintenance-flags';
            flags.appendChild(createFlag(idx.status, idx.status === 'ready' ? 'ok' : idx.status === 'loading' ? 'warn' : 'err'));
            flags.appendChild(createFlag(diag.path_exists ? 'index ok' : 'index absent', diag.path_exists ? 'ok' : 'err'));
            flags.appendChild(createFlag(diag.source_exists ? 'source ok' : 'source absent', diag.source_exists ? 'ok' : 'warn'));
            flags.appendChild(createFlag((diag.lockfiles || []).length ? 'lock tantivy' : 'pas de lock', (diag.lockfiles || []).length ? 'warn' : 'ok'));
            flags.appendChild(createFlag(diag.analyzer_lock_exists ? 'lock analyseur' : 'analyse idle', diag.analyzer_lock_exists ? 'warn' : 'ok'));

            head.appendChild(titleWrap);
            head.appendChild(flags);

            var body = document.createElement('div');
            body.className = 'maintenance-index-body';
            body.appendChild(createMeta('Path index', diag.path || idx.path || '-'));
            body.appendChild(createMeta('Path source', diag.source || idx.source || 'non configure'));
            body.appendChild(createMeta('Erreur', idx.error || 'aucune'));
            body.appendChild(createMeta('Retry', idx.retry_count ? ('#' + idx.retry_count + ' ' + formatRetry(idx.next_retry_at)) : 'aucun'));
            body.appendChild(createMeta('Dernier ready', formatTime(idx.last_ready_at)));
            body.appendChild(createMeta('Derniere erreur', formatTime(idx.last_error_at)));
            body.appendChild(createMeta('Lock analyseur', diag.analyzer_lock_exists ? (diag.analyzer_lock || 'present') : 'absent'));

            var locks = document.createElement('div');
            locks.className = 'maintenance-locks';
            var locksTitle = document.createElement('div');
            locksTitle.className = 'maintenance-locks-title';
            locksTitle.textContent = 'Lockfiles';
            locks.appendChild(locksTitle);
            if ((diag.lockfiles || []).length === 0) {
                var noLock = document.createElement('div');
                noLock.className = 'maintenance-lock-item';
                noLock.textContent = 'Aucun lock Tantivy';
                locks.appendChild(noLock);
            } else {
                diag.lockfiles.forEach(function (lock) {
                    var item = document.createElement('div');
                    item.className = 'maintenance-lock-item';
                    item.textContent = lock.name + ' - ' + lock.path;
                    locks.appendChild(item);
                });
            }
            body.appendChild(locks);

            var actions = document.createElement('div');
            actions.className = 'maintenance-card-actions';
            var analyzeBtn = document.createElement('button');
            analyzeBtn.className = 'maintenance-btn';
            analyzeBtn.textContent = 'Lancer analyse';
            analyzeBtn.disabled = !diag.source_exists;
            analyzeBtn.addEventListener('click', function () {
                analyzeIndex(idx.id, analyzeBtn);
            });
            actions.appendChild(analyzeBtn);
            if (idx.status !== 'ready') {
                var retryBtn = document.createElement('button');
                retryBtn.className = 'maintenance-btn';
                retryBtn.textContent = 'Retry index';
                retryBtn.addEventListener('click', function () {
                    retryIndex(idx.id, retryBtn);
                });
                actions.appendChild(retryBtn);
            }
            body.appendChild(actions);

            card.appendChild(head);
            card.appendChild(body);
            container.appendChild(card);
        });
    }

    function setOverview(indexesPayload, diagnosticsPayload, analyzePayload) {
        var ready = indexesPayload.filter(function (idx) { return idx.status === 'ready'; }).length;
        var loading = indexesPayload.filter(function (idx) { return idx.status === 'loading'; }).length;
        var errored = indexesPayload.filter(function (idx) { return idx.status === 'error'; }).length;

        document.getElementById('maintenance-ready-count').textContent = String(ready);
        document.getElementById('maintenance-loading-count').textContent = String(loading);
        document.getElementById('maintenance-error-count').textContent = String(errored);
        document.getElementById('maintenance-analyze-running').textContent = analyzePayload.running ? 'OUI' : 'NON';
        document.getElementById('maintenance-progress').textContent = String(analyzePayload.progress || 0);
        document.getElementById('maintenance-emails-found').textContent = String(analyzePayload.emails_found || 0);
        document.getElementById('maintenance-phones-found').textContent = String(analyzePayload.phones_found || 0);
        document.getElementById('maintenance-last-run').textContent = analyzePayload.last_run || '-';
        document.getElementById('maintenance-cache-dir').textContent = diagnosticsPayload.cache_dir || 'cache';
        document.getElementById('maintenance-index-count').textContent = indexesPayload.length + ' index';
    }

    function updateAnalyzeOverview(analyzePayload) {
        analyzePayload = analyzePayload || createAnalyzeFallback();
        document.getElementById('maintenance-analyze-running').textContent = analyzePayload.running ? 'OUI' : 'NON';
        document.getElementById('maintenance-progress').textContent = String(analyzePayload.progress || 0);
        document.getElementById('maintenance-emails-found').textContent = String(analyzePayload.emails_found || 0);
        document.getElementById('maintenance-phones-found').textContent = String(analyzePayload.phones_found || 0);
        document.getElementById('maintenance-last-run').textContent = analyzePayload.last_run || '-';
    }

    function createDiagnosticsFallback(indexesPayload) {
        return {
            cache_dir: 'cache',
            indexes: indexesPayload.map(function (idx) {
                return {
                    id: idx.id,
                    path: idx.path || '',
                    path_exists: false,
                    source: idx.source || '',
                    source_exists: false,
                    analyzer_lock: '',
                    analyzer_lock_exists: false,
                    lockfiles: []
                };
            })
        };
    }

    function createAnalyzeFallback() {
        return {
            running: false,
            progress: 0,
            emails_found: 0,
            phones_found: 0,
            last_run: null
        };
    }

    function loadMaintenance() {
        Promise.all([
            fetchSection('indexes', API_BASE + '/api/indexes'),
            fetchSection('diagnostics', API_BASE + '/api/diagnostics')
        ]).then(function (results) {
            var indexesResult = results[0];
            var diagnosticsResult = results[1];

            if (!indexesResult.ok) {
                showToast(indexesResult.error && indexesResult.error.message || 'Erreur chargement index', 'error');
                document.getElementById('maintenance-subtitle').textContent = 'Backend indisponible';
                return;
            }

            var indexesPayload = indexesResult.data;
            var diagnosticsPayload = diagnosticsResult.ok
                ? diagnosticsResult.data
                : createDiagnosticsFallback(indexesPayload);

            setOverview(indexesPayload, diagnosticsPayload, createAnalyzeFallback());
            renderIndexes(indexesPayload, diagnosticsPayload);

            if (!diagnosticsResult.ok) {
                var degradedParts = [];
                if (!diagnosticsResult.ok) degradedParts.push('diagnostics');
                document.getElementById('maintenance-subtitle').textContent =
                    'Vue partielle - sections indisponibles: ' + degradedParts.join(', ');
            } else {
                document.getElementById('maintenance-subtitle').textContent =
                    'Derniere actualisation: ' + new Date().toLocaleTimeString('fr-FR');
            }

            if (!diagnosticsResult.ok) {
                showToast('Diagnostics indisponibles - affichage partiel', 'error');
            }

            fetchSection('analyze', API_BASE + '/api/analyze/status').then(function (analyzeResult) {
                if (analyzeResult.ok) {
                    updateAnalyzeOverview(analyzeResult.data);
                    return;
                }
                updateAnalyzeOverview(createAnalyzeFallback());
                document.getElementById('maintenance-subtitle').textContent = diagnosticsResult.ok
                    ? 'Vue partielle - statut analyse indisponible'
                    : document.getElementById('maintenance-subtitle').textContent;
                showToast('Statut analyse indisponible - affichage partiel', 'error');
            });
        }).catch(function (err) {
            showToast(err.message || 'Erreur chargement maintenance', 'error');
            document.getElementById('maintenance-subtitle').textContent = 'Backend indisponible';
        });
    }

    function init() {
        if (window.Effects) window.Effects.triggerScan();
        var refreshBtn = document.getElementById('maintenance-refresh');
        var cleanupBtn = document.getElementById('maintenance-clean-locks');
        refreshBtn.addEventListener('click', loadMaintenance);
        cleanupBtn.addEventListener('click', function () {
            cleanupAnalyzerLocks(cleanupBtn);
        });
        loadMaintenance();
        setInterval(loadMaintenance, 10000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
