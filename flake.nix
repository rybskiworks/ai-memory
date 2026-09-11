{
  description = "Long-term memory for AI coding agents";

  inputs = {
    tooling.url = "github:rybskiworks/nix-tooling/34c287290245c20e9103f7cd0fcdaa244edd310b";
    nixpkgs.follows = "tooling/nixpkgs";
    flake-parts.follows = "tooling/flake-parts";
    fenix.follows = "tooling/fenix";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      perSystem =
        { pkgs, system, ... }:
        let
          # Nix builds and development share the family compiler. The upstream
          # rust-toolchain.toml and Cargo MSRV remain the non-Nix contract.
          rust = inputs.fenix.packages.${system}.stable;
          ai-memory = pkgs.callPackage ./nix/package.nix { inherit rust; };
        in
        {
          packages = {
            inherit ai-memory;
            default = ai-memory;
          };

          checks = {
            # The package includes the provider-free core library test suite.
            package = ai-memory;
            native-service =
              pkgs.runCommand "ai-memory-native-service-check"
                {
                  nativeBuildInputs = [
                    pkgs.python3
                    pkgs.gitMinimal
                  ];
                }
                ''
                  python ${./nix/checks/native-service.py} ${ai-memory} ${ai-memory.version} ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
                  touch "$out"
                '';
            rustfmt =
              pkgs.runCommand "ai-memory-rustfmt-check"
                {
                  nativeBuildInputs = [
                    rust.cargo
                    rust.rustfmt
                  ];
                }
                ''
                  export HOME="$TMPDIR/home"
                  mkdir -p "$HOME"
                  cd ${ai-memory.src}
                  cargo fmt --all -- --check
                  touch "$out"
                '';
          };

          formatter = pkgs.nixfmt-tree;

          devShells.default = pkgs.mkShell {
            inputsFrom = [ ai-memory ];
            packages = [
              rust.cargo
              rust.rustc
              rust.clippy
              rust.rustfmt
              pkgs.cargo-nextest
              pkgs.cargo-watch
              pkgs.gitMinimal
            ];
            # The build script downloads only when this is exactly "1".
            TAILWIND_BUILD = "0";
          };
        };
    };
}
