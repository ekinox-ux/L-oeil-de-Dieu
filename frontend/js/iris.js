/**
 * L'Oeil de Dieu - Theme Iris : fond de particules 3D et yeux animes.
 *
 * - Canvas #iris-canvas : galaxie de particules projetee en perspective,
 *   inclinee par la souris, aspiree en vortex pendant une recherche.
 * - Elements [data-eye] : l'iris suit le curseur (ou le texte saisi), la
 *   pupille se contracte ou se dilate, les paupieres clignent.
 * - prefers-reduced-motion : une seule image fixe, yeux ouverts au centre.
 *
 * Etats lus sur <body> : .is-scanning (recherche en cours, pose par search.js)
 * et .iris-dim (pages secondaires, fond attenue). La vue resultats est
 * detectee via #results-view et exposee en .iris-results pour le CSS.
 */
(function () {
    'use strict';

    var body = document.body;
    var canvas = document.getElementById('iris-canvas');
    var ctx = canvas && canvas.getContext ? canvas.getContext('2d') : null;
    var resultsView = document.getElementById('results-view');
    var mainEyeRoot = document.querySelector('[data-eye="main"]');
    var motionQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

    function reducedMotion() {
        return !!(motionQuery && motionQuery.matches);
    }

    function isVisible(el) {
        return !!(el && el.getClientRects().length);
    }

    function inResults() {
        return !!(resultsView && resultsView.classList.contains('state-active'));
    }

    function currentPhase() {
        if (body.classList.contains('is-scanning')) return 'scan';
        if (body.classList.contains('iris-dim') || inResults()) return 'results';
        return 'home';
    }

    var pointer = { x: 0, y: 0, nx: 0, ny: 0, at: -1e9, seen: false };
    var typing = { at: -1e9, len: 0 };
    var surpriseAt = -1e9;
    var blinkRequested = false;

    // ------------------------------------------------------------------
    // Particules
    // ------------------------------------------------------------------

    var W = 1;
    var H = 1;
    var particles = [];
    var tiltX = 0;
    var tiltY = 0;
    var center = { x: 0, y: 0, ready: false };

    function wantedCount() {
        return Math.max(160, Math.min(700, Math.round(W * H / 2600)));
    }

    function spawn(p, outer) {
        var r = outer ? 750 + Math.random() * 300 : 70 + Math.pow(Math.random(), 0.75) * 980;
        p.r = r;
        p.a = Math.random() * Math.PI * 2;
        p.y = (Math.random() - 0.5) * (30 + r * 0.28);
        p.s = 0.5 + Math.random() * 1.3;
        p.hot = Math.random() < 0.07;
        return p;
    }

    function resize() {
        W = Math.max(1, window.innerWidth);
        H = Math.max(1, window.innerHeight);
        if (!ctx) return;
        var dpr = Math.min(1.5, window.devicePixelRatio || 1);
        canvas.width = Math.round(W * dpr);
        canvas.height = Math.round(H * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = '#050505';
        ctx.fillRect(0, 0, W, H);
    }

    function vortexCenter(phase) {
        // Le vortex tourne autour du grand oeil quand il est affiche.
        if (isVisible(mainEyeRoot)) {
            var r = mainEyeRoot.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
        return { x: W / 2, y: phase === 'scan' ? H * 0.5 : H * 0.32 };
    }

    function drawParticles(dt, phase) {
        if (!ctx) return;
        var want = wantedCount();
        while (particles.length < want) particles.push(spawn({}, false));
        if (particles.length > want) particles.length = want;

        var speed = phase === 'scan' ? 7 : (phase === 'results' ? 0.45 : 1);
        var target = vortexCenter(phase);
        if (!center.ready) {
            center.x = target.x;
            center.y = target.y;
            center.ready = true;
        }
        var ease = 1 - Math.pow(0.94, dt);
        center.x += (target.x - center.x) * ease;
        center.y += (target.y - center.y) * ease;
        var follow = 1 - Math.pow(0.95, dt);
        tiltX += (pointer.ny * 0.22 - tiltX) * follow;
        tiltY += (pointer.nx * 0.38 - tiltY) * follow;

        // Voile noir translucide : laisse une trainee derriere chaque particule.
        ctx.globalCompositeOperation = 'source-over';
        ctx.fillStyle = phase === 'scan' ? 'rgba(5, 5, 5, 0.16)' : 'rgba(5, 5, 5, 0.3)';
        ctx.fillRect(0, 0, W, H);
        ctx.globalCompositeOperation = 'lighter';

        var tilt = 1.18 + tiltX;
        var cT = Math.cos(tilt);
        var sT = Math.sin(tilt);
        var cY = Math.cos(tiltY);
        var sY = Math.sin(tiltY);
        var dim = phase === 'results' ? 0.55 : 1;
        var fov = 760;

        for (var i = 0; i < particles.length; i++) {
            var p = particles[i];
            p.a += 0.004 * Math.sqrt(300 / (p.r + 60)) * speed * dt;
            if (phase === 'scan') {
                p.r *= Math.pow(0.986, dt);
                if (p.r < 45) spawn(p, true);
            }
            var x = Math.cos(p.a) * p.r;
            var z = Math.sin(p.a) * p.r;
            var x1 = x * cY - z * sY;
            var z1 = x * sY + z * cY;
            var y2 = p.y * cT - z1 * sT;
            var z2 = p.y * sT + z1 * cT;
            var k = fov / (1300 + z2);
            var sx = center.x + x1 * k;
            var sy = center.y + y2 * k;
            if (sx < -8 || sx > W + 8 || sy < -8 || sy > H + 8) continue;
            var near = Math.min(1, k);
            var alpha = Math.max(0.06, Math.min(1, k * 0.85)) * dim;
            ctx.fillStyle = p.hot
                ? 'rgba(255, 228, 230, ' + alpha.toFixed(3) + ')'
                : 'rgba(255, ' + Math.round(28 + 46 * near) + ', ' + Math.round(40 + 20 * near) + ', ' + (alpha * 0.85).toFixed(3) + ')';
            ctx.beginPath();
            ctx.arc(sx, sy, p.s * k * 1.5, 0, 6.2832);
            ctx.fill();
        }
    }

    // ------------------------------------------------------------------
    // Yeux
    // ------------------------------------------------------------------

    var eyes = [];
    Array.prototype.forEach.call(document.querySelectorAll('[data-eye]'), function (root) {
        var gaze = root.querySelector('.eye-gaze');
        if (!gaze) return;
        eyes.push({
            root: root,
            main: root.getAttribute('data-eye') === 'main',
            lid: root.querySelector('.eye-lid'),
            gaze: gaze,
            pupil: root.querySelector('.eye-pupil'),
            gx: 0,
            gy: 0
        });
    });

    var eyeState = {
        pupil: 1, open: 1,
        blinkT: -1, blinkAt: 0, dbl: false,
        sx: 0, sy: 0, nextSacc: 0,
        ix: 0, iy: 0, nextIdle: 0
    };

    function caretPoint() {
        // Pendant la frappe, l'oeil lit le texte : il vise la fin de la saisie.
        var el = document.activeElement;
        if (!el || el.tagName !== 'INPUT' || !isVisible(el)) return null;
        var r = el.getBoundingClientRect();
        var fontSize = parseFloat(window.getComputedStyle(el).fontSize) || 16;
        return {
            x: Math.min(r.right - 8, r.left + typing.len * fontSize * 0.6),
            y: r.top + r.height / 2
        };
    }

    function updateEyes(now, dt, phase) {
        var E = eyeState;
        if (!E.blinkAt) {
            E.blinkAt = now + 1600;
            E.nextIdle = now + 500;
        }
        var hasMouse = pointer.seen && now - pointer.at < 2600;
        var isTyping = phase !== 'scan' && now - typing.at < 1400;
        var surprised = now - surpriseAt < 380;

        // Saccades rapides pendant le scan, coups d'oeil au hasard au repos.
        if (phase === 'scan' && now > E.nextSacc) {
            E.sx = (Math.random() * 2 - 1) * 0.95;
            E.sy = (Math.random() * 2 - 1) * 0.75;
            E.nextSacc = now + 110 + Math.random() * 170;
        }
        if (now > E.nextIdle) {
            var centre = Math.random() < 0.3;
            E.ix = centre ? 0 : (Math.random() * 2 - 1) * 0.75;
            E.iy = centre ? 0 : (Math.random() * 2 - 1) * 0.55;
            E.nextIdle = now + 700 + Math.random() * 1900;
        }

        var focus = isTyping ? caretPoint() : null;
        var k = 1 - Math.pow(1 - (phase === 'scan' ? 0.5 : 0.22), dt);
        var near = false;
        var visible = [];

        for (var i = 0; i < eyes.length; i++) {
            var eye = eyes[i];
            if (!isVisible(eye.root)) continue;
            visible.push(eye);
            var box = eye.gaze.parentNode;
            var tx = E.ix;
            var ty = E.iy;
            if (phase === 'scan') {
                tx = E.sx;
                ty = E.sy;
            } else {
                var pt = focus || (hasMouse ? pointer : null);
                if (pt) {
                    var r = box.getBoundingClientRect();
                    var dx = pt.x - (r.left + r.width / 2);
                    var dy = pt.y - (r.top + r.height / 2);
                    var d = Math.sqrt(dx * dx + dy * dy) || 1;
                    var m = Math.min(1, d / (r.width * 1.1 + 60));
                    tx = dx / d * m;
                    ty = dy / d * m;
                    if (eye.main && !focus && d < r.width * 0.7) near = true;
                }
            }
            eye.gx += (tx - eye.gx) * k;
            eye.gy += (ty - eye.gy) * k;
            var ax = box.offsetWidth * 0.24;
            var ay = box.offsetHeight * 0.17;
            eye.gaze.style.transform = 'translate3d(' + (eye.gx * ax).toFixed(2) + 'px, ' + (eye.gy * ay).toFixed(2) + 'px, 0)' +
                (eye.main ? ' rotateY(' + (eye.gx * 30).toFixed(1) + 'deg) rotateX(' + (-eye.gy * 24).toFixed(1) + 'deg)' : '');
        }

        // Pupille : se contracte quand le curseur approche, se dilate pendant le scan.
        var pupilTarget;
        if (surprised) pupilTarget = 0.5;
        else if (phase === 'scan') pupilTarget = 1.3 + 0.22 * Math.sin(now / 95);
        else if (near) pupilTarget = 0.68;
        else if (isTyping) pupilTarget = 0.85;
        else pupilTarget = 1.05 + 0.05 * Math.sin(now / 900);
        E.pupil += (pupilTarget - E.pupil) * (1 - Math.pow(0.85, dt));

        // Clignement (parfois double), jamais pendant le scan.
        if (E.blinkT < 0 && phase !== 'scan' && (now > E.blinkAt || blinkRequested)) {
            E.blinkT = now;
            blinkRequested = false;
        }
        var b = 0;
        if (E.blinkT >= 0) {
            var u = now - E.blinkT;
            if (u < 75) b = u / 75;
            else if (u < 115) b = 1;
            else if (u < 245) b = 1 - (u - 115) / 130;
            else {
                E.blinkT = -1;
                if (!E.dbl && Math.random() < 0.22) {
                    E.dbl = true;
                    E.blinkAt = now + 110;
                } else {
                    E.dbl = false;
                    E.blinkAt = now + 2400 + Math.random() * 4300;
                }
            }
            b = b * b * (3 - 2 * b);
        }
        var openTarget = (surprised || phase === 'scan') ? 1.1 : 1;
        E.open += (openTarget - E.open) * (1 - Math.pow(0.8, dt));
        var lid = 'scaleY(' + (E.open * (1 - 0.93 * b)).toFixed(3) + ')';
        var pupil = 'scale(' + E.pupil.toFixed(3) + ')';

        for (var j = 0; j < visible.length; j++) {
            if (visible[j].lid) visible[j].lid.style.transform = lid;
            if (visible[j].pupil) visible[j].pupil.style.transform = pupil;
        }
    }

    function resetEyes() {
        eyes.forEach(function (eye) {
            eye.gaze.style.transform = '';
            if (eye.lid) eye.lid.style.transform = '';
            if (eye.pupil) eye.pupil.style.transform = '';
        });
    }

    // ------------------------------------------------------------------
    // Boucle
    // ------------------------------------------------------------------

    var running = false;
    var lastT = 0;
    var resultsClass = null;

    function syncResultsClass() {
        var on = inResults();
        if (on === resultsClass) return;
        resultsClass = on;
        body.classList.toggle('iris-results', on);
    }

    function tick(t) {
        if (!running) return;
        var dt = Math.min(3, Math.max(0.2, (t - lastT) / 16.67));
        lastT = t;
        var phase = currentPhase();
        syncResultsClass();
        drawParticles(dt, phase);
        updateEyes(t, dt, phase);
        window.requestAnimationFrame(tick);
    }

    function start() {
        if (reducedMotion()) {
            running = false;
            resetEyes();
            syncResultsClass();
            drawParticles(1, currentPhase());
            return;
        }
        if (running) return;
        running = true;
        lastT = window.performance.now();
        window.requestAnimationFrame(tick);
    }

    document.addEventListener('mousemove', function (e) {
        pointer.x = e.clientX;
        pointer.y = e.clientY;
        pointer.nx = (e.clientX / W) * 2 - 1;
        pointer.ny = (e.clientY / H) * 2 - 1;
        pointer.at = window.performance.now();
        pointer.seen = true;
    }, { passive: true });

    document.addEventListener('input', function (e) {
        var el = e.target;
        if (!el || el.tagName !== 'INPUT') return;
        typing.at = window.performance.now();
        typing.len = (el.value || '').length;
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT') {
            surpriseAt = window.performance.now();
        }
    });

    document.addEventListener('click', function (e) {
        var el = e.target;
        if (!el || !el.closest) return;
        if (el.closest('.search-submit')) surpriseAt = window.performance.now();
        if (el.closest('[data-eye="main"]')) {
            blinkRequested = true;
            surpriseAt = window.performance.now() - 150;
        }
    });

    window.addEventListener('resize', function () {
        resize();
        if (!running) drawParticles(1, currentPhase());
    });

    if (motionQuery) {
        var onMotionChange = function () {
            running = false;
            start();
        };
        if (motionQuery.addEventListener) motionQuery.addEventListener('change', onMotionChange);
        else if (motionQuery.addListener) motionQuery.addListener(onMotionChange);
    }

    resize();
    start();
})();
