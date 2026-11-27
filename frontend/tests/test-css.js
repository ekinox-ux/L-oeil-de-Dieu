/**
 * Verifie que chaque animation utilisee dans style.css a ses @keyframes.
 * Execute avec: node frontend/tests/test-css.js
 *
 * Une animation sans keyframes est ignoree silencieusement par le navigateur :
 * un element qui part de opacity 0 reste alors invisible.
 */

'use strict';

var fs = require('fs');
var path = require('path');

var css = fs.readFileSync(path.join(__dirname, '..', 'css', 'style.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');

var KEYWORDS = new Set([
    'none', 'initial', 'inherit', 'unset', 'infinite', 'normal', 'reverse',
    'alternate', 'alternate-reverse', 'forwards', 'backwards', 'both',
    'running', 'paused', 'linear', 'ease', 'ease-in', 'ease-out',
    'ease-in-out', 'step-start', 'step-end'
]);

var defined = new Set();
var keyframesRe = /@keyframes\s+([\w-]+)/g;
var match;
while ((match = keyframesRe.exec(css)) !== null) {
    defined.add(match[1]);
}

var used = new Set();
var animationRe = /(?:^|[\s;{])animation(?:-name)?\s*:\s*([^;}]+)/g;
while ((match = animationRe.exec(css)) !== null) {
    // Retire var(...), cubic-bezier(...), steps(...) avant de lire les noms.
    var value = match[1].replace(/[\w-]*\([^)]*\)/g, ' ');
    value.split(',').forEach(function (layer) {
        layer.trim().split(/\s+/).forEach(function (token) {
            if (/^-?[a-zA-Z_][\w-]*$/.test(token) && !KEYWORDS.has(token)) {
                used.add(token);
            }
        });
    });
}

var missing = Array.from(used).filter(function (name) { return !defined.has(name); }).sort();
var failed = 0;

if (used.size === 0) {
    failed++;
    console.error('FAIL: aucune animation trouvee dans style.css (regex a revoir)');
}
missing.forEach(function (name) {
    failed++;
    console.error('FAIL: animation "' + name + '" utilisee sans @keyframes');
});

console.log('\n' + used.size + ' animations verifiees, ' + failed + ' failed');
if (failed > 0) {
    process.exit(1);
}
