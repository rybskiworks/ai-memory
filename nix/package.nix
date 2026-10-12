{
  lib,
  makeRustPlatform,
  pkg-config,
  zlib,
  rust,
}:
let
  manifest = builtins.fromTOML (builtins.readFile ../Cargo.toml);
  rustPlatform = makeRustPlatform {
    inherit (rust) cargo rustc;
  };
in
rustPlatform.buildRustPackage {
  pname = "ai-memory";
  version = manifest.workspace.package.version;

  # Preserve workspace manifests, embedded prompts/CSS/logo, hooks and the
  # canonical routing snippet used by core tests. Nested checkouts and local
  # build/state directories are not part of this source contract.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../Cargo.toml
      ../Cargo.lock
      ../AGENTS.md
      ../rust-toolchain.toml
      ../crates
      ../evals
      ../hooks
      ../docs
    ];
  };
  cargoLock.lockFile = ../Cargo.lock;

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ zlib ];

  cargoBuildFlags = [
    "--locked"
    "--package"
    "ai-memory-cli"
    "--bin"
    "ai-memory"
  ];
  # This is deliberately a named native slice, not the complete workspace,
  # Docker-wrapper, companion, or provider integration acceptance gate.
  doCheck = true;
  cargoTestFlags = [
    "--locked"
    "--package"
    "ai-memory-core"
    "--lib"
  ];

  # Use the committed stylesheet; regeneration is a separate networked task.
  TAILWIND_BUILD = "0";

  preCheck = ''
    export HOME="$TMPDIR/check-home"
    export XDG_CONFIG_HOME="$TMPDIR/check-config"
    export XDG_CACHE_HOME="$TMPDIR/check-cache"
    export XDG_DATA_HOME="$TMPDIR/check-data"
    export XDG_STATE_HOME="$TMPDIR/check-state"
    mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
  '';

  postInstall = ''
    mkdir -p "$out/share/ai-memory" "$out/etc/ai-memory"
    cp -r hooks "$out/share/ai-memory/"
    cp crates/ai-memory-cli/templates/config.default.toml "$out/etc/ai-memory/config.default.toml"
  '';

  meta = {
    description = "Long-term memory for AI coding agents";
    homepage = "https://github.com/akitaonrails/ai-memory";
    license = lib.licenses.mit;
    mainProgram = "ai-memory";
  };
}
