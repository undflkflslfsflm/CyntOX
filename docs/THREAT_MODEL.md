# Threat Model

Scope: local testing of code and machines owned by the user. Out of scope: external scanning, exploitation, credential access, persistence, evasion, public-network probing, or third-party systems.

## Assets

- Host filesystem outside allowlisted roots
- Hidden evaluator data under `tests/hidden` and `.oslab/evaluator`
- Source, patches, logs, crash artifacts, prompts, and model outputs
- Local model endpoint and Qwen Code configuration
- SQLite experiment database and content-addressed artifact store
- Host CPU/GPU/RAM/disk resources

## Threats and Controls

| Threat | Control | Evidence |
| --- | --- | --- |
| Prompt injection from repository text or logs | The supervisor, not the model, owns acceptance. Tools are typed and policy checked. | `tests/integration/test_broker.py`, `oslab/tools/broker.py` |
| Tool abuse or arbitrary shell | The broker exposes named capabilities only. Qwen Code sees only two read-only MCP tools. | `tests/integration/test_live_qwen_code.py`, `.qwen/settings.json` |
| Path traversal | Paths are canonicalized and checked against allowed roots. `..` is denied. | `tests/unit/test_policy.py` |
| Symlink escape | Writes reject symlink chains before mutation. | `tests/unit/test_policy.py` |
| Command injection | Build and exceptional commands use reviewed argument arrays and allowlists, not model strings. | `oslab/process_runner.py`, `oslab/policy.py` |
| Evaluator tampering | Hidden/evaluator roots are denied for writes. Diffs are scanned for test weakening and evaluator edits. | `tests/integration/test_broker.py`, `oslab/policy.py` |
| Log spoofing and fake PASS events | Serial events are parsed from actual QEMU output; evaluator weakening/fake PASS patterns are detected in patches. | `tests/e2e/test_qemu_fixture.py`, `oslab/eval/verifier.py` |
| Resource exhaustion | Subprocesses have timeouts, output caps, process cleanup, and campaign iteration budgets. | `tests/unit/test_process_runner.py`, `tests/e2e/test_fixture_fuzz.py` |
| Malicious generated tests | Generated fixture tests are schema-bounded and run through the same broker and target allowlist. | `test.generate_fixture`, `test.run` broker handlers |
| Compromised artifacts | Artifacts are SHA-256 addressed and integrity-checked. | `tests/unit/test_artifacts.py`, `oslab integrity check` |
| Guest-to-host escape assumptions | QEMU runs in Docker with `-nic none`, no public/bridged/NAT networking, QMP published to `127.0.0.1` only. | `tests/e2e/test_qemu_fixture.py`, `oslab/qemu/backend.py` |
| Infrastructure failures counted as OS bugs | Outcomes include `INFRA_ERROR`, and fuzz campaigns include an induced infra mode. | `tests/e2e/test_fixture_fuzz.py` |
| Duplicate findings presented as novel | Fuzz and recovery paths deduplicate by fingerprint/unique constraints. | `tests/unit/test_fuzz.py`, `tests/integration/test_supervisor_recovery.py` |

## Residual Risk

The v1 fixture is deliberately tiny. It proves the control plane, not full kernel debugging depth. Real OS debug quality depends on the target plugin exposing symbols, serial logs, build profiles, and smoke tests. Optional offloaded runtimes are not enabled because they were not installed and benchmarked locally.
