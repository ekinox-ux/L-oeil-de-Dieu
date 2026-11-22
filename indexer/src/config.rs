use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

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
