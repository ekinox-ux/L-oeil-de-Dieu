(function () {
    'use strict';

    var AppUtils = window.AppUtils;
    var apiFetch = AppUtils.apiFetch;
    var API_BASE = AppUtils.apiBase;
    var LIMIT = 20;
    var CONTEXT_SIZE = 2500;
    // Alignes sur le bot: une recherche sur un gros index peut durer plusieurs
    // minutes, la verification des fuites interroge des API externes.
    var SEARCH_TIMEOUT_MS = 180000;
    var BREACHES_TIMEOUT_MS = 45000;

    function fetchJson(url, options) {
        return apiFetch(url, options).then(function (res) {
            if (!res.ok) {
                return res.json().then(function (err) {
                    var e = new Error(err.detail || 'Erreur serveur');
                    e.status = res.status;
                    throw e;
                });
            }
            return res.json();
        });
    }

    function buildStatsUrl(indexId) {
        var url = API_BASE + '/api/stats';
        if (indexId) {
            url += '?index=' + encodeURIComponent(indexId);
        }
        return url;
    }

    function buildIndexesUrl() {
        return API_BASE + '/api/indexes';
    }

    function buildSearchUrl(query, offset, indexId) {
        var url = API_BASE + '/api/search?q=' + encodeURIComponent(query) + '&limit=' + LIMIT + '&offset=' + offset + '&context=' + CONTEXT_SIZE;
        if (indexId) {
            url += '&index=' + encodeURIComponent(indexId);
        }
        return url;
    }

    function loadIndexes() {
        return fetchJson(buildIndexesUrl());
    }

    function loadStats(indexId) {
        return fetchJson(buildStatsUrl(indexId));
    }

    function search(query, offset, indexId) {
        return fetchJson(buildSearchUrl(query, offset, indexId), { timeoutMs: SEARCH_TIMEOUT_MS });
    }

    function fetchBreaches(email) {
        var url = API_BASE + '/api/breaches?email=' + encodeURIComponent(email);
        return apiFetch(url, { timeoutMs: BREACHES_TIMEOUT_MS }).then(function (res) {
            if (res.status === 429) {
                throw new Error('Rate limit atteint. Reessayez dans quelques secondes.');
            }
            if (!res.ok) {
                return res.json().then(function (err) { throw new Error(err.detail || 'Erreur'); });
            }
            return res.json();
        });
    }

    window.SearchApi = {
        defaultLimit: LIMIT,
        searchTimeoutMs: SEARCH_TIMEOUT_MS,
        loadIndexes: loadIndexes,
        loadStats: loadStats,
        search: search,
        fetchBreaches: fetchBreaches
    };
})();
