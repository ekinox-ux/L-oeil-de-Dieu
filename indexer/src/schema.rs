use std::path::Path;

use anyhow::{bail, Context, Result};
use tantivy::directory::MmapDirectory;
use tantivy::schema::{
    Field, IndexRecordOption, Schema, TextFieldIndexing, TextOptions, STORED, STRING,
};
use tantivy::tokenizer::{
    AsciiFoldingFilter, Language, LowerCaser, RemoveLongFilter, SimpleTokenizer, Stemmer,
    StopWordFilter, TextAnalyzer,
};
use tantivy::Index;

use crate::config::Config;

#[derive(Clone, Copy)]
pub struct IndexFields {
    pub source: Field,
    pub filename: Field,
    pub doc_type: Field,
    pub content: Field,
    pub content_raw: Option<Field>,
}

impl IndexFields {
    /// Retrouve les champs d'un schema existant, ancien format compris.
    pub fn from_schema(schema: &Schema) -> Result<Self> {
        let field = |name: &str| {
            schema
                .get_field(name)
                .with_context(|| format!("Champ '{name}' absent du schema de l'index"))
        };
        Ok(IndexFields {
            source: field("source")?,
            filename: field("filename")?,
            doc_type: field("type")?,
            content: field("content")?,
            content_raw: schema.get_field("content_raw").ok(),
        })
    }
}

/// Schema des nouveaux index.
///
/// Avec `index_raw_content`, `content` est seulement indexe: le backend ne lit
/// que `content_raw` (repli sur `content` pour les index sans contenu brut).
/// Stocker les deux dupliquait le texte de chaque document dans le doc store.
pub fn build_schema(config: &Config) -> (Schema, IndexFields) {
    let mut builder = Schema::builder();

    let tech_indexing = TextFieldIndexing::default()
        .set_tokenizer("custom_tech")
        .set_index_option(IndexRecordOption::WithFreqsAndPositions);
    let indexed_only = TextOptions::default().set_indexing_options(tech_indexing);
    let tech_options = indexed_only.clone().set_stored();
    let content_options = if config.index_raw_content {
        indexed_only
    } else {
        tech_options.clone()
    };

    let source = builder.add_text_field("source", STRING | STORED);
    let filename = builder.add_text_field("filename", tech_options);
    let doc_type = builder.add_text_field("type", STRING | STORED);
    let content = builder.add_text_field("content", content_options);

    let content_raw = if config.index_raw_content {
        Some(builder.add_text_field("content_raw", STORED))
    } else {
        None
    };

    let schema = builder.build();
    let fields = IndexFields {
        source,
        filename,
        doc_type,
        content,
        content_raw,
    };

    (schema, fields)
}

/// Ouvre un index existant avec SON schema, ou cree un index au schema actuel.
///
/// `Index::open_or_create` refuse tout schema different: les index deja
/// construits (ou `content` etait encore stocke) deviendraient inutilisables
/// pour l'indexation incrementale sans reconstruction complete.
pub fn open_or_create_index(index_path: &Path, config: &Config) -> Result<(Index, IndexFields)> {
    let directory = MmapDirectory::open(index_path).with_context(|| {
        format!(
            "Impossible d'ouvrir le dossier d'index {}",
            index_path.display()
        )
    })?;
    // Un index existant garde ses reglages (meta.json) : seule la commande
    // `compress` change la compression de son doc store.
    let index = if Index::exists(&directory)? {
        Index::open(directory)?
    } else {
        let (schema, _) = build_schema(config);
        Index::builder()
            .schema(schema)
            .settings(config.index_settings())
            .open_or_create(directory)?
    };
    let fields = IndexFields::from_schema(&index.schema())?;
    if config.index_raw_content && fields.content_raw.is_none() {
        bail!(
            "L'index {} n'a pas de champ content_raw alors que index_raw_content = true. \
             Utilisez --reset pour le reconstruire ou index_raw_content = false.",
            index_path.display()
        );
    }
    register_tokenizer(&index, config)?;
    Ok((index, fields))
}

/// Ouvre un index qui doit deja exister (purge, merge).
pub fn open_existing_index(index_path: &Path) -> Result<(Index, IndexFields)> {
    let directory = MmapDirectory::open(index_path).with_context(|| {
        format!(
            "Impossible d'ouvrir le dossier d'index {}",
            index_path.display()
        )
    })?;
    if !Index::exists(&directory)? {
        bail!("Aucun index Tantivy dans {}", index_path.display());
    }
    let index = Index::open(directory)?;
    let fields = IndexFields::from_schema(&index.schema())?;
    if let Some(config) = Config::load_from_index(index_path)? {
        register_tokenizer(&index, &config)?;
    }
    Ok((index, fields))
}

fn parse_language(lang: &str) -> Language {
    match lang {
        "fr" => Language::French,
        "en" => Language::English,
        "es" => Language::Spanish,
        "de" => Language::German,
        "it" => Language::Italian,
        "pt" => Language::Portuguese,
        _ => Language::English,
    }
}

pub fn register_tokenizer(index: &Index, config: &Config) -> Result<()> {
    let language = parse_language(&config.language);
    let limit = config.max_word_length;
    let stop_words = || StopWordFilter::new(language).expect("Stop words indisponibles");

    // Filtres dans un ordre fixe, repris par le backend (engine.py) :
    // remove_long, lowercase, stemmer, stop words, ascii folding. L'analyseur
    // reste statique (pas de `dynamic()`, plus lent a l'indexation).
    macro_rules! analyzer {
        ($($filter:expr),* $(,)?) => {
            TextAnalyzer::builder(SimpleTokenizer::default())
                .filter(RemoveLongFilter::limit(limit))
                .filter(LowerCaser)
                $(.filter($filter))*
                .build()
        };
    }

    let analyzer = match (
        config.use_stemming,
        config.use_stop_words,
        config.ascii_folding_enabled(),
    ) {
        (true, true, true) => analyzer!(Stemmer::new(language), stop_words(), AsciiFoldingFilter),
        (true, true, false) => analyzer!(Stemmer::new(language), stop_words()),
        (true, false, true) => analyzer!(Stemmer::new(language), AsciiFoldingFilter),
        (true, false, false) => analyzer!(Stemmer::new(language)),
        (false, true, true) => analyzer!(stop_words(), AsciiFoldingFilter),
        (false, true, false) => analyzer!(stop_words()),
        (false, false, true) => analyzer!(AsciiFoldingFilter),
        (false, false, false) => analyzer!(),
    };

    index.tokenizers().register("custom_tech", analyzer);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("{name}_{unique}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn is_stored(schema: &Schema, name: &str) -> bool {
        let field = schema.get_field(name).unwrap();
        schema.get_field_entry(field).is_stored()
    }

    #[test]
    fn build_schema_includes_raw_content_by_default() {
        let cfg = Config::default();
        let (schema, fields) = build_schema(&cfg);

        assert!(fields.content_raw.is_some());
        assert!(schema.get_field("content_raw").is_ok());
        assert!(schema.get_field_name(fields.source) == "source");
        assert!(schema.get_field_name(fields.filename) == "filename");
        assert!(schema.get_field_name(fields.doc_type) == "type");
        assert!(schema.get_field_name(fields.content) == "content");
    }

    #[test]
    fn content_is_not_stored_twice_when_raw_content_is_enabled() {
        let (schema, _) = build_schema(&Config::default());

        assert!(is_stored(&schema, "content_raw"));
        assert!(!is_stored(&schema, "content"));
        assert!(schema
            .get_field_entry(schema.get_field("content").unwrap())
            .is_indexed());
    }

    #[test]
    fn build_schema_excludes_raw_content_when_disabled() {
        let cfg = Config {
            index_raw_content: false,
            ..Config::default()
        };

        let (schema, fields) = build_schema(&cfg);

        assert!(fields.content_raw.is_none());
        assert!(schema.get_field("content_raw").is_err());
        // Sans contenu brut, content reste stocke: c'est le repli des snippets.
        assert!(is_stored(&schema, "content"));
    }

    #[test]
    fn existing_index_keeps_its_legacy_schema() {
        let dir = temp_dir("oeil_legacy_schema");
        // Ancien schema: content indexe ET stocke.
        let mut builder = Schema::builder();
        let tech = TextOptions::default()
            .set_indexing_options(
                TextFieldIndexing::default()
                    .set_tokenizer("custom_tech")
                    .set_index_option(IndexRecordOption::WithFreqsAndPositions),
            )
            .set_stored();
        builder.add_text_field("source", STRING | STORED);
        builder.add_text_field("filename", tech.clone());
        builder.add_text_field("type", STRING | STORED);
        builder.add_text_field("content", tech);
        builder.add_text_field("content_raw", STORED);
        Index::create_in_dir(&dir, builder.build()).unwrap();

        let (index, fields) = open_or_create_index(&dir, &Config::default()).unwrap();

        assert!(is_stored(&index.schema(), "content"));
        assert!(fields.content_raw.is_some());
        drop(index);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn new_index_uses_configured_docstore_compression() {
        use tantivy::store::{Compressor, ZstdCompressor};

        let zstd_dir = temp_dir("oeil_new_zstd");
        let (index, _) = open_or_create_index(&zstd_dir, &Config::default()).unwrap();
        assert_eq!(
            index.settings().docstore_compression,
            Compressor::Zstd(ZstdCompressor {
                compression_level: Some(3)
            })
        );
        assert_eq!(index.settings().docstore_blocksize, 128 * 1024);
        drop(index);
        // Rouvert, l'index garde ses reglages meme si la config change.
        let lz4 = Config {
            docstore_compression: "lz4".to_string(),
            ..Config::default()
        };
        let (index, _) = open_or_create_index(&zstd_dir, &lz4).unwrap();
        assert!(matches!(
            index.settings().docstore_compression,
            Compressor::Zstd(_)
        ));
        drop(index);
        fs::remove_dir_all(zstd_dir).unwrap();

        let lz4_dir = temp_dir("oeil_new_lz4");
        let (index, _) = open_or_create_index(&lz4_dir, &lz4).unwrap();
        assert_eq!(index.settings().docstore_compression, Compressor::Lz4);
        drop(index);
        fs::remove_dir_all(lz4_dir).unwrap();
    }

    #[test]
    fn new_index_uses_current_schema() {
        let dir = temp_dir("oeil_new_schema");

        let (index, _) = open_or_create_index(&dir, &Config::default()).unwrap();

        assert!(!is_stored(&index.schema(), "content"));
        drop(index);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn open_existing_index_rejects_empty_directory() {
        let dir = temp_dir("oeil_no_index");
        assert!(open_existing_index(&dir).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    fn tokens(index: &Index, text: &str) -> Vec<String> {
        let mut analyzer = index.tokenizers().get("custom_tech").unwrap();
        let mut stream = analyzer.token_stream(text);
        let mut out = Vec::new();
        while stream.advance() {
            out.push(stream.token().text.clone());
        }
        out
    }

    #[test]
    fn ascii_folding_removes_accents_only_when_enabled() {
        let dir = temp_dir("oeil_fold_tokens");
        let (index, _) = open_or_create_index(&dir, &Config::default()).unwrap();
        let folding = Config {
            ascii_folding: Some(true),
            ..Config::default()
        };

        register_tokenizer(&index, &Config::default()).unwrap();
        assert_eq!(tokens(&index, "Hélène Strauß"), ["hélène", "strauß"]);
        register_tokenizer(&index, &folding).unwrap();
        assert_eq!(
            tokens(&index, "Hélène Strauß Œuvre"),
            ["helene", "strauss", "oeuvre"]
        );

        let stemming = Config {
            use_stemming: true,
            use_stop_words: true,
            language: "fr".to_string(),
            ..folding
        };
        register_tokenizer(&index, &stemming).unwrap();
        // Racine francaise puis accents retires (élégantes -> élég -> eleg).
        assert_eq!(tokens(&index, "Élégantes"), ["eleg"]);
        drop(index);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn parse_language_falls_back_to_english() {
        assert_eq!(parse_language("fr"), Language::French);
        assert_eq!(parse_language("unknown"), Language::English);
    }
}
