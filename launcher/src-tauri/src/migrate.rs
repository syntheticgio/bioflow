//! Storage-location migration: walk, space-check, copy, validate, cleanup.
//! See docs/superpowers/specs/2026-08-07-bioinfo-home-storage-migration-design.md.

use std::path::{Path, PathBuf};

/// The result of walking the source directory once: how many files exist
/// and their total size. Drives both the progress bar's denominator and
/// the free-space check -- one walk serves both, per the design spec.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SourceScan {
    pub file_count: u64,
    pub total_bytes: u64,
}

/// Walks `source` recursively, counting files and summing their sizes.
/// Symlinks are counted by their own metadata (not followed), matching the
/// copy step's `cp -a`-style semantics -- a symlink's "size" here is the
/// size of the link itself, consistent with how it will be copied.
pub fn scan_source(source: &Path) -> std::io::Result<SourceScan> {
    let mut scan = SourceScan::default();
    scan_dir(source, &mut scan)?;
    Ok(scan)
}

fn scan_dir(dir: &Path, scan: &mut SourceScan) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            scan_dir(&entry.path(), scan)?;
        } else {
            let metadata = entry.metadata()?;
            scan.file_count += 1;
            scan.total_bytes += metadata.len();
        }
    }
    Ok(())
}

/// Flat margin required at the destination beyond the source's own size,
/// per the design spec: not a percentage of source size, but a fixed
/// buffer so the destination volume is never left with the OS, Docker, and
/// BioFlow itself running on single-digit gigabytes of free space after a
/// migration, independent of how large or small the copied data is.
pub const MIGRATION_SPACE_MARGIN_BYTES: u64 = 100 * 1024 * 1024 * 1024; // 100 GB

/// Whether `available_bytes` at the destination covers the source's total
/// size plus the fixed margin above.
pub fn has_sufficient_space(scan: &SourceScan, available_bytes: u64) -> bool {
    available_bytes >= scan.total_bytes.saturating_add(MIGRATION_SPACE_MARGIN_BYTES)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_empty_directory_scans_to_zero() {
        let tmp = tempfile::tempdir().unwrap();
        let scan = scan_source(tmp.path()).unwrap();
        assert_eq!(scan, SourceScan { file_count: 0, total_bytes: 0 });
    }

    #[test]
    fn counts_and_sums_files_at_the_top_level() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("a.txt"), b"12345").unwrap();
        std::fs::write(tmp.path().join("b.txt"), b"1234567890").unwrap();

        let scan = scan_source(tmp.path()).unwrap();
        assert_eq!(scan, SourceScan { file_count: 2, total_bytes: 15 });
    }

    #[test]
    fn counts_and_sums_files_in_nested_directories() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(tmp.path().join("objects/ab")).unwrap();
        std::fs::write(tmp.path().join("objects/ab/blob1"), b"1234567890").unwrap();
        std::fs::write(tmp.path().join("top.txt"), b"123").unwrap();

        let scan = scan_source(tmp.path()).unwrap();
        assert_eq!(scan, SourceScan { file_count: 2, total_bytes: 13 });
    }

    #[test]
    fn refuses_when_free_space_is_less_than_source_plus_margin() {
        // available_bytes is smaller than total_source_bytes + MIGRATION_SPACE_MARGIN_BYTES
        let scan = SourceScan { file_count: 1, total_bytes: 1_000 };
        let available = scan.total_bytes + MIGRATION_SPACE_MARGIN_BYTES - 1;
        assert!(!has_sufficient_space(&scan, available));
    }

    #[test]
    fn allows_when_free_space_exactly_covers_source_plus_margin() {
        let scan = SourceScan { file_count: 1, total_bytes: 1_000 };
        let available = scan.total_bytes + MIGRATION_SPACE_MARGIN_BYTES;
        assert!(has_sufficient_space(&scan, available));
    }
}
