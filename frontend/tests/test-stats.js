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

function createHtml() {
    return [
        '<!DOCTYPE html><html><body>',
        '<select id="stats-index-select"><option value="all">Tous les index</option></select>',
        '<div id="stats-system-status"></div>',
        '<div id="stats-index-status"></div>',
        '<div id="stats-header-status"></div>',
        '<div id="stats-subtitle"></div>',
        '<div class="kpi-label">Docs</div>',
        '<div class="kpi-label">Emails uniques</div>',
        '<div class="kpi-label">Telephones uniques</div>',
        '<div class="kpi-label">Taille index</div>',
        '<div class="kpi-value" data-target="12">12</div>',
        '<div class="kpi-value" data-target="34">34</div>',
        '<div class="kpi-value" data-target="56">56</div>',
        '<div class="kpi-value" data-target="1.5" data-format="size" data-sizetext="1.5 Go">1.5 Go</div>',
        '<div id="source-bars"><span>old bars</span></div>',
        '<div id="top-emails-bars"><span>old emails</span></div>',
        '<div><canvas id="chart-domains"></canvas></div>',
        '<div><canvas id="chart-pro-perso"></canvas></div>',
        '<div><canvas id="chart-tld"></canvas></div>',
        '<div><canvas id="chart-phones"></canvas></div>',
        '</body></html>'
    ].join('\n');
}

function makeResponse(status, body) {
    return {
        ok: status >= 200 && status < 300,
        status: status,
        json: function () {
            return Promise.resolve(body);
        }
    };
}

function wait(ms) {
    return new Promise(function (resolve) {
        setTimeout(resolve, ms);
    });
}

async function runScenario(apiFetch) {
    var dom = new JSDOM(createHtml(), {
        url: 'http://localhost:3000',
        runScripts: 'dangerously',
        resources: 'usable',
        pretendToBeVisual: true
    });
    var win = dom.window;
    var doc = win.document;

    win.AppUtils = {
        apiBase: 'http://localhost:8500',
        apiFetch: apiFetch
    };

    function ChartStub(ctx, config) {
        this.ctx = ctx;
        this.config = config;
    }
    ChartStub.prototype.destroy = function () {};
    ChartStub.defaults = { color: '', borderColor: '', font: {} };
    win.Chart = ChartStub;
    win.Effects = { triggerScan: function () {} };
    win.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(Date.now()); }, 0); };
    win.cancelAnimationFrame = function (id) { clearTimeout(id); };
    if (win.HTMLCanvasElement && win.HTMLCanvasElement.prototype) {
        win.HTMLCanvasElement.prototype.getContext = function () { return {}; };
    }

    var statsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'stats.js'), 'utf-8');
    win.eval(statsSrc);
    await wait(750);
    return { dom: dom, win: win, doc: doc };
}

async function main() {
    var locked = await runScenario(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve(makeResponse(200, [{ id: 'france', name: 'France', status: 'ready' }]));
        }
        return Promise.resolve(makeResponse(409, { detail: 'Index verrouille temporairement' }));
    });

    var lockedValues = locked.doc.querySelectorAll('.kpi-value');
    assertEqual(lockedValues[0].dataset.target, '0', 'stats409: resets docs target');
    assertEqual(lockedValues[1].dataset.target, '0', 'stats409: resets emails target');
    assertEqual(lockedValues[2].dataset.target, '0', 'stats409: resets phones target');
    assertEqual(lockedValues[3].dataset.target, '0', 'stats409: resets size target');
    assertEqual(lockedValues[3].dataset.sizetext, '0 Go', 'stats409: resets size label');
    assertEqual(lockedValues[0].textContent, '0', 'stats409: resets docs text');
    assertEqual(lockedValues[3].textContent, '0 Go', 'stats409: resets size text');
    assertEqual(locked.doc.getElementById('stats-subtitle').textContent, 'Index verrouille temporairement', 'stats409: shows backend detail');
    assertEqual(locked.doc.getElementById('stats-system-status').textContent, 'Backend joignable', 'stats409: shows reachable backend');
    assertEqual(locked.doc.getElementById('stats-index-status').textContent, 'Index verrouille temporairement', 'stats409: shows unavailable index detail');
    assertEqual(locked.doc.getElementById('stats-header-status').textContent, 'Backend joignable', 'stats409: header reflects reachable backend');
    assertEqual(locked.doc.getElementById('source-bars').textContent, '', 'stats409: clears old source bars');
    assertEqual(locked.doc.getElementById('top-emails-bars').textContent, '', 'stats409: clears old email bars');
    assertEqual(locked.doc.getElementById('stats-index-select').disabled, false, 'stats409: keeps selector enabled');
    locked.dom.window.close();

    var noReady = await runScenario(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve(makeResponse(200, [{ id: 'france', name: 'France', status: 'loading' }]));
        }
        return Promise.resolve(makeResponse(200, {}));
    });

    assertEqual(noReady.doc.getElementById('stats-system-status').textContent, 'Backend joignable', 'noReady: shows reachable backend');
    assertEqual(noReady.doc.getElementById('stats-index-status').textContent, 'Aucun index pret - chargement en cours', 'noReady: shows no ready index state');
    assertEqual(noReady.doc.getElementById('stats-header-status').textContent, 'Backend joignable', 'noReady: header shows reachable backend');
    assertEqual(noReady.doc.getElementById('stats-subtitle').textContent, 'Aucun index pret - chargement en cours', 'noReady: subtitle shows true state');
    assertEqual(noReady.doc.getElementById('stats-index-select').disabled, true, 'noReady: disables selector');
    noReady.dom.window.close();

    var unstable = await runScenario(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve(makeResponse(200, [{ id: 'france', name: 'France', status: 'ready' }]));
        }
        return Promise.reject(new Error('network down'));
    });

    assertEqual(unstable.doc.getElementById('stats-system-status').textContent, 'Backend instable', 'unstable: shows backend instability');
    assertEqual(unstable.doc.getElementById('stats-index-status').textContent, 'Etat stats temporairement indisponible', 'unstable: shows stale index state');
    assertEqual(unstable.doc.getElementById('stats-header-status').textContent, 'Backend instable', 'unstable: header shows instability');
    assertEqual(unstable.doc.getElementById('stats-index-select').disabled, true, 'unstable: disables selector');
    unstable.dom.window.close();

    // Changement rapide d'index : la reponse lente de l'ancien index arrive
    // apres celle du nouveau et ne doit pas l'ecraser.
    var race = await runScenario(function (url) {
        if (url.indexOf('/api/indexes') !== -1) {
            return Promise.resolve(makeResponse(200, [
                { id: 'lent', name: 'Lent', status: 'ready' },
                { id: 'rapide', name: 'Rapide', status: 'ready' }
            ]));
        }
        if (url.indexOf('index=lent') !== -1) {
            return wait(300).then(function () {
                return makeResponse(200, { total_docs: 111, index_size: '1 Go', index_size_bytes: 1 });
            });
        }
        if (url.indexOf('index=rapide') !== -1) {
            return Promise.resolve(makeResponse(200, { total_docs: 222, index_size: '2 Go', index_size_bytes: 2 }));
        }
        return Promise.resolve(makeResponse(200, { total_docs: 5, index_size: '5 Go', index_size_bytes: 5 }));
    });
    var raceSelect = race.doc.getElementById('stats-index-select');
    raceSelect.value = 'lent';
    raceSelect.dispatchEvent(new race.win.Event('change'));
    raceSelect.value = 'rapide';
    raceSelect.dispatchEvent(new race.win.Event('change'));
    await wait(600);
    assertEqual(race.doc.querySelectorAll('.kpi-value')[0].dataset.target, '222', 'race: stale response of previous index is ignored');
    race.dom.window.close();

    console.log('\n' + (passed + failed) + ' tests, ' + passed + ' passed, ' + failed + ' failed');
    if (failed > 0) process.exit(1);
}

main().catch(function (err) {
    console.error(err);
    process.exit(1);
});
