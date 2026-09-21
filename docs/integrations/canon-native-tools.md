# Canon integration ownership

Status: current contract for the Hermes Canon plugin and Canon CLI boundary.

## Owners

- `plugins/canon/schemas.py` defines the public Hermes tool schemas for launch, observe, checkpoint control and workflow control.
- `plugins/canon/transport.py` validates those requests, starts Canon through the isolated CLI entrypoint and validates one complete JSON response frame.
- `plugins/canon/background_runner.py` owns tracked background execution and completion transport.
- Canon's registered workflow declaration, compiler, runtime, durable journal, checkpoints and artifacts remain the semantic execution authority. The Hermes plugin transports their typed requests and results; it does not recreate Canon workflow state.
- `profile_runtime.py` owns the private Hermes model-session lifecycle used by the Canon profile adapter: prepare, fresh run, continuation and close.

The removed direct `tools/canon_gateway_*.py`, `tools/canon_workflow_*.py`, `tools/canon_profile_session.py` and direct Telegram callback paths are not current owners and must not be restored as fallbacks.

## Model continuation

A successful fresh turn reports an opaque backend `agentSessionRef`, a backend-attested `agentCheckpointRef`, `continuationMode=fresh` and `historyCount=0`.

A successful revise turn must:

- retain the same opaque `agentSessionRef`;
- return a new backend-attested `agentCheckpointRef`;
- report `continuationMode=replayed`;
- report `historyCount>0`;
- refuse a stale, foreign or settings-drifted continuation tuple before the provider call.

The public Human Gate checkpoint identity has class `public-gate-resume-alias`. It authorizes pause/resume of the workflow but is not the private backend model-session identity and must not be compared as if it were `agentSessionRef`.

The provider-visible second-turn correction is a semantic projection only. Canon run, gate, record, source and decision-attempt identities, evidence references and the complete feedback payload remain in Canon's durable authority and are not model correction content.

## Observe and inspect framing

`canon_workflow_observe` exposes `latest`, `list`, `full`, `inspect`, `report`, `timeline`, `artifacts` and `events`. Run-scoped surfaces require an explicit `run_id`.

`plugins/canon/transport.py:_decode_json` parses the entire bounded stdout value with `json.loads`, rejects duplicate keys and non-finite values, and fails closed on decode errors. It does not select the last JSON-looking line. Shell warnings, prefixes, suffixes or multiple frames therefore invalidate the response.

## Release proof

The current boundary is accepted only when all of these are true on the same source bytes:

1. installed launch → Human Gate revise → second model turn → approve reaches terminal completion;
2. inspect returns one valid protocol frame and contamination controls fail closed;
3. backend continuation reports the same session, a new checkpoint, replayed mode and non-zero history;
4. the actual second-turn model input contains only the requested semantic correction while durable Gate provenance remains readable;
5. full Canon/Hermes owner aggregates, compatibility-pointer checks, `git diff --check` and process cleanup pass.

Canonical regression commands are recorded in the active slice plan and its execution ledger; old direct-tool commands are not substitutes.
