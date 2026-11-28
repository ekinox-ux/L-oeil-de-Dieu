/**
 * Composants de rendu pour les resultats de recherche.
 *
 * Depend de window.AppUtils (app-utils.js charge avant).
 *
 * Conventions et invariants:
 *
 * - Donnees d'entree (hit):
 *     { score: number, source: string|null, filename: string|null,
 *       type: string|null, snippets: Array<{text: string, highlights: Array<[start,end]>}> }
 *   Tous les champs optionnels peuvent etre null/undefined — les fonctions
 *   utilisent des fallbacks ("", 0, []) plutot que d'echouer.
 *
 * - Echappement HTML:
 *     Tout texte utilisateur (source, snippet.text, query) passe par
 *     AppUtils.escapeHtml() avant insertion dans innerHTML.
 *     Les seuls elements inseres en innerHTML sont les <mark> de highlights
 *     generes par renderSnippet() — jamais de contenu utilisateur brut.
 *
 * - Extraction du nom de source:
 *     extractCompanyName() remonte le path pour trouver le dossier parent
 *     significatif (ignore FRANCE, FRANCE-CSV, volumes Windows).
 *     En cas d'echec, retourne le path complet.
 *
 * - Verification des fuites:
 *     Le bouton "Verifier fuites" n'apparait que si la query contient un email
 *     (regex EMAIL_RE). L'appel API est delegue a SearchApi.fetchBreaches()
 *     qui interroge /api/breaches (multi-provider: HIBP, BreachDirectory).
 *
 * - Positions des highlights:
 *     Le backend compte en caracteres Unicode (points de code), JavaScript en
 *     unites UTF-16 : renderSnippet() decoupe donc le texte par point de code,
 *     sinon le surlignage se decale apres un emoji ou un caractere rare.
 *
 * Exporte: window.SearchComponents = { renderCard, renderSnippet, safeHttpUrl }
 */
(function () {
    'use strict';

    var AppUtils = window.AppUtils;
    var escapeHtml = AppUtils.escapeHtml;
    var extractCompanyName = AppUtils.extractCompanyName;

    function renderSnippet(snippet) {
        if (!snippet || !snippet.text) return '';
        var text = snippet.text;
        var highlights = (snippet.highlights || []).slice();
        if (highlights.length === 0) {
            return escapeHtml(text);
        }

        // Array.from decoupe par point de code, comme les positions Python.
        var chars = Array.from(text);
        var slice = function (start, end) { return chars.slice(start, end).join(''); };
        highlights.sort(function (a, b) { return a[0] - b[0]; });
        var result = '';
        var lastEnd = 0;
        highlights.forEach(function (h) {
            var start = Math.max(Number(h[0]) || 0, lastEnd);
            var end = Math.min(Number(h[1]) || 0, chars.length);
            if (end <= start) return; // vide, hors texte ou chevauchant
            result += escapeHtml(slice(lastEnd, start));
            result += '<mark>' + escapeHtml(slice(start, end)) + '</mark>';
            lastEnd = end;
        });
        result += escapeHtml(slice(lastEnd, chars.length));
        return result;
    }

    /** Retourne l'URL si elle est en http(s), sinon null (javascript:, data:...). */
    function safeHttpUrl(value) {
        if (typeof value !== 'string' || !value) return null;
        try {
            var url = new URL(value);
            return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
        } catch (e) {
            return null;
        }
    }

    var EMAIL_RE = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/;

    function extractEmail(query) {
        var match = query.match(EMAIL_RE);
        return match ? match[0] : null;
    }

    function renderBreachesSection(data) {
        var div = document.createElement('div');
        div.className = 'hibp-section';

        var summary = data.summary || {};
        if (summary.total_hits === 0 && summary.providers_failed === 0) {
            div.innerHTML = '<div class="hibp-clean">Aucune fuite connue.</div>';
            return div;
        }

        var providers = data.providers || [];
        providers.forEach(function (provider) {
            var providerDiv = document.createElement('div');
            providerDiv.className = 'breach-provider';

            var providerHeader = document.createElement('div');
            providerHeader.className = 'breach-provider-header';
            var statusDot = document.createElement('span');
            statusDot.className = provider.ok ? 'breach-status-ok' : 'breach-status-err';
            statusDot.textContent = provider.ok ? '\u2713' : '\u2717';
            providerHeader.appendChild(statusDot);
            var providerName = document.createElement('span');
            providerName.className = 'breach-provider-name';
            providerName.textContent = ' ' + (provider.name || '');
            if (!provider.ok) {
                providerName.textContent += ' — ' + (provider.error || 'erreur');
            } else {
                providerName.textContent += ' — ' + provider.results.length + ' fuite(s)';
            }
            providerHeader.appendChild(providerName);
            providerDiv.appendChild(providerHeader);

            if (provider.ok && provider.results.length > 0) {
                provider.results.forEach(function (b) {
                    var entry = document.createElement('div');
                    entry.className = 'hibp-breach';

                    var logoUrl = safeHttpUrl(b.logo_url);
                    if (logoUrl) {
                        var logo = document.createElement('img');
                        logo.className = 'hibp-breach-logo';
                        logo.src = logoUrl;
                        logo.alt = b.name || '';
                        logo.width = 24;
                        logo.height = 24;
                        entry.appendChild(logo);
                    }

                    var info = document.createElement('div');
                    info.className = 'hibp-breach-info';

                    // -- Compact summary (always visible) --
                    var summary = document.createElement('div');
                    summary.className = 'hibp-breach-summary';

                    var nameSpan = document.createElement('span');
                    nameSpan.className = 'hibp-breach-name';
                    nameSpan.textContent = b.name || '?';
                    summary.appendChild(nameSpan);

                    if (b.password_exposed) {
                        var pwdBadge = document.createElement('span');
                        pwdBadge.className = 'hibp-breach-pwd';
                        pwdBadge.textContent = 'MDP expose';
                        summary.appendChild(pwdBadge);
                    }

                    if (b.breach_date) {
                        var dateSpan = document.createElement('span');
                        dateSpan.className = 'hibp-breach-date';
                        dateSpan.textContent = b.breach_date;
                        summary.appendChild(dateSpan);
                    }

                    var hibp = b.hibp_details;
                    if (hibp) {
                        var flagDefs = [
                            ['is_verified', 'verified', 'Verifie'],
                            ['is_sensitive', 'sensitive', 'Sensible'],
                            ['is_fabricated', 'fabricated', 'Fabrique'],
                            ['is_retired', 'retired', 'Retire'],
                            ['is_spam_list', 'spam', 'Spam'],
                            ['is_malware', 'malware', 'Malware'],
                            ['is_subscription_free', 'subfree', 'Abo gratuit'],
                            ['is_stealer_log', 'stealer', 'Stealer']
                        ];
                        flagDefs.forEach(function (def) {
                            if (hibp[def[0]]) {
                                var badge = document.createElement('span');
                                badge.className = 'hibp-badge ' + def[1];
                                badge.textContent = def[2];
                                summary.appendChild(badge);
                            }
                        });
                    }

                    var chevron = document.createElement('span');
                    chevron.className = 'hibp-breach-chevron';
                    chevron.textContent = '\u25B6';
                    summary.appendChild(chevron);

                    info.appendChild(summary);

                    // -- Details (hidden by default, shown on click) --
                    var details = document.createElement('div');
                    details.className = 'hibp-breach-details';

                    if (hibp) {
                        if (hibp.title && hibp.title !== b.name) {
                            var titleSpan = document.createElement('span');
                            titleSpan.className = 'hibp-breach-title';
                            titleSpan.textContent = hibp.title;
                            details.appendChild(titleSpan);
                        }
                    }

                    if (hibp && hibp.added_date) {
                        var addedSpan = document.createElement('span');
                        addedSpan.className = 'hibp-breach-date';
                        addedSpan.textContent = 'Ajout HIBP: ' + hibp.added_date.substring(0, 10);
                        details.appendChild(addedSpan);
                    }
                    if (hibp && hibp.modified_date) {
                        var modSpan = document.createElement('span');
                        modSpan.className = 'hibp-breach-date';
                        modSpan.textContent = 'Maj: ' + hibp.modified_date.substring(0, 10);
                        details.appendChild(modSpan);
                    }

                    if (b.domain) {
                        var domainSpan = document.createElement('span');
                        domainSpan.className = 'hibp-breach-domain';
                        domainSpan.textContent = b.domain;
                        details.appendChild(domainSpan);
                    }

                    if (hibp && hibp.pwn_count > 0) {
                        var pwnSpan = document.createElement('span');
                        pwnSpan.className = 'hibp-breach-pwn';
                        pwnSpan.textContent = hibp.pwn_count.toLocaleString('fr-FR') + ' comptes';
                        details.appendChild(pwnSpan);
                    }

                    if (hibp && hibp.attribution) {
                        var attrSpan = document.createElement('span');
                        attrSpan.className = 'hibp-breach-attr';
                        attrSpan.textContent = 'Attribution: ' + hibp.attribution;
                        details.appendChild(attrSpan);
                    }

                    var disclosureUrl = hibp ? safeHttpUrl(hibp.disclosure_url) : null;
                    if (disclosureUrl) {
                        var discLink = document.createElement('a');
                        discLink.className = 'hibp-breach-disclosure';
                        discLink.href = disclosureUrl;
                        discLink.target = '_blank';
                        discLink.rel = 'noopener noreferrer';
                        discLink.textContent = 'Disclosure';
                        details.appendChild(discLink);
                    }

                    var dataTypes = b.data_classes || [];
                    if (dataTypes.length > 0) {
                        var dataDiv = document.createElement('div');
                        dataDiv.className = 'hibp-breach-data';
                        dataDiv.textContent = dataTypes.join(', ');
                        details.appendChild(dataDiv);
                    }

                    if (b.description) {
                        var descDiv = document.createElement('div');
                        descDiv.className = 'hibp-breach-desc';
                        descDiv.textContent = b.description.replace(/<[^>]*>/g, '');
                        details.appendChild(descDiv);
                    }

                    var bdEntries = b.bd_entries || [];
                    if (bdEntries.length > 0) {
                        var credsDiv = document.createElement('div');
                        credsDiv.className = 'bd-credentials';
                        bdEntries.forEach(function (cred) {
                            var credLine = document.createElement('div');
                            credLine.className = 'bd-cred-line';
                            var parts = [];
                            if (cred.password) {
                                parts.push('mdp: ' + cred.password);
                            }
                            if (cred.hash_password) {
                                parts.push('hash_pwd: oui');
                            }
                            if (cred.sha1) {
                                parts.push('sha1: ' + cred.sha1);
                            }
                            if (cred.hash) {
                                parts.push('hash: ' + cred.hash);
                            }
                            if (parts.length === 0 && !cred.has_password) {
                                parts.push('pas de mot de passe');
                            }
                            credLine.textContent = parts.join(' | ');
                            credsDiv.appendChild(credLine);
                        });
                        details.appendChild(credsDiv);
                    }

                    info.appendChild(details);

                    // Click to toggle details
                    entry.addEventListener('click', function () {
                        entry.classList.toggle('expanded');
                    });

                    entry.appendChild(info);
                    providerDiv.appendChild(entry);
                });
            }

            div.appendChild(providerDiv);
        });

        return div;
    }

    function createCardIcon(source) {
        var name = extractCompanyName(source);
        var letter = name.charAt(0).toUpperCase();
        var iconDiv = document.createElement('div');
        iconDiv.className = 'card-icon';

        var span = document.createElement('span');
        span.className = 'letter-icon';
        span.textContent = letter;
        iconDiv.appendChild(span);

        return iconDiv;
    }

    function renderCard(hit, index, query) {
        var card = document.createElement('div');
        card.className = 'result-card';
        card.style.animationDelay = (index * 0.06) + 's';

        var snippetHtml = '';
        if (hit.snippets && hit.snippets.length > 0) {
            snippetHtml = hit.snippets.map(renderSnippet).join('<hr class="snippet-sep">');
        }

        var header = document.createElement('div');
        header.className = 'card-header';
        var icon = createCardIcon(hit.source || '');
        var titleGroup = document.createElement('div');
        titleGroup.className = 'card-title-group';

        var filename = document.createElement('div');
        filename.className = 'card-filename';
        filename.textContent = extractCompanyName(hit.source || '');

        var sourceDiv = document.createElement('div');
        sourceDiv.className = 'card-source';
        sourceDiv.textContent = hit.source || '';
        titleGroup.appendChild(filename);
        titleGroup.appendChild(sourceDiv);

        var scoreDiv = document.createElement('div');
        scoreDiv.className = 'card-score';
        scoreDiv.textContent = (hit.score || 0).toFixed(2);

        header.appendChild(icon);
        header.appendChild(titleGroup);
        header.appendChild(scoreDiv);

        var typeSpan = document.createElement('span');
        typeSpan.className = 'card-type';
        typeSpan.textContent = hit.type || 'unknown';

        var snippetDiv = document.createElement('div');
        snippetDiv.className = 'card-snippet';
        snippetDiv.innerHTML = snippetHtml;

        var expandHint = document.createElement('div');
        expandHint.className = 'card-expand-hint';
        expandHint.innerHTML = '<span class="hint-text-more">Cliquer pour voir tout</span><span class="hint-text-less">Cliquer pour reduire</span>';

        card.appendChild(header);
        card.appendChild(typeSpan);
        card.appendChild(snippetDiv);
        if (snippetHtml) {
            card.appendChild(expandHint);
        }

        card.addEventListener('click', function () {
            card.classList.toggle('expanded');
        });

        var email = extractEmail(query || '');
        if (email) {
            var breachBtn = document.createElement('button');
            breachBtn.className = 'hibp-btn';
            breachBtn.textContent = 'Verifier fuites';
            breachBtn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (breachBtn.disabled) return;
                breachBtn.disabled = true;
                breachBtn.textContent = 'Verification...';

                window.SearchApi.fetchBreaches(email)
                    .then(function (data) {
                        var section = renderBreachesSection(data);
                        breachBtn.parentNode.insertBefore(section, breachBtn.nextSibling);
                        breachBtn.remove();
                    })
                    .catch(function (err) {
                        breachBtn.disabled = false;
                        breachBtn.textContent = 'Verifier fuites';
                        var errDiv = document.createElement('div');
                        errDiv.className = 'hibp-section hibp-error';
                        errDiv.textContent = err.message || 'Erreur verification fuites';
                        breachBtn.parentNode.insertBefore(errDiv, breachBtn.nextSibling);
                    });
            });
            card.appendChild(breachBtn);
        }

        return card;
    }

    window.SearchComponents = {
        renderCard: renderCard,
        renderSnippet: renderSnippet,
        safeHttpUrl: safeHttpUrl
    };
})();
