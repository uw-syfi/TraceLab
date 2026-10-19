use anyhow::{anyhow, Result};

use super::Backend;

pub(crate) fn not_implemented() -> Result<Box<dyn Backend>> {
    Err(anyhow!(
        "--backend llamacpp is reserved for the next backend step but is not implemented yet"
    ))
}
