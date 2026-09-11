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

The earlier generated `session_before_refine` subscription was absent from this
Prime API and correctly made this check fail. Source
`c8f2702b3dd013cb097c6881a8e44e0c6f457957` removed that subscription; its native
package passed the actual loader and the two-session capture/recall check below
against the pinned Prime tree. Those results do not certify a later native
binary merely because it reports the same version. Unsupported subscriptions
must still fail; do not add them to a fake event list or turn a failure into an
expected-success assertion. This harness never modifies the generated adapter.
Delivery of the supported `refine_complete` event remains a separate capture
contract, not coverage established by the prompt/recall case.

The wrapper rejects generated-extension `fetch` requests outside the exact
native fixture origin and known MCP/hook/handoff paths while forwarding real
responses. This is a test guard for the current adapter transport, not a claim
of operating-system-wide network confinement.

## Driver regression checks

```sh
python3 -m unittest discover -s tests/prime-agent-compat -p 'test_*.py' -v
/nix/store/NODE_24_15_0/bin/node tests/prime-agent-compat/loader.test.mjs
/nix/store/NODE_24_15_0/bin/node tests/prime-agent-compat/session-support.test.mjs
```

These tests cover isolation, refusal of missing/inexact inputs, owned-child
shutdown, redaction and result classification. Their small synthetic objects
are **not** Prime compatibility evidence. The runtime drivers below use the
real loader and native service; the small oracle tests do not launch either.

## Two-session capture and FTS recall

`session.py` accepts the same explicit input arguments as `run.py` and defaults
to a 60-second session-child timeout (allowed range 20–60). It additionally
requires the pinned tree's retained test harness, utilities and session source,
workspace dist packages, and tsx 4.23.1. It checks their required source files
against the raw pin before allocating any state. It does not install them.

Run it by replacing `run.py` with `session.py` in the command above. The driver
first runs the original loader contract unchanged, then the separate
same-operator/same-project session case. A passing capture case **does not hide
any loader compatibility failure**: if either contract fails, the combined
command exits 1, with separate loader and session stages in the report. Missing
inputs exit 2; infrastructure/cleanup failures exit 3.

The session case uses Prime's actual retained `createHarness`,
`loadExtensions`, `createTestResourceLoader` and in-process faux model provider.
The native CLI-emitted extension is copied unchanged for two independently
loaded sessions. Both have fresh private session directories, real distinct
UUIDs, unique faux provider registrations, and explicit `.ai-memory.toml`
markers for `prime-compat/shared-capture`. No ambient auth/settings/resources,
default shell/IPython tools, automatic refinement, compaction or telemetry are
enabled. An explicit scratch TypeScript configuration is passed to the pinned
tsx loader; no root tsconfig or floating npm/npx command is assumed.

Session A submits an actual prompt containing a harmless unique canary. A
bounded explicit-scope native observation read establishes that its exact
session/agent/cwd and prompt observation have committed; it does not create a
page or fake a hook. A remains open. Session B's faux model then emits a real
`memory_query` tool call through the generated bridge, with no scope arguments.
The assertion checks the actual model-visible tool result and its call id,
`hits=[]`, and a raw FTS hit matching A's precise committed observation id,
session id, kind and canary snippet. A canary in a prompt, a canned page, or
another session's result cannot satisfy that assertion. The bridge must forward
B's real session id and the unmodified query arguments. Raw hits do not carry
workspace/project fields; scope is correlated through the explicit native
diagnostic and the emitted marker/cwd evidence.

The fixture activates only the actual `memory_query` tool for its model turns.
This is test scope, **not a change or recommendation for the production default
tool surface**. Only in-process faux responses run; there is no model HTTP
server, paid provider call, Prime CLI/daemon, Python kernel or VM.

Finally it uses Prime's real shutdown-event helper, observes eventual native
session-end ingestion, disposes each actual session, and unregisters its faux
provider. Disposal and unregistering are attempted independently even when
shutdown or the storage diagnostic fails. The private session directories and
transcripts remain under the retained artifact root; upstream harness cleanup
is not called because its synchronous removal retries can exceed the deadline.
The native service remains supervised by the shared exact-child cleanup.
These healthy-service waits **do not prove a bounded/durable shutdown-drain
contract**; that remains explicitly `not_tested` in the report.

## Coverage limits

The loader-only `run.py` does not claim actual `AgentSession` execution, faux-provider
turns, prompt/tool capture, session/project identity, subsequent-session recall,
refinement, shutdown queue delivery, native Python MCP, CLI startup or fleet
deployment. The report explicitly marks session capture and CLI execution as
`not_run`, even when the loader contract passes.

The two-session case covers only the one operator and explicit shared project.
It does not prove interleaved project/actor isolation, real refinement,
stateful MCP/SSE, native Python MCP ownership, outage/spool/replay/overflow,
shutdown durability, CLI startup, or fleet deployment. Those checks must remain
separate, with no second adapter implementation in downstream fixtures.

This directory is test-only and outside the native package source fileset. Its
addition does not change the already built native service or the upstream Rust
compatibility contract. Full repository contributor gates remain separate.
