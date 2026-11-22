/**
 * L'Oeil de Dieu - Effets visuels
 * Cursor glow, click ripples, scan animation
 */

(function () {
    'use strict';

    var cursorGlow = document.getElementById('cursor-glow');
    var scanLine = document.getElementById('scan-line');
    var mouseX = 0;
    var mouseY = 0;
    var glowX = 0;
    var glowY = 0;
    var animationFrameId = null;
    var glowActive = false;

    function updateGlow() {
        if (!glowActive) {
            animationFrameId = null;
            return;
        }

        glowX += (mouseX - glowX) * 0.12;
        glowY += (mouseY - glowY) * 0.12;
        cursorGlow.style.left = glowX + 'px';
        cursorGlow.style.top = glowY + 'px';
        animationFrameId = requestAnimationFrame(updateGlow);
    }

    function ensureGlowLoop() {
        if (!glowActive || animationFrameId !== null) {
            return;
        }
        animationFrameId = requestAnimationFrame(updateGlow);
    }

    document.addEventListener('mousemove', function (e) {
        mouseX = e.clientX;
        mouseY = e.clientY;
        glowActive = true;
        cursorGlow.classList.add('active');
        ensureGlowLoop();
    });

    document.addEventListener('mouseleave', function () {
        glowActive = false;
        cursorGlow.classList.remove('active');
    });

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            glowActive = false;
            cursorGlow.classList.remove('active');
            return;
        }
        if (cursorGlow.classList.contains('active')) {
            glowActive = true;
            ensureGlowLoop();
        }
    });

    document.addEventListener('click', function (e) {
        var ripple = document.createElement('div');
        ripple.className = 'click-ripple';
        ripple.style.left = e.clientX + 'px';
        ripple.style.top = e.clientY + 'px';
        document.body.appendChild(ripple);
        setTimeout(function () { ripple.remove(); }, 600);
    });

    window.Effects = {
        triggerScan: function () {
            scanLine.classList.remove('active');
            void scanLine.offsetWidth;
            scanLine.classList.add('active');
            setTimeout(function () {
                scanLine.classList.remove('active');
            }, 700);
        }
    };
})();
