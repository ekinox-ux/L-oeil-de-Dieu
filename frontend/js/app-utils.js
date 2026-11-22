(function () {
    'use strict';

    var API_BASE = (window.APP_CONFIG && window.APP_CONFIG.API_BASE) || 'http://127.0.0.1:8500';
    var DEFAULT_TIMEOUT_MS = 30000;
    var RETRY_DELAY_MS = 800;

    function wait(ms) {
        return new Promise(function (resolve) {
            setTimeout(resolve, ms);
        });
    }

    /**
     * fetch() vers le backend local.
     *
     * options.timeoutMs : delai max (defaut 30 s), non transmis a fetch().
     *
     * Un timeout n'est jamais relance: le backend a recu la requete et continue
     * de la traiter, la relancer doublerait la charge (recherches lourdes) et le
     * quota consomme. Seule une erreur reseau (TypeError: requete non aboutie)
     * declenche un unique retry, et uniquement pour un GET.
     */
    function apiFetch(url, options, attempt) {
        options = options || {};
        attempt = attempt || 1;
        var timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS;
        var controller = new AbortController();
        var timedOut = false;
        var timeoutId = setTimeout(function () {
            timedOut = true;
            controller.abort();
        }, timeoutMs);
        var fetchOptions = Object.assign({}, options);
        delete fetchOptions.timeoutMs;
        fetchOptions.headers = Object.assign({}, options.headers || {}, {
            'X-Oeil-Local-Client': '1'
        });
        fetchOptions.signal = controller.signal;

        return fetch(url, fetchOptions)
            .then(function (res) {
                clearTimeout(timeoutId);
                return res;
            })
            .catch(function (err) {
                clearTimeout(timeoutId);
                if (timedOut) {
                    var timeoutError = new Error(
                        'Le serveur ne repond pas (delai de ' + Math.round(timeoutMs / 1000) + ' s depasse).'
                    );
                    timeoutError.name = 'TimeoutError';
                    throw timeoutError;
                }
                var method = String(options.method || 'GET').toUpperCase();
                if (err && err.name === 'TypeError' && method === 'GET' && attempt < 2) {
                    return wait(RETRY_DELAY_MS).then(function () {
                        return apiFetch(url, options, attempt + 1);
                    });
                }
                throw err;
            });
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function extractCompanyName(source) {
        var parts = String(source || '').replace(/\\/g, '/').split('/');
        for (var i = parts.length - 2; i >= 0; i--) {
            if (parts[i] && parts[i] !== 'FRANCE' && parts[i] !== 'FRANCE-CSV' && parts[i].indexOf(':') === -1) {
                return parts[i];
            }
        }
        return String(source || '');
    }

    window.AppUtils = {
        apiBase: API_BASE,
        apiFetch: apiFetch,
        escapeHtml: escapeHtml,
        extractCompanyName: extractCompanyName
    };
})();
