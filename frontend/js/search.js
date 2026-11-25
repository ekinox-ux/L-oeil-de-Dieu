/**
 * L'Oeil de Dieu - Logique de recherche
 */

(function () {
    'use strict';

    var SearchApi = window.SearchApi;
    var SearchComponents = window.SearchComponents;

    var hero = document.getElementById('hero');
    var resultsView = document.getElementById('results-view');
    var searchInput = document.getElementById('search-input');
    var searchInputHeader = document.getElementById('search-input-header');
    var searchSubmit = document.getElementById('search-submit');
    var searchSubmitHeader = document.getElementById('search-submit-header');
    var btnHome = document.getElementById('btn-home');
    var resultsCount = document.getElementById('results-count');
    var resultsTime = document.getElementById('results-time');
    var resultsList = document.getElementById('results-list');
    var indexSelect = document.getElementById('index-select');
    var heroSystemStatus = document.getElementById('hero-system-status');
    var heroIndexStatus = document.getElementById('hero-index-status');
    var headerSystemStatus = document.getElementById('header-system-status');
    var LIMIT = SearchApi.defaultLimit;

    var state = {
        currentIndexId: null,
        currentQuery: null,
        currentOffset: 0,
        isSearching: false,
        indexesLoadedOnce: false,
        backendDegraded: false,
        hasKpiData: false
    };

    var toastTimer = null;
    var backendRetryTimer = null;

    function setAvailabilityState(mode, detail) {
        var systemText = 'Connexion backend...';
        var indexText = detail || 'Aucun index pret';
        var disableSearch = false;

        if (mode === 'ready') {
            systemText = 'Systeme actif';
            indexText = detail || '';
        } else if (mode === 'unstable') {
            systemText = 'Backend instable';
            indexText = detail || 'Etat potentiellement perime';
            disableSearch = true;
        } else if (mode === 'no_ready') {
            systemText = 'Backend joignable';
            indexText = detail || 'Aucun index pret';
            disableSearch = true;
        } else if (mode === 'connecting') {
            systemText = 'Connexion backend...';
            indexText = detail || 'Verification index...';
            disableSearch = true;
        }

        document.body.setAttribute('data-backend', mode);
        if (heroSystemStatus) heroSystemStatus.textContent = systemText;
        if (heroIndexStatus) heroIndexStatus.textContent = indexText;
        if (headerSystemStatus) headerSystemStatus.textContent = systemText;
        if (heroIndexStatus && heroIndexStatus.parentElement) {
            heroIndexStatus.parentElement.style.display = mode === 'ready' ? 'none' : '';
        }

        if (!state.isSearching) {
            [searchInput, searchInputHeader, indexSelect, searchSubmit, searchSubmitHeader].forEach(function (input) {
                if (!input) return;
                input.disabled = disableSearch;
            });
        }
    }

    function ensureSearchLoader(container) {
        if (!container) return null;
        var loader = container.querySelector('.search-loader');
        if (loader) {
            return loader;
        }
        loader = document.createElement('div');
        loader.className = 'search-loader';
        loader.setAttribute('aria-hidden', 'true');
        loader.innerHTML = '<span class="search-loader-ring"></span><span class="search-loader-core"></span>';
        container.appendChild(loader);
        return loader;
    }

    function setSearchLoading(isLoading) {
        state.isSearching = isLoading;
        // L'oeil (iris.js) passe en mode balayage tant que la recherche tourne.
        document.body.classList.toggle('is-scanning', isLoading);

        [searchInput, searchInputHeader].forEach(function (input) {
            if (!input) return;
            input.disabled = isLoading;
            input.setAttribute('aria-busy', isLoading ? 'true' : 'false');
        });

        [searchSubmit, searchSubmitHeader].forEach(function (button) {
            if (!button) return;
            button.disabled = isLoading;
        });

        if (indexSelect) {
            indexSelect.disabled = isLoading;
        }

        [
            document.getElementById('search-bar-hero'),
            document.getElementById('search-bar-header')
        ].forEach(function (bar) {
            if (!bar) return;
            ensureSearchLoader(bar);
            bar.classList.toggle('search-loading', isLoading);
        });

        if (isLoading) {
            resultsTime.textContent = 'Recherche en cours...';
        }
    }

    function ensureToast() {
        var toast = document.getElementById('app-toast');
        if (toast) {
            return toast;
        }
        toast = document.createElement('div');
        toast.id = 'app-toast';
        toast.className = 'app-toast';
        document.body.appendChild(toast);
        return toast;
    }

    function showToast(message, type) {
        var toast = ensureToast();
        toast.className = 'app-toast app-toast-' + (type || 'info');
        toast.textContent = message;
        toast.classList.add('visible');
        if (toastTimer) {
            clearTimeout(toastTimer);
        }
        toastTimer = setTimeout(function () {
            toast.classList.remove('visible');
        }, 4000);
    }

    // Compteur monotone: chaque appel animateHeroKpi incremente _kpiGeneration.
    // Les callbacks (retry inclus) ignorent leur resultat si une generation plus
    // recente a demarre entre-temps.
    var _kpiGeneration = 0;

    function _setKpiLoading() {
        var kpiContainer = document.querySelector('.hero-kpi');
        if (kpiContainer) kpiContainer.classList.add('hero-kpi-loading');
        if (state.hasKpiData) {
            return;
        }
        var kpiEls = document.querySelectorAll('.hero-kpi-value');
        kpiEls.forEach(function (el) {
            el.textContent = '--';
            el.dataset.target = '0';
            el.dataset.skip = 'true';
        });
    }

    function _setKpiFailed() {
        var kpiContainer = document.querySelector('.hero-kpi');
        if (kpiContainer) kpiContainer.classList.remove('hero-kpi-loading');
        if (state.hasKpiData) {
            return;
        }
        var kpiEls = document.querySelectorAll('.hero-kpi-value');
        kpiEls.forEach(function (el) {
            el.textContent = '--';
            el.dataset.skip = 'true';
        });
    }

    function _applyKpiData(data) {
        var kpiContainer = document.querySelector('.hero-kpi');
        if (kpiContainer) kpiContainer.classList.remove('hero-kpi-loading');
        state.hasKpiData = true;
        var kpiEls = document.querySelectorAll('.hero-kpi-value');
        if (kpiEls[0]) { kpiEls[0].dataset.target = data.total_docs || 0; kpiEls[0].dataset.skip = ''; }
        if (kpiEls[1]) { kpiEls[1].dataset.target = data.num_segments || 0; kpiEls[1].dataset.skip = ''; }
        if (kpiEls[2]) {
            kpiEls[2].textContent = data.index_size || '0';
            kpiEls[2].dataset.skip = 'true';
        }
        animateCounters();
    }

    function animateHeroKpi() {
        var gen = ++_kpiGeneration;
        _setKpiLoading();
        SearchApi.loadStats(state.currentIndexId)
            .then(function (data) {
                if (gen !== _kpiGeneration) return;
                _applyKpiData(data);
            })
            .catch(function () {
                if (gen !== _kpiGeneration) return;
                setTimeout(function () {
                    if (gen !== _kpiGeneration) return;
                    SearchApi.loadStats(state.currentIndexId)
                        .then(function (data) {
                            if (gen !== _kpiGeneration) return;
                            _applyKpiData(data);
                        })
                        .catch(function () {
                            if (gen !== _kpiGeneration) return;
                            _setKpiFailed();
                        });
                }, 3000);
            });
    }

    function ensureBackendRetry() {
        if (backendRetryTimer) return;
        backendRetryTimer = setInterval(function () {
            loadIndexes();
        }, 5000);
    }

    function clearBackendRetry() {
        if (!backendRetryTimer) return;
        clearInterval(backendRetryTimer);
        backendRetryTimer = null;
    }

    var _pollTimer = null;

    function loadIndexes() {
        SearchApi.loadIndexes()
            .then(function (idxList) {
                var readyIndexes = idxList.filter(function (idx) { return idx.status === 'ready'; });
                if (state.indexesLoadedOnce && readyIndexes.length === 0 && state.currentIndexId) {
                    if (!state.backendDegraded) {
                        showToast('Backend instable. Recherche desactivee jusqu au retour d un index pret.', 'warning');
                        state.backendDegraded = true;
                    }
                    setAvailabilityState('no_ready', 'Aucun index pret - dernier affichage stale');
                    ensureBackendRetry();
                    return;
                }
                if (state.backendDegraded) {
                    showToast('Connexion backend retablie', 'info');
                    state.backendDegraded = false;
                }
                clearBackendRetry();
                var hasLoading = false;
                var previousId = state.currentIndexId;
                indexSelect.innerHTML = '';
                state.currentIndexId = null;
                state.indexesLoadedOnce = true;

                idxList.forEach(function (idx) {
                    var opt = document.createElement('option');
                    opt.value = idx.id;
                    if (idx.status === 'loading') {
                        opt.textContent = idx.name + ' (chargement...)';
                        opt.disabled = true;
                        hasLoading = true;
                    } else if (idx.status === 'error') {
                        opt.textContent = idx.name + ' (erreur)';
                        opt.disabled = true;
                    } else {
                        opt.textContent = idx.name;
                    }
                    indexSelect.appendChild(opt);
                });

                // Selectionner le premier index ready (ou restaurer la selection precedente)
                for (var j = 0; j < idxList.length; j++) {
                    if (idxList[j].status === 'ready') {
                        if (previousId && idxList[j].id === previousId) {
                            state.currentIndexId = previousId;
                            break;
                        }
                        if (!state.currentIndexId) {
                            state.currentIndexId = idxList[j].id;
                        }
                    }
                }
                if (state.currentIndexId) {
                    indexSelect.value = state.currentIndexId;
                    setAvailabilityState('ready', '');
                } else if (readyIndexes.length === 0) {
                    setAvailabilityState('no_ready', 'Aucun index pret');
                } else {
                    setAvailabilityState('ready');
                }

                if (idxList.length <= 1) {
                    indexSelect.parentElement.style.display = idxList.length === 0 ? 'none' : '';
                }
                animateHeroKpi();

                // Poll tant qu'un index est en chargement
                if (hasLoading && !_pollTimer) {
                    _pollTimer = setInterval(function () {
                        loadIndexes();
                    }, 5000);
                } else if (!hasLoading && _pollTimer) {
                    clearInterval(_pollTimer);
                    _pollTimer = null;
                }
            })
            .catch(function () {
                if (!state.backendDegraded) {
                    showToast(
                        state.indexesLoadedOnce
                            ? 'Backend temporairement indisponible. Etat affiche potentiellement perime.'
                            : 'Connexion backend en cours...',
                        state.indexesLoadedOnce ? 'warning' : 'error'
                    );
                    state.backendDegraded = true;
                }
                if (!state.indexesLoadedOnce) {
                    state.currentIndexId = null;
                    indexSelect.innerHTML = '<option value="">Connexion backend...</option>';
                    _setKpiFailed();
                    setAvailabilityState('connecting', 'Connexion backend...');
                } else {
                    setAvailabilityState('unstable', 'Etat affiche potentiellement perime');
                }
                ensureBackendRetry();
            });
    }

    function animateCounters() {
        var kpis = document.querySelectorAll('.hero-kpi-value');
        kpis.forEach(function (el) {
            if (el.dataset.skip === 'true') return;
            var target = parseInt(el.dataset.target, 10);
            if (isNaN(target) || target === 0) return;
            var duration = 1500;
            var startTime = null;

            function step(timestamp) {
                if (!startTime) startTime = timestamp;
                var progress = Math.min((timestamp - startTime) / duration, 1);
                var eased = 1 - Math.pow(1 - progress, 3);
                el.textContent = Math.floor(target * eased).toLocaleString('fr-FR');
                if (progress < 1) requestAnimationFrame(step);
            }

            requestAnimationFrame(step);
        });
    }

    function renderPagination(data, offset) {
        var existing = document.getElementById('pagination');
        if (existing) existing.remove();
        var totalPages = data.total_pages || Math.ceil(data.total / LIMIT);
        if (totalPages <= 1) return;

        var currentPage = Math.floor(offset / LIMIT) + 1;
        var hasMore = data.has_more !== undefined ? data.has_more : (offset + LIMIT < data.total);
        var nav = document.createElement('div');
        nav.id = 'pagination';
        nav.className = 'pagination';

        var btnPrev = document.createElement('button');
        btnPrev.className = 'pagination-btn';
        btnPrev.textContent = '<- Precedent';
        btnPrev.disabled = offset === 0;
        btnPrev.addEventListener('click', function () {
            performSearch(state.currentQuery, Math.max(0, offset - LIMIT));
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });

        var info = document.createElement('span');
        info.className = 'pagination-info';
        info.textContent = 'Page ' + currentPage + ' / ' + totalPages;

        var btnNext = document.createElement('button');
        btnNext.className = 'pagination-btn';
        btnNext.textContent = 'Suivant ->';
        btnNext.disabled = !hasMore;
        btnNext.addEventListener('click', function () {
            performSearch(state.currentQuery, offset + LIMIT);
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });

        nav.appendChild(btnPrev);
        nav.appendChild(info);
        nav.appendChild(btnNext);
        document.getElementById('results-main').appendChild(nav);
    }

    function renderResults(query, data, offset) {
        resultsList.innerHTML = '';
        data.hits.forEach(function (hit, i) {
            resultsList.appendChild(SearchComponents.renderCard(hit, i, query));
        });

        var page = Math.floor(offset / LIMIT) + 1;
        var totalPages = data.total_pages || Math.ceil(data.total / LIMIT);
        var label = data.total === 1 ? ' resultat' : ' resultats';
        resultsCount.textContent = data.total + label + (totalPages > 1 ? ' - page ' + page + '/' + totalPages : '');
        resultsTime.textContent = data._elapsed || '';
        searchInputHeader.value = query;
        renderPagination(data, offset);
    }

    function showResults(query, data, offset) {
        if (window.Effects) window.Effects.triggerScan();

        setTimeout(function () {
            hero.className = 'state-hidden';
            resultsView.className = 'state-active';
            renderResults(query, data, offset);
            searchInputHeader.focus();
        }, 350);
    }

    function showHero() {
        resultsView.className = 'state-hidden';
        hero.className = 'state-active';
        searchInput.value = '';
        setSearchLoading(false);
        searchInput.focus();
    }

    function performSearch(query, offset) {
        if (!query.trim() || state.isSearching) return;
        if (offset === undefined) offset = 0;

        setSearchLoading(true);
        state.currentQuery = query;
        state.currentOffset = offset;

        var startTime = performance.now();
        SearchApi.search(query, offset, state.currentIndexId)
            .then(function (data) {
                var elapsed = ((performance.now() - startTime) / 1000).toFixed(3);
                data._elapsed = elapsed + 's';
                setSearchLoading(false);
                showResults(query, data, offset);
            })
            .catch(function (err) {
                setSearchLoading(false);
                if (err.name === 'TimeoutError') {
                    showToast('Timeout: ' + err.message, 'error');
                    loadIndexes();
                    return;
                }
                if (err.status === 409) {
                    showToast(err.message, 'warning');
                    loadIndexes();
                    return;
                }
                showToast('Erreur: ' + err.message, 'error');
                loadIndexes();
            });
    }

    function bindEvents() {
        searchInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                performSearch(this.value);
            }
        });

        searchInputHeader.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                performSearch(this.value);
            }
        });

        [[searchSubmit, searchInput], [searchSubmitHeader, searchInputHeader]].forEach(function (pair) {
            var button = pair[0];
            var input = pair[1];
            if (!button || !input) return;
            button.addEventListener('click', function () {
                if (input.disabled) return;
                performSearch(input.value);
            });
        });

        btnHome.addEventListener('click', function (e) {
            e.preventDefault();
            showHero();
        });

        indexSelect.addEventListener('change', function () {
            state.currentIndexId = this.value;
            animateHeroKpi();
        });
    }

    function init() {
        bindEvents();
        setSearchLoading(false);
        setAvailabilityState('connecting', 'Connexion backend...');
        searchInput.focus();
        loadIndexes();
    }

    init();
})();
