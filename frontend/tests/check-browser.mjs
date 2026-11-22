// Integration check using a running Chromium/Edge remote-debugging instance.
// Start the application and browser first, then run: node frontend/tests/check-browser.mjs
import assert from 'node:assert/strict';

const debugBase = process.env.OEIL_BROWSER_DEBUG_URL || 'http://127.0.0.1:9224';
const frontendBase = process.env.OEIL_FRONTEND_URL || 'http://127.0.0.1:3000';
const tabs = await (await fetch(`${debugBase}/json/list`)).json();
const tab = tabs.find(item => item.type === 'page');
assert(tab, 'A browser page must be available');
const socket = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
});
let sequence = 0;
const pending = new Map();
const responses = [];
const failures = [];
socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
        const request = pending.get(message.id);
        if (!request) return;
        pending.delete(message.id);
        clearTimeout(request.timeout);
        if (message.error) request.reject(new Error(message.error.message));
        else request.resolve(message.result);
    }
    if (message.method === 'Network.responseReceived') {
        const response = message.params.response;
        if (response.url.includes('/api/')) {
            responses.push({ url: response.url, status: response.status });
        }
    }
    if (message.method === 'Network.loadingFailed') failures.push(message.params);
    if (message.method === 'Runtime.exceptionThrown') {
        failures.push({ errorText: message.params.exceptionDetails.text });
    }
});

function command(method, params = {}) {
    return new Promise((resolve, reject) => {
        const id = ++sequence;
        const timeout = setTimeout(() => {
            pending.delete(id);
            reject(new Error(`Browser command timed out: ${method}`));
        }, 10000);
        pending.set(id, { resolve, reject, timeout });
        socket.send(JSON.stringify({ id, method, params }));
    });
}

try {
    await command('Page.enable');
    await command('Network.enable');
    await command('Runtime.enable');
    for (const page of ['/', '/stats.html', '/maintenance.html']) {
        responses.length = 0;
        failures.length = 0;
        await command('Page.navigate', { url: `${frontendBase}${page}` });
        const deadline = Date.now() + 25000;
        let state;
        do {
            await new Promise(resolve => setTimeout(resolve, 250));
            const result = await command('Runtime.evaluate', {
                expression: `JSON.stringify({
                    api: window.AppUtils && window.AppUtils.apiBase,
                    status: (document.querySelector('#hero-system-status, #stats-system-status') || {}).textContent,
                    indexes: document.querySelectorAll('#index-select option, #stats-index-select option').length,
                    maintenanceIndexes: document.getElementById('maintenance-index-count')?.textContent
                })`,
                returnByValue: true
            });
            state = JSON.parse(result.result.value);
            if (responses.some(item => item.url.includes('/api/indexes') && item.status === 200)
                && (state.status === 'Systeme actif' || (state.maintenanceIndexes || '').includes('2'))) break;
        } while (Date.now() < deadline);
        console.log(JSON.stringify({ page, state, responses, failures }));
        assert.equal(state.api, 'http://127.0.0.1:8500');
        assert(responses.some(item => item.url.includes('/api/indexes') && item.status === 200),
            `${page}: the browser must receive the index list`);
        assert(!responses.some(item => item.status >= 400), `${page}: no API request should be refused`);
        assert(!failures.some(item => item.blockedReason || item.corsErrorStatus),
            `${page}: no CSP or CORS failure should occur`);
        if (page !== '/maintenance.html') assert.equal(state.status, 'Systeme actif');
        else assert(state.maintenanceIndexes.includes('2'), 'The maintenance UI must show the two indexes');
    }
} finally {
    if (process.argv.includes('--close-browser')) await command('Browser.close');
    socket.close();
}
