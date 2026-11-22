use anyhow::{bail, Context, Result};
use indicatif::{ProgressBar, ProgressStyle};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    mpsc, Arc, Mutex,
};
use std::time::{Duration, Instant, UNIX_EPOCH};
use tantivy::Index;

const COMMIT_MAX_RETRIES: u32 = 5;
const COMMIT_RETRY_DELAY: Duration = Duration::from_secs(3);

use crate::config::Config;
use crate::sanitize::{flatten_json_values, sanitize_content};
use crate::schema::{open_existing_index, open_or_create_index, IndexFields};

const MAX_ERROR_LOGS: usize = 50;
/// Au-dela, la fin d'indexation suggere la commande `merge`.
const SEGMENT_HINT_THRESHOLD: usize = 64;

#[derive(Debug, Default)]
struct MetaData {
    filename: Option<String>,
    doc_type: Option<String>,
}

enum PipelineEvent {
    Document(tantivy::TantivyDocument),
    /// Termes `source` deja indexes a supprimer avant les nouveaux documents
    /// (plusieurs graphies d'un meme chemin peuvent coexister).
    BeginReplace(Vec<String>),
    EndReplace,
    ErrorLog {
        file: String,
        error: String,
    },
}

#[derive(Debug, Clone, Copy, Deserialize, Eq, PartialEq, Serialize)]
struct SourceFingerprint {
    size: u64,
    modified_ns: u64,
    #[serde(default)]
    sample_hash: u64,
}

#[derive(Debug, Deserialize, Serialize)]
struct PreparedDocument {
    filename: String,
    doc_type: String,
    /// Valeurs aplaties: texte indexe (champ `content`).
    content: String,
    /// Document JSON compact, cles comprises: texte des snippets (`content_raw`).
    #[serde(default, skip_serializing_if = "String::is_empty")]
    raw: String,
}

/// Etat des sources, indexe par `source_key` (graphie normalisee du chemin).
type SourceState = HashMap<String, SourceFingerprint>;
/// `source_key` -> termes `source` reellement presents dans l'index.
type KnownSources = HashMap<String, Vec<String>>;

struct ProcessContext<'a> {
    fields: &'a IndexFields,
    config: &'a Config,
    sanitize_set: &'a HashSet<char>,
    tx: &'a mpsc::SyncSender<PipelineEvent>,
    running: &'a AtomicBool,
    doc_count: &'a AtomicUsize,
    error_count: &'a AtomicUsize,
    logged_errors: &'a AtomicUsize,
    /// Termes deja indexes pour ce fichier; vide si ce n'est pas un remplacement.
    replace_sources: &'a [String],
}

pub fn run(
    source: &str,
    index_dir: &str,
    config_path: Option<&str>,
    reset: bool,
    no_dedup: bool,
) -> Result<()> {
    let start = Instant::now();
    let config = Config::load(config_path)?;
    let source_path = Path::new(source);
    let index_path = Path::new(index_dir);

    if !source_path.is_dir() {
        bail!("Dossier source introuvable ou invalide : {}", source);
    }

    let reset_target = if reset && index_path.exists() {
        Some(validate_reset_target(source_path, index_path)?)
    } else {
        None
    };

    check_schema_conflict(index_path, &config, reset)?;

    if let Some(target) = reset_target {
        println!(
            "Suppression de l'index Tantivy valide : {}",
            target.display()
        );
        fs::remove_dir_all(&target).with_context(|| {
            format!(
                "Impossible de supprimer l'index valide : {}",
                target.display()
            )
        })?;
    }

    if !index_path.exists() {
        fs::create_dir_all(index_path).context("Impossible de creer le dossier d'index")?;
    }

    // Log a cote du dossier d'index (pas dedans, sinon conflit MmapDirectory sur Windows)
    let log_file_path = PathBuf::from(format!("{}_errors.log", index_dir));
    let log_file = BufWriter::new(
        OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&log_file_path)
            .context("Impossible de creer le fichier de log")?,
    );

    println!(
        "Configuration chargee (Langue: {} | Raw content: {})",
        config.language, config.index_raw_content
    );

    let (index, fields) = open_or_create_index(index_path, &config)?;
    config.save_to_index(index_path)?;

    // Anti-doublon : charger les sources deja indexees
    let known_sources = load_known_sources(&index, fields.source, reset || no_dedup)?;
    let source_state = Arc::new(Mutex::new(load_source_state(index_path)?));

    // Ctrl+C
    let running = Arc::new(AtomicBool::new(true));
    {
        let r = running.clone();
        ctrlc::set_handler(move || {
            println!("\nInterruption detectee (Ctrl+C). Fin des taches en cours...");
            r.store(false, Ordering::SeqCst);
        })
        .expect("Erreur lors de l'installation du handler Ctrl+C");
    }

    let files = collect_jsonl_files(source)?;
    if files.is_empty() {
        println!("Aucun fichier .jsonl trouve.");
        return Ok(());
    }

    let pb = create_progress_bar(files.len() as u64);
    let sanitize_set = config.sanitize_set();

    println!(
        "Indexation demarree (Anti-doublon: {} | Buffer: {} MB | Lecteurs: {} | Ecrivains: {} | Commit tous les {} docs)",
        if no_dedup { "desactive" } else { "actif" },
        config.writer_memory_mb,
        config.reader_threads,
        config.writer_threads,
        config.commit_every_n_docs
    );

    // Pipeline producteur-consommateur
    // Canal borné : empêche les producers (rayon) d'inonder la RAM pendant un commit.
    // Sans limite, les workers lisent les fichiers bien plus vite que le writer n'écrit,
    // et le canal accumule des Go de TantivyDocument en mémoire → OOM.
    // Avec NoMergePolicy les commits sont courts (juste un flush disque), donc le
    // backpressure ne dure que quelques secondes au pire.
    let (tx, rx) = mpsc::sync_channel(config.channel_size);

    let doc_count = Arc::new(AtomicUsize::new(0));
    let error_count = Arc::new(AtomicUsize::new(0));
    let logged_errors = Arc::new(AtomicUsize::new(0));
    let skipped_count = Arc::new(AtomicUsize::new(0));

    // Thread producteur : lit les fichiers en parallele avec rayon
    let producer_handle = {
        let config = config.clone();
        let pb = pb.clone();
        let running = running.clone();
        let known_sources = Arc::new(known_sources);
        let sanitize_set = Arc::new(sanitize_set);
        let doc_count = doc_count.clone();
        let error_count = error_count.clone();
        let logged_errors = logged_errors.clone();
        let skipped_count = skipped_count.clone();
        let source_state = source_state.clone();

        std::thread::Builder::new()
            .name("producer".into())
            .spawn(move || -> Result<()> {
                let reader_pool = rayon::ThreadPoolBuilder::new()
                    .num_threads(config.reader_threads)
                    .thread_name(|index| format!("reader-{index}"))
                    .build()
                    .context("Impossible de creer le pool de lecteurs")?;

                reader_pool.install(|| {
                    files.par_iter().try_for_each(|file_path| -> Result<()> {
                        if !running.load(Ordering::SeqCst) {
                            return Ok(());
                        }

                        let path_str = file_path.to_string_lossy().to_string();
                        let key = source_key(&path_str);
                        let fingerprint = match source_fingerprint(file_path) {
                            Ok(value) => value,
                            Err(error) => {
                                error_count.fetch_add(1, Ordering::Relaxed);
                                if logged_errors.fetch_add(1, Ordering::Relaxed) < MAX_ERROR_LOGS {
                                    let _ = tx.send(PipelineEvent::ErrorLog {
                                        file: path_str.clone(),
                                        error: format!("Metadonnees illisibles: {}", error),
                                    });
                                }
                                pb.inc(1);
                                return Ok(());
                            }
                        };

                        // La comparaison passe par la graphie normalisee: un meme
                        // fichier designe par "X:/a" puis "x:\\a" n'est jamais
                        // reindexe comme nouveau (ce qui creerait des doublons).
                        let existing_terms: &[String] =
                            known_sources.get(&key).map(Vec::as_slice).unwrap_or(&[]);
                        let is_known = !existing_terms.is_empty();
                        let previous_fingerprint = source_state
                            .lock()
                            .ok()
                            .and_then(|state| state.get(&key).copied());
                        let unchanged = is_known && previous_fingerprint == Some(fingerprint);

                        if unchanged {
                            if let Ok(mut state) = source_state.lock() {
                                state.insert(key.clone(), fingerprint);
                            }
                            skipped_count.fetch_add(1, Ordering::Relaxed);
                            pb.inc(1);
                            return Ok(());
                        }

                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            let context = ProcessContext {
                                fields: &fields,
                                config: &config,
                                sanitize_set: &sanitize_set,
                                tx: &tx,
                                running: &running,
                                doc_count: &doc_count,
                                error_count: &error_count,
                                logged_errors: &logged_errors,
                                replace_sources: if no_dedup { &[] } else { existing_terms },
                            };
                            if process_file_transactional(file_path, &path_str, &context) {
                                if let Ok(mut state) = source_state.lock() {
                                    state.insert(key.clone(), fingerprint);
                                }
                            }
                        }));
                        if result.is_err() {
                            error_count.fetch_add(1, Ordering::Relaxed);
                            if logged_errors.fetch_add(1, Ordering::Relaxed) < MAX_ERROR_LOGS {
                                let _ = tx.send(PipelineEvent::ErrorLog {
                                    file: path_str.to_string(),
                                    error: "Panic lors du traitement du fichier (fichier ignore)"
                                        .to_string(),
                                });
                            }
                        }

                        pb.inc(1);
                        Ok(())
                    })
                })
            })?
    };

    // Consommateur : ecrit dans l'index de maniere sequentielle
    // NoMergePolicy pendant le bulk indexing : évite que LogMergePolicy fusionne
    // plusieurs segments en RAM simultanément → OOM confirmé (kernel a tué le process
    // à 21-23 GB RSS). C'est la pratique standard pour les indexeurs batch.
    // Contrepartie : chaque checkpoint ajoute un segment. La commande `merge`
    // les fusionne ensuite par lots bornes, hors indexation.
    let mut writer = index
        .writer_with_num_threads(config.writer_threads, config.writer_memory_mb * 1024 * 1024)?;
    writer.set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));
    let mut log_writer = log_file;
    let mut local_count: usize = 0;
    let mut next_checkpoint = config.commit_every_n_docs;
    let mut active_replacements: usize = 0;

    writeln!(log_writer, "--- Journal des erreurs d'indexation ---")?;

    for event in rx {
        match event {
            PipelineEvent::Document(doc) => {
                writer.add_document(doc)?;
                local_count += 1;
                if local_count >= next_checkpoint && active_replacements == 0 {
                    pb.set_message(format!("Checkpoint ({} docs)...", local_count));
                    commit_with_retry(
                        &mut writer,
                        &pb,
                        &format!("Checkpoint ({} docs)", local_count),
                    )?;
                    while next_checkpoint <= local_count {
                        next_checkpoint += config.commit_every_n_docs;
                    }
                }
            }
            PipelineEvent::BeginReplace(sources) => {
                for source in &sources {
                    writer.delete_term(tantivy::Term::from_field_text(fields.source, source));
                }
                active_replacements += 1;
            }
            PipelineEvent::EndReplace => {
                active_replacements = active_replacements.saturating_sub(1);
                if active_replacements == 0 && local_count >= next_checkpoint {
                    pb.set_message(format!("Checkpoint ({} docs)...", local_count));
                    commit_with_retry(
                        &mut writer,
                        &pb,
                        &format!("Checkpoint ({} docs)", local_count),
                    )?;
                    while next_checkpoint <= local_count {
                        next_checkpoint += config.commit_every_n_docs;
                    }
                }
            }
            PipelineEvent::ErrorLog { file, error } => {
                writeln!(log_writer, "[{}] {}", file, error)?;
            }
        }
    }

    // Attendre le producteur et gerer son resultat proprement
    match producer_handle.join() {
        Ok(Ok(())) => {}
        Ok(Err(e)) => {
            pb.abandon_with_message(format!("Erreur fatale: {:#}", e));
            return Err(e);
        }
        Err(_) => {
            pb.abandon_with_message("Le thread producteur a paniqued");
            return Err(anyhow::anyhow!("Le thread producteur a paniqued"));
        }
    }

    if active_replacements != 0 {
        pb.abandon_with_message("Remplacement incomplet: commit annule");
        return Err(anyhow::anyhow!(
            "{} remplacement(s) de source incomplet(s); aucune suppression partielle n'a ete commit",
            active_replacements,
        ));
    }

    pb.finish_with_message("Traitement termine");
    let commit_start = Instant::now();
    let spinner = indicatif::ProgressBar::new_spinner();
    spinner.set_style(
        indicatif::ProgressStyle::default_spinner()
            .template("{spinner:.cyan} Commit final en cours... {elapsed}")
            .unwrap(),
    );
    spinner.enable_steady_tick(Duration::from_millis(100));
    commit_with_retry(&mut writer, &spinner, "Commit final")?;
    if let Ok(state) = source_state.lock() {
        save_source_state(index_path, &state)?;
    }
    spinner.finish_with_message(format!(
        "Commit final termine en {:.1}s",
        commit_start.elapsed().as_secs_f64()
    ));

    // Avec NoMergePolicy aucune fusion n'est en attente: cet appel ferme
    // simplement les threads du writer et libere son verrou.
    writer.wait_merging_threads()?;

    log_writer.flush()?;

    print_summary(
        start.elapsed(),
        doc_count.load(Ordering::Relaxed),
        skipped_count.load(Ordering::Relaxed),
        error_count.load(Ordering::Relaxed),
        &log_file_path,
    );

    let segment_count = index.searchable_segment_ids()?.len();
    println!("Segments          : {}", segment_count);
    if segment_count > SEGMENT_HINT_THRESHOLD {
        println!(
            "Conseil : beaucoup de segments ralentissent l'ouverture et la recherche. \
             Lancez `merge \"{}\"` (backend arrete de preference).",
            index_dir
        );
    }

    Ok(())
}

fn validate_reset_target(source_path: &Path, index_path: &Path) -> Result<PathBuf> {
    let metadata = fs::symlink_metadata(index_path).with_context(|| {
        format!(
            "Impossible d'inspecter la cible du reset : {}",
            index_path.display()
        )
    })?;
    if is_link_or_reparse_point(&metadata) {
        bail!(
            "Reset refuse : la cible est un lien symbolique ou une jonction : {}",
            index_path.display()
        );
    }
    if !metadata.is_dir() {
        bail!(
            "Reset refuse : la cible n'est pas un dossier : {}",
            index_path.display()
        );
    }

    let target = index_path.canonicalize().with_context(|| {
        format!(
            "Impossible de resoudre la cible du reset : {}",
            index_path.display()
        )
    })?;
    let source = source_path.canonicalize().with_context(|| {
        format!(
            "Impossible de resoudre le dossier source : {}",
            source_path.display()
        )
    })?;

    if target.parent().is_none() {
        bail!("Reset refuse : une racine de systeme de fichiers ne peut jamais etre supprimee");
    }
    if source.starts_with(&target) {
        bail!(
            "Reset refuse : la cible {} contient le dossier source {}",
            target.display(),
            source.display()
        );
    }

    let meta_path = target.join("meta.json");
    let meta_content = fs::read_to_string(&meta_path).with_context(|| {
        format!(
            "Reset refuse : {} n'est pas un index Tantivy identifiable (meta.json absent ou illisible)",
            target.display()
        )
    })?;
    let meta: Value = serde_json::from_str(&meta_content).with_context(|| {
        format!(
            "Reset refuse : le meta.json de {} est invalide",
            target.display()
        )
    })?;
    let is_tantivy_index = meta
        .as_object()
        .is_some_and(|object| object.contains_key("segments") && object.contains_key("schema"));
    if !is_tantivy_index {
        bail!(
            "Reset refuse : {} ne contient pas les marqueurs d'un index Tantivy",
            target.display()
        );
    }

    Ok(target)
}

fn is_link_or_reparse_point(metadata: &fs::Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }

    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }

    #[cfg(not(windows))]
    false
}

pub fn purge(source_dir: &str, index_dir: &str) -> Result<()> {
    let start = Instant::now();
    let index_path = Path::new(index_dir);
    let source_path = Path::new(source_dir);

    if !index_path.exists() {
        return Err(anyhow::anyhow!("Index introuvable : {}", index_dir));
    }
    if !source_path.exists() {
        return Err(anyhow::anyhow!(
            "Dossier source introuvable : {}. Le disque est-il monte ?",
            source_dir
        ));
    }

    let (index, fields) = open_existing_index(index_path)?;

    // Charger tous les chemins sources connus dans l'index
    let known_sources = load_known_sources(&index, fields.source, false)?;
    if known_sources.is_empty() {
        println!("L'index est vide, rien a purger.");
        return Ok(());
    }

    // Lister les fichiers actuellement presents dans le dossier source
    let current_keys: HashSet<String> = collect_jsonl_files(source_dir)?
        .into_iter()
        .map(|p| source_key(&p.to_string_lossy()))
        .collect();

    println!(
        "Index : {} source(s) | Disque : {} fichier(s)",
        known_sources.len(),
        current_keys.len()
    );

    // Orphelins = dans la racine demandee et absents du disque. Les documents
    // provenant d'autres racines du meme index ne doivent jamais etre purges.
    // Toutes les graphies indexees d'un chemin orphelin sont supprimees.
    let mut orphan_keys: Vec<&String> = known_sources
        .iter()
        .filter(|(key, terms)| {
            !current_keys.contains(*key)
                && terms
                    .iter()
                    .any(|term| source_is_in_scope(term, source_path))
        })
        .map(|(key, _)| key)
        .collect();
    orphan_keys.sort();
    let orphans: Vec<&String> = orphan_keys
        .iter()
        .flat_map(|key| known_sources[*key].iter())
        .collect();

    if orphans.is_empty() {
        println!(
            "Aucun document orphelin. L'index est synchronise avec {}.",
            source_dir
        );
        return Ok(());
    }

    println!(
        "{} source(s) orpheline(s) (absentes de {}).",
        orphan_keys.len(),
        source_dir
    );

    // Afficher quelques exemples
    for path_str in orphans.iter().take(10) {
        println!("  - {}", path_str);
    }
    if orphans.len() > 10 {
        println!("  ... et {} autres", orphans.len() - 10);
    }

    println!("\nSuppression...");

    let mut writer: tantivy::IndexWriter<tantivy::TantivyDocument> =
        index.writer_with_num_threads(1, 64 * 1024 * 1024)?;
    // Sans cela, la politique par defaut peut declencher apres le commit une
    // fusion geante et imprevue sur un index de plusieurs centaines de Go.
    writer.set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));
    for path_str in &orphans {
        writer.delete_term(tantivy::Term::from_field_text(fields.source, path_str));
    }
    writer.commit()?;
    writer.wait_merging_threads()?;

    let mut source_state = load_source_state(index_path)?;
    for key in &orphan_keys {
        source_state.remove(*key);
    }
    save_source_state(index_path, &source_state)?;

    println!("---------------------------------------------------");
    println!("Purge terminee en {:.1}s", start.elapsed().as_secs_f64());
    println!("Documents purges : {} source(s)", orphan_keys.len());
    println!("---------------------------------------------------");

    Ok(())
}

/// Commande `merge` : reduit le nombre de segments par lots bornes.
pub fn merge(index_dir: &str, target_segments: usize, batch_size: usize) -> Result<()> {
    let running = Arc::new(AtomicBool::new(true));
    {
        let r = running.clone();
        ctrlc::set_handler(move || {
            println!("\nInterruption demandee : arret apres la fusion en cours...");
            r.store(false, Ordering::SeqCst);
        })
        .context("Erreur lors de l'installation du handler Ctrl+C")?;
    }
    merge_segments(Path::new(index_dir), target_segments, batch_size, &running)
}

/// Fusionne les plus petits segments, `batch_size` au plus a la fois, jusqu'a
/// `target_segments`. Chaque fusion est validee dans meta.json par Tantivy:
/// une interruption entre deux lots laisse un index coherent. Les documents
/// supprimes sont purges au passage. L'espace disque temporaire necessaire est
/// borne par la taille d'un lot.
fn merge_segments(
    index_path: &Path,
    target_segments: usize,
    batch_size: usize,
    running: &AtomicBool,
) -> Result<()> {
    if target_segments == 0 {
        bail!("--target-segments doit etre superieur a zero");
    }
    if batch_size < 2 {
        bail!("--batch-size doit etre au moins 2");
    }
    let start = Instant::now();
    let (index, _fields) = open_existing_index(index_path)?;
    let mut writer: tantivy::IndexWriter<tantivy::TantivyDocument> =
        index.writer_with_num_threads(1, 64 * 1024 * 1024)?;
    writer.set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));

    let initial = index.searchable_segment_metas()?.len();
    println!(
        "Segments : {} (objectif : {}, lots de {} au plus)",
        initial, target_segments, batch_size
    );
    let mut merges = 0_usize;
    loop {
        if !running.load(Ordering::SeqCst) {
            println!("Arret demande : fusion interrompue proprement.");
            break;
        }
        let mut metas = index.searchable_segment_metas()?;
        if metas.len() <= target_segments {
            break;
        }
        metas.sort_by_key(|meta| meta.num_docs());
        // Fusionner k segments en retire k - 1 : ne jamais descendre sous l'objectif.
        let take = batch_size.min(metas.len() - target_segments + 1);
        let ids: Vec<tantivy::index::SegmentId> =
            metas.iter().take(take).map(|meta| meta.id()).collect();
        let docs: u64 = metas
            .iter()
            .take(take)
            .map(|meta| u64::from(meta.num_docs()))
            .sum();
        merges += 1;
        println!(
            "[{}] Fusion de {} segments ({} documents), {} segments restants...",
            merges,
            take,
            docs,
            metas.len()
        );
        writer
            .merge(&ids)
            .wait()
            .context("Echec de la fusion de segments")?;
    }

    // Sous Windows, des fichiers encore ouverts par le backend ne peuvent pas
    // etre supprimes: ils le seront lors d'une prochaine passe.
    if let Err(error) = writer.garbage_collect_files().wait() {
        eprintln!("Avertissement : nettoyage des anciens fichiers incomplet : {error}");
    }
    writer.wait_merging_threads()?;

    let remaining = index.searchable_segment_metas()?.len();
    println!("---------------------------------------------------");
    println!(
        "Fusion terminee en {:.1}s : {} -> {} segments",
        start.elapsed().as_secs_f64(),
        initial,
        remaining
    );
    println!("---------------------------------------------------");
    Ok(())
}

fn check_schema_conflict(index_path: &Path, config: &Config, reset: bool) -> Result<()> {
    if index_path.exists() && !reset {
        if let Some(existing) = Config::load_from_index(index_path)? {
            let existing_hash = existing.schema_hash();
            let new_hash = config.schema_hash();
            if existing_hash != new_hash {
                return Err(anyhow::anyhow!(
                    "Conflit de schema ! Existant: '{}', Nouveau: '{}'. Utilisez --reset pour reinitialiser.",
                    existing_hash,
                    new_hash,
                ));
            }
        }
    }
    Ok(())
}

/// Graphie normalisee d'un chemin source, pour les comparaisons uniquement.
///
/// Sous Windows les chemins sont insensibles a la casse et `/` equivaut a `\`:
/// "X:/Data/a.jsonl" et "x:\data\a.jsonl" designent le meme fichier. Le terme
/// stocke dans l'index reste la graphie d'origine.
fn source_key(path: &str) -> String {
    if cfg!(windows) {
        path.replace('/', "\\").to_lowercase()
    } else {
        path.to_string()
    }
}

fn load_known_sources(
    index: &Index,
    source_field: tantivy::schema::Field,
    reset: bool,
) -> Result<KnownSources> {
    let mut known = KnownSources::new();
    if reset {
        return Ok(known);
    }

    if let Ok(reader) = index.reader() {
        println!("Analyse de l'index existant pour eviter les doublons...");
        let searcher = reader.searcher();
        let mut seen = HashSet::new();
        for segment_reader in searcher.segment_readers() {
            let inverted_index = segment_reader.inverted_index(source_field)?;
            let mut term_stream = inverted_index.terms().stream()?;
            while let Some((term_bytes, _)) = term_stream.next() {
                if let Ok(term_str) = std::str::from_utf8(term_bytes) {
                    if seen.insert(term_str.to_string()) {
                        known
                            .entry(source_key(term_str))
                            .or_default()
                            .push(term_str.to_string());
                    }
                }
            }
        }
        if !known.is_empty() {
            println!("Fichiers deja indexes : {}", known.len());
        }
    }

    Ok(known)
}

fn collect_jsonl_files(source: &str) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    for entry in walkdir::WalkDir::new(source) {
        let entry =
            entry.with_context(|| format!("Impossible de parcourir la source: {}", source))?;
        if entry
            .path()
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("jsonl"))
        {
            files.push(entry.path().to_owned());
        }
    }
    Ok(files)
}

fn source_fingerprint(path: &Path) -> Result<SourceFingerprint> {
    let metadata = fs::metadata(path)?;
    let modified_ns = metadata
        .modified()?
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64;
    Ok(SourceFingerprint {
        size: metadata.len(),
        modified_ns,
        sample_hash: hash_file_samples(path, metadata.len())?,
    })
}

fn hash_file_samples(path: &Path, size: u64) -> Result<u64> {
    const SAMPLE_SIZE: usize = 64 * 1024;
    const FNV_OFFSET: u64 = 0xcbf29ce484222325;
    const FNV_PRIME: u64 = 0x100000001b3;

    let mut file = File::open(path)?;
    let mut hash = FNV_OFFSET;
    let mut buffer = vec![0_u8; SAMPLE_SIZE];
    let mut offsets = vec![0_u64];
    if size > SAMPLE_SIZE as u64 {
        offsets.push((size / 2).saturating_sub((SAMPLE_SIZE / 2) as u64));
        offsets.push(size.saturating_sub(SAMPLE_SIZE as u64));
    }
    offsets.sort_unstable();
    offsets.dedup();

    for offset in offsets {
        file.seek(SeekFrom::Start(offset))?;
        let read = file.read(&mut buffer)?;
        for byte in offset.to_le_bytes().iter().chain(buffer[..read].iter()) {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(FNV_PRIME);
        }
    }
    Ok(hash)
}

fn source_state_path(index_path: &Path) -> PathBuf {
    index_path.join(".source_state.json")
}

fn load_source_state(index_path: &Path) -> Result<SourceState> {
    let path = source_state_path(index_path);
    if !path.is_file() {
        return Ok(HashMap::new());
    }
    let content = fs::read_to_string(&path)
        .with_context(|| format!("Impossible de lire l'etat des sources: {}", path.display()))?;
    match serde_json::from_str::<SourceState>(&content) {
        // Les anciens etats etaient indexes par chemin brut: on normalise.
        Ok(state) => Ok(state
            .into_iter()
            .map(|(path, fingerprint)| (source_key(&path), fingerprint))
            .collect()),
        Err(error) => {
            eprintln!(
                "Avertissement: etat des sources invalide ({}), il sera reconstruit: {error}",
                path.display()
            );
            Ok(HashMap::new())
        }
    }
}

fn save_source_state(index_path: &Path, state: &SourceState) -> Result<()> {
    let path = source_state_path(index_path);
    let tmp_path = index_path.join(".source_state.json.tmp");
    let mut file = File::create(&tmp_path).with_context(|| {
        format!(
            "Impossible de creer l'etat temporaire: {}",
            tmp_path.display()
        )
    })?;
    serde_json::to_writer(&mut file, state)?;
    file.flush()?;
    file.sync_all()?;
    atomic_replace_file(&tmp_path, &path).with_context(|| {
        format!(
            "Impossible de sauvegarder l'etat des sources: {}",
            path.display()
        )
    })
}

#[cfg(not(windows))]
fn atomic_replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    fs::rename(source, destination)
}

#[cfg(windows)]
fn atomic_replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "Kernel32")]
    extern "system" {
        fn MoveFileExW(existing: *const u16, replacement: *const u16, flags: u32) -> i32;
    }

    const MOVEFILE_REPLACE_EXISTING: u32 = 0x1;
    const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
    let existing: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let replacement: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let result = unsafe {
        MoveFileExW(
            existing.as_ptr(),
            replacement.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn source_is_in_scope(path_str: &str, source_root: &Path) -> bool {
    let candidate = Path::new(path_str);
    if candidate.starts_with(source_root) {
        return true;
    }

    let Ok(canonical_root) = source_root.canonicalize() else {
        return false;
    };
    if candidate.starts_with(&canonical_root) {
        return true;
    }

    // Un fichier orphelin ne peut plus etre canonicalise. On conserve donc
    // une comparaison textuelle avec frontiere de composant, insensible a la
    // casse uniquement sous Windows.
    let normalize = |path: &Path| {
        let value = path.to_string_lossy().replace('\\', "/");
        let value = value.trim_end_matches('/').to_string();
        if cfg!(windows) {
            value.to_lowercase()
        } else {
            value
        }
    };
    let root = normalize(&canonical_root);
    let path = normalize(candidate);
    path == root || path.starts_with(&(root + "/"))
}

fn create_progress_bar(total: u64) -> ProgressBar {
    let pb = ProgressBar::new(total);
    let style = ProgressStyle::default_bar()
        .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({per_sec}) {msg}")
        .expect("Template de barre invalide");
    pb.set_style(style);
    pb
}

fn read_bounded_line<R: BufRead>(
    reader: &mut R,
    output: &mut Vec<u8>,
    max_bytes: usize,
) -> io::Result<Option<bool>> {
    output.clear();
    let mut too_long = false;
    let mut read_any = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if read_any {
                Ok(Some(!too_long))
            } else {
                Ok(None)
            };
        }
        read_any = true;
        let newline = available.iter().position(|byte| *byte == b'\n');
        let consumed = newline.map_or(available.len(), |position| position + 1);
        if !too_long && output.len() + consumed <= max_bytes {
            output.extend_from_slice(&available[..consumed]);
        } else {
            too_long = true;
        }
        reader.consume(consumed);
        if newline.is_some() {
            return Ok(Some(!too_long));
        }
    }
}

static STAGING_COUNTER: AtomicUsize = AtomicUsize::new(0);

fn create_staging_file() -> io::Result<(PathBuf, BufWriter<File>)> {
    for _ in 0..100 {
        let id = STAGING_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("oeil-indexer-{}-{}.jsonl", std::process::id(), id,));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((path, BufWriter::new(file))),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "impossible de reserver un fichier temporaire unique",
    ))
}

fn log_file_error(context: &ProcessContext<'_>, path_str: &str, error: String) {
    context.error_count.fetch_add(1, Ordering::Relaxed);
    if context.logged_errors.fetch_add(1, Ordering::Relaxed) < MAX_ERROR_LOGS {
        let _ = context.tx.send(PipelineEvent::ErrorLog {
            file: path_str.to_string(),
            error,
        });
    }
}

fn sanitize_json_bytes(bytes: &mut [u8]) {
    for byte in bytes {
        if *byte < 32 && *byte != b'\n' {
            *byte = b' ';
        }
    }
}

fn truncate_at_char_boundary(text: &mut String, max_bytes: usize) {
    if text.len() > max_bytes {
        let boundary = text.floor_char_boundary(max_bytes);
        text.truncate(boundary);
    }
}

/// Prepare un document a partir d'une ligne JSON deja parsee.
///
/// `content` (indexe) contient les seules valeurs, sans les cles, pour ne pas
/// faire matcher les noms de champs. `raw` (snippets) est le JSON compact avec
/// ses cles, dans l'ordre d'origine, sans echappement \uXXXX: c'est le contenu
/// brut fidele promis par `index_raw_content`.
fn prepare_document(
    json_value: &Value,
    default_filename: &str,
    config: &Config,
    content: &mut String,
) -> Option<PreparedDocument> {
    let mut metadata = MetaData::default();
    if let Value::Object(map) = json_value {
        if let Some(value) = map.get("filename").or(map.get("title")) {
            metadata.filename = value.as_str().map(String::from);
        }
        if let Some(value) = map.get("type").or(map.get("doc_type")) {
            metadata.doc_type = value.as_str().map(String::from);
        }
    }
    content.clear();
    flatten_json_values(json_value, content);
    if content.trim().is_empty() {
        return None;
    }
    truncate_at_char_boundary(content, config.max_doc_content_bytes);
    let raw = if config.index_raw_content {
        let mut raw = serde_json::to_string(json_value).unwrap_or_default();
        truncate_at_char_boundary(&mut raw, config.max_doc_content_bytes);
        raw
    } else {
        String::new()
    };
    Some(PreparedDocument {
        filename: metadata
            .filename
            .unwrap_or_else(|| default_filename.to_string()),
        doc_type: metadata.doc_type.unwrap_or_else(|| "unknown".to_string()),
        content: content.clone(),
        raw,
    })
}

fn process_file_transactional(
    file_path: &Path,
    path_str: &str,
    context: &ProcessContext<'_>,
) -> bool {
    let file = match File::open(file_path) {
        Ok(file) => file,
        Err(error) => {
            log_file_error(context, path_str, format!("Impossible d'ouvrir: {}", error));
            return false;
        }
    };
    let (staging_path, mut staging_writer) = match create_staging_file() {
        Ok(value) => value,
        Err(error) => {
            log_file_error(context, path_str, format!("Staging impossible: {}", error));
            return false;
        }
    };

    let default_filename = file_path
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_else(|| "unknown.jsonl".to_string());
    let mut reader = BufReader::with_capacity(256 * 1024, file);
    let mut line = Vec::with_capacity(context.config.max_doc_input_bytes.min(256 * 1024));
    let mut content = String::new();
    let mut line_number = 0_usize;
    let mut file_has_errors = false;

    loop {
        if !context.running.load(Ordering::Relaxed) {
            drop(staging_writer);
            let _ = fs::remove_file(&staging_path);
            return false;
        }
        let within_limit =
            match read_bounded_line(&mut reader, &mut line, context.config.max_doc_input_bytes) {
                Ok(Some(value)) => value,
                Ok(None) => break,
                Err(error) => {
                    log_file_error(context, path_str, format!("Erreur de lecture: {}", error));
                    drop(staging_writer);
                    let _ = fs::remove_file(&staging_path);
                    return false;
                }
            };
        line_number += 1;
        if !within_limit {
            file_has_errors = true;
            log_file_error(
                context,
                path_str,
                format!(
                    "Document JSON #{} superieur a max_doc_input_bytes ({})",
                    line_number, context.config.max_doc_input_bytes,
                ),
            );
            continue;
        }
        if line.iter().all(|byte| byte.is_ascii_whitespace()) {
            continue;
        }
        sanitize_json_bytes(&mut line);
        let json_value: Value = match serde_json::from_slice(&line) {
            Ok(value) => value,
            Err(error) => {
                file_has_errors = true;
                log_file_error(
                    context,
                    path_str,
                    format!("Erreur JSON #{}: {}", line_number, error),
                );
                continue;
            }
        };

        let Some(prepared) =
            prepare_document(&json_value, &default_filename, context.config, &mut content)
        else {
            continue;
        };
        if let Err(error) = serde_json::to_writer(&mut staging_writer, &prepared) {
            log_file_error(
                context,
                path_str,
                format!("Ecriture staging impossible: {}", error),
            );
            drop(staging_writer);
            let _ = fs::remove_file(&staging_path);
            return false;
        }
        if let Err(error) = writeln!(staging_writer) {
            log_file_error(
                context,
                path_str,
                format!("Ecriture staging impossible: {}", error),
            );
            drop(staging_writer);
            let _ = fs::remove_file(&staging_path);
            return false;
        }
    }

    if let Err(error) = staging_writer.flush() {
        log_file_error(
            context,
            path_str,
            format!("Flush staging impossible: {}", error),
        );
        drop(staging_writer);
        let _ = fs::remove_file(&staging_path);
        return false;
    }
    drop(staging_writer);
    if file_has_errors {
        let _ = fs::remove_file(&staging_path);
        return false;
    }

    let validation_file = match File::open(&staging_path) {
        Ok(file) => file,
        Err(error) => {
            log_file_error(context, path_str, format!("Staging illisible: {}", error));
            let _ = fs::remove_file(&staging_path);
            return false;
        }
    };
    if serde_json::Deserializer::from_reader(BufReader::new(validation_file))
        .into_iter::<PreparedDocument>()
        .any(|value| value.is_err())
    {
        log_file_error(
            context,
            path_str,
            "Staging invalide apres ecriture".to_string(),
        );
        let _ = fs::remove_file(&staging_path);
        return false;
    }

    let is_replacement = !context.replace_sources.is_empty();
    if is_replacement
        && context
            .tx
            .send(PipelineEvent::BeginReplace(
                context.replace_sources.to_vec(),
            ))
            .is_err()
    {
        let _ = fs::remove_file(&staging_path);
        return false;
    }
    let staged_file = match File::open(&staging_path) {
        Ok(file) => file,
        Err(error) => {
            log_file_error(context, path_str, format!("Staging illisible: {}", error));
            let _ = fs::remove_file(&staging_path);
            return false;
        }
    };
    for prepared in serde_json::Deserializer::from_reader(BufReader::new(staged_file))
        .into_iter::<PreparedDocument>()
    {
        let prepared = match prepared {
            Ok(value) => value,
            Err(error) => {
                log_file_error(
                    context,
                    path_str,
                    format!("Relecture staging impossible: {}", error),
                );
                let _ = fs::remove_file(&staging_path);
                return false;
            }
        };
        let mut doc = tantivy::TantivyDocument::default();
        doc.add_text(context.fields.source, path_str);
        doc.add_text(
            context.fields.filename,
            sanitize_content(
                &prepared.filename,
                context.sanitize_set,
                context.config.max_word_length,
            ),
        );
        doc.add_text(context.fields.doc_type, &prepared.doc_type);
        doc.add_text(
            context.fields.content,
            sanitize_content(
                &prepared.content,
                context.sanitize_set,
                context.config.max_word_length,
            ),
        );
        if let Some(raw_field) = context.fields.content_raw {
            let raw = if prepared.raw.is_empty() {
                &prepared.content
            } else {
                &prepared.raw
            };
            doc.add_text(raw_field, raw);
        }
        if context.tx.send(PipelineEvent::Document(doc)).is_err() {
            let _ = fs::remove_file(&staging_path);
            return false;
        }
        context.doc_count.fetch_add(1, Ordering::Relaxed);
    }
    if is_replacement && context.tx.send(PipelineEvent::EndReplace).is_err() {
        let _ = fs::remove_file(&staging_path);
        return false;
    }
    let _ = fs::remove_file(&staging_path);
    true
}

fn commit_with_retry(
    writer: &mut tantivy::IndexWriter,
    pb: &ProgressBar,
    context: &str,
) -> Result<()> {
    for attempt in 1..=COMMIT_MAX_RETRIES {
        match writer.commit() {
            Ok(_) => return Ok(()),
            Err(e) if attempt < COMMIT_MAX_RETRIES => {
                pb.set_message(format!(
                    "{} — Erreur: {} (tentative {}/{}), retry dans {}s...",
                    context,
                    e,
                    attempt,
                    COMMIT_MAX_RETRIES,
                    COMMIT_RETRY_DELAY.as_secs(),
                ));
                std::thread::sleep(COMMIT_RETRY_DELAY);
            }
            Err(e) => {
                return Err(anyhow::anyhow!(
                    "{} — Echec apres {} tentatives: {}",
                    context,
                    COMMIT_MAX_RETRIES,
                    e
                ));
            }
        }
    }
    unreachable!()
}

fn print_summary(
    elapsed: std::time::Duration,
    docs: usize,
    skipped: usize,
    errors: usize,
    log_path: &Path,
) {
    println!("---------------------------------------------------");
    println!("Temps total       : {:?}", elapsed);
    println!("Documents ajoutes : {}", docs);
    println!("Fichiers ignores  : {}", skipped);
    if errors > 0 {
        println!(
            "Erreurs           : {} (Voir {})",
            errors,
            log_path.display()
        );
    } else {
        println!("Erreurs           : 0");
    }
    println!("---------------------------------------------------");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(name: &str) -> PathBuf {
        let mut dir = std::env::temp_dir();
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        dir.push(format!("{}_{}", name, unique));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn collect_jsonl_files_finds_jsonl_only() {
        let dir = temp_dir("oeil_collect");
        fs::write(dir.join("a.jsonl"), "{}").unwrap();
        fs::write(dir.join("b.JSONL"), "{}").unwrap();
        fs::write(dir.join("c.json"), "{}").unwrap();
        fs::write(dir.join("d.txt"), "hello").unwrap();

        let files = collect_jsonl_files(dir.to_str().unwrap()).unwrap();
        assert_eq!(files.len(), 2, "Should find exactly 2 .jsonl files");

        let names: Vec<String> = files
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().to_string())
            .collect();
        assert!(names.contains(&"a.jsonl".to_string()));
        assert!(names.contains(&"b.JSONL".to_string()));

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn collect_jsonl_files_recurses_subdirectories() {
        let dir = temp_dir("oeil_collect_recursive");
        let sub = dir.join("subdir");
        fs::create_dir_all(&sub).unwrap();
        fs::write(dir.join("top.jsonl"), "{}").unwrap();
        fs::write(sub.join("nested.jsonl"), "{}").unwrap();

        let files = collect_jsonl_files(dir.to_str().unwrap()).unwrap();
        assert_eq!(files.len(), 2);

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn collect_jsonl_files_empty_directory() {
        let dir = temp_dir("oeil_collect_empty");
        let files = collect_jsonl_files(dir.to_str().unwrap()).unwrap();
        assert!(files.is_empty());

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn purge_scope_excludes_other_source_roots() {
        let root = temp_dir("oeil_purge_scope");
        let source_a = root.join("source_a");
        let source_b = root.join("source_b");
        fs::create_dir_all(&source_a).unwrap();
        fs::create_dir_all(&source_b).unwrap();

        let in_scope = source_a.join("deleted.jsonl").to_string_lossy().to_string();
        let out_of_scope = source_b.join("keep.jsonl").to_string_lossy().to_string();

        assert!(source_is_in_scope(&in_scope, &source_a));
        assert!(!source_is_in_scope(&out_of_scope, &source_a));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn source_state_roundtrip_and_fingerprint_change() {
        let dir = temp_dir("oeil_source_state");
        let source = dir.join("source.jsonl");
        fs::write(&source, "{\"value\":1}\n").unwrap();
        let first = source_fingerprint(&source).unwrap();

        let mut state = SourceState::new();
        state.insert(source_key(&source.to_string_lossy()), first);
        save_source_state(&dir, &state).unwrap();
        assert_eq!(load_source_state(&dir).unwrap(), state);

        fs::write(&source, "{\"value\":12345}\n").unwrap();
        let second = source_fingerprint(&source).unwrap();
        assert_ne!(first, second);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn invalid_source_state_is_rebuilt() {
        let dir = temp_dir("oeil_invalid_source_state");
        fs::write(source_state_path(&dir), b"{not json").unwrap();

        assert!(load_source_state(&dir).unwrap().is_empty());

        fs::remove_dir_all(dir).unwrap();
    }

    fn write_tantivy_meta(path: &Path) {
        fs::write(path.join("meta.json"), r#"{"segments":[],"schema":[]}"#).unwrap();
    }

    #[test]
    fn reset_accepts_valid_tantivy_index_without_schema_config() {
        let root = temp_dir("oeil_reset_valid");
        let source = root.join("source");
        let index = root.join("index");
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(&index).unwrap();
        write_tantivy_meta(&index);

        let validated = validate_reset_target(&source, &index).unwrap();
        assert_eq!(validated, index.canonicalize().unwrap());

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn reset_rejects_arbitrary_directory() {
        let root = temp_dir("oeil_reset_arbitrary");
        let source = root.join("source");
        let target = root.join("not_an_index");
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(&target).unwrap();

        let error = validate_reset_target(&source, &target).unwrap_err();
        assert!(error.to_string().contains("meta.json"));

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn reset_rejects_directory_containing_source() {
        let target = temp_dir("oeil_reset_contains_source");
        let source = target.join("source");
        fs::create_dir_all(&source).unwrap();
        write_tantivy_meta(&target);

        let error = validate_reset_target(&source, &target).unwrap_err();
        assert!(error.to_string().contains("contient le dossier source"));

        fs::remove_dir_all(target).unwrap();
    }

    #[test]
    fn reset_rejects_invalid_tantivy_metadata() {
        let root = temp_dir("oeil_reset_invalid_meta");
        let source = root.join("source");
        let target = root.join("index");
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(&target).unwrap();
        fs::write(target.join("meta.json"), "not json").unwrap();

        let error = validate_reset_target(&source, &target).unwrap_err();
        assert!(error.to_string().contains("meta.json"));

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn reset_rejects_filesystem_root() {
        let source = std::env::current_dir().unwrap().canonicalize().unwrap();
        let filesystem_root = source.ancestors().last().unwrap();

        let error = validate_reset_target(&source, filesystem_root).unwrap_err();
        assert!(error.to_string().contains("racine de systeme de fichiers"));
    }

    #[test]
    fn check_schema_conflict_allows_matching_config() {
        let dir = temp_dir("oeil_schema_match");
        let config = Config::default();
        config.save_to_index(&dir).unwrap();

        let result = check_schema_conflict(&dir, &config, false);
        assert!(result.is_ok());

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn check_schema_conflict_detects_mismatch() {
        let dir = temp_dir("oeil_schema_mismatch");
        let old_config = Config {
            language: "fr".to_string(),
            ..Config::default()
        };
        old_config.save_to_index(&dir).unwrap();

        let new_config = Config::default(); // language = "en"
        let result = check_schema_conflict(&dir, &new_config, false);
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("Conflit de schema"));

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn check_schema_conflict_ignored_with_reset() {
        let dir = temp_dir("oeil_schema_reset");
        let old_config = Config {
            language: "fr".to_string(),
            ..Config::default()
        };
        old_config.save_to_index(&dir).unwrap();

        let new_config = Config::default();
        let result = check_schema_conflict(&dir, &new_config, true);
        assert!(result.is_ok(), "reset=true should skip conflict check");

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn check_schema_conflict_ok_for_new_index() {
        let dir = temp_dir("oeil_schema_new");
        fs::remove_dir_all(&dir).unwrap(); // ensure it doesn't exist

        let config = Config::default();
        let result = check_schema_conflict(&dir, &config, false);
        assert!(result.is_ok(), "non-existent index should not conflict");
    }

    #[test]
    fn bounded_line_discards_oversized_input_and_resumes() {
        let input = b"0123456789\n{}\n";
        let mut reader = BufReader::new(&input[..]);
        let mut output = Vec::new();

        assert_eq!(
            read_bounded_line(&mut reader, &mut output, 5).unwrap(),
            Some(false)
        );
        assert!(output.is_empty());
        assert_eq!(
            read_bounded_line(&mut reader, &mut output, 5).unwrap(),
            Some(true)
        );
        assert_eq!(output, b"{}\n");
    }

    #[test]
    fn legacy_fingerprint_without_sample_hash_is_supported() {
        let fingerprint: SourceFingerprint =
            serde_json::from_str(r#"{"size":42,"modified_ns":7}"#).unwrap();
        assert_eq!(fingerprint.sample_hash, 0);
    }

    #[test]
    fn json_byte_sanitizer_preserves_newlines() {
        let mut bytes = b"hello\x01world\nok".to_vec();
        sanitize_json_bytes(&mut bytes);
        assert_eq!(bytes, b"hello world\nok");
    }

    #[cfg(windows)]
    #[test]
    fn source_key_ignores_case_and_separator_on_windows() {
        assert_eq!(
            source_key("X:/Data/a.jsonl"),
            source_key("x:\\data\\A.JSONL")
        );
    }

    #[test]
    fn legacy_source_state_keys_are_normalized_on_load() {
        let dir = temp_dir("oeil_state_keys");
        let raw_path = "X:/Backup/A.jsonl";
        let mut legacy = SourceState::new();
        legacy.insert(
            raw_path.to_string(),
            SourceFingerprint {
                size: 1,
                modified_ns: 2,
                sample_hash: 3,
            },
        );
        fs::write(
            source_state_path(&dir),
            serde_json::to_string(&legacy).unwrap(),
        )
        .unwrap();

        let state = load_source_state(&dir).unwrap();

        assert!(state.contains_key(&source_key(raw_path)));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn prepared_document_keeps_raw_json_with_keys_and_unescaped_text() {
        let line = br#"{"name":"Jos\u00e9","email":"jose@example.com","age":42}"#;
        let value: Value = serde_json::from_slice(line).unwrap();
        let mut content = String::new();

        let prepared =
            prepare_document(&value, "file.jsonl", &Config::default(), &mut content).unwrap();

        assert_eq!(
            prepared.raw,
            r#"{"name":"José","email":"jose@example.com","age":42}"#
        );
        assert_eq!(prepared.content, "José jose@example.com 42");
        assert_eq!(prepared.filename, "file.jsonl");
    }

    #[test]
    fn prepared_document_has_no_raw_copy_when_disabled() {
        let value: Value = serde_json::json!({"name": "x"});
        let config = Config {
            index_raw_content: false,
            ..Config::default()
        };
        let mut content = String::new();

        let prepared = prepare_document(&value, "f.jsonl", &config, &mut content).unwrap();

        assert!(prepared.raw.is_empty());
    }

    #[test]
    fn prepared_document_truncates_on_char_boundary() {
        let value: Value = serde_json::json!({"text": "ééééé"});
        let config = Config {
            max_doc_content_bytes: 5,
            ..Config::default()
        };
        let mut content = String::new();

        let prepared = prepare_document(&value, "f.jsonl", &config, &mut content).unwrap();

        assert_eq!(prepared.content, "éé");
        assert!(prepared.raw.len() <= 5);
    }

    fn index_with_segments(dir: &Path, sources: &[&str]) -> (Index, IndexFields) {
        let (index, fields) = open_or_create_index(dir, &Config::default()).unwrap();
        let mut writer: tantivy::IndexWriter<tantivy::TantivyDocument> =
            index.writer_with_num_threads(1, 15_000_000).unwrap();
        writer.set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));
        for source in sources {
            let mut doc = tantivy::TantivyDocument::default();
            doc.add_text(fields.source, source);
            doc.add_text(fields.filename, "f.jsonl");
            doc.add_text(fields.doc_type, "record");
            doc.add_text(fields.content, "hello world");
            if let Some(raw) = fields.content_raw {
                doc.add_text(raw, r#"{"v":"hello world"}"#);
            }
            writer.add_document(doc).unwrap();
            writer.commit().unwrap(); // un segment par document
        }
        writer.wait_merging_threads().unwrap();
        (index, fields)
    }

    #[test]
    fn merge_reduces_segments_and_keeps_documents() {
        let dir = temp_dir("oeil_merge");
        let sources = ["a", "b", "c", "d", "e", "f", "g"];
        let (index, _) = index_with_segments(&dir, &sources);
        assert_eq!(index.searchable_segment_ids().unwrap().len(), sources.len());

        merge_segments(&dir, 2, 3, &AtomicBool::new(true)).unwrap();

        let metas = index.searchable_segment_metas().unwrap();
        assert_eq!(metas.len(), 2);
        let docs: u32 = metas.iter().map(|meta| meta.num_docs()).sum();
        assert_eq!(docs as usize, sources.len());
        drop(index);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn merge_stops_cleanly_when_interrupted() {
        let dir = temp_dir("oeil_merge_stop");
        let (index, _) = index_with_segments(&dir, &["a", "b", "c"]);

        merge_segments(&dir, 1, 2, &AtomicBool::new(false)).unwrap();

        assert_eq!(index.searchable_segment_ids().unwrap().len(), 3);
        drop(index);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn merge_rejects_invalid_parameters() {
        let dir = temp_dir("oeil_merge_params");
        assert!(merge_segments(&dir, 0, 8, &AtomicBool::new(true)).is_err());
        assert!(merge_segments(&dir, 4, 1, &AtomicBool::new(true)).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn known_sources_group_spellings_of_the_same_file() {
        let dir = temp_dir("oeil_known_sources");
        let (index, fields) = index_with_segments(
            &dir,
            &["X:/Data/a.jsonl", "x:\\data\\a.jsonl", "X:\\Data\\b.jsonl"],
        );

        let known = load_known_sources(&index, fields.source, false).unwrap();

        assert_eq!(known.len(), 2);
        assert_eq!(known[&source_key("X:\\Data\\a.jsonl")].len(), 2);
        drop(index);
        fs::remove_dir_all(dir).unwrap();
    }
}
