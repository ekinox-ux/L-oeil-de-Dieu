'use strict';

var fs = require('fs');
var path = require('path');
var JSDOM = require('jsdom').JSDOM;

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
            finalize();
        }
    }, intervalMs);
}

var jsDir = path.join(__dirname, '..', 'js');

var HTML = [
    '<!DOCTYPE html><html><body>',
    '<div id="maintenance-subtitle"></div>',
    '<button id="maintenance-clean-locks">Nettoyer</button>',
    '<button id="maintenance-refresh">Actualiser</button>',
    '<div id="maintenance-ready-count">0</div>',
    '<div id="maintenance-loading-count">0</div>',
    '<div id="maintenance-error-count">0</div>',
    '<div id="maintenance-analyze-running">NON</div>',
    '<div id="maintenance-progress">0</div>',
    '<div id="maintenance-emails-found">0</div>',
    '<div id="maintenance-phones-found">0</div>',
    '<div id="maintenance-last-run">-</div>',
    '<div id="maintenance-cache-dir">cache</div>',
    '<div id="maintenance-index-count">0 index</div>',
    '<div id="maintenance-indexes"></div>',
    '</body></html>'
].join('\n');

var fetchCalls = [];

var dom = new JSDOM(HTML, {
    url: 'http://localhost:3000',
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true
});

var win = dom.window;
win.APP_CONFIG = { API_BASE: 'http://localhost:8500' };
win.Effects = { triggerScan: function () {} };
win.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(Date.now()); }, 0); };
win.fetch = function (url, options) {
    fetchCalls.push({ url: url, options: options || {} });
    if (url.indexOf('/api/indexes') !== -1) {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve([{
                    id: 'france',
                    name: 'France',
                    status: 'error',
                    error: 'lock stale',
                    retry_count: 2,
                    next_retry_at: Math.floor(Date.now() / 1000) + 15,
                    last_ready_at: 0,
                    last_error_at: 0
                }]);
            }
        });
    }
    if (url.indexOf('/api/diagnostics') !== -1) {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({
                    cache_dir: 'C:/cache',
                    indexes: [{
                        id: 'france',
                        path: 'E:/index',
                        path_exists: true,
                        source: 'X:/Data',
                        source_exists: true,
                        analyzer_lock: 'C:/cache/analyzer.lock',
                        analyzer_lock_exists: true,
                        lockfiles: [{ name: '.tantivy-meta.lock', path: 'E:/index/.tantivy-meta.lock' }]
                    }]
                });
            }
        });
    }
    if (url.indexOf('/api/diagnostics/analyzer-locks/cleanup') !== -1) {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({ removed_count: 1, removed: ['X:/Data'], kept_count: 0, kept: [] });
            }
        });
    }
    if (url.indexOf('/api/diagnostics/indexes/retry?id=france') !== -1) {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({ status: 'ok', id: 'france', index_status: 'loading' });
            }
        });
    }
    if (url.indexOf('/api/analyze/status') !== -1) {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({
                    running: true,
                    progress: 7,
                    emails_found: 120,
                    phones_found: 12,
                    last_run: '2026-04-10 12:00:00'
                });
            }
        });
    }
    if (url.indexOf('/api/analyze?index=france') !== -1) {
        return Promise.resolve({
            ok: true,
            json: function () {
                return Promise.resolve({ status: 'started' });
            }
        });
    }
    return Promise.reject(new Error('unexpected url ' + url));
};

['config.js', 'app-utils.js', 'maintenance.js'].forEach(function (file) {
    var src = fs.readFileSync(path.join(jsDir, file), 'utf-8');
    win.eval(src);
});

function finalize() {
    console.log('\n' + (passed + failed) + ' tests, ' + passed + ' passed, ' + failed + ' failed');
    dom.window.close();
    if (failed > 0) process.exit(1);
}

function runPartialFailureScenario(done) {
    var partialFetchCalls = [];
    var partialDom = new JSDOM(HTML, {
        url: 'http://localhost:3000',
        runScripts: 'dangerously',
        resources: 'usable',
        pretendToBeVisual: true
    });
    var partialWin = partialDom.window;
    partialWin.APP_CONFIG = { API_BASE: 'http://localhost:8500' };
    partialWin.Effects = { triggerScan: function () {} };
    partialWin.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(Date.now()); }, 0); };
    partialWin.fetch = function (url) {
        partialFetchCalls.push(url);
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve({
                ok: true,
                json: function () {
                    return Promise.resolve([{
                        id: 'france',
                        name: 'France',
                        path: 'E:/index',
                        source: 'X:/Data',
                        status: 'ready',
                        error: '',
                        retry_count: 0,
                        next_retry_at: 0,
                        last_ready_at: 0,
                        last_error_at: 0
                    }]);
                }
            });
        }
        if (url.indexOf('/api/diagnostics') !== -1) {
            return Promise.reject(new Error('diagnostics down'));
        }
        if (url.indexOf('/api/analyze/status') !== -1) {
            return Promise.resolve({
                ok: true,
                json: function () {
                    return Promise.resolve({
                        running: false,
                        progress: 0,
                        emails_found: 0,
                        phones_found: 0,
                        last_run: null
                    });
                }
            });
        }
        return Promise.reject(new Error('unexpected url ' + url));
    };

    ['config.js', 'app-utils.js', 'maintenance.js'].forEach(function (file) {
        var src = fs.readFileSync(path.join(jsDir, file), 'utf-8');
        partialWin.eval(src);
    });

    waitFor(
        function () { return partialDom.window.document.querySelector('.maintenance-index-card') !== null; },
        'maintenance partial failure rendered',
        function () {
            var doc = partialDom.window.document;
            assertEqual(doc.getElementById('maintenance-ready-count').textContent, '1', 'maintenance partial: ready count');
            assert(doc.getElementById('maintenance-subtitle').textContent.indexOf('Vue partielle') !== -1, 'maintenance partial: subtitle degraded');
            assert(doc.body.textContent.indexOf('France') !== -1, 'maintenance partial: index still rendered');
            partialDom.window.close();
            done();
        }
    );
}

waitFor(
    function () { return dom.window.document.querySelector('.maintenance-index-card') !== null; },
    'maintenance cards rendered',
    function () {
        var doc = dom.window.document;
        assertEqual(doc.getElementById('maintenance-ready-count').textContent, '0', 'maintenance: ready count');
        assertEqual(doc.getElementById('maintenance-error-count').textContent, '1', 'maintenance: error count');
        assertEqual(doc.getElementById('maintenance-analyze-running').textContent, 'OUI', 'maintenance: running flag');
        assertEqual(doc.getElementById('maintenance-progress').textContent, '7', 'maintenance: progress');
        assert(doc.getElementById('maintenance-cache-dir').textContent.indexOf('C:/cache') !== -1, 'maintenance: cache dir shown');

        var flags = doc.querySelectorAll('.maintenance-flag');
        assert(flags.length >= 4, 'maintenance: status flags rendered');
        assert(doc.body.textContent.indexOf('C:/cache/analyzer.lock') !== -1, 'maintenance: analyzer lock path shown');

        var analyzeBtn = Array.prototype.slice.call(doc.querySelectorAll('.maintenance-btn')).filter(function (btn) {
            return btn.textContent === 'Lancer analyse';
        })[0];
        assert(analyzeBtn !== null, 'maintenance: analyze button rendered');
        analyzeBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

        waitFor(
            function () {
                return fetchCalls.some(function (call) {
                    return call.url.indexOf('/api/analyze?index=france') !== -1 && call.options.method === 'POST';
                });
            },
            'maintenance analyze POST',
            function () {
                assert(fetchCalls.some(function (call) {
                    return call.url.indexOf('/api/analyze?index=france') !== -1 && call.options.method === 'POST';
                }), 'maintenance: analyze POST sent');
                var retryBtn = Array.prototype.slice.call(doc.querySelectorAll('.maintenance-btn')).filter(function (btn) {
                    return btn.textContent === 'Retry index';
                })[0];
                assert(retryBtn !== undefined, 'maintenance: retry button rendered');
                retryBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

                waitFor(
                    function () {
                        return fetchCalls.some(function (call) {
                            return call.url.indexOf('/api/diagnostics/indexes/retry?id=france') !== -1 && call.options.method === 'POST';
                        });
                    },
                    'maintenance retry POST',
                    function () {
                        assert(fetchCalls.some(function (call) {
                            return call.url.indexOf('/api/diagnostics/indexes/retry?id=france') !== -1 && call.options.method === 'POST';
                        }), 'maintenance: retry POST sent');

                        var cleanupBtn = doc.getElementById('maintenance-clean-locks');
                        cleanupBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

                        waitFor(
                            function () {
                                return fetchCalls.some(function (call) {
                                    return call.url.indexOf('/api/diagnostics/analyzer-locks/cleanup') !== -1 && call.options.method === 'POST';
                                });
                            },
                            'maintenance cleanup POST',
                            function () {
                                assert(fetchCalls.some(function (call) {
                                    return call.url.indexOf('/api/diagnostics/analyzer-locks/cleanup') !== -1 && call.options.method === 'POST';
                                }), 'maintenance: cleanup POST sent');
                                runPartialFailureScenario(finalize);
                            }
                        );
                    }
                );
            }
        );
    }
);
