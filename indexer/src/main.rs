mod config;
mod indexing;
mod sanitize;
mod schema;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "L'Oeil de Dieu",
    version,
    about = "Indexeur haute performance de fichiers JSONL avec Tantivy"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Indexer un dossier de fichiers JSONL
    Index {
        /// Chemin vers le dossier contenant les fichiers JSONL
        source_path: String,
        /// Chemin vers le dossier de l'index Tantivy
        index_path: String,
        /// Chemin vers un fichier de configuration TOML personnalise
        #[arg(short, long)]
        config: Option<String>,
        /// Supprimer et reconstruire un index Tantivy valide depuis zero
        #[arg(short, long)]
        reset: bool,
        /// Desactiver la deduplication (evite de lire l'index existant, reduit la RAM)
        #[arg(long)]
        no_dedup: bool,
    },
    /// Purger les documents dont les sources ne sont plus dans le dossier source actuel
    Purge {
        /// Chemin vers le dossier contenant les fichiers JSONL actuels
        source_path: String,
        /// Chemin vers le dossier de l'index Tantivy
        index_path: String,
    },
    /// Fusionner les segments d'un index (accelere ouverture et recherche)
    ///
    /// A lancer hors indexation, backend arrete de preference: sous Windows,
    /// les fichiers encore ouverts ne peuvent pas etre liberes tout de suite.
    Merge {
        /// Chemin vers le dossier de l'index Tantivy
        index_path: String,
        /// Nombre de segments a atteindre
        #[arg(long, default_value_t = 16)]
        target_segments: usize,
        /// Nombre maximal de segments fusionnes a la fois (borne RAM et disque)
        #[arg(long, default_value_t = 8)]
        batch_size: usize,
    },
    /// Recompresser le doc store d'un index existant en zstd (gain de place)
    ///
    /// Reecrit chaque segment encore en lz4 par lots de --batch-size, sans
    /// reindexer ni changer les resultats. Reprenable : relancee apres une
    /// interruption, la commande ne traite que les segments restants.
    /// Backend arrete : il doit ensuite lire le zstd (build_tantivy_zstd.ps1).
    Compress {
        /// Chemin vers le dossier de l'index Tantivy
        index_path: String,
        /// Niveau zstd (1 a 22 ; au-dela de 3, gain faible et fusions plus lentes)
        #[arg(long, default_value_t = 3)]
        level: i32,
        /// Taille des blocs compresses du doc store, en octets
        #[arg(long, default_value_t = 131_072)]
        blocksize: usize,
        /// Nombre de segments reecrits par fusion (borne RAM et disque temporaire)
        #[arg(long, default_value_t = 64)]
        batch_size: usize,
    },
    /// Gerer la configuration
    Config {
        #[command(subcommand)]
        action: ConfigAction,
    },
}

#[derive(Subcommand)]
enum ConfigAction {
    /// Creer un fichier de configuration par defaut
    Init,
    /// Afficher la configuration actuelle
    Show,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Index {
            source_path,
            index_path,
            config,
            reset,
            no_dedup,
        } => indexing::run(
            &source_path,
            &index_path,
            config.as_deref(),
            reset,
            no_dedup,
        ),
        Commands::Purge {
            source_path,
            index_path,
        } => indexing::purge(&source_path, &index_path),
        Commands::Merge {
            index_path,
            target_segments,
            batch_size,
        } => indexing::merge(&index_path, target_segments, batch_size),
        Commands::Compress {
            index_path,
            level,
            blocksize,
            batch_size,
        } => indexing::compress(&index_path, level, blocksize, batch_size),
        Commands::Config { action } => match action {
            ConfigAction::Init => {
                let cfg = config::Config::default();
                cfg.save()?;
                Ok(())
            }
            ConfigAction::Show => {
                let cfg = config::Config::load(None)?;
                println!("{}", toml::to_string_pretty(&cfg)?);
                Ok(())
            }
        },
    }
}
