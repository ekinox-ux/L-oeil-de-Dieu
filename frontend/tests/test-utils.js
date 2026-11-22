/**
 * Tests unitaires pour les fonctions utilitaires frontend.
 * Execute avec: node frontend/tests/test-utils.js
 *
 * Charge les fichiers de production (app-utils.js, search-api.js) via leurs
 * IIFEs au lieu de re-implementer les fonctions localement.
 */

'use strict';

var fs = require('fs');
var path = require('path');
var vm = require('vm');

var passed = 0;
var failed = 0;

function assert(condition, message) {
    if (condition) {
        passed++;
    } else {
        failed++;
        console.error('FAIL: ' + message);
    }
}

function assertEqual(actual, expected, message) {
    if (actual === expected) {
        passed++;
    } else {
        failed++;
        console.error('FAIL: ' + message + ' — got ' + JSON.stringify(actual) + ', expected ' + JSON.stringify(expected));
    }
}

// ---- Minimal DOM/browser shims for Node.js ----

function createMinimalDocument() {
    function Element(tag) {
        this.tagName = tag.toUpperCase();
        this._textContent = '';
    }
    Object.defineProperty(Element.prototype, 'textContent', {
        get: function () { return this._textContent; },
        set: function (v) {
            this._textContent = v == null ? '' : String(v);
            // Simulate innerHTML escaping (matches browser behavior)
            this.innerHTML = this._textContent
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }
    });
    Element.prototype.innerHTML = '';

    return {
        createElement: function (tag) { return new Element(tag); }
    };
}

var mockWindow = {
    APP_CONFIG: { API_BASE: 'http://localhost:8500' },
    location: { protocol: 'http:', hostname: 'localhost' }
};

var mockDocument = createMinimalDocument();

// ---- Load production files in a shared context ----

var jsDir = path.join(__dirname, '..', 'js');

var context = vm.createContext({
    window: mockWindow,
    document: mockDocument,
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    fetch: function () { return Promise.reject(new Error('fetch not available in tests')); },
    AbortController: function () {
        this.signal = {};
        this.abort = function () {};
    },
    encodeURIComponent: encodeURIComponent
});

// Load config.js first (sets window.APP_CONFIG)
var configSrc = fs.readFileSync(path.join(jsDir, 'config.js'), 'utf-8');
vm.runInContext(configSrc, context, { filename: 'config.js' });

// Load app-utils.js (creates window.AppUtils)
var appUtilsSrc = fs.readFileSync(path.join(jsDir, 'app-utils.js'), 'utf-8');
vm.runInContext(appUtilsSrc, context, { filename: 'app-utils.js' });

// Load search-api.js (creates window.SearchApi, uses window.AppUtils)
var searchApiSrc = fs.readFileSync(path.join(jsDir, 'search-api.js'), 'utf-8');
vm.runInContext(searchApiSrc, context, { filename: 'search-api.js' });

var AppUtils = context.window.AppUtils;
var SearchApi = context.window.SearchApi;

assert(AppUtils !== undefined, 'AppUtils loaded from production code');
assert(SearchApi !== undefined, 'SearchApi loaded from production code');

// ---- extractCompanyName (production: AppUtils.extractCompanyName) ----

assertEqual(AppUtils.extractCompanyName('C:\\data\\ACME\\file.jsonl'), 'ACME', 'extractCompanyName: backslash path');
assertEqual(AppUtils.extractCompanyName('C:/data/Corp/row.jsonl'), 'Corp', 'extractCompanyName: forward slash path');
assertEqual(AppUtils.extractCompanyName('/mnt/FRANCE/ACME/file.jsonl'), 'ACME', 'extractCompanyName: skips FRANCE');
assertEqual(AppUtils.extractCompanyName('/mnt/FRANCE-CSV/ACME/file.jsonl'), 'ACME', 'extractCompanyName: skips FRANCE-CSV');
assertEqual(AppUtils.extractCompanyName(''), '', 'extractCompanyName: empty string');
assertEqual(AppUtils.extractCompanyName(null), '', 'extractCompanyName: null');
assertEqual(AppUtils.extractCompanyName('file.jsonl'), 'file.jsonl', 'extractCompanyName: no directory');

// ---- escapeHtml (production: AppUtils.escapeHtml, uses DOM shim) ----

assertEqual(AppUtils.escapeHtml('<script>'), '&lt;script&gt;', 'escapeHtml: tags');
assertEqual(AppUtils.escapeHtml('a&b'), 'a&amp;b', 'escapeHtml: ampersand');
assertEqual(AppUtils.escapeHtml(null), '', 'escapeHtml: null');
assertEqual(AppUtils.escapeHtml('hello'), 'hello', 'escapeHtml: no special chars');
assertEqual(AppUtils.escapeHtml('"quoted"'), '&quot;quoted&quot;', 'escapeHtml: double quotes');

// ---- apiBase (production: AppUtils.apiBase from APP_CONFIG) ----

assertEqual(AppUtils.apiBase, 'http://localhost:8500', 'apiBase: reads from APP_CONFIG');

// ---- SearchApi.defaultLimit ----

assertEqual(SearchApi.defaultLimit, 20, 'SearchApi.defaultLimit is 20');

// ---- buildSearchUrl (tested via internal behavior — construct URL and verify) ----
// SearchApi doesn't expose buildSearchUrl directly, but search() calls fetchJson(buildSearchUrl(...))
// We can verify URL construction by intercepting fetch calls.

var capturedUrls = [];
var capturedOptions = [];
var origFetch = context.fetch;
context.fetch = function (url, options) {
    capturedUrls.push(url);
    capturedOptions.push(options || {});
    return Promise.resolve({
        ok: true,
        json: function () {
            return Promise.resolve({ query: 'test', total: 0, hits: [], limit: 20, offset: 0, has_more: false, total_pages: 1 });
        }
    });
};

// ---- apiFetch: timeout sans retry, erreur reseau avec un seul retry ----

function runTimeoutAndRetryTests(done) {
    var originalAbortController = context.AbortController;
    var calls = 0;
    context.AbortController = function () {
        var listeners = [];
        this.signal = { addEventListener: function (_type, cb) { listeners.push(cb); } };
        this.abort = function () { listeners.forEach(function (cb) { cb(); }); };
    };
    context.fetch = function (_url, options) {
        calls++;
        return new Promise(function (_resolve, reject) {
            options.signal.addEventListener('abort', function () {
                var abortError = new Error('aborted');
                abortError.name = 'AbortError';
                reject(abortError);
            });
        });
    };

    AppUtils.apiFetch('http://localhost:8500/api/search?q=slow', { timeoutMs: 20 })
        .then(function () {
            assert(false, 'apiFetch timeout: must reject');
        }, function (err) {
            assertEqual(err.name, 'TimeoutError', 'apiFetch timeout: rejects with TimeoutError');
            assertEqual(calls, 1, 'apiFetch timeout: slow request is never sent twice');
            calls = 0;
            context.fetch = function () {
                calls++;
                return Promise.reject(new TypeError('Failed to fetch'));
            };
            return AppUtils.apiFetch('http://localhost:8500/api/indexes').then(function () {
                assert(false, 'apiFetch network error: must reject');
            }, function (networkErr) {
                assertEqual(networkErr.name, 'TypeError', 'apiFetch network error: original error kept');
                assertEqual(calls, 2, 'apiFetch network error: one retry for GET');
            });
        })
        .then(function () {
            calls = 0;
            return AppUtils.apiFetch('http://localhost:8500/api/analyze', { method: 'POST' }).then(function () {
                assert(false, 'apiFetch POST network error: must reject');
            }, function () {
                assertEqual(calls, 1, 'apiFetch network error: POST is never retried');
            });
        })
        .then(function () {
            context.AbortController = originalAbortController;
            context.fetch = origFetch;
            done();
        });
}

// Test basic search URL
capturedUrls = [];
SearchApi.search('john doe', 0, null);
// fetch is called asynchronously via the IIFE chain, we need to let the microtask run
setTimeout(function () {
    assertEqual(capturedUrls.length, 1, 'search triggers one fetch call');
    var url = capturedUrls[0];
    assert(url.indexOf('/api/search?q=john%20doe') !== -1, 'buildSearchUrl: encodes query');
    assert(url.indexOf('&limit=20') !== -1, 'buildSearchUrl: includes limit');
    assert(url.indexOf('&offset=0') !== -1, 'buildSearchUrl: includes offset');
    assert(url.indexOf('&context=2500') !== -1, 'buildSearchUrl: includes context');
    assert(url.indexOf('&index=') === -1, 'buildSearchUrl: no index param when null');
    assertEqual(capturedOptions[0].headers['X-Oeil-Local-Client'], '1', 'apiFetch: identifies official local frontend');
    assert(capturedOptions[0].timeoutMs === undefined, 'apiFetch: timeoutMs is not forwarded to fetch');
    assertEqual(SearchApi.searchTimeoutMs, 180000, 'SearchApi: search timeout aligned with the bot (180 s)');

    // Test with index
    capturedUrls = [];
    SearchApi.search('test', 20, 'france');
    setTimeout(function () {
        var url2 = capturedUrls[0];
        assert(url2.indexOf('&offset=20') !== -1, 'buildSearchUrl: offset=20');
        assert(url2.indexOf('&index=france') !== -1, 'buildSearchUrl: includes index param');

        // Test email encoding
        capturedUrls = [];
        SearchApi.search('a@b.com', 0, null);
        setTimeout(function () {
            var url3 = capturedUrls[0];
            assert(url3.indexOf('q=a%40b.com') !== -1, 'buildSearchUrl: encodes email @');

            // Restore fetch
            context.fetch = origFetch;

            runTimeoutAndRetryTests(function () {
                // ---- Summary ----
                console.log('\n' + (passed + failed) + ' tests, ' + passed + ' passed, ' + failed + ' failed');
                if (failed > 0) {
                    process.exit(1);
                }
            });
        }, 10);
    }, 10);
}, 10);
