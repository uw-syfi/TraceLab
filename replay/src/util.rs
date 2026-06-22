use std::time::{SystemTime, UNIX_EPOCH};

/// Planned prefix-cache hit rate: prior-context tokens over total prompt tokens.
pub(crate) fn prefix_hit_rate(prefix_tokens: usize, prompt_tokens: usize) -> f64 {
    ratio(prefix_tokens, prompt_tokens).unwrap_or(0.0)
}

/// Safe ratio that returns `None` instead of dividing by zero.
pub(crate) fn ratio(numerator: usize, denominator: usize) -> Option<f64> {
    if denominator == 0 {
        None
    } else {
        Some(numerator as f64 / denominator as f64)
    }
}

pub(crate) fn unix_seconds_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub(crate) fn elapsed_ms(start: SystemTime) -> f64 {
    let ms = SystemTime::now()
        .duration_since(start)
        .unwrap_or_default()
        .as_secs_f64()
        * 1000.0;
    (ms * 100.0).round() / 100.0
}
