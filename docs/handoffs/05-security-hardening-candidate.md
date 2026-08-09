# Security hardening candidate receipt

## Status

**PR #15 IS OPEN; HOSTED CI PASSED AT `03193f5`. SIX REVIEW-CORRECTION
REGRESSIONS NOW PASS LOCALLY BUT REMAIN UNCOMMITTED AND UNPUSHED. THE CANDIDATE
IS NOT MERGE-APPROVED AND HAS NO RELEASE CLAIM.**

This is the isolated hardening branch `codex/supermem-security-hardening`, based
on BM-0 commit `a101015eb4cb3de558dd635a5eaecca09d292fac`. PR #15 contains the
BM-0 parent, the hardening commit `90c19df`, and the standard-checkout CI fix
`03193f5`. No merge, deployment, publication, or primary-checkout change has
been performed.

The initial change-aware scan sealed five validated regressions in this dirty
candidate: pre-session primary-HTTP auth, primary HTTP loopback binding,
`io.FileIO` private I/O, ZIP pathname/handle rebinding, and forged classic-EOCD
member counts. This receipt records the local remediation below, but it is not
candidate-approved. The latest phase was explicitly limited to implementation
and local verification, so it remains unapproved and carries no release claim.
During the first remediation, root also reproduced a private read through the
public `_frozen_importlib_external.SourceFileLoader` alias; the final allowed
cycle below denies that alias and the related frozen/zip loader aliases before
they can reach the guarded loader implementation.

Fresh scan `76887b26-db42-4010-b15d-373da6c7a6e6` and a fresh-context
independent reviewer then found a high-confidence release veto: the documented
primary server entrypoint called synchronous FastMCP `run()` from inside
`asyncio.run()`, so the locked dependency raised `RuntimeError: Already running
asyncio in this thread` before either stdio or HTTP served requests. The third
slice replaces that entrypoint with FastMCP's async lifespan and `run_async()`,
adds real temporary-root subprocess controls for stdio and loopback HTTP, and
bridges SIGTERM into cleanup. Those controls are local integration proof only;
they are not installed, staging, production, or formal security-review proof.

The product decision for original finding 2 is now frozen: raw Agent vault
content/metadata inspection and Tier 4 Agent memory navigation are unavailable
until a source-to-observation lifecycle broker exists. All callers are capped
at Tier 3. Direct `Agent.chat` fails closed without a model reply, tool call,
executor run, or `agent_reply` persistence.

## Frozen security invariants

1. Stdio trust requires the exact real `fastmcp.Context` owned by the primary
   server inside an active request context, or the private internal sentinel.
   Missing, malformed, compatibility-only, and spoofed contexts default deny
   and cap retrieval at Tier 3.
2. Primary MCP HTTP rejects a missing or incorrect configured Bearer key before
   protocol handling and uses a stateless FastMCP profile: initialize requests
   issue no MCP session ID and retain no transport session. Protected Worker
   HTTP also fails closed. HTTP binds only to loopback, is capped at Tier 3,
   and cannot invoke `Agent.chat` or the model-produced restricted executor.
3. Legacy HTTP and SSE compatibility adapters expose no usable protocol/tools,
   execute no memory or Agent path, do not parse request bodies, and bind to
   loopback when invoked directly.
4. Ordinary restricted-executor reads through `open`, `Path.read_text`,
   `Path.read_bytes`, `_io.open`, `io.FileIO`, and `io.open_code` do not return
   private blocks. Raw byte backends are denied; `update_file` rejects a
   private-bearing file without mutation; rename/replace/link operations cannot
   mutate a private source or destination; direct `posix`/`nt` imports are
   denied; ordinary public writes remain.
5. Notion accepts valid ZIP exports larger than 65,557 bytes while validating
   EOCD and a bounded central directory from one opened source before `ZipFile`
   construction and while enforcing bounded extraction.
6. Nuclino has the same valid-large-ZIP and bounded-extraction boundary.
7. BM-0 remains local SQLite/FTS only. No OAuth, hostile-code sandbox,
   installed product, staging, remote-production, or production capability is
   claimed.
8. Every supported memory surface stops at Tier 3. Tier 4/raw-vault Agent
   navigation, file content inspection, and metadata probes remain unavailable
   until a source-aware lifecycle broker enforces active/retracted/deleted
   source policy.
9. File-backed BM-0 source URIs use canonical UTF-8 percent-encoded path
   segments; content-identical metadata changes create immutable revisions;
   lifecycle timestamps are finite; and the Worker dashboard supplies its
   bearer only from in-memory user input while escaping imported summaries.

## Seven-finding acceptance matrix

| Finding | Candidate control | Local evidence | Status |
|---|---|---|---|
| 1. Primary MCP compatibility auth bypass | Exact active primary `Context` classifier; stateless HTTP outer Bearer middleware rejects before protocol handling | Static/local ASGI regressions plus process HTTP missing-Bearer 401 and repeated initialize controls | Local verification complete; no new formal review |
| 2. Lifecycle-blind raw Agent vault reads | Freeze Tier 4/raw Agent inspection unavailable; raw content/metadata routes deny and direct `Agent.chat` fails closed | Unit regression covers retracted/deleted BM-0 canaries, no model/tool/executor route, no `agent_reply` write; Worker list excludes a retracted control | Local verification complete; no new formal review |
| 3. Standalone HTTP adapter reaches Agent | Disabled metadata-only legacy HTTP shim with no Agent/tool path or body parsing | Local ASGI malformed-body regression | Local verification complete; no new formal review |
| 4. Standalone SSE adapter reaches Agent | Disabled metadata-only legacy SSE shim with no Agent/tool path or body parsing | Local ASGI malformed-body regression | Local verification complete; no new formal review |
| 5. Remote legacy adapters reach executor | All supported retrieval caps at Tier 3; no MCP/Worker fallback constructs Agent or runs executor; direct platform raw-I/O imports denied | Unit/local ASGI regressions plus primary stdio/HTTP process lifecycle controls | Local verification complete; no new formal review |
| 6. Notion archive extraction | One-handle EOCD/central-directory preparse; count/ratio/path/type/stream limits | Valid >65,557-byte and forged-metadata-before-`ZipFile` regressions | Local verification complete; no new formal review |
| 7. Nuclino archive extraction | Same shared bounded ZIP boundary | Same parameterized regressions | Local verification complete; no new formal review |

## Initial-scan remediation (unapproved)

- `mcp_server/server.py` now supplies an outer `PrimaryHTTPAuthMiddleware` to
  the exact FastMCP HTTP `run_async` path. It returns 401 for missing/wrong
  Bearer credentials before protocol handling, and the stateless profile never
  retains MCP transport sessions; the existing tool
  guard remains defense in depth. The same module rejects wildcard, remote,
  and malformed `MCP_HOST` values before any socket bind, while permitting only
  loopback IP literals and `localhost`.
- `agent/engine.py` now mediates direct `_io.open` through the ordinary
  redaction and write-preservation policy, and denies direct `io.FileIO`,
  `_io.FileIO`, `io.open_code`, and `_io.open_code`. CPython import loading is
  allowed only from an exact frozen `FileLoader.get_data` code object—not a
  spoofable filename—and direct `importlib`/`agent` loader access is denied.
  This is an ordinary local-executor I/O boundary, not a hostile-code sandbox
  claim.
- The final alias correction denies direct `importlib`, `agent`,
  `_frozen_importlib_external`, `_frozen_importlib`, and `zipimport` imports
  from executor code. The loader allow path remains the exact frozen code
  object identity, never a user-provided module name, filename, or ContextVar.
- `memory_connectors/archive.py` opens once, validates EOCD and a bounded
  central directory from that descriptor, then constructs an owning `ZipFile`
  from the same descriptor. It rejects a forged 10,001-entry central directory
  whose classic EOCD counts claim one member before the parser is constructed.

- `memory_connectors/archive.py` locates EOCD metadata from the ZIP tail,
  accepts ordinary exports larger than the maximum comment window, rejects
  malformed trailing data and oversized central-directory/member metadata before
  constructing `ZipFile`, and streams bounded extraction.
- `mcp_server/server.py` only recognizes an exact primary FastMCP context with
  an active request. Compatibility objects, `RuntimeError` fallbacks, mocks,
  and class spoofs do not inherit stdio authority.
- Private content is filtered at ordinary Agent/executor read boundaries;
  `update_file` preserves private-bearing source bytes by rejecting the update.
  The executor-level overwrite preflight remains a conservative extra guard;
  the acceptance contract does not depend on closure-introspection resistance.
- Legacy standalone HTTP/SSE modules are metadata-only disabled shims. The
  primary server supports only trusted local stdio and authenticated loopback
  HTTP.
- Root/package metadata, contributor guidance, README tool parameters and HTTP
  auth wording, `.env.example`, and the changelog now match the runtime
  boundary. Direct `sse-starlette` extras were removed; it remains in `uv.lock`
  as an MCP transitive dependency and is not claimed absent.

## Pre-veto local evidence (superseded)

The following was recorded before the sealed five-finding veto and must not be
read as evidence for the post-veto remediation below. All commands used the
isolated environment unless stated otherwise:

```text
UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
uv run --offline pytest tests/unit/test_mcp_server.py \
  tests/unit/test_security_hardening.py tests/test_engine.py tests/test_tools.py -q
Result: 160 passed, 4 existing deprecation warnings.

/private/tmp/supermem-security-e554-20260808/bin/pytest tests/ -q \
  --cov=supermem --cov=agent \
  --cov-report=term --cov-fail-under=60
Result: 317 passed, 2 skipped, 4 warnings; total coverage 68.68%.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
uv run --offline ruff check agent/engine.py agent/tools.py \
  mcp_server/http_server.py mcp_server/mcp_http_server.py \
  mcp_server/mcp_sse_server.py mcp_server/server.py \
  memory_connectors/archive.py memory_connectors/notion/parser.py \
  memory_connectors/nuclino/parser.py supermem/privacy/filter.py \
  tests/test_engine.py tests/test_tools.py tests/unit/test_mcp_server.py \
  tests/unit/test_security_hardening.py worker/app.py
Result: passed.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
uv run --offline black --target-version py313 --check agent/engine.py \
  agent/tools.py mcp_server/http_server.py mcp_server/mcp_http_server.py \
  mcp_server/mcp_sse_server.py mcp_server/server.py \
  memory_connectors/archive.py memory_connectors/notion/parser.py \
  memory_connectors/nuclino/parser.py supermem/privacy/filter.py \
  tests/test_engine.py tests/test_tools.py tests/unit/test_mcp_server.py \
  tests/unit/test_security_hardening.py worker/app.py
Result: passed.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
uv run --offline ruff check supermem/ agent/ mcp_server/
Result: passed.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
uv run --offline black --check .
Result: 107 files unchanged. Black emitted its Python 3.13-versus-3.15
safety-check warning, but exited successfully.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
UV_TOOL_DIR=/private/tmp/supermem-mypy-tools \
uvx --offline --from mypy==1.20.2 mypy supermem/ \
  --ignore-missing-imports --follow-imports=skip --no-error-summary
Result: exit 0, no diagnostics.

git diff --check
Result: passed after final exact-diff inspection.
```

The first required offline lock refresh could not resolve the universal
`requires-python >=3.11` graph because the cache lacked Python 3.14 registry
metadata. With explicit reviewer authorization limited to lock synchronization,
the normal registry-backed command completed:

```text
UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv lock
Result: resolved 223 packages; `uv.lock` changed only three direct
`sse-starlette` edges in the root package metadata. No package version changed.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv lock --check --offline
Result: passed.
```

Two independent BM-0 runs used fresh temporary SQLite roots:

| Artifact | Candidate digest | Lock digest | Normalized outcome | Result |
|---|---|---|---|---|
| `/private/tmp/supermem-bm0-security-20260808-c/bm0-20260808T044549473264Z-ed4faf36af00` | `ebb85c2264ae4572194c7a2830e9228add2302673d72c886889dba8f1ad30fd8` | `7dc762c5099004b1b9b01adc190ba5c2542d0acdeb65be4bc1f6d8cf9b8008f8` | `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289` | 12 passed, 0 failed/timeout/unsupported/inconclusive/prohibited |
| `/private/tmp/supermem-bm0-security-20260808-d/bm0-20260808T044555182357Z-ed4faf36af00` | same | same | same | 12 passed, 0 failed/timeout/unsupported/inconclusive/prohibited |

The two `cases.jsonl` files are byte-identical. This is a BM-0 identity only,
not a full security-candidate digest.

## Post-veto local remediation evidence

All verification below is local static, unit, or local integration/simulator
evidence only; it does not replace the required fresh change-aware scan.

```text
/private/tmp/supermem-security-e554-20260808/bin/python -m pytest tests/unit -q
Result: 248 passed, 2 skipped, 4 existing deprecation warnings.

/private/tmp/supermem-security-e554-20260808/bin/python -m pytest \
  tests/test_engine.py tests/test_tools.py -q
Result: 60 passed, 1 existing Pydantic warning.

/private/tmp/supermem-security-e554-20260808/bin/python -m pytest \
  tests/test_agent.py tests/integration -q
Result: 24 passed, 1 existing Pydantic warning.
```

The three bounded groups cover the full tests/ tree: 332 passed, 2 skipped, and
the same existing warning classes. They were split only because the command runner
did not reliably return a monolithic report after about 27 seconds.

Combined isolated coverage used COVERAGE_FILE=/private/tmp/supermem-security-e554-final.coverage
with --cov=supermem --cov=agent on the two groups and --cov-append on the
second group, followed by `python -m coverage report --fail-under=60`.
Result: 68% total coverage; the 60% gate passed.

Targeted raw-I/O regression: tests/test_engine.py::TestPathRestriction::
test_raw_io_backends_cannot_read_private_blocks and
test_raw_io_fileio_write_preserves_private_file.
Result: 2 passed, 1 existing Pydantic warning. It proves an actually-unloaded
standard-library import still works, public io/_io FileIO and open_code paths
do not disclose or overwrite private bytes, direct importlib/agent-engine
loader access is denied, direct frozen/zip loader aliases are denied, and a
compiled frozen-loader-filename spoof cannot read with io.open_code while
ordinary io.open remains redacted.

Targeted Ruff and Black (15 touched Python files), broad
`ruff check supermem/ agent/ mcp_server/`, and `black --check .` all passed.
Black reported its known Python 3.13-versus-3.15 safety-check warning and
exited 0 with 107 files unchanged. Pinned `mypy==1.20.2` passed both the
touched `agent/engine.py`, `mcp_server/server.py`, and
`memory_connectors/archive.py` group and the canonical `supermem/` group with
no diagnostics. `git diff --check` passed.

Two post-veto BM-0 runs used distinct fresh SQLite roots:

| Artifact | Lock digest | Normalized outcome | Result |
|---|---|---|---|
| `/private/tmp/supermem-bm0-security-20260808-i/bm0-20260808T065703470199Z-ed4faf36af00` | `7dc762c5099004b1b9b01adc190ba5c2542d0acdeb65be4bc1f6d8cf9b8008f8` | `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289` | 12 passed, 0 failed/timeout/unsupported/inconclusive/prohibited |
| `/private/tmp/supermem-bm0-security-20260808-j/bm0-20260808T065710005840Z-ed4faf36af00` | same | same | 12 passed, 0 failed/timeout/unsupported/inconclusive/prohibited |

The two final `cases.jsonl` files are byte-identical at SHA-256
`6fe1fabc30d294d3ae2998240913bbe4d9cc8482bbd2908e1877952cd40fc8d8`.
This preserves the BM-0 local SQLite/FTS identity only; it is not a security
scan or a production claim.

## Third-slice exact-tip evidence

The third slice revalidated the former nested-event-loop failure with the
shipped `python -m mcp_server.server` entrypoint in a temporary-root stdio
child: before the change it exited 1 with `RuntimeError: Already running asyncio
in this thread`. After the change, a temporary-root stdio child completed
startup, initialize/tool listing, EOF shutdown, and session closure without
that error. The exact-tip process regression also exercises an
three authenticated stateless loopback HTTP initializes, verifies a missing
Bearer request is 401 and no MCP session ID is returned, then sends SIGTERM and
verifies FastMCP lifespan cleanup and a 143 exit.

Focused and broad exact-tip test evidence:

```text
/private/tmp/supermem-security-e554-20260808/bin/python -m pytest \
  tests/test_engine.py tests/unit/test_agent_retriever.py \
  tests/unit/test_hybrid.py tests/unit/test_security_hardening.py \
  tests/integration/test_primary_mcp_process.py -q
Result: 97 passed, 4 existing Pydantic/FastAPI deprecation warnings.

/private/tmp/supermem-security-e554-20260808/bin/python -m pytest \
  tests/unit/test_mcp_server.py -q
Result: 59 passed.

COVERAGE_FILE=/private/tmp/supermem-third-slice-final2.coverage \
/private/tmp/supermem-security-e554-20260808/bin/python -m pytest tests/ -q \
  --cov=supermem --cov=agent --cov-report=term --cov-fail-under=60
Result: 332 passed, 2 skipped, total coverage 67.30%; the 60% gate passed.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline ruff check \
  agent/agent.py agent/engine.py agent/tools.py mcp_server/server.py \
  supermem/config.py supermem/retrieval/agent.py \
  supermem/retrieval/hybrid.py worker/app.py tests/test_agent.py \
  tests/test_engine.py tests/test_tools.py tests/unit/test_agent_retriever.py \
  tests/unit/test_hybrid.py tests/unit/test_mcp_server.py \
  tests/unit/test_security_hardening.py tests/integration/test_primary_mcp_process.py
Result: passed.

UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-security-e554-20260808 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline black \
  --target-version py313 --check .
Result: 108 files would be left unchanged.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
UV_TOOL_DIR=/private/tmp/supermem-mypy-tools uvx --offline \
  --from mypy==1.20.2 mypy agent/agent.py agent/engine.py agent/tools.py \
  mcp_server/server.py supermem/config.py supermem/retrieval/agent.py \
  supermem/retrieval/hybrid.py worker/app.py --ignore-missing-imports \
  --follow-imports=skip --no-error-summary
Result: passed with no diagnostics.

The broader `ruff check supermem/ agent/ mcp_server/ worker/
memory_connectors/ tests/` command exits 1 with 79 legacy findings in untouched
connector/test files. The targeted changed surface above passes; this receipt
does not present the repository-wide Ruff result as green. `git diff --check`
passed.
```

Two fresh-root BM-0 runs also passed all 12 cases with 0 failures and the same
normalized digest `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289`.
Their `cases.jsonl` files are byte-identical at SHA-256
`6fe1fabc30d294d3ae2998240913bbe4d9cc8482bbd2908e1877952cd40fc8d8`:

| Artifact | Candidate digest | Result |
|---|---|---|
| `/private/tmp/supermem-bm0-third-a-DoaBDF/bm0-20260809T115251724856Z-ed4faf36af00` | `ebb85c2264ae4572194c7a2830e9228add2302673d72c886889dba8f1ad30fd8` | 12 passed, 0 failed |
| `/private/tmp/supermem-bm0-third-b-knvaEa/bm0-20260809T115255703675Z-ed4faf36af00` | same | 12 passed, 0 failed |

## Adjacent fail-closed exact-tip evidence

The latest implementation-only phase made four bounded corrections:

- primary authenticated loopback HTTP is stateless and retains no MCP protocol
  sessions across initialize requests;
- authenticated Worker `GET /observations` returns active observations only;
- retired standalone HTTP/SSE POST routes return disabled without parsing the
  request body; and
- the retained internal executor denies direct `posix` and `nt` raw-I/O module
  imports.

Changed files in this phase were `.env.example`, `CHANGELOG.md`, `CLAUDE.md`,
`CONTRIBUTING.md`, `README.md`, `SECURITY.md`, `agent/engine.py`,
`mcp_server/mcp_http_server.py`, `mcp_server/mcp_sse_server.py`,
`mcp_server/server.py`, `worker/app.py`, `tests/test_engine.py`,
`tests/integration/test_primary_mcp_process.py`,
`tests/unit/test_mcp_server.py`, `tests/unit/test_security_hardening.py`, and
this receipt. Other dirty candidate files were preserved.

Boundary-first reproduction used the eventual regression tests before product
edits. The command exited 1 with 6 failures and 1 legitimate wrapper control
passing: stateful ASGI/process initialization returned and retained MCP session
state; Worker listing returned the retracted canary; both retired adapters
parsed the malformed body; and direct `posix` raw I/O read the private canary.

```text
uv run pytest -q \
  tests/unit/test_security_hardening.py::test_primary_http_is_authenticated_and_does_not_retain_protocol_sessions \
  tests/unit/test_security_hardening.py::test_worker_observation_listing_excludes_retracted_rows \
  tests/unit/test_security_hardening.py::test_retired_json_adapters_disable_without_parsing_request_body \
  tests/unit/test_security_hardening.py::test_legacy_http_wrapper_disables_without_parsing_request_body \
  tests/test_engine.py::TestPathRestriction::test_platform_raw_io_module_cannot_read_private_blocks \
  tests/integration/test_primary_mcp_process.py::test_primary_http_process_requires_bearer_and_gracefully_handles_sigterm
Result before product edits: 6 failed, 1 passed, 4 warnings.
Result after product edits: 7 passed, 4 existing deprecation warnings.

uv run pytest -q tests/unit/test_mcp_server.py \
  tests/unit/test_security_hardening.py tests/test_engine.py \
  tests/integration/test_primary_mcp_process.py
Result: 150 passed, 4 existing deprecation warnings.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
COVERAGE_FILE=/private/tmp/supermem-third-slice-adjacent.coverage \
uv run --offline pytest tests/ -q --cov=supermem --cov=agent \
  --cov-report=term --cov-fail-under=60
Result: 337 passed, 2 skipped, 67.30% coverage; the 60% gate passed.
Warnings: four known Pydantic/FastAPI deprecations and two previously recorded
delayed unclosed-SQLite ResourceWarnings. An initial invocation without the
temporary `UV_CACHE_DIR` exited 2 because the managed environment denied uv's
home-cache initialization; the isolated-cache command above is the successful
evidence command.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline ruff check \
  agent/agent.py agent/engine.py agent/tools.py mcp_server/http_server.py \
  mcp_server/mcp_http_server.py mcp_server/mcp_sse_server.py \
  mcp_server/server.py memory_connectors/archive.py \
  memory_connectors/notion/parser.py memory_connectors/nuclino/parser.py \
  supermem/config.py supermem/privacy/filter.py supermem/retrieval/agent.py \
  supermem/retrieval/hybrid.py worker/app.py tests/test_agent.py \
  tests/test_engine.py tests/test_tools.py tests/unit/test_agent_retriever.py \
  tests/unit/test_hybrid.py tests/unit/test_mcp_server.py \
  tests/unit/test_security_hardening.py \
  tests/integration/test_primary_mcp_process.py
Result: passed.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --offline black \
  --target-version py313 --check .
Result: 108 files would be left unchanged.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
UV_TOOL_DIR=/private/tmp/supermem-mypy-tools uvx --offline \
  --from mypy==1.20.2 mypy agent/agent.py agent/engine.py agent/tools.py \
  mcp_server/server.py supermem/config.py supermem/retrieval/agent.py \
  supermem/retrieval/hybrid.py worker/app.py --ignore-missing-imports \
  --follow-imports=skip --no-error-summary
Result: passed with no diagnostics.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache \
UV_TOOL_DIR=/private/tmp/supermem-mypy-tools uvx --offline \
  --from mypy==1.20.2 mypy mcp_server/http_server.py \
  mcp_server/mcp_http_server.py mcp_server/mcp_sse_server.py \
  memory_connectors/archive.py memory_connectors/notion/parser.py \
  memory_connectors/nuclino/parser.py supermem/privacy/filter.py \
  --ignore-missing-imports --follow-imports=skip --no-error-summary
Result: passed with no diagnostics.

UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv lock --check --offline
Result: resolved 223 packages in 26ms; exit 0.

git diff --check
Result: passed.
```

The broader `ruff check supermem/ agent/ mcp_server/ worker/
memory_connectors/ tests/` still exits 1 with the same 79 legacy findings in
untouched connector/test files. The exact targeted changed surface is green;
repository-wide Ruff is not claimed green.

Two more fresh-root BM-0 runs preserved the frozen local SQLite/FTS result:

| Artifact | Candidate digest | Normalized outcome | Result |
|---|---|---|---|
| `/private/tmp/supermem-bm0-adjacent-a.HAs9oL/bm0-20260809T134206803383Z-ed4faf36af00` | `ebb85c2264ae4572194c7a2830e9228add2302673d72c886889dba8f1ad30fd8` | `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289` | 12 passed, 0 failed/timeout/unsupported/inconclusive/prohibited |
| `/private/tmp/supermem-bm0-adjacent-b.LJj5VM/bm0-20260809T134213066557Z-ed4faf36af00` | same | same | 12 passed, 0 failed/timeout/unsupported/inconclusive/prohibited |

Their `cases.jsonl` files are byte-identical at SHA-256
`6fe1fabc30d294d3ae2998240913bbe4d9cc8482bbd2908e1877952cd40fc8d8`.
This is BM-0 local benchmark proof only, not a general security, installed,
staging, or production claim.

Proof labels: static (diff, Ruff, Black, mypy); unit (Agent, retrieval,
executor, and MCP handler tests); local integration/simulator (ASGI and the
temporary-root primary-process stdio/loopback-HTTP tests); and BM-0 local
SQLite/FTS. No real dependency/device, installed product, staging, or production
proof exists.

## GitHub CI checkout-layout correction

The first draft-PR check at commit `90c19df8eb07e01774c83e2e514ddd9aec326a55`
failed only two BM-0 runner tests on GitHub Actions. Standard Actions checkout
uses a `.git` directory, while `benchmarks.runner._git_head()` incorrectly
assumed `.git` was a linked-worktree text file. Ruff, Black, mypy, the coverage
gate, and the other 335 tests passed in that hosted run.

The bounded correction resolves `HEAD` through `git rev-parse --verify HEAD`
and adds a regression that creates a conventional repository with a `.git`
directory. Local Python 3.11 evidence matching the CI dependency lock and test
command is:

```text
UV_PROJECT_ENVIRONMENT=/private/tmp/supermem-ci311 \
UV_CACHE_DIR=/private/tmp/supermem-uv-cache uv run --frozen pytest tests/ \
  --cov=supermem --cov=agent --cov-report=term-missing \
  --cov-report=xml:/private/tmp/supermem-ci311-coverage.xml \
  --cov-fail-under=60
Result: 338 passed, 2 skipped, 4 existing deprecation warnings;
67.26% coverage and the 60% gate passed.

ruff 0.15.12 check supermem/ agent/ mcp_server/: passed.
black --check . under the Python 3.11 environment: 108 files unchanged.
mypy 1.20.2 supermem/: passed with no diagnostics.
git diff --check: passed.
```

Two fresh-root BM-0 runs after the correction each passed 12/12 with normalized
digest `ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289`
and byte-identical `cases.jsonl` SHA-256
`6fe1fabc30d294d3ae2998240913bbe4d9cc8482bbd2908e1877952cd40fc8d8`:

- `/private/tmp/supermem-bm0-ci-fix-a.XXXXXX.aQkKwP6r10/bm0-20260809T150310809419Z-ed4faf36af00`
- `/private/tmp/supermem-bm0-ci-fix-b.XXXXXX.SqwETzlpV5/bm0-20260809T150310810037Z-ed4faf36af00`

This local macOS/Python 3.11 parity evidence was subsequently confirmed by
GitHub Actions run `31320247107`: lint/type/test, package build, and Docker build
all passed at commit `03193f5`.

## PR review corrections

Codex review on PR #15 reported six actionable boundaries. Before product
edits, the eventual regression set produced 15 failures: private destination
replacement destroyed its canary; spaced/Unicode paths failed ingestion;
content-identical expiry renewal returned the stale revision; nine `NaN`/infinity
timestamp cases were accepted; the dashboard supplied no bearer; keyless HTTP
startup did not fail; and the Make target rejected dotenv-only configuration.

The local correction now:

- rejects restricted-executor rename/replace/link operations involving a
  private file or a directory while retaining a public `Path.replace` control;
- constructs canonical UTF-8 percent-encoded memory URI path segments and
  rejects encoded traversal/non-canonical encodings;
- creates a new immutable source revision when lifecycle metadata changes while
  retaining exact content-plus-metadata idempotency;
- rejects non-finite lifecycle and query timestamps before SQLite persistence;
- prompts for the Worker bearer, holds it in page memory only, attaches it to
  API calls, escapes imported summaries/fields, and removes the stale Tier-4 UI
  option; and
- lets Python load `.env`, then fails primary HTTP startup if the resolved key
  remains empty.

Exact local evidence after correction:

```text
Focused six-comment regression set: 15 passed, 4 existing warnings.
Adjacent engine/BM-0/MCP/security/process suite: 174 passed, 4 existing warnings.
Python 3.11 full coverage suite: 353 passed, 2 skipped, 4 existing warnings;
67.25% coverage and the 60% gate passed.
Ruff 0.15.12 changed tests and CI source scope: passed.
Black --target-version py311 --check .: 108 files unchanged.
mypy 1.20.2 supermem/: passed with no diagnostics.
Worker dashboard JavaScript node --check: passed.
git diff --check: passed.
```

Two fresh BM-0 roots each passed 12/12 with normalized digest
`ed4faf36af0092bcc0697d8c70391f98b9d5350c855d8cc7cdfbb1081998f289`
and byte-identical cases SHA-256
`6fe1fabc30d294d3ae2998240913bbe4d9cc8482bbd2908e1877952cd40fc8d8`:

- `/private/tmp/supermem-bm0-pr15-review-a.XXXXXX.H7Jr0i54Bh/bm0-20260809T153232700905Z-ed4faf36af00`
- `/private/tmp/supermem-bm0-pr15-review-b.XXXXXX.Tftr1s0hov/bm0-20260809T153232700881Z-ed4faf36af00`

These are static, unit, local integration/process, and local SQLite/FTS proof
only. Review replies, thread resolution, commit, and push remain separately
authorized actions.

## Residuals and required next action

- The Python executor is still not a hostile-code sandbox. Closure/object
  introspection remains outside this slice’s guarantee; hostile code needs an
  OS/container boundary.
- Static Bearer authentication is not OAuth, identity/scope validation, or
  multi-tenant remote authorization.
- No installed-artifact, real remote client, staging, or production evidence
  exists. Local ASGI/unit/process evidence does not prove those layers.
- The full coverage run emitted six warnings: four Pydantic/FastAPI deprecations
  and two delayed unclosed-SQLite `ResourceWarning`s. The Agent-only coverage
  test did not reproduce the SQLite warnings; their allocation source was not
  established in this slice, so they remain a local test-hygiene residual.

No new formal security scan or independent veto review was requested or run in
this latest phase. Before candidate acceptance or release, that omitted gate
remains explicit unless the user changes the release policy. The current review
correction is local and uncommitted; no reply, thread resolution, push, merge,
deployment, or publication is authorized by this receipt.
