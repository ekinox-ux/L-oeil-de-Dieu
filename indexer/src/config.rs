use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use tantivy::store::{Compressor, ZstdCompressor};
use tantivy::IndexSettings;

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Config {
    #[serde(default = "default_writer_memory")]
    pub writer_memory_mb: usize,
    #[serde(default = "default_channel_size")]
    pub channel_size: usize,
    #[serde(default = "default_max_word_length")]
    pub max_word_length: usize,
    #[serde(default = "default_sanitize_chars")]
    pub sanitize_chars: Vec<char>,
    #[serde(default = "default_index_raw_content")]
    pub index_raw_content: bool,
    #[serde(default = "default_language")]
    pub language: String,
    #[serde(default = "default_use_stemming")]
    pub use_stemming: bool,
    #[serde(default = "default_use_stop_words")]
    pub use_stop_words: bool,
    #[serde(default = "default_commit_frequency")]
    pub commit_every_n_docs: usize,
    #[serde(default = "default_writer_threads")]
    pub writer_threads: usize,
    #[serde(default = "default_reader_threads")]
    pub reader_threads: usize,
    #[serde(default = "default_max_doc_content_bytes")]
    pub max_doc_content_bytes: usize,
    #[serde(default = "default_max_doc_input_bytes")]
    pub max_doc_input_bytes: usize,
    /// Compression du doc store des index CREES avec cette config ("zstd" ou
    /// "lz4"). Un index existant garde la sienne : `compress` le convertit.
    #[serde(default = "default_docstore_compression")]
    pub docstore_compression: String,
    #[serde(default = "default_zstd_level")]
    pub zstd_level: i32,
    /// Taille des blocs compresses du doc store, en octets.
    #[serde(default = "default_docstore_blocksize")]
    pub docstore_blocksize: usize,
}

pub const ZSTD_LEVEL_RANGE: std::ops::RangeInclusive<i32> = 1..=22;
pub const DOCSTORE_BLOCKSIZE_RANGE: std::ops::RangeInclusive<usize> = 4 * 1024..=4 * 1024 * 1024;

pub fn validate_zstd_level(level: i32) -> Result<()> {
    if !ZSTD_LEVEL_RANGE.contains(&level) {
        bail!(
            "zstd_level doit etre compris entre {} et {}",
            ZSTD_LEVEL_RANGE.start(),
            ZSTD_LEVEL_RANGE.end()
        );
    }
    Ok(())
}

pub fn validate_docstore_blocksize(blocksize: usize) -> Result<()> {
    if !DOCSTORE_BLOCKSIZE_RANGE.contains(&blocksize) {
        bail!(
            "docstore_blocksize doit etre compris entre {} et {} octets",
            DOCSTORE_BLOCKSIZE_RANGE.start(),
            DOCSTORE_BLOCKSIZE_RANGE.end()
        );
    }
    Ok(())
}

/// Reglages Tantivy d'un doc store zstd.
pub fn zstd_index_settings(level: i32, blocksize: usize) -> IndexSettings {
    IndexSettings {
        docstore_compression: Compressor::Zstd(ZstdCompressor {
            compression_level: Some(level),
        }),
        docstore_blocksize: blocksize,
        ..IndexSettings::default()
    }
}

fn default_writer_memory() -> usize {
    500
}
fn default_channel_size() -> usize {
    1000
}
fn default_writer_threads() -> usize {
    1
}
fn default_reader_threads() -> usize {
    2
}
fn default_max_doc_content_bytes() -> usize {
    2 * 1024 * 1024
}
fn default_max_doc_input_bytes() -> usize {
    8 * 1024 * 1024
}
fn default_max_word_length() -> usize {
    250
}
fn default_sanitize_chars() -> Vec<char> {
    vec!['{', '}', '[', ']', ':', '"', ',', '(', ')', '=', ';']
}
fn default_index_raw_content() -> bool {
    true
}
fn default_language() -> String {
    "en".to_string()
}
fn default_use_stemming() -> bool {
    false
}
fn default_use_stop_words() -> bool {
    false
}
fn default_commit_frequency() -> usize {
    100_000
}
fn default_docstore_compression() -> String {
    "zstd".to_string()
}
fn default_zstd_level() -> i32 {
    3
}
fn default_docstore_blocksize() -> usize {
    128 * 1024
}

impl Default for Config {
    fn default() -> Self {
        Config {
            writer_memory_mb: default_writer_memory(),
            channel_size: default_channel_size(),
            max_word_length: default_max_word_length(),
            sanitize_chars: default_sanitize_chars(),
            index_raw_content: default_index_raw_content(),
            language: default_language(),
            use_stemming: default_use_stemming(),
            use_stop_words: default_use_stop_words(),
            commit_every_n_docs: default_commit_frequency(),
            writer_threads: default_writer_threads(),
            reader_threads: default_reader_threads(),
            max_doc_content_bytes: default_max_doc_content_bytes(),
            max_doc_input_bytes: default_max_doc_input_bytes(),
            docstore_compression: default_docstore_compression(),
            zstd_level: default_zstd_level(),
            docstore_blocksize: default_docstore_blocksize(),
        }
    }
}

impl Config {
    pub fn load(path: Option<&str>) -> Result<Self> {
        let config = if let Some(p) = path {
            let content = fs::read_to_string(p)
                .context(format!("Impossible de lire le fichier de config: {}", p))?;
            toml::from_str(&content).context("Erreur de parsing TOML")
        } else if let Some(config_path) = Self::default_path() {
            if config_path.exists() {
                let content = fs::read_to_string(&config_path)?;
                toml::from_str(&content).context("Erreur de parsing TOML")
            } else {
                Ok(Config::default())
            }
        } else {
            Ok(Config::default())
        }?;
        config.validate()?;
        Ok(config)
    }

    pub fn default_path() -> Option<PathBuf> {
        directories::ProjectDirs::from("com", "dieuclaude", "oeil-de-dieu")
            .map(|dirs| dirs.config_dir().join("config.toml"))
    }

    pub fn save(&self) -> Result<()> {
        let path = Self::default_path().context("Impossible de determiner le chemin de config")?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let content = toml::to_string_pretty(self)?;
        fs::write(&path, content)?;
        println!("Configuration sauvegardee dans: {}", path.display());
        Ok(())
    }

    pub fn save_to_index(&self, index_dir: &Path) -> Result<()> {
        let config_path = index_dir.join(".schema_config.toml");
        let content = toml::to_string_pretty(self)?;
        fs::write(&config_path, content)?;
        Ok(())
    }

    pub fn load_from_index(index_dir: &Path) -> Result<Option<Self>> {
        let config_path = index_dir.join(".schema_config.toml");
        if config_path.exists() {
            let content = fs::read_to_string(&config_path)?;
            let config: Self = toml::from_str(&content)?;
            config.validate()?;
            Ok(Some(config))
        } else {
            Ok(None)
        }
    }

    pub fn schema_hash(&self) -> String {
        format!(
            "raw:{}_lang:{}_stem:{}_stop:{}",
            self.index_raw_content, self.language, self.use_stemming, self.use_stop_words,
        )
    }

    /// Reglages du doc store pour un index cree avec cette config.
    pub fn index_settings(&self) -> IndexSettings {
        match self.docstore_compression.as_str() {
            "lz4" => IndexSettings {
                docstore_compression: Compressor::Lz4,
                docstore_blocksize: self.docstore_blocksize,
                ..IndexSettings::default()
            },
            _ => zstd_index_settings(self.zstd_level, self.docstore_blocksize),
        }
    }

    pub fn sanitize_set(&self) -> HashSet<char> {
        self.sanitize_chars.iter().copied().collect()
    }

    fn validate(&self) -> Result<()> {
        if self.commit_every_n_docs == 0 {
            bail!("commit_every_n_docs doit etre superieur a zero");
        }
        if self.writer_threads == 0 {
            bail!("writer_threads doit etre superieur a zero");
        }
        if self.reader_threads == 0 {
            bail!("reader_threads doit etre superieur a zero");
        }
        if self.writer_memory_mb == 0 {
            bail!("writer_memory_mb doit etre superieur a zero");
        }
        if self.max_word_length == 0 {
            bail!("max_word_length doit etre superieur a zero");
        }
        if self.max_doc_content_bytes == 0 {
            bail!("max_doc_content_bytes doit etre superieur a zero");
        }
        if self.max_doc_input_bytes == 0 {
            bail!("max_doc_input_bytes doit etre superieur a zero");
        }
        if !matches!(self.docstore_compression.as_str(), "zstd" | "lz4") {
            bail!(
                "docstore_compression doit valoir \"zstd\" ou \"lz4\" (recu : \"{}\")",
                self.docstore_compression
            );
        }
        validate_zstd_level(self.zstd_level)?;
        validate_docstore_blocksize(self.docstore_blocksize)?;
        Ok(())
    }
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
    fn default_config_has_expected_values() {
        let cfg = Config::default();
        assert_eq!(cfg.writer_memory_mb, 500);
        assert_eq!(cfg.reader_threads, 2);
        assert_eq!(cfg.language, "en");
        assert!(cfg.index_raw_content);
        assert_eq!(cfg.max_doc_content_bytes, 2 * 1024 * 1024);
        assert_eq!(cfg.max_doc_input_bytes, 8 * 1024 * 1024);
        assert_eq!(cfg.docstore_compression, "zstd");
        assert_eq!(cfg.zstd_level, 3);
        assert_eq!(cfg.docstore_blocksize, 128 * 1024);
    }

    #[test]
    fn index_settings_follow_docstore_options() {
        let zstd = Config::default().index_settings();
        assert_eq!(
            zstd.docstore_compression,
            Compressor::Zstd(ZstdCompressor {
                compression_level: Some(3)
            })
        );
        assert_eq!(zstd.docstore_blocksize, 128 * 1024);

        let lz4 = Config {
            docstore_compression: "lz4".to_string(),
            docstore_blocksize: 16 * 1024,
            ..Config::default()
        }
        .index_settings();
        assert_eq!(lz4.docstore_compression, Compressor::Lz4);
        assert_eq!(lz4.docstore_blocksize, 16 * 1024);
    }

    #[test]
    fn legacy_index_config_without_docstore_options_still_loads() {
        let dir = temp_dir("oeil_cfg_legacy_docstore");
        fs::write(
            dir.join(".schema_config.toml"),
            "index_raw_content = true
language = \"en\"
",
        )
        .unwrap();

        let loaded = Config::load_from_index(&dir).unwrap().unwrap();

        assert_eq!(loaded.docstore_compression, "zstd");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn load_rejects_invalid_docstore_options() {
        let dir = temp_dir("oeil_cfg_invalid_docstore");
        for (content, expected) in [
            (
                "docstore_compression = \"brotli\"
",
                "docstore_compression",
            ),
            (
                "zstd_level = 0
",
                "zstd_level",
            ),
            (
                "zstd_level = 23
",
                "zstd_level",
            ),
            (
                "docstore_blocksize = 1024
",
                "docstore_blocksize",
            ),
        ] {
            let path = dir.join("config.toml");
            fs::write(&path, content).unwrap();

            let error = Config::load(path.to_str()).unwrap_err().to_string();

            assert!(error.contains(expected), "{content} -> {error}");
        }
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn schema_hash_reflects_search_related_options() {
        let cfg = Config {
            index_raw_content: true,
            language: "fr".to_string(),
            use_stemming: true,
            ..Config::default()
        };
        assert_eq!(cfg.schema_hash(), "raw:true_lang:fr_stem:true_stop:false");
    }

    #[test]
    fn save_to_index_roundtrip() {
        let dir = temp_dir("oeil_cfg_roundtrip");
        let cfg = Config {
            language: "fr".to_string(),
            index_raw_content: true,
            ..Config::default()
        };

        cfg.save_to_index(&dir).unwrap();
        let loaded = Config::load_from_index(&dir).unwrap().unwrap();

        assert_eq!(loaded.language, "fr");
        assert!(loaded.index_raw_content);

        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn load_rejects_zero_commit_frequency() {
        let dir = temp_dir("oeil_cfg_invalid_commit");
        let path = dir.join("config.toml");
        fs::write(&path, "commit_every_n_docs = 0\n").unwrap();

        let result = Config::load(path.to_str());

        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("commit_every_n_docs"));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn load_rejects_zero_input_limit() {
        let dir = temp_dir("oeil_cfg_invalid_input_limit");
        let path = dir.join("config.toml");
        fs::write(&path, "max_doc_input_bytes = 0\n").unwrap();

        let result = Config::load(path.to_str());

        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("max_doc_input_bytes"));
        fs::remove_dir_all(dir).unwrap();
    }
}
