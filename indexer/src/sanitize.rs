use serde_json::Value;
use std::collections::HashSet;

/// Sanitize du texte : remplace les caracteres configures par des espaces,
/// collapse les espaces consecutifs, et coupe les mots trop longs.
pub fn sanitize_content(
    text: &str,
    sanitize_set: &HashSet<char>,
    max_word_length: usize,
) -> String {
    let mut result = String::with_capacity(text.len());
    let mut word_len = 0;
    let mut last_was_space = true;

    for c in text.chars() {
        let c = if sanitize_set.contains(&c) { ' ' } else { c };

        if c.is_whitespace() {
            if !last_was_space {
                result.push(' ');
                last_was_space = true;
            }
            word_len = 0;
        } else {
            word_len += 1;
            if word_len > max_word_length {
                result.push(' ');
                word_len = 1;
            }
            result.push(c);
            last_was_space = false;
        }
    }

    if result.ends_with(' ') {
        result.pop();
    }

    result
}

/// Extrait recursivement toutes les valeurs textuelles d'un JSON
/// dans un buffer unique, sans allocations intermediaires.
pub fn flatten_json_values(v: &Value, buf: &mut String) {
    match v {
        Value::String(s) => {
            if !s.is_empty() {
                if !buf.is_empty() {
                    buf.push(' ');
                }
                buf.push_str(s);
            }
        }
        Value::Number(n) => {
            if !buf.is_empty() {
                buf.push(' ');
            }
            use std::fmt::Write;
            let _ = write!(buf, "{}", n);
        }
        Value::Bool(b) => {
            if !buf.is_empty() {
                buf.push(' ');
            }
            buf.push_str(if *b { "true" } else { "false" });
        }
        Value::Array(arr) => {
            for item in arr {
                flatten_json_values(item, buf);
            }
        }
        Value::Object(map) => {
            for value in map.values() {
                flatten_json_values(value, buf);
            }
        }
        Value::Null => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_set(chars: &[char]) -> HashSet<char> {
        chars.iter().copied().collect()
    }

    #[test]
    fn sanitize_replaces_configured_chars() {
        let set = make_set(&['{', '}', ':']);
        assert_eq!(sanitize_content("{key: value}", &set, 250), "key value");
    }

    #[test]
    fn sanitize_collapses_whitespace() {
        let set = make_set(&[]);
        assert_eq!(sanitize_content("hello   world", &set, 250), "hello world");
    }

    #[test]
    fn sanitize_trims_edges() {
        let set = make_set(&[]);
        assert_eq!(sanitize_content("  hello  ", &set, 250), "hello");
    }

    #[test]
    fn sanitize_breaks_long_words() {
        let set = make_set(&[]);
        let long = "a".repeat(10);
        let result = sanitize_content(&long, &set, 5);
        assert_eq!(result, "aaaaa aaaaa");
    }

    #[test]
    fn sanitize_empty_input() {
        let set = make_set(&[]);
        assert_eq!(sanitize_content("", &set, 250), "");
    }

    #[test]
    fn flatten_simple_object() {
        let json: Value = serde_json::json!({
            "name": "test",
            "count": 42,
            "active": true
        });
        let mut buf = String::new();
        flatten_json_values(&json, &mut buf);
        assert!(buf.contains("test"));
        assert!(buf.contains("42"));
        assert!(buf.contains("true"));
    }

    #[test]
    fn flatten_nested_object() {
        let json: Value = serde_json::json!({
            "outer": {
                "inner": "deep_value"
            }
        });
        let mut buf = String::new();
        flatten_json_values(&json, &mut buf);
        assert!(buf.contains("deep_value"));
    }

    #[test]
    fn flatten_null_ignored() {
        let json: Value = serde_json::json!(null);
        let mut buf = String::new();
        flatten_json_values(&json, &mut buf);
        assert!(buf.is_empty());
    }

    #[test]
    fn flatten_reuses_buffer() {
        let json: Value = serde_json::json!({"a": "hello"});
        let mut buf = String::new();
        flatten_json_values(&json, &mut buf);
        flatten_json_values(&json, &mut buf);
        // Le buffer accumule sans clear (c'est a l'appelant de clear)
        assert_eq!(buf, "hello hello");
    }
}
