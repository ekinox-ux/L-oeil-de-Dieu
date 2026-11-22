# Architecture backend

## Analyse des sources

L'analyse des fichiers JSONL (`analyzer.py`) tourne dans un **process Python separe**,
lance via `subprocess.Popen` depuis la route `/api/analyze`.

### Serialisation

- Le **lockfile** (`CACHE_DIR/analyzer_<nom>.lock`) est acquis **cote parent**
  (dans `run_analysis()`) avant le `Popen`. Cela garantit qu'un seul worker peut etre
  lance a la fois, meme en cas d'appels rapproches.
- Le lockfile contient un JSON avec `source_dir`, `owner_pid`, puis `worker_pid` et
  un `heartbeat_at` renseignes par le worker.
- Si `Popen` echoue, le lock est libere immediatement et `run_analysis()` retourne `False`.
- Le **worker enfant** supprime le lockfile en `finally`; un thread watchdog cote
  parent le libere aussi si le worker meurt sans nettoyer.

### Cle des artefacts

Tous les artefacts d'une source sont nommes a partir du chemin **tel que configure**
dans `indexes.local.json` (libelle + empreinte SHA-256; les anciens noms
`<chemin_aplati>` sont encore relus). `/api/analyze` valide le chemin resolu contre
les racines autorisees mais transmet la cle configuree au worker: les routes de
statistiques, les diagnostics et `doctor.py` relisent ainsi exactement les memes
fichiers, meme si le chemin resolu differe (lecteur reseau resolu en UNC).

### Fichiers persistants

- **Lockfile** : `CACHE_DIR/analyzer_<nom>.lock` — present = analyse en cours
- **Progression** : `CACHE_DIR/progress_<nom>.sqlite` — base SQLite (WAL) mise a jour
  fichier par fichier, dans une transaction par fichier source. Elle conserve les
  uniques exacts (`seen_*`), les compteurs de classement plafonnes, les fichiers
  traites et en echec. Supprimee quand l'analyse est complete et sans echec.
- **Resultat** : `CACHE_DIR/analysis_<nom>.json` — instantane ecrit toutes les
  `OEIL_ANALYZER_SAVE_EVERY` fichiers puis en fin d'analyse.
- **Journal** : `CACHE_DIR/analyzer.log` — ecrit par le seul worker (rotation 3 x 10 Mo).

Une progression illisible est renommee en `progress_<nom>.sqlite.corrupt` (une seule
quarantaine conservee) et l'analyse repart de zero. Une ancienne progression JSON est
migree en streaming (ijson) vers SQLite.

### Status apres restart backend

`/api/analyze/status` appelle `get_status()` qui **scanne les lockfiles** dans `CACHE_DIR`
pour detecter une analyse en cours. Il ne depend d'aucune variable memoire volatile.

Apres un restart du backend :
- Si un lockfile existe, `get_status()` le trouve et lit le `source_dir` depuis son contenu
- Il lit ensuite le resume de progression pour remplir l'avancement
- Si aucun lockfile n'existe, `get_status()` retombe sur le cache d'analyse le plus recent
- Le status est donc **reconstruit entierement depuis les fichiers persistants**

### Lockfile orphelin

Si le worker crash sans cleanup (kill -9, coupure courant), le lockfile peut rester.
- Un nettoyage automatique supprime les lockfiles de plus de 24 h, sauf si la
  progression associee a ete modifiee recemment.
- `POST /api/diagnostics/analyzer-locks/cleanup` (bouton de la page Maintenance)
  applique la meme regle a la demande.
- `stop_project.ps1` arrete un ancien worker uniquement si sa ligne de commande
  prouve qu'il s'agit de l'analyseur de ce projet, puis retire son lock.

Le worker peut etre lance manuellement :
```
python analyzer.py <source_dir> [--no-resume]
```

## Cache des statistiques

`SearchEngine.stats()` utilise **deux caches separes** :

| Donnee | TTL | Recalcul |
|--------|-----|----------|
| `num_docs`, `num_segments`, `segments` | 5 min | `index.reload()` + `searcher` (rapide) |
| `index_size_bytes` | 1 h | `os.walk` du dossier index (stale-while-revalidate) |

### Comportement detaille

- **Dans les 5 premieres minutes** : tout est servi depuis le cache. Aucun recalcul.
- **Apres 5 min** : `num_docs`/`num_segments` sont recalcules (rapide). `index_size`
  reste en cache tant que son TTL (1h) n'est pas depasse.
- **Apres 1h** : `index_size` retourne la **valeur perimee immediatement** et lance
  un recalcul en arriere-plan (thread daemon). Un flag `_index_size_refreshing` empeche
  les recalculs concurrents. La valeur fraiche est disponible au prochain appel stats.
- **Premier appel** (demarrage, aucun cache) : la taille est lancee en
  arriere-plan; la reponse indique `index_size_pending=true` jusqu'a sa disponibilite.

## Analyseur de requetes

`SearchEngine` reconstruit l'analyseur de l'indexeur Rust a partir de
`<index>/.schema_config.toml`, dans le meme ordre de filtres (remove_long,
lowercase, stemmer, stopword). Les codes de langue de l'indexeur (`fr`, `en`, ...)
sont traduits vers les noms attendus par tantivy-py (`french`, `english`, ...).
Aucun filtre n'est ignore silencieusement: un analyseur different de celui de
l'indexation ferait manquer des resultats sans erreur. En cas d'echec, l'index
passe en erreur avec un message explicite.

## Securite HTTP

- `TrustedHostMiddleware` (`OEIL_ALLOWED_HOSTS`) rejette les en-tetes `Host`
  inattendus avant tout traitement (DNS rebinding).
- `verify_api_key` accepte `X-API-Key` ou, depuis la boucle locale uniquement,
  l'en-tete `X-Oeil-Local-Client: 1` avec une origine autorisee.
- Le limiteur compte les requetes par adresse et par type de client
  (`rate_limit_key`), apres l'authentification.
- Les logs d'acces uvicorn sont filtres pour retirer la query string.

## Donnees temps reel vs cache

| Champ | Fraicheur |
|-------|-----------|
| `total_docs` | Recalcule toutes les 5 min (reload tantivy) |
| `num_segments` | Recalcule toutes les 5 min |
| `segments` | Recalcule toutes les 5 min |
| `index_size` | Recalcule toutes les 1h (stale-while-revalidate, thread) |
| `analysis.*` | Cache fichier, mis a jour par le worker analyse |

## Frontend KPI

La homepage affiche 3 KPI (documents, segments, taille index) via `/api/stats`.

- Etat **loading** : les valeurs affichent `--` avec animation pulse
- En cas d'**erreur** : un retry automatique apres 3s, puis fallback `--`
- Un compteur monotone (`_kpiGeneration`) empeche un retry obsolete de mettre a jour
  l'UI si un nouvel appel KPI a demarre entre-temps
- Le reste de la page n'est pas bloque par un echec des stats

## Tests

- `backend/tests/conftest.py` isole les tests: `CACHE_DIR`, configuration des
  index et cles des providers pointent vers un dossier temporaire, supprime en
  fin de session. Aucun test n'ecrit dans les vrais fichiers du projet.
- `frontend/tests/` : tests jsdom du code de production (`npm test`).
- `indexer/` : `cargo test` (schema, reset protege, remplacement, merge, ...).

## Limites connues

- Le worker analyse herite du meme filesystem. Si le disque reseau est lent,
  l'analyse sera lente — mais l'API n'est pas impactee.
- `os.walk` pour `index_size` reste le mecanisme de calcul. Il est toujours
  execute en arriere-plan (premier calcul et stale-while-revalidate, au plus 1x/heure).
- Un seul worker analyse a la fois (pas de parallelisme).
