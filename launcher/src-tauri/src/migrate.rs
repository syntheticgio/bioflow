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

/// Recursively copies `source` into `dest`, creating `dest` if needed.
/// Symlinks are recreated as symlinks (not followed/dereferenced) --
/// matching `cp -a` semantics per the design spec, since a followed
/// symlink could silently balloon the copy size or duplicate data that
/// was deliberately shared on disk.
///
/// `on_progress` is called after each file (not directory, not symlink) is
/// copied, with the cumulative byte count copied so far -- this is what
/// drives the launcher UI's progress bar and the CLI script's terminal
/// output.
pub fn copy_tree(
    source: &Path,
    dest: &Path,
    mut on_progress: impl FnMut(u64),
) -> std::io::Result<()> {
    let mut bytes_so_far = 0u64;
    copy_dir_recursive(source, dest, &mut bytes_so_far, &mut on_progress)
}

fn copy_dir_recursive(
    source: &Path,
    dest: &Path,
    bytes_so_far: &mut u64,
    on_progress: &mut impl FnMut(u64),
) -> std::io::Result<()> {
    std::fs::create_dir_all(dest)?;

    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let src_path = entry.path();
        let dest_path = dest.join(entry.file_name());

        if file_type.is_dir() {
            copy_dir_recursive(&src_path, &dest_path, bytes_so_far, on_progress)?;
        } else if file_type.is_symlink() {
            copy_symlink(&src_path, &dest_path)?;
        } else {
            let bytes_copied = std::fs::copy(&src_path, &dest_path)?;
            *bytes_so_far += bytes_copied;
            on_progress(*bytes_so_far);
        }
    }

    Ok(())
}

#[cfg(unix)]
fn copy_symlink(src: &Path, dest: &Path) -> std::io::Result<()> {
    let target = std::fs::read_link(src)?;
    std::os::unix::fs::symlink(target, dest)
}

#[cfg(not(unix))]
fn copy_symlink(src: &Path, dest: &Path) -> std::io::Result<()> {
    // The launcher targets macOS and Linux only (see
    // launcher/src-tauri/tauri.macos.conf.json / tauri.linux.conf.json) --
    // this arm exists only so the crate compiles on any other host during
    // development; it is not a supported migration path.
    std::fs::copy(src, dest).map(|_| ())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ValidationResult {
    Ok,
    /// Count/size validation failed: what the pre-copy scan expected vs.
    /// what actually landed at the destination.
    Mismatch {
        expected_files: u64,
        actual_files: u64,
        expected_bytes: u64,
        actual_bytes: u64,
    },
    /// Hash validation failed: this specific file's contents differ
    /// between source and destination. Relative to the tree root, not an
    /// absolute path, so the message stays meaningful regardless of where
    /// source/dest happen to live on this machine.
    HashMismatch { file: PathBuf },
}

/// The fast default validation: does the destination have the same file
/// count and total byte size the pre-copy scan of the source found. This
/// is what actually catches the realistic failure mode (copy interrupted,
/// disk filled mid-copy) without re-reading every byte of potentially very
/// large files.
pub fn validate_count_and_size(
    source_scan: &SourceScan,
    dest: &Path,
) -> std::io::Result<ValidationResult> {
    let dest_scan = scan_source(dest)?;
    if dest_scan.file_count == source_scan.file_count
        && dest_scan.total_bytes == source_scan.total_bytes
    {
        Ok(ValidationResult::Ok)
    } else {
        Ok(ValidationResult::Mismatch {
            expected_files: source_scan.file_count,
            actual_files: dest_scan.file_count,
            expected_bytes: source_scan.total_bytes,
            actual_bytes: dest_scan.total_bytes,
        })
    }
}

/// The slow, opt-in validation: hash every file on both sides and compare.
/// Stops at the first mismatch rather than collecting all of them -- one
/// mismatch is already enough to fail the migration and report a concrete,
/// actionable file.
pub fn validate_by_hash(source: &Path, dest: &Path) -> std::io::Result<ValidationResult> {
    validate_hash_dir(source, dest, Path::new(""))
}

fn validate_hash_dir(
    source_root: &Path,
    dest_root: &Path,
    relative: &Path,
) -> std::io::Result<ValidationResult> {
    let source_dir = source_root.join(relative);
    for entry in std::fs::read_dir(&source_dir)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let entry_relative = relative.join(entry.file_name());

        if file_type.is_dir() {
            let result = validate_hash_dir(source_root, dest_root, &entry_relative)?;
            if result != ValidationResult::Ok {
                return Ok(result);
            }
        } else if file_type.is_file() {
            let source_hash = hash_file(&source_root.join(&entry_relative))?;
            let dest_hash = hash_file(&dest_root.join(&entry_relative))?;
            if source_hash != dest_hash {
                return Ok(ValidationResult::HashMismatch { file: entry_relative });
            }
        }
        // Symlinks are not hashed -- validate_count_and_size already
        // covers their presence via file_count, and hashing a link target
        // that may point outside the tree is out of scope here.
    }
    Ok(ValidationResult::Ok)
}

/// Deletes the entire original storage directory -- not just its contents
/// -- per the design spec's "just a single directory... gone" framing.
/// Callers must only invoke this after validation (`validate_count_and_size`
/// or `validate_by_hash`) has already returned `ValidationResult::Ok` for
/// the new location; this function itself does not re-check that, since it
/// is a pure "delete this path" primitive.
pub fn remove_original(original: &Path) -> std::io::Result<()> {
    std::fs::remove_dir_all(original)
}

fn hash_file(path: &Path) -> std::io::Result<[u8; 32]> {
    use sha2::{Digest, Sha256};
    use std::io::Read;

    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let bytes_read = file.read(&mut buffer)?;
        if bytes_read == 0 {
            break;
        }
        hasher.update(&buffer[..bytes_read]);
    }
    Ok(hasher.finalize().into())
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

    #[test]
    fn copies_a_flat_directory_and_reports_progress_per_file() {
        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        let dest = tmp.path().join("dest");
        std::fs::create_dir_all(&source).unwrap();
        std::fs::write(source.join("a.txt"), b"12345").unwrap();
        std::fs::write(source.join("b.txt"), b"1234567890").unwrap();

        let mut progress_calls: Vec<u64> = Vec::new();
        copy_tree(&source, &dest, |bytes_so_far| progress_calls.push(bytes_so_far)).unwrap();

        assert_eq!(std::fs::read(dest.join("a.txt")).unwrap(), b"12345");
        assert_eq!(std::fs::read(dest.join("b.txt")).unwrap(), b"1234567890");
        // One callback per file copied, cumulative, ending at the total.
        assert_eq!(progress_calls.last(), Some(&15u64));
    }

    #[test]
    fn copies_nested_directories_preserving_structure() {
        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        let dest = tmp.path().join("dest");
        std::fs::create_dir_all(source.join("objects/ab")).unwrap();
        std::fs::write(source.join("objects/ab/blob1"), b"hello").unwrap();

        copy_tree(&source, &dest, |_| {}).unwrap();

        assert_eq!(std::fs::read(dest.join("objects/ab/blob1")).unwrap(), b"hello");
    }

    #[test]
    #[cfg(unix)]
    fn preserves_symlinks_rather_than_following_them() {
        use std::os::unix::fs::symlink;

        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        std::fs::create_dir_all(&source).unwrap();
        std::fs::write(source.join("real.txt"), b"target").unwrap();
        symlink(source.join("real.txt"), source.join("link.txt")).unwrap();
        let dest = tmp.path().join("dest");

        copy_tree(&source, &dest, |_| {}).unwrap();

        let dest_link = dest.join("link.txt");
        assert!(dest_link.symlink_metadata().unwrap().file_type().is_symlink());
    }

    #[test]
    fn count_and_size_validation_passes_for_a_correct_copy() {
        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        let dest = tmp.path().join("dest");
        std::fs::create_dir_all(&source).unwrap();
        std::fs::write(source.join("a.txt"), b"12345").unwrap();

        copy_tree(&source, &dest, |_| {}).unwrap();
        let source_scan = scan_source(&source).unwrap();

        assert_eq!(
            validate_count_and_size(&source_scan, &dest).unwrap(),
            ValidationResult::Ok
        );
    }

    #[test]
    fn count_and_size_validation_fails_when_a_file_is_missing_from_the_copy() {
        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        let dest = tmp.path().join("dest");
        std::fs::create_dir_all(&source).unwrap();
        std::fs::write(source.join("a.txt"), b"12345").unwrap();
        std::fs::write(source.join("b.txt"), b"1234567890").unwrap();

        // Simulate an interrupted copy: only one of two files landed.
        std::fs::create_dir_all(&dest).unwrap();
        std::fs::write(dest.join("a.txt"), b"12345").unwrap();
        let source_scan = scan_source(&source).unwrap();

        let result = validate_count_and_size(&source_scan, &dest).unwrap();
        assert_eq!(
            result,
            ValidationResult::Mismatch {
                expected_files: 2,
                actual_files: 1,
                expected_bytes: 15,
                actual_bytes: 5,
            }
        );
    }

    #[test]
    fn hash_validation_passes_for_a_correct_copy() {
        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        let dest = tmp.path().join("dest");
        std::fs::create_dir_all(&source).unwrap();
        std::fs::write(source.join("a.txt"), b"12345").unwrap();
        std::fs::create_dir_all(source.join("nested")).unwrap();
        std::fs::write(source.join("nested/b.txt"), b"1234567890").unwrap();

        copy_tree(&source, &dest, |_| {}).unwrap();

        assert_eq!(validate_by_hash(&source, &dest).unwrap(), ValidationResult::Ok);
    }

    #[test]
    fn hash_validation_fails_when_a_copied_file_has_different_contents() {
        let tmp = tempfile::tempdir().unwrap();
        let source = tmp.path().join("source");
        let dest = tmp.path().join("dest");
        std::fs::create_dir_all(&source).unwrap();
        std::fs::write(source.join("a.txt"), b"original").unwrap();

        copy_tree(&source, &dest, |_| {}).unwrap();
        // Corrupt the copy after the fact.
        std::fs::write(dest.join("a.txt"), b"corrupted").unwrap();

        let result = validate_by_hash(&source, &dest).unwrap();
        assert_eq!(result, ValidationResult::HashMismatch { file: PathBuf::from("a.txt") });
    }

    #[test]
    fn remove_original_deletes_the_whole_directory() {
        let tmp = tempfile::tempdir().unwrap();
        let original = tmp.path().join("old-storage");
        std::fs::create_dir_all(original.join("objects")).unwrap();
        std::fs::write(original.join("objects/blob1"), b"data").unwrap();

        remove_original(&original).unwrap();

        assert!(!original.exists(), "the whole directory should be gone, not just its contents");
    }
}
