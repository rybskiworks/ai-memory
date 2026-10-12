//! Complete, immutable Codex hook bundles shared by concurrent installers.

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};

const GENERATIONS: &str = ".generations";
const CURRENT: &str = ".current-generation";

#[derive(Debug, PartialEq, Eq)]
struct Asset {
    bytes: Vec<u8>,
    executable: bool,
}

type Bundle = BTreeMap<String, Asset>;

fn directory(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("inspecting hook directory {}", path.display()))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        bail!("hook directory is not a real directory: {}", path.display());
    }
    Ok(())
}

fn create_directory(path: &Path) -> Result<()> {
    fs::create_dir_all(path)?;
    directory(path)
}

fn script(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|s| s.to_str()),
        Some("sh" | "ps1")
    )
}

fn asset(bundle: &mut Bundle, from: &Path, relative: String, executable: bool) -> Result<()> {
    let bytes = fs::read(from).with_context(|| format!("reading hook asset {}", from.display()))?;
    bundle.insert(relative, Asset { bytes, executable });
    Ok(())
}

fn name(path: &Path) -> Result<&str> {
    path.file_name()
        .and_then(|value| value.to_str())
        .context("hook asset filename is not valid UTF-8")
}

fn snapshot(source: &Path) -> Result<Bundle> {
    let mut bundle = Bundle::new();
    let mut events = 0;
    for entry in fs::read_dir(source)? {
        let from = entry?.path();
        if from.is_file() && script(&from) {
            let filename = name(&from)?;
            events += usize::from(filename != "_lib.sh");
            asset(&mut bundle, &from, format!("codex/{filename}"), true)?;
        }
    }
    if events == 0 {
        bail!("no Codex event scripts found at {}", source.display());
    }
    if let Some(parent) = source.parent() {
        let shared = parent.join("_lib.sh");
        // A previously staged bundle has its effective helper beside the events.
        if !bundle.contains_key("codex/_lib.sh") && shared.is_file() {
            asset(&mut bundle, &shared, "codex/_lib.sh".into(), true)?;
        }
        let support = parent.join("lib");
        if support.is_dir() {
            for entry in fs::read_dir(support)? {
                let from = entry?.path();
                if from.is_file() && from.extension().and_then(|s| s.to_str()) == Some("ps1") {
                    asset(&mut bundle, &from, format!("lib/{}", name(&from)?), false)?;
                }
            }
        }
    }
    Ok(bundle)
}

fn fingerprint(bundle: &Bundle) -> String {
    let mut hash = Sha256::new();
    hash.update(b"ai-memory-codex-hook-bundle-v1\0");
    for (path, asset) in bundle {
        hash.update((path.len() as u64).to_le_bytes());
        hash.update(path.as_bytes());
        hash.update([u8::from(asset.executable)]);
        hash.update((asset.bytes.len() as u64).to_le_bytes());
        hash.update(&asset.bytes);
    }
    format!("{:x}", hash.finalize())
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn verify(generation: &Path, expected: Option<&Bundle>) -> Result<Bundle> {
    directory(generation)?;
    let mut actual = Bundle::new();
    for entry in fs::read_dir(generation)? {
        let dir = entry?.path();
        directory(&dir)?;
        let dirname = name(&dir)?;
        if !matches!(dirname, "codex" | "lib") {
            bail!("unexpected hook generation directory: {}", dir.display());
        }
        for entry in fs::read_dir(&dir)? {
            let file = entry?.path();
            let metadata = fs::symlink_metadata(&file)?;
            if !metadata.is_file() || metadata.file_type().is_symlink() || !script(&file) {
                bail!("unexpected hook generation asset: {}", file.display());
            }
            let executable = dirname == "codex";
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = if executable { 0o755 } else { 0o600 };
                if metadata.permissions().mode() & 0o777 != mode {
                    bail!("hook generation permissions differ: {}", file.display());
                }
            }
            asset(
                &mut actual,
                &file,
                format!("{dirname}/{}", name(&file)?),
                executable,
            )?;
        }
    }
    if !actual
        .keys()
        .any(|path| path.starts_with("codex/") && path != "codex/_lib.sh")
    {
        bail!("hook generation has no Codex event scripts");
    }
    if fingerprint(&actual) != name(generation)? || expected.is_some_and(|bundle| *bundle != actual)
    {
        bail!("hook generation content differs: {}", generation.display());
    }
    Ok(actual)
}

fn sync_directory(path: &Path) {
    // As with the canonical atomic-file helper, directory fsync is best effort.
    if let Ok(file) = File::open(path) {
        let _ = file.sync_all();
    }
}

fn prepare(temporary: &Path, bundle: &Bundle) -> Result<()> {
    for (relative, asset) in bundle {
        let target = temporary.join(relative);
        let parent = target.parent().context("hook asset has no parent")?;
        fs::create_dir_all(parent)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)?;
        file.write_all(&asset.bytes)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(if asset.executable {
                0o755
            } else {
                0o600
            }))?;
        }
        file.sync_all()?;
    }
    for entry in fs::read_dir(temporary)? {
        sync_directory(&entry?.path());
    }
    sync_directory(temporary);
    Ok(())
}

/// Stage a complete Codex bundle without modifying any published generation.
pub(super) fn stage(source: &Path, data_dir: &Path) -> Result<PathBuf> {
    let bundle = snapshot(source)?;
    let agent = data_dir.join("hooks/codex");
    create_directory(&agent)?;
    let generations = agent.join(GENERATIONS);
    create_directory(&generations)?;
    let digest = fingerprint(&bundle);
    let published = generations.join(&digest);
    match fs::symlink_metadata(&published) {
        Ok(_) => {
            verify(&published, Some(&bundle))?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let temporary = tempfile::Builder::new()
                .prefix(".pending-")
                .tempdir_in(&generations)?;
            prepare(temporary.path(), &bundle)?;
            // A complete generation is nonempty, so rename cannot replace one.
            if let Err(error) = fs::rename(temporary.path(), &published)
                && fs::symlink_metadata(&published).is_err()
            {
                return Err(error).context("publishing Codex hook generation");
            }
            verify(&published, Some(&bundle))?;
            sync_directory(&generations);
        }
        Err(error) => return Err(error).context("inspecting Codex hook generation"),
    }
    let pointer = agent.join(CURRENT);
    if pointer.is_symlink() {
        bail!(
            "refusing a symlinked Codex generation pointer: {}",
            pointer.display()
        );
    }
    let value = format!("{digest}\n");
    if fs::read(&pointer).ok().as_deref() != Some(value.as_bytes()) {
        ai_memory_wiki::write_atomic(&pointer, value.as_bytes())?;
    }
    Ok(published.join("codex"))
}

/// Resolve generation-only installs while continuing to accept flat bundles.
pub(super) fn source(directory_path: &Path, explicit: bool) -> Result<PathBuf> {
    if explicit {
        for entry in fs::read_dir(directory_path)? {
            let path = entry?.path();
            if path.is_file() && script(&path) && name(&path)? != "_lib.sh" {
                return Ok(directory_path.into());
            }
        }
    }
    let pointer = directory_path.join(CURRENT);
    match fs::symlink_metadata(&pointer) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(directory_path.into());
        }
        Err(error) => return Err(error).context("inspecting Codex generation pointer"),
        Ok(metadata) if !metadata.is_file() || metadata.file_type().is_symlink() => {
            bail!("Codex generation pointer is not a regular file");
        }
        Ok(_) => {}
    }
    let digest = fs::read_to_string(pointer)?;
    let digest = digest.trim();
    if !valid_digest(digest) {
        bail!("invalid Codex generation pointer");
    }
    let generations = directory_path.join(GENERATIONS);
    directory(&generations)?;
    let published = generations.join(digest);
    verify(&published, None)?;
    Ok(published.join("codex"))
}

/// Preserve the generation suffix when a container maps hooks to the host.
pub(super) fn command_suffix(staged: &Path) -> Option<PathBuf> {
    let generation = staged.parent()?;
    let generations = generation.parent()?;
    let agent = generations.parent()?;
    let digest = generation.file_name()?.to_str()?;
    (staged.file_name()? == "codex"
        && generations.file_name()? == GENERATIONS
        && agent.file_name()? == "codex"
        && valid_digest(digest))
    .then(|| {
        Path::new("codex")
            .join(GENERATIONS)
            .join(digest)
            .join("codex")
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};

    fn fixture(root: &Path, content: &[u8]) -> Result<PathBuf> {
        let source = root.join("codex");
        fs::create_dir_all(&source)?;
        fs::create_dir_all(root.join("lib"))?;
        for name in ["session-start.sh", "session-start.ps1"] {
            fs::write(source.join(name), content)?;
        }
        fs::write(root.join("_lib.sh"), b"# shared shell helper\n")?;
        fs::write(
            root.join("lib/ai-memory-hook.ps1"),
            b"# shared powershell helper\n",
        )?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for path in [
                source.join("session-start.sh"),
                source.join("session-start.ps1"),
                root.join("_lib.sh"),
                root.join("lib/ai-memory-hook.ps1"),
            ] {
                fs::set_permissions(path, fs::Permissions::from_mode(0o444))?;
            }
        }
        Ok(source)
    }

    #[test]
    fn concurrent_installers_publish_one_complete_immutable_bundle() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let bundle_source = fixture(&temporary.path().join("source"), b"# event\n")?;
        let original = snapshot(&bundle_source)?;
        let data = temporary.path().join("data");
        let barrier = Arc::new(Barrier::new(8));
        let mut workers = Vec::new();
        for _ in 0..8 {
            let bundle_source = bundle_source.clone();
            let data = data.clone();
            let barrier = barrier.clone();
            workers.push(std::thread::spawn(move || {
                barrier.wait();
                stage(&bundle_source, &data)
            }));
        }
        let mut paths = Vec::new();
        for worker in workers {
            paths.push(
                worker
                    .join()
                    .map_err(|_| anyhow::anyhow!("installer thread panicked"))??,
            );
        }
        let published = paths.first().context("no installation completed")?;
        assert!(paths.iter().all(|path| path == published));
        assert_eq!(snapshot(&bundle_source)?, original);
        assert_eq!(snapshot(published)?, original);
        assert_eq!(source(&data.join("hooks/codex"), false)?, *published);
        let before = fs::metadata(published.join("session-start.sh"))?.modified()?;
        assert_eq!(stage(&bundle_source, &data)?, *published);
        assert_eq!(
            fs::metadata(published.join("session-start.sh"))?.modified()?,
            before
        );
        assert_eq!(
            fs::read_dir(data.join("hooks/codex/.generations"))?.count(),
            1
        );
        Ok(())
    }

    #[test]
    fn upgrade_and_interrupted_preparation_preserve_old_executable_paths() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let first = fixture(&temporary.path().join("first"), b"# first\n")?;
        let next = fixture(&temporary.path().join("next"), b"# next\n")?;
        let data = temporary.path().join("data");
        let old = stage(&first, &data)?;
        let old_bundle = snapshot(&old)?;
        let generations = data.join("hooks/codex/.generations");
        let abandoned = generations.join(".pending-interrupted");
        fs::create_dir(&abandoned)?;
        fs::write(abandoned.join("partial"), b"unfinished")?;
        assert_eq!(source(&data.join("hooks/codex"), false)?, old);
        let new = stage(&next, &data)?;
        assert_ne!(new, old);
        assert_eq!(snapshot(&old)?, old_bundle);
        assert_eq!(fs::read(new.join("session-start.sh"))?, b"# next\n");
        assert_eq!(source(&data.join("hooks/codex"), false)?, new);
        assert!(abandoned.join("partial").is_file());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(old.join("session-start.sh"))?
                    .permissions()
                    .mode()
                    & 0o777,
                0o755
            );
        }
        let suffix = command_suffix(&new).context("missing host-mapped generation suffix")?;
        assert_eq!(
            Path::new("/host/hooks").join(suffix),
            Path::new("/host/hooks").join(new.strip_prefix(data.join("hooks"))?)
        );
        Ok(())
    }

    #[test]
    fn flat_sources_and_local_helpers_survive_generation_reinstallation() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let data = temporary.path();
        let legacy = fixture(&data.join("hooks"), b"# legacy event\n")?;
        fs::write(legacy.join("_lib.sh"), b"# effective local helper\n")?;
        assert_eq!(source(&legacy, false)?, legacy);
        let staged = stage(&source(&legacy, false)?, data)?;
        assert_eq!(
            fs::read(staged.join("_lib.sh"))?,
            b"# effective local helper\n"
        );
        assert_eq!(
            fs::read(staged.join("../lib/ai-memory-hook.ps1"))?,
            b"# shared powershell helper\n"
        );
        assert_eq!(stage(&source(&legacy, false)?, data)?, staged);
        assert_eq!(
            fs::read(legacy.join("session-start.sh"))?,
            b"# legacy event\n"
        );
        assert!(command_suffix(&legacy).is_none());
        let refreshed = fixture(&temporary.path().join("refreshed"), b"# refreshed event\n")?;
        let newest = stage(&refreshed, data)?;
        assert_eq!(source(&legacy, false)?, newest);
        assert_eq!(source(&legacy, true)?, legacy);
        assert_eq!(
            fs::read(source(&legacy, true)?.join("session-start.sh"))?,
            b"# legacy event\n"
        );
        Ok(())
    }

    #[test]
    fn incomplete_published_generations_are_rejected_without_repair() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let bundle_source = fixture(&temporary.path().join("source"), b"# event\n")?;
        let data = temporary.path().join("data");
        let digest = fingerprint(&snapshot(&bundle_source)?);
        let incomplete = data.join("hooks/codex/.generations").join(digest);
        fs::create_dir_all(&incomplete)?;
        fs::write(incomplete.join("sentinel"), b"keep")?;
        assert!(stage(&bundle_source, &data).is_err());
        assert_eq!(fs::read(incomplete.join("sentinel"))?, b"keep");
        assert!(!data.join("hooks/codex/.current-generation").exists());
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn published_symlinks_and_changed_permissions_are_rejected() -> Result<()> {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let temporary = tempfile::tempdir()?;
        let bundle_source = fixture(&temporary.path().join("source"), b"# event\n")?;
        let data = temporary.path().join("data");
        let staged = stage(&bundle_source, &data)?;
        let script = staged.join("session-start.sh");
        fs::set_permissions(&script, fs::Permissions::from_mode(0o600))?;
        assert!(stage(&bundle_source, &data).is_err());
        assert_eq!(fs::metadata(&script)?.permissions().mode() & 0o777, 0o600);
        fs::remove_file(&script)?;
        symlink(bundle_source.join("session-start.sh"), &script)?;
        assert!(stage(&bundle_source, &data).is_err());
        assert!(script.is_symlink());
        Ok(())
    }
}
