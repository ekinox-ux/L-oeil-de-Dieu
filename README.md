# L'Oeil de Dieu

Projet local d'indexation et d'exploration OSINT autour de fichiers `jsonl`.

## Composants
- `indexer/`: indexeur Rust/Tantivy pour construire les index de recherche.
- `backend/`: API FastAPI qui expose recherche, stats, analyse des sources et breach lookup.
- `frontend/`: interface HTML/CSS/JS pour la recherche et les statistiques.
- `bot/`: bot Discord branche sur l'API backend.

## Demarrage rapide Windows
1. Prerequis:
   - Python 3.14 (`py -3.14`);
   - Rust via [rustup](https://rustup.rs) (la version exacte est fixee par
     `rust-toolchain.toml` et installee automatiquement par rustup);
   - outils C++ MSVC: Visual Studio ou Build Tools, charge de travail
     "Developpement Desktop C++" (editeur de liens et compilation de mimalloc).
2. Creer l'environnement: `powershell -ExecutionPolicy Bypass -File .\setup.ps1`
   (`-Development` ajoute pytest et Ruff; `-Recreate` archive un ancien venv invalide).
3. Configurer `backend\.env` et `bot\.env` a partir des fichiers `.env.example`.
4. Copier `backend\indexes.example.json` en `backend\indexes.local.json` puis
   l'adapter a vos chemins source/index.
5. Compiler l'indexeur Rust: `powershell -ExecutionPolicy Bypass -File indexer\build_windows.ps1`.
   Les index compresses en zstd exigent aussi un tantivy Python recompile:
   `powershell -ExecutionPolicy Bypass -File .\build_tantivy_zstd.ps1 -Install`
   (voir "Compression zstd et commande `compress`").
6. Lancer l'ensemble: `start.bat`.


## Fichiers de configuration
- `backend/.env`: ports, cle API, racines autorisees, chemin de config des index.
- `backend/indexes.local.json`: declaration des index disponibles pour l'API (defaut au demarrage).
- `bot/.env`: token Discord et URL d'API.
- `frontend/js/config.js`: URL du backend pour le front local.

Les fichiers `.env`, `indexes.local.json`, les journaux et tous les artefacts de
l'analyseur (`analysis_*.json`, `progress_*`) sont exclus du depot: ils
contiennent des secrets, des chemins locaux ou des donnees personnelles.

## Architecture backend
Le backend est organise en modules:
- `main.py`: point d'entree FastAPI, middleware, lifespan.
- `routes/`: endpoints separes par domaine (search, stats, indexes, breaches).
- `services/`: logique metier isolee (breach_lookup: agregation multi-provider).
- `models.py`: modeles Pydantic pour les reponses API.
- `deps.py`: dependances partagees (auth, validation, gestion des index, rate limiter).
- `engine.py`: wrapper tantivy-py pour la recherche et les stats.
- `analyzer.py`: analyse des fichiers JSONL sources (emails, telephones).
- `snippets.py`: extraction de snippets avec surlignage.
- `query_terms.py`: decoupage des requetes comme le tokenizer de l'index
  (repli des requetes refusees par le parseur, regex de surlignage).
- `settings.py`: configuration centralisee via variables d'environnement.

### Chargement des index

- Tous les index de `backend/indexes.local.json` sont enregistres immediatement
  puis charges en arriere-plan. Le serveur HTTP reste disponible pendant ce chargement.
- `GET /api/indexes` expose pour chaque index: `id`, `name`, `path`, `source`, `status`, `error`, metadonnees de retry (`retry_count`, `next_retry_at`, `retry_delay_seconds`, `last_error_at`, `last_ready_at`).
- Les statuts possibles sont `ready`, `loading`, `error`.
- `GET /api/health` distingue:
  - `indexes`: liste complete des index configures
  - `indexes_ready`: index prets
  - `indexes_loading`: index encore en chargement
  - `indexes_error`: index actuellement en erreur
- `GET /api/health/live` indique uniquement que le processus HTTP vit.
- `GET /api/health/ready` renvoie `200` lorsqu'au moins un index est pret,
  sinon `503`. Le champ `ready` est aussi present dans `GET /api/health`.
- `GET /api/diagnostics` fournit une vue locale exploitable: etat des index, retries, presence des lockfiles Tantivy, presence des chemins source et index.

### Lockfiles Tantivy

- L'ouverture des index tente d'abord un `open` normal.
- Si Tantivy signale un verrou, le backend inspecte uniquement:
  - `.tantivy-meta.lock`
  - `.tantivy-writer.lock`
- Aucun lockfile Tantivy n'est supprime automatiquement: son age ne permet pas
  de conclure que le writer qui le detient est mort. Le diagnostic donne son
  chemin et son age; toute intervention reste manuelle apres arret du writer.

Variables utiles:

| Variable | Description | Defaut |
|----------|-------------|--------|
| `OEIL_INDEX_RETRY_DELAY_SECONDS` | Delai initial entre retries de chargement d'index | `10` |
| `OEIL_INDEX_MAX_RETRY_DELAY_SECONDS` | Plafond du backoff de retry | `60` |

### Analyse des sources

- `POST /api/analyze` lance l'analyse dans un **process Python separe**.
- Un lockfile simple dans `CACHE_DIR` empeche le double lancement.
- `GET /api/analyze/status` reconstruit l'etat depuis les fichiers persistants (lockfile, progress).
- `current_file` indique le fichier effectivement en cours.
- Chaque fichier est traite dans une transaction SQLite: un fichier illisible
  est annule, liste dans `analysis.failed_files` et retente a la prochaine reprise.
- Les uniques exacts restent sur disque; seuls les compteurs des tops sont
  plafonnes en memoire. La progression est mise a jour incrementiellement,
  sans reconstruire une copie `.sqlite.tmp` complete.
- Une ligne JSONL depassant `OEIL_ANALYZER_MAX_LINE_BYTES` (8 Mio par defaut)
  met le fichier en erreur au lieu de provoquer une allocation sans limite.
- Un lockfile dont le worker (`worker_pid`) ne tourne plus est supprime
  aussitot, y compris par le bouton de nettoyage. Avant que le worker ait
  inscrit son PID, le lock appartient au backend qui l'a lance (`owner_pid`),
  avec 2 minutes de grace au demarrage. Sinon, les lockfiles trop anciens sont
  auto-nettoyes (TTL 24h) sauf si un `progress` recent indique encore de
  l'activite.
- La cle des artefacts (cache, progression, lock) est le chemin source **tel que
  configure** dans `indexes.local.json`, celui que relisent `/api/stats`,
  `/api/diagnostics` et `doctor.py`. Le chemin resolu sert uniquement au
  controle des racines autorisees: un lecteur reseau resolu en UNC ne cree donc
  plus de cache orphelin. `~` et les variables (`%VAR%`, `${VAR}`) sont
  developpes uniquement pour le parcours du disque.
- Une progression SQLite illisible est mise de cote (`progress_*.sqlite.corrupt`)
  et l'analyse repart de zero au lieu d'echouer a chaque lancement.
- Seul le worker ecrit `analyzer.log` (rotation 3 x 10 Mo). Dans l'API, les
  messages de l'analyseur rejoignent le journal uvicorn.
- `OEIL_ANALYZER_MAX_EMAIL_ENTRIES`, `..._PHONE_ENTRIES` et `..._DOMAIN_ENTRIES`
  plafonnent les compteurs de classement (defauts 100 000 / 50 000 / 100 000).
  Une valeur vide, nulle ou negative applique le defaut: l'ancien `0` ("illimite")
  ne peut plus tronquer silencieusement les classements a une seule entree.

## Contrat API de recherche
`GET /api/search?q=...&limit=20&offset=0&context=2500&index=france`

Une requete que le parseur Tantivy refuse (apostrophe comme `O'Brien`, URL
`http://...` lue comme un champ, adresse IPv6, parenthese seule) n'echoue plus
en `400` : chaque mot est relance comme la phrase de ses tokens
(`o'brien` -> `"o brien"`), ce que l'index aurait de toute facon compare. Un
`400` ne reste possible que si ce repli echoue aussi.

La pagination standard ne deduplique pas les resultats: `total`, `hits` et les
pages restent ainsi strictement coherents. `deduplicate=true` active une
deduplication optionnelle limitee a la page; dans ce mode `total_is_raw=true`.

Reponse:
```json
{
  "query": "john",
  "total": 150,
  "limit": 20,
  "offset": 0,
  "has_more": true,
  "total_pages": 8,
  "deduplicated": false,
  "total_is_raw": false,
  "index_id": "france",
  "index_name": "France",
  "hits": [
    {
      "score": 1.5,
      "source": "C:/data/company/file.jsonl",
      "filename": "file.jsonl",
      "type": "record",
      "snippets": [{"text": "...john...", "highlights": [[3, 7]]}]
    }
  ]
}
```

### Metadonnees de recherche

| Champ | Type | Description |
|-------|------|-------------|
| `total` | int | Nombre total de documents correspondants dans l'index |
| `limit` | int | Nombre max de hits demandes par page |
| `offset` | int | Decalage dans la liste de resultats |
| `has_more` | bool | `true` s'il reste des resultats apres cette page |
| `total_pages` | int | Nombre total de pages (`ceil(total / limit)`, minimum 1) |
| `deduplicated` | bool | Indique si la deduplication optionnelle de page a ete appliquee |
| `total_is_raw` | bool | `true` si `total` compte les hits bruts avant deduplication |
| `index_id` | str | Identifiant de l'index interroge |
| `index_name` | str | Nom d'affichage de l'index |

Cas limites:
- 0 resultat: `total=0`, `has_more=false`, `total_pages=1`, `hits=[]`
- Derniere page: `has_more=false`, `offset + limit >= total`
- Offset au-dela du total: `hits=[]`, `has_more=false`
- L'offset est limite a `10000` par defaut pour eviter les recherches Tantivy
  excessivement couteuses. Cette limite se configure avec
  `OEIL_MAX_SEARCH_OFFSET` (`0` autorise uniquement la premiere page).

## Comportement des snippets

Les snippets sont extraits du champ `content_raw` stocke par l'indexeur.
Par defaut (`index_raw_content = true`), `content_raw` contient le document
JSON compact **avec ses cles**, dans l'ordre d'origine et sans echappement
`\uXXXX` (ex.: `{"name":"José","email":"jose@example.com"}`). Les extraits
indiquent ainsi a quel champ appartient chaque valeur.

Le champ `content`, lui, ne contient que les valeurs (sans les cles, pour ne
pas faire correspondre les noms de champs) et sert uniquement a la recherche:
dans les nouveaux index il est indexe mais plus stocke, ce qui evite de
dupliquer chaque document dans le doc store. Les index existants gardent leur
schema d'origine et restent utilisables pour l'indexation incrementale; leurs
anciens documents conservent un `content_raw` compose des seules valeurs.

### Fallback content_raw -> content

Pour les index anciens sans `content_raw`, le backend utilise le champ
`content` (version sanitisee) comme fallback. Les snippets restent
fonctionnels mais les caracteres speciaux (accolades, crochets, etc.)
auront ete remplaces par des espaces. Avec `index_raw_content = false`,
`content` reste stocke pour assurer ce repli.

La logique de fallback (dans `engine.py`):
```python
raw = doc.get_first("content_raw") or doc.get_first("content") or ""
```

Si les deux champs sont absents, les snippets seront vides (`[]`) sans erreur.

### Structure d'un snippet
```json
{"text": "contexte autour du match", "highlights": [[start, end], ...]}
```
- `text`: extrait de `context` caracteres (defaut 2500) autour de chaque occurrence.
- `highlights`: paires [debut, fin] indiquant les positions du terme dans `text`,
  comptees en caracteres Unicode (points de code), pas en unites UTF-16.
- Le surlignage decoupe la requete comme l'index : `jose.martin@example.com`
  surligne `jose_martin@example.com`, `o'brien` surligne `O'Brien`, et `martin`
  ne surligne pas `martinez`. Les termes exclus (`-mot`, `NOT mot`) ne sont pas
  surlignes. Si une phrase de l'index est coupee par une cle dans le JSON brut,
  ses mots sont surlignes un par un.
- Quand aucun terme n'apparait dans le contenu brut (correspondance sur le nom
  de fichier seul), l'extrait est le debut du document, sans surlignage. Une
  requete faite uniquement d'operateurs ou d'exclusions donne `[]`.

## Pagination par canal

### Frontend (recherche web)
- Pagination **serveur** via `offset`/`limit` dans l'API.
- 20 resultats par page (`SearchApi.defaultLimit`).
- Navigation: boutons Precedent/Suivant, desactives selon `has_more` et `offset`.
- Affiche `total_pages` dans le compteur de resultats.
- Timeouts alignes sur le bot: `180 s` pour la recherche, `45 s` pour les
  fuites, `30 s` ailleurs. Un timeout n'est **jamais** relance automatiquement
  (le backend traite peut-etre encore la requete); seule une erreur reseau sur
  un `GET` donne lieu a un unique nouvel essai.

### Bot Discord
- Pagination **serveur** : chaque bouton charge la page voulue via
  `offset`/`limit`, sans conserver jusqu'a 50 resultats en memoire.
- Affiche 5 resultats par page (`RESULTS_PER_PAGE`) avec des boutons Discord.
- Demande `context=300` a l'API et partage les 4 096 caracteres de l'embed
  entre les resultats de la page : chaque extrait est centre sur sa premiere
  correspondance, et un long document ne masque plus les suivants.
- Le parametre `limit` de `/search` fixe le nombre maximal de resultats a
  parcourir (50 au maximum); le footer distingue ce plafond du total brut.
- Timeout des boutons et vues : 10 minutes (600s), avec desactivation propre a expiration.

### Limites par canal

| Canal | Resultats/page | Max par requete | Pagination |
|-------|---------------|-----------------|------------|
| Frontend | 20 | 100 (API limit) | Serveur (offset/limit) |
| Bot Discord | 5 | 5 par appel, 50 parcourables | Serveur (offset/limit) |
| API directe | configurable | 100 | Serveur (offset/limit) |

### Robustesse bot

- Le bot utilise `http://127.0.0.1:8500` par defaut.
- Les appels backend distinguent:
  - backend injoignable
  - timeout
  - rate limit
  - index indisponible (`409`)
  - erreur backend (`500+`)
- Timeouts par route:
  - `/api/search`: `180s`
  - `/api/breaches`: `45s`
  - autres routes: `30s`
- Un retry automatique est applique sur erreur de connexion locale, et sur
  timeout sauf pour `/api/search` : la recherche tourne encore cote backend,
  la relancer doublerait la charge et l'attente.
- Un cooldown depasse (`/search`, `/stats` : 5 appels par minute) recoit un
  message ephemere indiquant le delai restant.

### Acces local au backend

- Le frontend envoie l'en-tete non secret `X-Oeil-Local-Client: 1`.
- Le backend accepte cet en-tete uniquement depuis la boucle locale
  (`OEIL_ALLOW_LOCALHOST_NO_AUTH=true`) et avec une origine absente ou
  autorisee par `ALLOWED_ORIGINS`.
- Les clients comme le bot utilisent `X-API-Key` (valeur de `API_KEY`).
- Protection contre le DNS rebinding: toute requete dont l'en-tete `Host`
  n'est pas dans `OEIL_ALLOWED_HOSTS` (defaut `127.0.0.1,localhost`) est
  rejetee en `400` avant toute authentification.
- Les quotas (`10/minute` pour la recherche et les fuites, `30/minute` pour les
  stats, `2/heure` pour l'analyse) sont comptes par adresse **et** par type de
  client: le bot et le frontend, sur la meme machine, ne se partagent plus un
  quota unique.

### Journalisation et donnees personnelles

- Les logs d'acces uvicorn sont journalises sans query string: les emails,
  noms et telephones recherches n'apparaissent pas dans `uvicorn.log`.
- Les erreurs de recherche ne journalisent que la longueur de la requete; les
  erreurs des providers de fuites ne journalisent pas l'email consulte.

## Verification des fuites (breach lookup)

`GET /api/breaches?email=<email>` interroge 2 providers en parallele et
renvoie une reponse agrégée normalisee.

### Providers

| Provider | Type | Auth | Statut |
|----------|------|------|--------|
| HIBP (Have I Been Pwned) | GET | `HIBP_API_KEY` (header `hibp-api-key`) | actif — logo + password_exposed |
| BreachDirectory (RapidAPI) | GET | `BREACHDIRECTORY_RAPIDAPI_KEY` | actif — password_exposed |

### Reponse

```json
{
  "query": "user@example.com",
  "providers": [
    {
      "name": "hibp",
      "ok": true,
      "results": [
        {
          "name": "Adobe",
          "domain": "adobe.com",
          "breach_date": "2013-10-04",
          "description": "...",
          "data_classes": ["Email addresses", "Passwords"],
          "source_provider": "hibp",
          "logo_url": "https://logos.haveibeenpwned.com/Adobe.png",
          "password_exposed": true
        }
      ],
      "error": ""
    }
  ],
  "summary": {
    "providers_ok": 2,
    "providers_failed": 0,
    "total_hits": 2
  }
}
```

### Comportement en cas d'echec partiel

Chaque provider est interroge independamment. Si un provider echoue (timeout,
cle absente, erreur HTTP), les autres renvoient quand meme leurs resultats.
Le provider en erreur apparait avec `ok: false` et un champ `error` descriptif.
La route reste en `200` meme si tous les providers echouent — le client doit
consulter `summary.providers_failed` pour detecter les erreurs.

### Variables d'environnement

| Variable | Description | Defaut |
|----------|-------------|--------|
| `HIBP_API_KEY` | Cle API Have I Been Pwned | _(vide = provider desactive)_ |
| `BREACHDIRECTORY_RAPIDAPI_KEY` | Cle RapidAPI BreachDirectory | _(vide = provider desactive)_ |
| `BREACHDIRECTORY_RAPIDAPI_HOST` | Host RapidAPI | `breachdirectory.p.rapidapi.com` |
| `OEIL_BREACH_LOOKUP_TIMEOUT` | Timeout HTTP par provider (secondes) | `10` |

### Integration frontend et bot

- **Frontend**: le bouton "Verifier fuites" apparait sur chaque resultat de
  recherche contenant un email. Il appelle `/api/breaches` et affiche les
  resultats groupes par provider avec indicateurs de succes/echec.
- **Bot Discord**: le bouton "Fuites" sur le paginateur de recherche appelle
  `/api/breaches` et affiche un embed avec les fuites par provider,
  les dates et types de donnees exposees.

## Fichier .schema_config.toml
L'indexeur Rust sauvegarde sa configuration dans `<index>/.schema_config.toml`
lors de la creation de l'index. Le backend Python le relit au demarrage pour
aligner le tokenizer de recherche (stemming, stop words, langue). Ce fichier
est gere automatiquement, il ne faut pas le modifier manuellement.

L'indexeur conserve aussi `<index>/.source_state.json`. Ce petit fichier lui
permet de detecter un fichier source modifie et de remplacer ses anciens
documents au lieu de les dupliquer. L'empreinte combine taille, date de
modification haute resolution et echantillons du contenu. Si l'etat manque ou
est illisible, les sources deja indexees sont revalidees et remplacees.

La comparaison des chemins tolere les differences de graphie sous Windows
(casse, `/` ou `\`): reindexer `X:/Data` apres `X:\Data` ne cree pas de
doublons, et toutes les graphies deja indexees d'un fichier modifie sont
remplacees ensemble (y compris par `purge`).

Une ligne JSON invalide ou plus longue que `max_doc_input_bytes` (8 Mio par
defaut, verifie avant le parsing) est ignoree, sans ecarter le reste du
fichier. Le journal `<index>_errors.log` detaille les premieres lignes
ignorees de chaque fichier puis donne toujours son bilan (lignes ignorees et
lignes indexees), et le resume de fin affiche le total des lignes ignorees.
Un fichier sans aucune ligne valide n'est pas memorise : il sera relu au
prochain passage.

Rien n'est recopie sur disque pendant l'indexation. Pour remplacer un fichier
deja indexe, l'indexeur le lit une premiere fois en entier avant d'envoyer la
suppression des anciens documents a Tantivy, puis une seconde fois pour les
indexer. Une erreur de lecture pendant la premiere passe laisse donc l'index
intact, et un fichier devenu entierement invalide ne supprime jamais ses
anciens documents. Les checkpoints sont suspendus tant qu'un remplacement est
incomplet : un Ctrl+C ou une lecture partielle ne peut pas valider la
suppression seule. `reader_threads` (2 par defaut) borne le nombre de fichiers
lus en parallele et donc la pression sur les disques.
Le fichier `.source_state.json` est remplace atomiquement apres le commit final.

### Segments et commande `merge`

Pendant l'indexation, la fusion automatique des segments est desactivee
(`NoMergePolicy`): fusionner pendant un import massif avait provoque des
depassements de memoire. Chaque checkpoint ajoute donc un segment, et un index
tres fragmente s'ouvre et se parcourt plus lentement. La fin d'indexation
affiche le nombre de segments et suggere une fusion au-dela de 64.

```
l_oeil_de_dieu.exe merge D:\index --target-segments 16 --batch-size 8
```

- Fusionne les plus petits segments, `--batch-size` au plus a la fois: la
  memoire et l'espace disque temporaire restent bornes par la taille d'un lot.
- Chaque fusion est validee par Tantivy: un Ctrl+C attend la fin du lot en
  cours et laisse un index coherent. Les documents supprimes sont purges.
- A lancer hors indexation, backend arrete de preference: sous Windows, les
  fichiers encore ouverts par le backend ne sont liberes qu'a une passe suivante.
- `purge` n'enclenche plus de fusion implicite apres sa suppression.

### Compression zstd et commande `compress`

Le doc store (texte brut des documents, plus de la moitie de l'index) etait
compresse en lz4 par blocs de 16 Ko. Les nouveaux index sont crees en zstd
niveau 3 par blocs de 128 Ko (`docstore_compression`, `zstd_level`,
`docstore_blocksize` dans la config). Un index existant garde ses reglages
tant qu'il n'est pas converti:

```
l_oeil_de_dieu.exe compress D:\index --batch-size 64
```

- Reecrit chaque segment encore en lz4 par fusions de `--batch-size`
  segments: Tantivy recompresse les documents quand le compresseur cible
  change. Aucune reindexation, et les resultats ne changent pas (positions,
  termes et documents sont recopies tels quels).
- Les anciens fichiers sont supprimes au fil des fusions: l'espace temporaire
  reste borne par un lot. Un Ctrl+C attend la fin du lot en cours; relancer la
  commande reprend avec les segments restants.
- Affiche la taille du doc store et de l'index avant/apres.
- Une fois l'index converti, les indexations suivantes ecrivent en zstd.

Le wheel PyPI de tantivy n'active pas la feature Rust `zstd-compression`: le
backend refuse alors d'ouvrir un index zstd (message explicite au demarrage).
`build_tantivy_zstd.ps1` telecharge les sources de la version epinglee dans
`backend/requirements.txt`, active la feature et construit
`build\wheels\tantivy-X.Y.Z+zstd-*.whl`; `-Install` l'installe dans le venv
(backend arrete: Windows verrouille le module charge). `tantivy==X.Y.Z` est
satisfait par `X.Y.Z+zstd`, donc `setup.ps1` garde ce build; il le reinstalle
s'il trouve le wheel apres un venv recree. A refaire a chaque montee de
version de tantivy (Dependabot ignore ce paquet cote pip pour cette raison).
`venv\Scripts\python backend\check_tantivy_zstd.py` verifie le build installe.

Ordre conseille: arreter l'application, `build_tantivy_zstd.ps1 -Install`,
`compress` sur chaque index, puis relancer. `merge` peut suivre pour reduire
le nombre de segments: entre segments zstd, il recopie les blocs sans les
recompresser.

### Protection du reset

L'option `index --reset` supprime uniquement une cible reconnue comme index
Tantivy valide par son `meta.json`. Le reset est refuse pour une racine de
disque, un lien symbolique ou une jonction, un dossier arbitraire et tout
dossier contenant la source. Les anciens index Tantivy sans
`.schema_config.toml` restent acceptes.

## Statistiques

`GET /api/stats` renvoie deux niveaux de donnees distincts:

### Donnees moteur (toujours presentes)
Metriques internes Tantivy, refletant l'etat physique de l'index:

| Champ | Description |
|-------|-------------|
| `total_docs` | Nombre total de documents dans l'index |
| `num_segments` | Nombre de segments internes Tantivy |
| `index_size_bytes` | Taille de l'index en octets |
| `index_size` | Taille formatee (ex: "1.2 Go") |
| `index_size_pending` | `true` pendant le premier calcul asynchrone |
| `segments` | Detail par segment: `id` (8 premiers caracteres) et `docs` (nombre de documents) |

Les segments sont des structures internes de stockage Tantivy. Ce ne sont
**pas** des sources de donnees metier.

### Donnees analyse (sous `analysis`, optionnelles)
Resultats du scan des fichiers JSONL sources, disponibles uniquement apres
un appel a `POST /api/analyze`:

| Champ | Description |
|-------|-------------|
| `analysis.scan_date` | Date du dernier scan |
| `analysis.total_files` | Nombre de fichiers JSONL scannes |
| `analysis.emails` | Statistiques emails: `unique_count`, `top_emails`, `top_domains`, `tld_distribution`, `perso_count`/`pro_count` |
| `analysis.phones` | Statistiques telephones: `unique_count`, `top_phones` |
| `analysis.sources` | Liste des dossiers sources avec nombre de fichiers par dossier |
| `analysis.limits` | Plafonds appliques pendant l'analyse (`email_counter_limit`, `phone_counter_limit`) |
| `analysis.failed_files` | Fichiers annules avec leur erreur, conserves pour reprise |

Quand `analysis.limits.*` est non nul:
- les tops (`top_emails`, `top_phones`) peuvent etre tronques pour contenir l'usage RAM
- `unique_count` reste exact a l'echelle d'un index, y compris apres reprise d'analyse
- en vue globale `index=all`, `unique_count` reste **approximatif** car il additionne les index sans dedup inter-index

`analysis.sources` designe les **dossiers de fichiers JSONL** scannes — a ne
pas confondre avec `segments` qui sont des structures internes Tantivy.

### Vue globale multi-index

- `GET /api/stats?index=all` agrege tous les index `ready`.
- Les index non prets sont listes dans `skipped_indexes`.
- En mode global:
  - `top_emails`, `top_domains`, `top_phones`, `sources` sont fusionnes par cle
  - `emails.unique_count` et `phones.unique_count` sont **approximatifs**
    (somme des index, sans dedup inter-index)

### Cache des stats moteur

- `total_docs`, `num_segments`, `segments`: cache leger `5 min`
- `index_size`: cache dedie `1 h`
- Apres expiration du TTL long, si une valeur precedente existe, `index_size`
  est servi en **stale-while-revalidate**: derniere valeur connue immediate,
  recalcul en arriere-plan, puis visible au prochain appel
- Au premier appel, la taille est calculee en arriere-plan; la route retourne
  immediatement `index_size_pending=true` et `index_size="Calcul..."`

## Commandes utiles
- Installation/reparation: `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Development`
- Diagnostic local hors API: `venv\Scripts\python backend\doctor.py`
- Diagnostic local JSON: `venv\Scripts\python backend\doctor.py --json`
- Tests backend: `venv\Scripts\python -m pytest backend\tests -q`
- Tests bot: `venv\Scripts\python -m pytest bot\tests -q`
- Tests frontend complets: `npm test`
- Tests Python complets: `venv\Scripts\python -m pytest` (les tests backend
  s'executent dans un dossier temporaire: aucun fichier reel n'est modifie)
- Tests Rust: `cd indexer && cargo test`
- Lint Rust: `cd indexer && cargo clippy --all-targets -- -D warnings`
- Lint Python: `venv\Scripts\python -m ruff check backend bot`
- Audit Python: `venv\Scripts\python -m pip_audit`
- Audit npm: `npm audit`
- Build Rust release: `powershell -ExecutionPolicy Bypass -File indexer\build_windows.ps1`
- Fusion des segments: `indexer\target\release\l_oeil_de_dieu.exe merge <index>`
- Compression zstd d'un index existant: `indexer\target\release\l_oeil_de_dieu.exe compress <index>`
- tantivy Python avec zstd (backend arrete): `powershell -ExecutionPolicy Bypass -File .\build_tantivy_zstd.ps1 -Install`
- Apercu du nettoyage: `powershell -File .\cleanup.ps1`
- Nettoyage verifie (caches, venv casses archives, `.sqlite.tmp` orphelins,
  locks de tests, temporaires du projet dans `%TEMP%`): `powershell -File .\cleanup.ps1 -Execute`
- Ajouter builds/logs au nettoyage: options `-BuildArtifacts` et `-Logs`

## Exploitation locale

Lancer `start.bat`, puis ouvrir `http://127.0.0.1:3000/` dans le navigateur.
Le frontend utilise l'API locale `http://127.0.0.1:8500`.
Le serveur `serve_frontend.py` ecoute sur IPv4 et sert les fichiers sans cache
pour appliquer les changements de configuration au rechargement de la page.
Si une page etait deja ouverte avant une mise a jour, la recharger avec `Ctrl+F5`.

### Verifier l'etat du systeme

- Backend: `http://localhost:8500/api/health`
- Index et retries: `http://localhost:8500/api/indexes`
- Diagnostic local detaille: `http://localhost:8500/api/diagnostics`
- Diagnostic hors API: `venv\Scripts\python backend\doctor.py`

### Si un index reste en erreur

1. Verifier `GET /api/indexes` pour lire `error`, `retry_count` et `next_retry_at`.
2. Verifier `GET /api/diagnostics` pour voir:
   - si le chemin d'index existe
   - si le chemin source existe
   - si un lockfile Tantivy est present
3. Si l'erreur parle de lock et qu'aucun process legitime n'utilise l'index, redemarrer proprement le backend.
4. Si un lock subsiste apres l'arret confirme de l'indexeur, sauvegarder
   l'index puis intervenir manuellement sur le chemin exact indique par le diagnostic.

### Si les stats d'analyse sont absentes

- La recherche peut rester disponible meme si le chemin source `X:\...` n'est pas monte.
- `GET /api/stats` sans `analysis` signifie generalement:
  - analyse jamais lancee
  - cache absent
  - ou chemin source indisponible pour lancer le scan

### Demarrage / arret

- `start.bat` ne tue plus automatiquement tous les processus qui ecoutent les ports.
- A l'arret, seuls les PID suivis dont la ligne de commande contient le chemin
  absolu de cette copie du projet sont termines.
- Si un backend tiers ecoute deja sur le port configure, le script le laisse intact.
- Superviseur: le backend et le bot sont relances s'ils s'arretent (au plus une
  fois par minute environ). Avant une relance du backend, ses logs sont archives
  (`uvicorn.log.1` a `.3`): la cause du crash n'est plus ecrasee.

## Limites actuelles
- `deduplicate=true` deduplique uniquement la page courante et conserve un total brut
- la vue statistique multi-index additionne les uniques sans deduplication inter-index
