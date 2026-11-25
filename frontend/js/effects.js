/**
 * L'Oeil de Dieu - Effets visuels
 * Balayage lumineux au lancement d'une recherche.
 * (Le fond de particules et les yeux animes sont dans iris.js.)
 */

(function () {
    'use strict';

    var scanLine = document.getElementById('scan-line');

    window.Effects = {
        triggerScan: function () {
            if (!scanLine) return;
            scanLine.classList.remove('active');
            void scanLine.offsetWidth;
            scanLine.classList.add('active');
            setTimeout(function () {
                scanLine.classList.remove('active');
            }, 700);
        }
    };
})();
