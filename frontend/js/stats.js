/**
 * L'Oeil de Dieu - Statistiques
 */

(function () {
    'use strict';

    var AppUtils = window.AppUtils;
    var API_BASE = AppUtils.apiBase;
    var apiFetch = AppUtils.apiFetch;

    Chart.defaults.color = '#737373';
    Chart.defaults.borderColor = 'rgba(255, 255, 255, 0.06)';
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = 12;

    var COLORS = {
        blue: '#3b82f6',
        cyan: '#22d3ee',
        purple: '#a855f7',
        emerald: '#10b981',
        amber: '#f59e0b',
        rose: '#f43f5e',
        indigo: '#6366f1',
        teal: '#14b8a6'
    };
    var COLOR_LIST = Object.values(COLORS);

    function alphaColor(hex, alpha) {
        var r = parseInt(hex.slice(1, 3), 16);
        var g = parseInt(hex.slice(3, 5), 16);
        var b = parseInt(hex.slice(5, 7), 16);
        return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    }

    function animateCounters() {
        var counters = document.querySelectorAll('.kpi-value');
        counters.forEach(function (el) {
            var target = parseFloat(el.dataset.target);
            var format = el.dataset.format;
            var duration = 1800;
            var startTime = null;

            function step(timestamp) {
                if (!startTime) startTime = timestamp;
                var progress = Math.min((timestamp - startTime) / duration, 1);
                var eased = 1 - Math.pow(1 - progress, 3);
                var current = target * eased;

                if (format === 'size') {
                    var sizeText = el.dataset.sizetext;
                    if (sizeText) {
                        var numPart = parseFloat(sizeText);
                        var unitPart = sizeText.replace(/[\d.]+/, '').trim();
                        var renderedSize = numPart * eased;
                        if (renderedSize === 0) {
                            el.textContent = '0 ' + unitPart;
                        } else {
                            el.textContent = renderedSize.toFixed(1) + ' ' + unitPart;
                        }
                    } else {
                        el.textContent = current.toFixed(1) + ' Go';
                    }
                } else {
                    el.textContent = Math.floor(current).toLocaleString('fr-FR');
                }

                if (progress < 1) {
                    requestAnimationFrame(step);
                }
            }

            requestAnimationFrame(step);
        });
    }

    function resetKpis() {
        var kpis = document.querySelectorAll('.kpi-value');
        if (kpis[0]) {
            kpis[0].dataset.target = 0;
            kpis[0].textContent = '0';
        }
        if (kpis[1]) {
            kpis[1].dataset.target = 0;
            kpis[1].textContent = '0';
        }
        if (kpis[2]) {
            kpis[2].dataset.target = 0;
            kpis[2].textContent = '0';
        }
        if (kpis[3]) {
            kpis[3].dataset.target = 0;
            kpis[3].dataset.sizetext = '0 Go';
            kpis[3].textContent = '0 Go';
        }
    }

    function setEmptyAnalysisState(message) {
        var subtitle = document.getElementById('stats-subtitle');
        if (subtitle) subtitle.textContent = message;
        resetKpis();
        destroyCharts();
    }

    function createDomainsChart(domains) {
        var top = domains.slice(0, 10);
        var ctx = document.getElementById('chart-domains').getContext('2d');
        return new Chart(ctx, {
            type: 'bar',
            data: {
                labels: top.map(function (d) { return d.domain; }),
                datasets: [{
                    data: top.map(function (d) { return d.count; }),
                    backgroundColor: top.map(function (_, i) { return alphaColor(COLOR_LIST[i % COLOR_LIST.length], 0.7); }),
                    borderColor: top.map(function (_, i) { return COLOR_LIST[i % COLOR_LIST.length]; }),
                    borderWidth: 1,
                    borderRadius: 6,
                    borderSkipped: false
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 1200, easing: 'easeOutQuart' },
                plugins: { legend: { display: false } }
            }
        });
    }

    function createProPersoChart(emails) {
        var persoCount = emails.perso_count || 0;
        var proCount = emails.pro_count || 0;
        if (persoCount === 0 && proCount === 0) return;

        var ctx = document.getElementById('chart-pro-perso').getContext('2d');
        return new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['Perso', 'Pro / Autre'],
                datasets: [{
                    data: [persoCount, proCount],
                    backgroundColor: [alphaColor(COLORS.blue, 0.75), alphaColor(COLORS.amber, 0.75)],
                    borderColor: '#0a0a0a',
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '65%'
            }
        });
    }

    function createTldChart(tldDistribution) {
        if (!tldDistribution || tldDistribution.length === 0) return;
        var top5 = tldDistribution.slice(0, 5);
        var restSum = tldDistribution.slice(5).reduce(function (sum, e) { return sum + e.count; }, 0);
        var labels = top5.map(function (e) { return e.tld; });
        var values = top5.map(function (e) { return e.count; });
        if (restSum > 0) {
            labels.push('Autres');
            values.push(restSum);
        }

        var ctx = document.getElementById('chart-tld').getContext('2d');
        return new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: COLOR_LIST.map(function (c) { return alphaColor(c, 0.75); }),
                    borderColor: '#0a0a0a',
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '65%'
            }
        });
    }

    function createPhonesChart(phones) {
        var prefixCounts = {};
        var prefixLabels = {
            '01': '01 (Ile-de-France)',
            '02': '02 (Nord-Ouest)',
            '03': '03 (Nord-Est)',
            '04': '04 (Sud-Est)',
            '05': '05 (Sud-Ouest)',
            '06': '06 (Mobile)',
            '07': '07 (Mobile)',
            '09': '09 (VoIP/Box)'
        };

        phones.forEach(function (p) {
            var num = p.phone.replace(/^\+33/, '0');
            if (!num.startsWith('0')) return;
            var prefix = num.substring(0, 2);
            prefixCounts[prefix] = (prefixCounts[prefix] || 0) + p.count;
        });

        var sorted = Object.entries(prefixCounts).sort(function (a, b) { return b[1] - a[1]; });
        var labels = sorted.map(function (e) { return prefixLabels[e[0]] || e[0]; });
        var values = sorted.map(function (e) { return e[1]; });

        var ctx = document.getElementById('chart-phones').getContext('2d');
        return new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: values.map(function (_, i) { return alphaColor(COLOR_LIST[i % COLOR_LIST.length], 0.7); }),
                    borderColor: values.map(function (_, i) { return COLOR_LIST[i % COLOR_LIST.length]; }),
                    borderWidth: 1,
                    borderRadius: 6,
                    borderSkipped: false
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 1200, easing: 'easeOutQuart' },
                plugins: { legend: { display: false } }
            }
        });
    }

    function createBars(containerId, items, labelKey, valueKey, valueLabel) {
        var container = document.getElementById(containerId);
        if (!items || items.length === 0) {
            var p = document.createElement('p');
            p.style.color = '#737373';
            p.textContent = 'Aucune donnee - lancez une analyse';
            container.appendChild(p);
            return;
        }

        var maxVal = items[0][valueKey];
        items.forEach(function (item, i) {
            var pct = ((item[valueKey] / maxVal) * 100).toFixed(1);
            var color = COLOR_LIST[i % COLOR_LIST.length];
            var row = document.createElement('div');
            row.className = 'source-bar-row';
            row.style.animationDelay = (0.6 + i * 0.06) + 's';

            var info = document.createElement('div');
            info.className = 'source-bar-info';
            var nameSpan = document.createElement('span');
            nameSpan.className = 'source-bar-name';
            nameSpan.textContent = item[labelKey];
            var metaSpan = document.createElement('span');
            metaSpan.className = 'source-bar-meta';
            metaSpan.textContent = item[valueKey].toLocaleString('fr-FR') + ' ' + valueLabel;
            info.appendChild(nameSpan);
            info.appendChild(metaSpan);

            var track = document.createElement('div');
            track.className = 'source-bar-track';
            var fill = document.createElement('div');
            fill.className = 'source-bar-fill';
            fill.style.width = '0%';
            fill.style.background = color;
            fill.style.setProperty('--target-width', pct + '%');
            track.appendChild(fill);

            row.appendChild(info);
            row.appendChild(track);
            container.appendChild(row);
        });

        setTimeout(function () {
            var fills = container.querySelectorAll('.source-bar-fill');
            fills.forEach(function (fill) {
                fill.style.width = fill.style.getPropertyValue('--target-width');
            });
        }, 300);
    }

    // References aux charts pour pouvoir les detruire au rechargement
    var activeCharts = [];

    function destroyCharts() {
        activeCharts.forEach(function (c) { c.destroy(); });
        activeCharts = [];
        ['source-bars', 'top-emails-bars'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.innerHTML = '';
        });
        // Recreer les canvas (Chart.js ne reutilise pas un canvas detruit)
        ['chart-domains', 'chart-pro-perso', 'chart-tld', 'chart-phones'].forEach(function (id) {
            var old = document.getElementById(id);
            if (old) {
                var parent = old.parentNode;
                var canvas = document.createElement('canvas');
                canvas.id = id;
                parent.replaceChild(canvas, old);
            }
        });
    }

    var currentIndexId = 'all';
    var _statsBackendRetryTimer = null;
    var _indexSizeRetryTimer = null;
    var _indexSizeRetryUrl = '';
    var statsState = {
        indexesLoadedOnce: false,
        backendDegraded: false,
        readyIndexes: [],
        lastIndexList: [],
        noReadyDetail: 'Aucun index pret'
    };

    function getSelectedReadyIndex() {
        for (var i = 0; i < statsState.readyIndexes.length; i++) {
            if (statsState.readyIndexes[i].id === currentIndexId) {
                return statsState.readyIndexes[i];
            }
        }
        return null;
    }

    function setStatsAvailabilityState(mode, detail) {
        var systemText = 'Connexion backend...';
        var indexText = detail || 'Aucun index pret';
        var disableSelect = false;

        if (mode === 'ready') {
            systemText = 'Systeme actif';
            indexText = detail || 'Index operationnel';
        } else if (mode === 'index_unavailable') {
            systemText = 'Backend joignable';
            indexText = detail || 'Index indisponible';
        } else if (mode === 'no_ready') {
            systemText = 'Backend joignable';
            indexText = detail || 'Aucun index pret';
            disableSelect = true;
        } else if (mode === 'unstable') {
            systemText = 'Backend instable';
            indexText = detail || 'Etat potentiellement perime';
            disableSelect = true;
        } else if (mode === 'connecting') {
            systemText = 'Connexion backend...';
            indexText = detail || 'Verification index...';
            disableSelect = true;
        }

        var systemEl = document.getElementById('stats-system-status');
        var indexEl = document.getElementById('stats-index-status');
        var headerEl = document.getElementById('stats-header-status');
        var select = document.getElementById('stats-index-select');

        if (systemEl) systemEl.textContent = systemText;
        if (indexEl) indexEl.textContent = indexText;
        if (headerEl) headerEl.textContent = systemText;
        if (select) select.disabled = disableSelect;
    }

    function updateReadyAvailability() {
        var readyNames = statsState.readyIndexes.map(function (idx) { return idx.name || idx.id; });
        var selectedReady = getSelectedReadyIndex();

        if (currentIndexId !== 'all' && selectedReady) {
            setStatsAvailabilityState('ready', 'Disponible');
            return;
        }

        if (readyNames.length > 0) {
            setStatsAvailabilityState('ready', 'Disponibles');
            return;
        }

        setStatsAvailabilityState('no_ready', 'Aucun index pret');
    }

    function ensureStatsBackendRetry() {
        if (_statsBackendRetryTimer) return;
        _statsBackendRetryTimer = setInterval(function () {
            loadIndexSelector();
            loadStats(currentIndexId);
        }, 5000);
    }

    function clearStatsBackendRetry() {
        if (!_statsBackendRetryTimer) return;
        clearInterval(_statsBackendRetryTimer);
        _statsBackendRetryTimer = null;
    }

    function clearIndexSizeRefresh() {
        if (_indexSizeRetryTimer) clearTimeout(_indexSizeRetryTimer);
        _indexSizeRetryTimer = null;
        _indexSizeRetryUrl = '';
    }

    function scheduleIndexSizeRefresh(url) {
        if (_indexSizeRetryTimer && _indexSizeRetryUrl === url) return;
        clearIndexSizeRefresh();
        _indexSizeRetryUrl = url;
        _indexSizeRetryTimer = setTimeout(function () {
            _indexSizeRetryTimer = null;
            apiFetch(url)
                .then(function (res) {
                    if (!res.ok) throw new Error('Taille index indisponible');
                    return res.json();
                })
                .then(function (data) {
                    var sizeKpi = document.querySelectorAll('.kpi-value')[3];
                    if (sizeKpi) {
                        sizeKpi.dataset.target = (data.index_size_bytes || 0) / 1073741824;
                        sizeKpi.dataset.sizetext = data.index_size_pending ? 'Calcul...' : (data.index_size || '0 Go');
                        sizeKpi.textContent = sizeKpi.dataset.sizetext;
                    }
                    if (_indexSizeRetryUrl !== url) return;
                    if (data.index_size_pending) {
                        scheduleIndexSizeRefresh(url);
                    } else {
                        _indexSizeRetryUrl = '';
                    }
                })
                .catch(function () {
                    if (_indexSizeRetryUrl === url) _indexSizeRetryUrl = '';
                    // Le prochain chargement normal des stats retentera le calcul.
                });
        }, 2000);
    }

    function loadStats(indexId) {
        var url = API_BASE + '/api/stats';
        if (indexId) {
            url += '?index=' + encodeURIComponent(indexId);
        }
        clearIndexSizeRefresh();
        destroyCharts();

        if (statsState.indexesLoadedOnce && statsState.readyIndexes.length === 0) {
            setEmptyAnalysisState(statsState.noReadyDetail || 'Aucun index pret');
            setStatsAvailabilityState('no_ready', statsState.noReadyDetail || 'Aucun index pret');
            animateCounters();
            return;
        }

        apiFetch(url)
            .then(function (res) {
                clearStatsBackendRetry();
                if (res.status === 409) {
                    return res.json().then(function (err) {
                        var detail = err.detail || 'Index indisponible';
                        setEmptyAnalysisState(detail);
                        setStatsAvailabilityState('index_unavailable', detail);
                        animateCounters();
                        return null;
                    });
                }
                if (!res.ok) {
                    return res.json().then(function (err) { throw new Error(err.detail || 'Erreur'); });
                }
                return res.json();
            })
            .then(function (data) {
                if (!data) return;
                statsState.backendDegraded = false;
                updateReadyAvailability();
                var analysis = data.analysis || null;
                var emails = analysis && analysis.emails || {};
                var phones = analysis && analysis.phones || {};
                var kpis = document.querySelectorAll('.kpi-value');
                if (kpis[0]) kpis[0].dataset.target = data.total_docs || 0;
                if (kpis[1]) kpis[1].dataset.target = emails.unique_count || 0;
                if (kpis[2]) kpis[2].dataset.target = phones.unique_count || 0;
                if (kpis[3]) {
                    var sizeGo = (data.index_size_bytes || 0) / 1073741824;
                    kpis[3].dataset.target = sizeGo;
                    kpis[3].dataset.sizetext = data.index_size_pending ? 'Calcul...' : (data.index_size || '0 Go');
                }
                if (data.index_size_pending) scheduleIndexSizeRefresh(url);

                // Label approximatif pour les uniques en mode global
                var emailLabel = document.querySelectorAll('.kpi-label')[1];
                var phoneLabel = document.querySelectorAll('.kpi-label')[2];
                if (emailLabel) emailLabel.textContent = indexId === 'all' ? 'Emails uniques (approx.)' : 'Emails uniques';
                if (phoneLabel) phoneLabel.textContent = indexId === 'all' ? 'Telephones uniques (approx.)' : 'Telephones uniques';

                animateCounters();

                if (analysis) {
                    var subtitle = 'Analyse du ' + analysis.scan_date + ' - ' + analysis.total_files + ' fichiers scannes en ' + analysis.scan_duration_seconds + 's';
                    if (indexId === 'all') {
                        subtitle = 'Derniere analyse: ' + analysis.scan_date + ' - ' + analysis.total_files + ' fichiers au total';
                    }
                    document.getElementById('stats-subtitle').textContent = subtitle;
                    var topDomains = emails.top_domains || [];
                    var topPhones = phones.top_phones || [];
                    var topEmails = emails.top_emails || [];
                    var tldDist = emails.tld_distribution || [];

                    if (topDomains.length > 0) {
                        var c1 = createDomainsChart(topDomains);
                        if (c1) activeCharts.push(c1);
                        var c2 = createProPersoChart(emails);
                        if (c2) activeCharts.push(c2);
                        var c3 = createTldChart(tldDist);
                        if (c3) activeCharts.push(c3);
                    }
                    if (topPhones.length > 0) {
                        var c4 = createPhonesChart(topPhones);
                        if (c4) activeCharts.push(c4);
                    }
                    createBars('source-bars', analysis.sources, 'name', 'files', 'fichiers');
                    createBars('top-emails-bars', topEmails.slice(0, 20), 'email', 'count', 'occurrences');
                } else {
                    document.getElementById('stats-subtitle').textContent = 'Lancez une analyse via /api/analyze pour voir les statistiques detaillees';
                    ['source-bars', 'top-emails-bars'].forEach(function (id) {
                        var container = document.getElementById(id);
                        var p = document.createElement('p');
                        p.style.color = '#737373';
                        p.textContent = 'Aucune analyse disponible';
                        container.appendChild(p);
                    });
                }
            })
            .catch(function (err) {
                console.error('Erreur chargement stats:', err);
                setEmptyAnalysisState('Backend ou index temporairement indisponible');
                setStatsAvailabilityState(
                    statsState.indexesLoadedOnce ? 'unstable' : 'connecting',
                    statsState.indexesLoadedOnce
                        ? 'Etat stats temporairement indisponible'
                        : 'Connexion backend...'
                );
                statsState.backendDegraded = true;
                ensureStatsBackendRetry();
                animateCounters();
            });
    }

    var _statsPollTimer = null;

    function _updateLoadingBanner(idxList) {
        var banner = document.getElementById('stats-loading-banner');
        var loadingNames = idxList
            .filter(function (i) { return i.status === 'loading'; })
            .map(function (i) { return i.name; });

        if (loadingNames.length > 0) {
            if (!banner) {
                banner = document.createElement('p');
                banner.id = 'stats-loading-banner';
                banner.style.color = '#f59e0b';
                banner.style.fontSize = '0.85rem';
                banner.style.margin = '0.5rem 0 0 0';
                var subtitle = document.getElementById('stats-subtitle');
                if (subtitle && subtitle.parentNode) {
                    subtitle.parentNode.insertBefore(banner, subtitle.nextSibling);
                }
            }
            banner.textContent = 'Index en chargement : ' + loadingNames.join(', ') + ' \u2014 donnees partielles';
        } else if (banner) {
            banner.remove();
        }
    }

    function loadIndexSelector() {
        var select = document.getElementById('stats-index-select');
        if (!select) return;

        apiFetch(API_BASE + '/api/indexes')
            .then(function (res) { return res.json(); })
            .then(function (idxList) {
                clearStatsBackendRetry();
                statsState.indexesLoadedOnce = true;
                statsState.lastIndexList = idxList.slice();
                statsState.readyIndexes = idxList.filter(function (idx) { return idx.status === 'ready'; });
                statsState.noReadyDetail = 'Aucun index pret';
                if (statsState.backendDegraded) {
                    statsState.backendDegraded = false;
                }
                var hasLoading = false;
                var prevValue = select.value;
                // Reconstruire les options (garder "Tous les index" en premier)
                select.innerHTML = '<option value="all">Tous les index</option>';
                idxList.forEach(function (idx) {
                    var opt = document.createElement('option');
                    opt.value = idx.id;
                    if (idx.status === 'loading') {
                        opt.textContent = (idx.name || idx.id) + ' (chargement...)';
                        opt.disabled = true;
                        hasLoading = true;
                    } else if (idx.status === 'error') {
                        opt.textContent = (idx.name || idx.id) + ' (erreur)';
                        opt.disabled = true;
                    } else {
                        opt.textContent = idx.name || idx.id;
                    }
                    select.appendChild(opt);
                });

                // Restaurer la selection precedente si toujours valide
                if (prevValue) {
                    select.value = prevValue;
                    // Si la valeur restauree est disabled, revenir a 'all'
                    var selectedOpt = select.options[select.selectedIndex];
                    if (selectedOpt && selectedOpt.disabled) {
                        select.value = 'all';
                        currentIndexId = 'all';
                    }
                }

                if (select.value === '' || select.value === 'all') {
                    currentIndexId = 'all';
                } else {
                    currentIndexId = select.value;
                }

                if (statsState.readyIndexes.length === 0) {
                    statsState.noReadyDetail = hasLoading ? 'Aucun index pret - chargement en cours' : 'Aucun index pret';
                    setEmptyAnalysisState(statsState.noReadyDetail);
                    setStatsAvailabilityState(
                        'no_ready',
                        statsState.noReadyDetail
                    );
                    if (hasLoading && !_statsPollTimer) {
                        _statsPollTimer = setInterval(function () {
                            loadIndexSelector();
                        }, 5000);
                    } else if (!hasLoading && _statsPollTimer) {
                        clearInterval(_statsPollTimer);
                        _statsPollTimer = null;
                    }
                    return;
                }

                _updateLoadingBanner(idxList);
                updateReadyAvailability();

                // Poll tant qu'un index est en chargement
                if (hasLoading && !_statsPollTimer) {
                    _statsPollTimer = setInterval(function () {
                        loadIndexSelector();
                    }, 5000);
                } else if (!hasLoading && _statsPollTimer) {
                    clearInterval(_statsPollTimer);
                    _statsPollTimer = null;
                    // Recharger les stats pour inclure les index fraichement prets
                        loadStats(currentIndexId);
                }
            })
            .catch(function () {
                statsState.backendDegraded = true;
                if (!statsState.indexesLoadedOnce) {
                    select.innerHTML = '<option value="all">Connexion backend...</option>';
                    setStatsAvailabilityState('connecting', 'Connexion backend...');
                } else {
                    setStatsAvailabilityState('unstable', 'Etat index potentiellement perime');
                }
                ensureStatsBackendRetry();
            });

        // Bind change une seule fois
        if (!select.dataset.bound) {
            select.addEventListener('change', function () {
                currentIndexId = this.value;
                loadStats(currentIndexId);
            });
            select.dataset.bound = 'true';
        }
    }

    function init() {
        if (window.Effects) window.Effects.triggerScan();
        setStatsAvailabilityState('connecting', 'Connexion backend...');
        loadIndexSelector();
        setTimeout(function () { loadStats(currentIndexId); }, 400);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
