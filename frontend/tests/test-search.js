/**
 * Tests comportementaux pour search.js (rendu, pagination, transitions).
 * Execute avec: node frontend/tests/test-search.js
 *
 * Charge le vrai code de production dans un DOM jsdom.
 * Utilise du polling (waitFor) au lieu de setTimeout fixes pour la robustesse.
 */

'use strict';

var fs = require('fs');
var path = require('path');
var JSDOM = require('jsdom').JSDOM;

var passed = 0;
var failed = 0;
var pendingTests = 0;

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

/**
 * Poll a condition every intervalMs until it returns true or timeoutMs is reached.
 * Calls callback() when the condition is met, or fails with the given label.
 */
function waitFor(conditionFn, label, callback, timeoutMs, intervalMs) {
    timeoutMs = timeoutMs || 3000;
    intervalMs = intervalMs || 50;
    var elapsed = 0;
    var timer = setInterval(function () {
        elapsed += intervalMs;
        if (conditionFn()) {
            clearInterval(timer);
            callback();
        } else if (elapsed >= timeoutMs) {
            clearInterval(timer);
            failed++;
            console.error('FAIL: waitFor timeout — ' + label);
            testDone();
        }
    }, intervalMs);
}

function testDone() {
    pendingTests--;
    if (pendingTests === 0) {
        console.log('\n' + (passed + failed) + ' tests, ' + passed + ' passed, ' + failed + ' failed');
        if (failed > 0) {
            process.exit(1);
        }
    }
}

// ---- Minimal HTML matching index.html structure ----

var HTML = [
    '<!DOCTYPE html><html><head></head><body>',
    '<div id="app">',
    '  <div id="hero" class="state-active">',
    '    <div class="hero-content">',
    '      <div class="search-container">',
    '        <select id="index-select"><option value="">Chargement...</option></select>',
    '        <input type="text" id="search-input" />',
    '      </div>',
    '      <div class="hero-kpi">',
    '        <div class="hero-kpi-item"><span class="hero-kpi-value" data-target="0">0</span></div>',
    '        <div class="hero-kpi-item"><span class="hero-kpi-value" data-target="0">0</span></div>',
    '        <div class="hero-kpi-item"><span class="hero-kpi-value" data-target="0" data-sizetext="">0</span></div>',
    '      </div>',
    '      <div class="hero-status">',
    '        <div class="status-chip"><span id="hero-system-status">Connexion backend...</span></div>',
    '        <div class="status-chip"><span id="hero-index-status">Aucun index pret</span></div>',
    '      </div>',
    '    </div>',
    '  </div>',
    '  <div id="results-view" class="state-hidden">',
    '    <a href="#" id="btn-home">HOME</a>',
    '    <input type="text" id="search-input-header" />',
    '    <div id="header-system-status">Connexion backend...</div>',
    '    <main id="results-main">',
    '      <div id="results-meta">',
    '        <span id="results-count"></span>',
    '        <span id="results-time"></span>',
    '      </div>',
    '      <div id="results-list"></div>',
    '    </main>',
    '  </div>',
    '</div>',
    '</body></html>'
].join('\n');

// ---- Helper: create a fresh jsdom environment and load production code ----

var jsDir = path.join(__dirname, '..', 'js');

function loadProductionCode(fetchMock) {
    var dom = new JSDOM(HTML, {
        url: 'http://localhost:3000',
        runScripts: 'dangerously',
        resources: 'usable',
        pretendToBeVisual: true,
    });
    var win = dom.window;

    if (!win.performance) {
        Object.defineProperty(win, 'performance', {
            value: { now: function () { return Date.now(); } },
            writable: true, configurable: true
        });
    }
    try { win.scrollTo = function () {}; } catch (e) {
        Object.defineProperty(win, 'scrollTo', { value: function () {}, writable: true, configurable: true });
    }

    win.fetch = fetchMock || function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    };

    if (!win.AbortController) {
        win.AbortController = function () {
            this.signal = {};
            this.abort = function () {};
        };
    }

    var files = ['config.js', 'app-utils.js', 'search-api.js', 'search-components.js'];
    files.forEach(function (f) {
        var src = fs.readFileSync(path.join(jsDir, f), 'utf-8');
        win.eval(src);
    });

    return dom;
}

function loadWithSearch(fetchMock) {
    var dom = loadProductionCode(fetchMock);
    var searchSrc = fs.readFileSync(path.join(jsDir, 'search.js'), 'utf-8');
    dom.window.eval(searchSrc);
    return dom;
}

function makeFetchMock(searchResponse) {
    return function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve([{ id: 'test', name: 'Test' }]); } });
        }
        if (url.indexOf('/api/stats') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ total_docs: 100, num_segments: 2, index_size: '1 Mo' }); } });
        }
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve(searchResponse); } });
    };
}

function triggerSearch(dom, query) {
    var doc = dom.window.document;
    var input = doc.getElementById('search-input');
    input.value = query;
    input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
}

// ---- Tests ----

// Test 1: renderResults with multiple hits
pendingTests++;
(function testRenderMultipleHits() {
    var dom = loadWithSearch(makeFetchMock({
        query: 'john', total: 45, limit: 20, offset: 0,
        has_more: true, total_pages: 3,
        hits: [
            { score: 1.5, source: 'C:/data/ACME/file.jsonl', type: 'record', snippets: [{ text: 'john doe', highlights: [[0, 4]] }] },
            { score: 1.2, source: 'C:/data/Corp/row.jsonl', type: 'record', snippets: [] },
        ]
    }));
    var doc = dom.window.document;
    triggerSearch(dom, 'john');

    waitFor(
        function () { return doc.getElementById('results-list').querySelectorAll('.result-card').length > 0; },
        'renderMultipleHits: cards appear',
        function () {
            var cards = doc.getElementById('results-list').querySelectorAll('.result-card');
            assertEqual(cards.length, 2, 'renderResults: renders 2 cards for 2 hits');

            var countEl = doc.getElementById('results-count');
            assert(countEl.textContent.indexOf('45 resultats') !== -1, 'renderResults: shows total count');
            assert(countEl.textContent.indexOf('page 1/3') !== -1, 'renderResults: shows page info');

            var pagination = doc.getElementById('pagination');
            assert(pagination !== null, 'renderPagination: pagination element created');

            var info = pagination.querySelector('.pagination-info');
            assertEqual(info.textContent, 'Page 1 / 3', 'renderPagination: shows correct page');

            var btns = pagination.querySelectorAll('.pagination-btn');
            assert(btns[0].disabled === true, 'renderPagination: prev disabled on first page');
            assert(btns[1].disabled === false, 'renderPagination: next enabled when has_more');

            var hero = doc.getElementById('hero');
            var resultsView = doc.getElementById('results-view');
            assertEqual(hero.className, 'state-hidden', 'showResults: hero hidden');
            assertEqual(resultsView.className, 'state-active', 'showResults: results visible');

            dom.window.close();
            testDone();
        }
    );
})();

// Test 2: 0 results
pendingTests++;
(function testZeroResults() {
    var dom = loadWithSearch(makeFetchMock({
        query: 'xyz', total: 0, limit: 20, offset: 0,
        has_more: false, total_pages: 1, hits: []
    }));
    var doc = dom.window.document;
    triggerSearch(dom, 'xyz');

    waitFor(
        function () { return doc.getElementById('results-view').className === 'state-active'; },
        'zeroResults: results view active',
        function () {
            var cards = doc.getElementById('results-list').querySelectorAll('.result-card');
            assertEqual(cards.length, 0, 'zeroResults: no cards rendered');

            var countEl = doc.getElementById('results-count');
            assertEqual(countEl.textContent, '0 resultats', 'zeroResults: shows 0 resultats');

            var pagination = doc.getElementById('pagination');
            assert(pagination === null, 'zeroResults: no pagination');

            dom.window.close();
            testDone();
        }
    );
})();

// Test 3: single result (singular)
pendingTests++;
(function testSingleResult() {
    var dom = loadWithSearch(makeFetchMock({
        query: 'unique', total: 1, limit: 20, offset: 0,
        has_more: false, total_pages: 1,
        hits: [{ score: 2.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] }]
    }));
    var doc = dom.window.document;
    triggerSearch(dom, 'unique');

    waitFor(
        function () { return doc.getElementById('results-count').textContent !== ''; },
        'singleResult: count appears',
        function () {
            assertEqual(doc.getElementById('results-count').textContent, '1 resultat', 'singleResult: singular label');
            dom.window.close();
            testDone();
        }
    );
})();

// Test 4: last page (has_more=false)
pendingTests++;
(function testLastPage() {
    var dom = loadWithSearch(makeFetchMock({
        query: 'test', total: 45, limit: 20, offset: 40,
        has_more: false, total_pages: 3,
        hits: [{ score: 1.0, source: 'C:/data/Z/f.jsonl', type: 'record', snippets: [] }]
    }));
    var doc = dom.window.document;
    triggerSearch(dom, 'test');

    waitFor(
        function () { return doc.getElementById('pagination') !== null; },
        'lastPage: pagination appears',
        function () {
            var btns = doc.getElementById('pagination').querySelectorAll('.pagination-btn');
            var nextBtn = btns[1];
            assert(nextBtn.disabled === true, 'lastPage: next disabled when has_more=false');
            dom.window.close();
            testDone();
        }
    );
})();

// Test 5: return to hero
pendingTests++;
(function testReturnToHero() {
    var dom = loadWithSearch(makeFetchMock({
        query: 'test', total: 1, limit: 20, offset: 0,
        has_more: false, total_pages: 1,
        hits: [{ score: 1.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] }]
    }));
    var doc = dom.window.document;
    triggerSearch(dom, 'test');

    waitFor(
        function () { return doc.getElementById('results-view').className === 'state-active'; },
        'returnToHero: results appear first',
        function () {
            var btnHome = doc.getElementById('btn-home');
            btnHome.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));

            var hero = doc.getElementById('hero');
            var resultsView = doc.getElementById('results-view');
            assertEqual(hero.className, 'state-active', 'returnToHero: hero visible');
            assertEqual(resultsView.className, 'state-hidden', 'returnToHero: results hidden');
            assertEqual(doc.getElementById('search-input').value, '', 'returnToHero: search input cleared');

            dom.window.close();
            testDone();
        }
    );
})();

// Test 6: breach button appears for email query and renders provider results
pendingTests++;
(function testBreachButtonRendersProviders() {
    var breachResponse = {
        query: 'alice@example.com',
        summary: { providers_ok: 1, providers_failed: 1, total_hits: 2 },
        providers: [
            { name: 'hibp', ok: true, results: [
                { name: 'Adobe', domain: 'adobe.com', breach_date: '2013-10-04',
                  description: 'Leaked data <em>with</em> HTML',
                  data_classes: ['Emails', 'Passwords'], source_provider: 'hibp',
                  logo_url: 'https://logos.haveibeenpwned.com/Adobe.png', password_exposed: true,
                  hibp_details: { title: 'Adobe', pwn_count: 152445165, added_date: '2013-12-04T00:00:00Z',
                    modified_date: '2022-05-15T23:52:49Z', is_verified: true, is_sensitive: false,
                    is_fabricated: false, is_retired: false, is_spam_list: false, is_malware: false,
                    is_subscription_free: false, is_stealer_log: false, attribution: '', disclosure_url: '' },
                  bd_entries: [] },
                { name: 'LinkedIn', domain: 'linkedin.com', breach_date: '2012-05-05', data_classes: [],
                  source_provider: 'hibp', logo_url: 'https://logos.haveibeenpwned.com/LinkedIn.png',
                  password_exposed: false, hibp_details: { title: 'LinkedIn', pwn_count: 164611595,
                    is_verified: true, is_sensitive: false, is_fabricated: false }, bd_entries: [] }
            ], error: '' },
            { name: 'breachdirectory', ok: false, results: [], error: 'timeout' }
        ]
    };

    var dom = loadProductionCode(function (url) {
        if (url.indexOf('/api/breaches') !== -1) {
            return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(breachResponse); } });
        }
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    });
    var doc = dom.window.document;
    var card = dom.window.SearchComponents.renderCard(
        { score: 1.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] },
        0, 'alice@example.com'
    );
    doc.body.appendChild(card);

    var btn = card.querySelector('.hibp-btn');
    assert(btn !== null, 'breach: button present for email query');
    assertEqual(btn.textContent, 'Verifier fuites', 'breach: button label');

    btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

    waitFor(
        function () { return card.querySelector('.hibp-section') !== null; },
        'breach: section renders after click',
        function () {
            var section = card.querySelector('.hibp-section');
            var providers = section.querySelectorAll('.breach-provider');
            assertEqual(providers.length, 2, 'breach: 2 provider blocks rendered');

            // HIBP provider: ok with 2 results
            var hibpHeader = providers[0].querySelector('.breach-provider-header');
            assert(hibpHeader.textContent.indexOf('hibp') !== -1, 'breach: hibp provider shown');
            assert(hibpHeader.textContent.indexOf('2 fuite(s)') !== -1, 'breach: hibp shows 2 results');
            var hibpBreaches = providers[0].querySelectorAll('.hibp-breach');
            assertEqual(hibpBreaches.length, 2, 'breach: hibp has 2 breach entries');
            var adobeName = providers[0].querySelector('.hibp-breach-name');
            assertEqual(adobeName.textContent, 'Adobe', 'breach: first breach name is Adobe');

            // HIBP logo present
            var adobeLogo = hibpBreaches[0].querySelector('.hibp-breach-logo');
            assert(adobeLogo !== null, 'breach: Adobe logo img present');
            assertEqual(adobeLogo.src, 'https://logos.haveibeenpwned.com/Adobe.png', 'breach: Adobe logo src');

            // HIBP password badge on Adobe (password_exposed=true)
            var adobePwd = hibpBreaches[0].querySelector('.hibp-breach-pwd');
            assert(adobePwd !== null, 'breach: Adobe shows password badge');
            assertEqual(adobePwd.textContent, 'MDP expose', 'breach: password badge text');

            // HIBP verified badge
            var verifiedBadge = hibpBreaches[0].querySelector('.hibp-badge.verified');
            assert(verifiedBadge !== null, 'breach: Adobe shows verified badge');
            assertEqual(verifiedBadge.textContent, 'Verifie', 'breach: verified badge text');

            // Compact summary: breach_date visible in summary
            var summaryDate = hibpBreaches[0].querySelector('.hibp-breach-summary .hibp-breach-date');
            assert(summaryDate !== null, 'breach: summary shows breach_date');
            assert(summaryDate.textContent.indexOf('2013-10-04') !== -1, 'breach: summary date value');

            // Details: added_date, modified_date in details section
            var detailDates = hibpBreaches[0].querySelectorAll('.hibp-breach-details .hibp-breach-date');
            var dateTexts = [];
            for (var di = 0; di < detailDates.length; di++) { dateTexts.push(detailDates[di].textContent); }
            assert(dateTexts.some(function(t) { return t.indexOf('Ajout HIBP') !== -1; }), 'breach: shows added_date');
            assert(dateTexts.some(function(t) { return t.indexOf('Maj:') !== -1; }), 'breach: shows modified_date');

            // Collapsible: details hidden by default, expanded on click
            assert(!hibpBreaches[0].classList.contains('expanded'), 'breach: not expanded by default');
            var chevron = hibpBreaches[0].querySelector('.hibp-breach-chevron');
            assert(chevron !== null, 'breach: chevron present');
            hibpBreaches[0].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
            assert(hibpBreaches[0].classList.contains('expanded'), 'breach: expanded after click');
            hibpBreaches[0].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
            assert(!hibpBreaches[0].classList.contains('expanded'), 'breach: collapsed after second click');

            // HIBP pwn count
            var pwnSpan = hibpBreaches[0].querySelector('.hibp-breach-pwn');
            assert(pwnSpan !== null, 'breach: Adobe shows pwn count');
            assert(pwnSpan.textContent.indexOf('comptes') !== -1, 'breach: pwn count text');

            // HIBP description rendered
            var descDiv = hibpBreaches[0].querySelector('.hibp-breach-desc');
            assert(descDiv !== null, 'breach: Adobe shows description');
            assert(descDiv.textContent.indexOf('Leaked data') !== -1, 'breach: description content');
            // HTML stripped
            assert(descDiv.textContent.indexOf('<em>') === -1, 'breach: HTML stripped from desc');

            // LinkedIn: no password badge (password_exposed=false)
            var linkedinPwd = hibpBreaches[1].querySelector('.hibp-breach-pwd');
            assert(linkedinPwd === null, 'breach: LinkedIn has no password badge');

            // BreachDirectory provider: failed
            var bdHeader = providers[1].querySelector('.breach-provider-header');
            assert(bdHeader.textContent.indexOf('timeout') !== -1, 'breach: failed provider shows error');

            // Button should be removed
            assert(card.querySelector('.hibp-btn') === null, 'breach: button removed after success');

            dom.window.close();
            testDone();
        }
    );
})();

// Test 7: no breach button for non-email query
pendingTests++;
(function testNoBreachButtonForNonEmail() {
    var dom = loadProductionCode();
    var card = dom.window.SearchComponents.renderCard(
        { score: 1.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] },
        0, 'john doe'
    );
    var btn = card.querySelector('.hibp-btn');
    assert(btn === null, 'breach: no button for non-email query');
    dom.window.close();
    passed++; // testDone expects matching pendingTests
    testDone();
})();

// Test 8: breach clean result (0 hits, 0 failures)
pendingTests++;
(function testBreachCleanResult() {
    var cleanResponse = {
        query: 'clean@example.com',
        summary: { providers_ok: 2, providers_failed: 0, total_hits: 0 },
        providers: [
            { name: 'hibp', ok: true, results: [], error: '' },
            { name: 'breachdirectory', ok: true, results: [], error: '' }
        ]
    };

    var dom = loadProductionCode(function (url) {
        if (url.indexOf('/api/breaches') !== -1) {
            return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(cleanResponse); } });
        }
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    });
    var doc = dom.window.document;
    var card = dom.window.SearchComponents.renderCard(
        { score: 1.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] },
        0, 'clean@example.com'
    );
    doc.body.appendChild(card);

    card.querySelector('.hibp-btn').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

    waitFor(
        function () { return card.querySelector('.hibp-section') !== null; },
        'breachClean: section renders',
        function () {
            var section = card.querySelector('.hibp-section');
            assert(section.querySelector('.hibp-clean') !== null, 'breachClean: shows clean message');
            assert(section.textContent.indexOf('Aucune fuite connue') !== -1, 'breachClean: clean text');
            dom.window.close();
            testDone();
        }
    );
})();

// Test 9: breach API error shows error div and re-enables button
pendingTests++;
(function testBreachApiError() {
    var dom = loadProductionCode(function (url) {
        if (url.indexOf('/api/breaches') !== -1) {
            return Promise.resolve({
                ok: false, status: 500,
                json: function () { return Promise.resolve({ detail: 'Serveur indisponible' }); }
            });
        }
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    });
    var doc = dom.window.document;
    var card = dom.window.SearchComponents.renderCard(
        { score: 1.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] },
        0, 'fail@example.com'
    );
    doc.body.appendChild(card);

    card.querySelector('.hibp-btn').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

    waitFor(
        function () { return card.querySelector('.hibp-error') !== null; },
        'breachError: error div appears',
        function () {
            var errDiv = card.querySelector('.hibp-error');
            assert(errDiv.textContent.indexOf('Serveur indisponible') !== -1, 'breachError: shows error message');
            var btn = card.querySelector('.hibp-btn');
            assert(btn !== null, 'breachError: button still present');
            assert(!btn.disabled, 'breachError: button re-enabled');
            assertEqual(btn.textContent, 'Verifier fuites', 'breachError: button label restored');
            dom.window.close();
            testDone();
        }
    );
})();

// Test 10: BreachDirectory credentials rendering
pendingTests++;
(function testBreachDirectoryCredentials() {
    var bdResponse = {
        query: 'bd@example.com',
        summary: { providers_ok: 1, providers_failed: 0, total_hits: 1 },
        providers: [
            { name: 'breachdirectory & co', ok: true, results: [
                { name: 'LinkedIn.com', source_provider: 'breachdirectory', password_exposed: true,
                  data_classes: ['Passwords'], hibp_details: null,
                  bd_entries: [
                      { source: 'LinkedIn.com', password: 'Test&1***', hash_password: true,
                        sha1: '8308651804facb7b9af8ffc53a33a22d6a1c8ac2', hash: '59A/Ao+PVv7K', has_password: true },
                      { source: 'LinkedIn.com', password: '', hash_password: false,
                        sha1: '', hash: 'GCW5GDO8TYze', has_password: false }
                  ] }
            ], error: '' }
        ]
    };

    var dom = loadProductionCode(function (url) {
        if (url.indexOf('/api/breaches') !== -1) {
            return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(bdResponse); } });
        }
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    });
    var doc = dom.window.document;
    var card = dom.window.SearchComponents.renderCard(
        { score: 1.0, source: 'C:/data/X/f.jsonl', type: 'record', snippets: [] },
        0, 'bd@example.com'
    );
    doc.body.appendChild(card);
    card.querySelector('.hibp-btn').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

    waitFor(
        function () { return card.querySelector('.hibp-section') !== null; },
        'bdCreds: section renders',
        function () {
            var providerName = card.querySelector('.breach-provider-name');
            assert(providerName.textContent.indexOf('breachdirectory & co') !== -1, 'bdCreds: provider name not double-escaped');
            assert(providerName.textContent.indexOf('&amp;') === -1, 'bdCreds: provider name shows raw ampersand');
            var creds = card.querySelector('.bd-credentials');
            assert(creds !== null, 'bdCreds: credentials section present');
            var credLines = creds.querySelectorAll('.bd-cred-line');
            assertEqual(credLines.length, 2, 'bdCreds: 2 credential lines');
            // First line: password + hash_pwd + sha1 + hash (all fields)
            assert(credLines[0].textContent.indexOf('mdp: Test&1***') !== -1, 'bdCreds: shows password');
            assert(credLines[0].textContent.indexOf('&amp;') === -1, 'bdCreds: password not double-escaped');
            assert(credLines[0].textContent.indexOf('hash_pwd: oui') !== -1, 'bdCreds: shows hash_password flag');
            assert(credLines[0].textContent.indexOf('sha1: 8308651804facb7b9af8ffc53a33a22d6a1c8ac2') !== -1, 'bdCreds: shows full sha1');
            assert(credLines[0].textContent.indexOf('hash: 59A/Ao+PVv7K') !== -1, 'bdCreds: shows hash');
            // Second line: no password, no sha1 → shows hash only
            assert(credLines[1].textContent.indexOf('hash: GCW5GDO8TYze') !== -1, 'bdCreds: second line hash');
            dom.window.close();
            testDone();
        }
    );
})();

// Test 11: search error shows toast
pendingTests++;
(function testSearchErrorShowsToast() {
    var dom = loadWithSearch(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve([{ id: 'test', name: 'Test' }]); } });
        }
        if (url.indexOf('/api/stats') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ total_docs: 0, num_segments: 0, index_size: '0' }); } });
        }
        return Promise.resolve({
            ok: false, status: 500,
            json: function () { return Promise.resolve({ detail: 'Erreur serveur test' }); }
        });
    });
    var doc = dom.window.document;
    triggerSearch(dom, 'error');

    waitFor(
        function () { return doc.getElementById('app-toast') !== null; },
        'searchError: toast appears',
        function () {
            var toast = doc.getElementById('app-toast');
            assert(toast.textContent.indexOf('Erreur') !== -1, 'searchError: toast shows error message');
            assert(toast.className.indexOf('error') !== -1, 'searchError: toast has error class');
            dom.window.close();
            testDone();
        }
    );
})();

// Test 12: loadIndexes keeps previous state on transient backend failure
pendingTests++;
(function testLoadIndexesKeepsPreviousStateOnFailure() {
    var indexesCalls = 0;
    var dom = loadWithSearch(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            indexesCalls += 1;
            if (indexesCalls === 1) {
                return Promise.resolve({
                    ok: true,
                    json: function () { return Promise.resolve([{ id: 'france', name: 'France', status: 'ready' }]); }
                });
            }
            return Promise.reject(new Error('backend down'));
        }
        if (url.indexOf('/api/stats') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ total_docs: 100, num_segments: 2, index_size: '1 Mo' }); } });
        }
        return Promise.resolve({
            ok: false,
            status: 500,
            json: function () { return Promise.resolve({ detail: 'boom' }); }
        });
    });
    var doc = dom.window.document;

    waitFor(
        function () { return doc.getElementById('index-select').options.length === 1; },
        'loadIndexes first success',
        function () {
            triggerSearch(dom, 'test');
            waitFor(
                function () {
                    return doc.getElementById('hero-system-status').textContent.indexOf('Backend instable') !== -1;
                },
                'loadIndexes degraded status',
                function () {
                    assertEqual(doc.getElementById('index-select').options[0].textContent, 'France', 'loadIndexes: keep previous option on failure');
                    assert(doc.getElementById('hero-system-status').textContent.indexOf('Backend instable') !== -1, 'loadIndexes: status shows unstable');
                    dom.window.close();
                    testDone();
                }
            );
        }
    );
})();

// Test 13: loadIndexes keeps previous ready selection if backend returns no ready index transiently
pendingTests++;
(function testLoadIndexesKeepsPreviousReadySelectionOnNoReadyPayload() {
    var indexesCalls = 0;
    var dom = loadWithSearch(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            indexesCalls += 1;
            if (indexesCalls === 1) {
                return Promise.resolve({
                    ok: true,
                    json: function () { return Promise.resolve([{ id: 'france', name: 'France', status: 'ready' }]); }
                });
            }
            return Promise.resolve({
                ok: true,
                json: function () { return Promise.resolve([{ id: 'france', name: 'France', status: 'loading' }]); }
            });
        }
        if (url.indexOf('/api/stats') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ total_docs: 100, num_segments: 2, index_size: '1 Mo' }); } });
        }
        return Promise.resolve({
            ok: false,
            status: 500,
            json: function () { return Promise.resolve({ detail: 'boom' }); }
        });
    });
    var doc = dom.window.document;

    waitFor(
        function () { return doc.getElementById('index-select').options.length === 1; },
        'loadIndexes no ready first success',
        function () {
            triggerSearch(dom, 'test');
            waitFor(
                function () {
                    return doc.getElementById('hero-index-status').textContent.indexOf('Aucun index pret') !== -1;
                },
                'loadIndexes no ready status',
                function () {
                    assertEqual(doc.getElementById('index-select').options[0].textContent, 'France', 'loadIndexes: keep last ready option on no-ready payload');
                    assert(doc.getElementById('hero-index-status').textContent.indexOf('Aucun index pret') !== -1, 'loadIndexes: status shows no ready index');
                    assert(doc.getElementById('search-input').disabled, 'loadIndexes: search disabled when no ready index');
                    dom.window.close();
                    testDone();
                }
            );
        }
    );
})();

// Positions des highlights en points de code (backend Python) et URLs sures
pendingTests++;
(function testSnippetOffsetsAndSafeUrls() {
    var dom = loadProductionCode(function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    });
    var components = dom.window.SearchComponents;

    // "😀 " occupe 2 points de code mais 3 unites UTF-16.
    var html = components.renderSnippet({ text: '😀 jose martin', highlights: [[2, 6]] });
    assertEqual(html, '😀 <mark>jose</mark> martin', 'renderSnippet: offsets after emoji use code points');

    html = components.renderSnippet({ text: 'a<b> c', highlights: [[0, 4], [2, 3], [9, 12]] });
    assertEqual(html, '<mark>a&lt;b&gt;</mark> c', 'renderSnippet: overlapping and out-of-range highlights ignored');

    assertEqual(components.safeHttpUrl('https://example.com/a'), 'https://example.com/a', 'safeHttpUrl: keeps https');
    assertEqual(components.safeHttpUrl('javascript:alert(1)'), null, 'safeHttpUrl: rejects javascript:');
    assertEqual(components.safeHttpUrl('data:text/html,x'), null, 'safeHttpUrl: rejects data:');
    assertEqual(components.safeHttpUrl('pas une url'), null, 'safeHttpUrl: rejects garbage');
    dom.window.close();
    testDone();
})();

// ---- Pertinence : mode et total repris par la pagination, tous les index ----

function recordingFetch(indexes, pages, urls) {
    return function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve(indexes); } });
        }
        if (url.indexOf('/api/stats') !== -1) {
            return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ total_docs: 1, num_segments: 1, index_size: '1 Ko' }); } });
        }
        urls.push(url);
        var payload = pages[Math.min(urls.length - 1, pages.length - 1)];
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve(payload); } });
    };
}

// La page 2 reprend le mode de la page 1 et ne recompte pas le total.
pendingTests++;
(function testPaginationReusesModeAndTotal() {
    var urls = [];
    var hit = { score: 1, source: 'C:/data/ACME/f.jsonl', type: 'record', snippets: [] };
    var dom = loadWithSearch(recordingFetch([{ id: 'test', name: 'Test', status: 'ready' }], [
        { query: 'jean lyon', total: 45, limit: 20, offset: 0, has_more: true, total_pages: 3,
          match_mode: 'any', relaxed: true, hits: [hit] },
        { query: 'jean lyon', total: null, limit: 20, offset: 20, has_more: true, total_pages: null,
          match_mode: 'any', relaxed: false, hits: [hit] }
    ], urls));
    var doc = dom.window.document;

    waitFor(
        function () { return doc.getElementById('index-select').value === 'test'; },
        'pagination mode: index loaded',
        function () {
            triggerSearch(dom, 'jean lyon');
            waitFor(
                function () { return doc.getElementById('pagination') !== null; },
                'pagination mode: first page rendered',
                function () {
                    assert(urls[0].indexOf('match=') === -1, 'first page: auto mode (no match param)');
                    assert(urls[0].indexOf('count=false') === -1, 'first page: total counted');
                    var notice = doc.getElementById('results-notice');
                    assert(notice && notice.textContent.indexOf('au moins un') !== -1, 'relaxed notice shown');

                    doc.getElementById('pagination').querySelectorAll('.pagination-btn')[1].click();
                    waitFor(
                        function () { return doc.getElementById('results-count').textContent.indexOf('page 2/3') !== -1; },
                        'pagination mode: second page rendered',
                        function () {
                            assert(urls[1].indexOf('&match=any') !== -1, 'second page: keeps match mode');
                            assert(urls[1].indexOf('&count=false') !== -1, 'second page: total not recounted');
                            assert(urls[1].indexOf('&index=test') !== -1, 'second page: same index');
                            assert(doc.getElementById('results-count').textContent.indexOf('45 resultats') !== -1,
                                'second page: total from first page');
                            assert(doc.getElementById('results-notice') !== null, 'relaxed notice kept on page 2');
                            dom.window.close();
                            testDone();
                        }
                    );
                }
            );
        }
    );
})();

// Plusieurs index prets : option "Tous les index", origine de chaque resultat.
pendingTests++;
(function testAllIndexesOption() {
    var urls = [];
    var dom = loadWithSearch(recordingFetch([
        { id: 'a', name: 'Fuite A', status: 'ready' },
        { id: 'b', name: 'Fuite B', status: 'ready' }
    ], [
        { query: 'alice', total: 2, limit: 20, offset: 0, has_more: false, total_pages: 1,
          match_mode: 'all', relaxed: false, partial: true,
          skipped_indexes: [{ id: 'b', name: 'Fuite B', reason: 'timeout' }],
          hits: [{ score: 2, source: 'C:/data/ACME/f.jsonl', type: 'record', index_name: 'Fuite A', snippets: [] }] }
    ], urls));
    var doc = dom.window.document;
    var select = doc.getElementById('index-select');

    waitFor(
        function () { return select.options.length === 3; },
        'all indexes: options loaded',
        function () {
            assertEqual(select.options[0].value, 'all', 'all indexes option listed first');
            assertEqual(select.options[0].textContent, 'Tous les index', 'all indexes option label');
            select.value = 'all';
            select.dispatchEvent(new dom.window.Event('change'));
            triggerSearch(dom, 'alice');
            waitFor(
                function () { return doc.getElementById('results-list').querySelectorAll('.result-card').length > 0; },
                'all indexes: results rendered',
                function () {
                    assert(urls[0].indexOf('&index=all') !== -1, 'search sent to all indexes');
                    var badge = doc.querySelector('.result-card .card-index');
                    assert(badge && badge.textContent === 'Fuite A', 'card shows source index');
                    var notice = doc.getElementById('results-notice');
                    assert(notice && notice.textContent.indexOf('Fuite B') !== -1, 'skipped index reported');
                    dom.window.close();
                    testDone();
                }
            );
        }
    );
})();

// Un seul index pret : pas d'option "Tous les index", pas de badge.
pendingTests++;
(function testSingleIndexHasNoAllOption() {
    var dom = loadWithSearch(recordingFetch([
        { id: 'a', name: 'Fuite A', status: 'ready' },
        { id: 'b', name: 'Fuite B', status: 'loading' }
    ], [{}], []));
    var doc = dom.window.document;
    var select = doc.getElementById('index-select');

    waitFor(
        function () { return select.options.length === 2; },
        'single ready index: options loaded',
        function () {
            var values = Array.prototype.map.call(select.options, function (o) { return o.value; });
            assert(values.indexOf('all') === -1, 'no all option with one ready index');
            var card = dom.window.SearchComponents.renderCard({ score: 1, source: 'C:/x/y.jsonl', snippets: [] }, 0, 'x');
            assertEqual(card.querySelector('.card-index'), null, 'no index badge without index_name');
            var url = dom.window.SearchApi.buildSearchUrl('a b', 0, 'a', { match: 'bogus', count: true });
            assert(url.indexOf('match=') === -1 && url.indexOf('count=') === -1, 'buildSearchUrl ignores defaults');
            dom.window.close();
            testDone();
        }
    );
})();
