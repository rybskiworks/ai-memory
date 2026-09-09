# Prime extension compatibility

This focused check runs the installed `ai-memory install-hooks` command in a
fresh directory and loads its emitted TypeScript through **Prime's actual
`loadExtensions` implementation**. The adapter and its HTTP responses are not
replaced with mocks. A disposable native ai-memory service supplies MCP, with
an ephemeral bearer and embeddings, LLM providers, watcher and maintenance
disabled. The test neither changes an existing configuration nor starts Prime's
CLI, daemon, Python kernel or a VM.

The reviewed Prime source is
[`d1b072686d6b7b1b7d2ad773541e33aba1f578d9`](https://github.com/PrimeIntellect-ai/prime-agent/tree/d1b072686d6b7b1b7d2ad773541e33aba1f578d9),
version 0.7.1, NAR hash
`sha256-MwAnWnAusPCiFburJdiDWOIBPAAqeHCb0Ob5+vlUKJY=`. The executable is Node
24.15.0. The driver requires explicit absolute paths; it verifies the raw source
hash and compares the built tree's package manifest and extension API/loader
source with that source. Supply the immutable built tree from a separately
reviewed build retaining `packages/`, workspace `dist/` outputs, `node_modules/`
and TypeScript. No `npm`, `npx`, package installation or Nix build fallback runs.
The copied source checks are not an independent compiler/reproducibility audit.

## Run

From the repository root, with the separately built artifacts available:

```sh
python3 tests/prime-agent-compat/run.py \
  --ai-memory /nix/store/AI_MEMORY_PACKAGE/bin/ai-memory \
  --ai-memory-revision FULL_FORK_GIT_REVISION \
  --prime-source /nix/store/PRIME_SOURCE \
  --prime-tree /nix/store/PRIME_BUILT_TREE \
  --node /nix/store/NODE_24_15_0/bin/node \
  --nix /nix/store/NIX_PACKAGE/bin/nix \
  --ca-bundle /nix/store/CA_PACKAGE/etc/ssl/certs/ca-bundle.crt
```

The uppercase values are explicit artifact placeholders, not downloadable
selectors. The native Git revision is caller-declared build provenance; the
report separately records the actual binary SHA256 and reported version rather
than pretending that `--version` proves its source revision.

Exit codes are 0 (focused contract passed), 1 (compatibility assertion failed),
2 (required artifact/pin missing or mismatched), and 3 (harness/service failure).
Missing built inputs block before allocating state or launching a child.
The driver creates a private temporary artifact directory and emits its path
and structured report. This directory is retained for diagnosis; it contains
only disposable state, generated extension and synthetic credentials. Reports
and captured installer/loader logs redact the ephemeral bearer. No production
credentials are inherited. The exact service child is stopped and reaped even
when the loader fails; each subprocess/readiness/discovery step has a deadline.

The loader derives supported subscriptions from the pinned `ExtensionAPI`
declarations using that build's TypeScript parser. It reports unsupported names,
actual loader errors, registered tool names and real MCP HTTP statuses. It waits
only a bounded time for the generated bridge's asynchronous discovery; this is
not proof of tool readiness before a first model prompt. The driver requires
`memory_query` and `memory_read_page` to appear but **does not choose a read-only
or full-tool policy**. Extra tools are reported, not silently removed.

The current generated `session_before_refine` subscription is absent from this
Prime API. It must make this check red; do not add it to a fake event list or
turn a failure into an expected-success assertion. The generated adapter is not
modified by this harness. Its supported `refine_complete` event is a separate
capture contract to exercise next.

The wrapper rejects generated-extension `fetch` requests outside the exact
native fixture origin and known MCP/hook/handoff paths while forwarding real
responses. This is a test guard for the current adapter transport, not a claim
of operating-system-wide network confinement.

## Driver regression checks

```sh
python3 -m unittest discover -s tests/prime-agent-compat -p 'test_*.py' -v
/nix/store/NODE_24_15_0/bin/node tests/prime-agent-compat/loader.test.mjs
```

These tests cover isolation, refusal of missing/inexact inputs, owned-child
shutdown, redaction and result classification. Their small synthetic objects
are **not** Prime compatibility evidence. Only `run.py` imports the real loader
and starts the real native service.

## Deliberately not covered yet

This first check does not claim actual `AgentSession` execution, faux-provider
turns, prompt/tool capture, session/project identity, subsequent-session recall,
refinement, shutdown queue delivery, native Python MCP, CLI startup or fleet
deployment. The report explicitly marks session capture and CLI execution as
`not_run`, even when the loader contract passes.

The next source-owned test should use the pinned
`packages/coding-agent/test/suite/harness.ts` (`AgentSession` and
`registerFauxProvider`) with the installer-emitted extension, fresh marker scope
and the same native service. It must assert server-ingested actor/session/project
and retrieve an actual captured canary, without pre-writing that canary as a
page. Any source test imports need an explicit scratch TypeScript configuration:
the reviewed built tree need not include root `tsconfig*.json`. Keep actual
client/image deployment checks downstream, and do not maintain a second adapter
implementation in those tests.

This directory is test-only and outside the native package source fileset. Its
addition does not change the already built native service or the upstream Rust
compatibility contract. Full repository contributor gates remain separate.
