pub(crate) fn sse_data(line: &str) -> Option<&str> {
    let line = line.trim();
    if !line.starts_with("data:") {
        return None;
    }
    Some(line.trim_start_matches("data:").trim())
}
