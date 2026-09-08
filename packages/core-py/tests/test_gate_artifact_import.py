"""unit artifact-import: the external-artifact boundary, treated as hostile input.

Every case in ``contracts/v1/conformance/artifact-import-cases.json`` is built here as a
real filesystem under a temporary import root and run through the real session. The point
is not that the happy path works - it is that each *specific* way a producer could lie
about its artifact is refused with a bounded code, and that a refusal leaves nothing
behind.

Two properties are asserted on every refusal rather than case by case:

* no handle exists afterwards, so a rejected import cannot be resolved later;
* the envelope carries no path, no manifest text and no artifact bytes.

The flow-through tests then prove the imported handle is an ordinary handle: the reader,
the citation verifier, deterministic inspect (lines, bytes and literal search), stats and
TTL all treat it exactly like a gate-blocked capture, because it is one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from context_shunt.artifacts import (
    NATIVE_IMPORT_CONTRACT,
    ArtifactImporter,
    normalize_manifest,
)
from context_shunt.config import load as load_config
from context_shunt.errors import ShuntError
from context_shunt.limits import EMITTED_SCHEMA_VERSION
from context_shunt.paths import PathPolicy
from context_shunt.schema import validate_envelope
from context_shunt.session import ShuntSession

from .support import FakeLuna, answer_json, make_capability

pytestmark = pytest.mark.gate_artifact_import

REPO = Path(__file__).resolve().parents[3]
CASES_PATH = REPO / "contracts" / "v1" / "conformance" / "artifact-import-cases.json"

#: The synthetic foreign producer schema. It is a *fixture* identifier: the corpus and
#: these tests never contain real producer content, and the profile is only reachable when
#: a deployment allowlists the schema.
FOREIGN_SCHEMA = "hermes.tool_result_artifact_manifest.v1"
ALL_SCHEMAS = (NATIVE_IMPORT_CONTRACT, FOREIGN_SCHEMA)

#: Codes the envelope publishes as ``blocked`` rather than ``error``. Imported from the
#: envelope module so the two cannot drift.
from context_shunt.envelope import _BLOCKED_CODES as BLOCKED_CODES  # noqa: E402


def _cases() -> list[dict[str, Any]]:
    with CASES_PATH.open("rb") as fh:
        return json.load(fh)["cases"]


def _corpus() -> dict[str, Any]:
    with CASES_PATH.open("rb") as fh:
        return json.load(fh)


# -- world building ---------------------------------------------------------


class World:
    """One case's filesystem: an import root, a private cache, and an artifact in it."""

    def __init__(self, tmp_path: Path, case: dict[str, Any]):
        self.root = tmp_path / "world"
        self.import_root = self.root / "artifacts"
        self.outside = self.root / "outside"
        self.cache = self.root / "cache"
        self.workspace = self.root / "ws"
        for directory in (self.import_root, self.outside, self.workspace):
            directory.mkdir(parents=True, exist_ok=True)
        self.case = case
        self.setup = dict(case.get("setup") or {})
        self.artifact_path = self._write_artifact()
        self.manifest_path = self._write_manifest()
        self._apply_post_manifest_setup()

    # -- artifact ------------------------------------------------------------

    def _artifact_bytes(self) -> bytes:
        spec = dict(self.setup.get("artifact") or {})
        if "content_base64" in spec:
            return base64.b64decode(spec["content_base64"])
        if "repeat" in spec:
            unit = str(spec["repeat"]["unit"]).encode("utf-8")
            want = int(spec["repeat"]["bytes"])
            return (unit * (want // len(unit) + 1))[:want]
        return str(spec.get("content", "")).encode("utf-8")

    def _write_artifact(self) -> Path:
        spec = dict(self.setup.get("artifact") or {})
        name = str(spec.get("name") or "artifact.log")
        path = self.import_root / name
        path.write_bytes(self._artifact_bytes())
        if self.setup.get("outside"):
            (self.outside / "neighbour.log").write_bytes(
                str(self.setup["outside"]["content"]).encode("utf-8")
            )
        if self.setup.get("symlink"):
            link = self.import_root / "artifact-link.log"
            link.symlink_to(path)
        if self.setup.get("directory"):
            (self.import_root / "artifact-dir").mkdir(exist_ok=True)
        if self.setup.get("fifo"):
            os.mkfifo(self.import_root / "artifact-fifo")
        if self.setup.get("hardlink"):
            os.link(path, self.import_root / "artifact-hardlink.log")
        return path

    # -- the path the manifest points at ------------------------------------

    def declared_path(self) -> str:
        mode = str((self.case.get("manifest") or {}).get("path_mode") or "absolute")
        if mode == "absolute":
            return str(self.artifact_path)
        if mode == "relative":
            return self.artifact_path.name
        if mode == "traversal_to_outside":
            return str(self.import_root / ".." / "outside" / "neighbour.log")
        if mode == "outside":
            return str(self.outside / "neighbour.log")
        if mode == "symlink":
            return str(self.import_root / "artifact-link.log")
        if mode == "directory":
            return str(self.import_root / "artifact-dir")
        if mode == "fifo":
            return str(self.import_root / "artifact-fifo")
        if mode == "cache_root":
            return str(self.cache / "store.sqlite3")
        if mode == "file_uri":
            return f"file://{self.artifact_path}"
        if mode == "https_uri":
            return "https://example.invalid/artifact.log"
        raise AssertionError(f"unknown path_mode: {mode}")

    # -- manifest ------------------------------------------------------------

    def _manifest_document(self) -> dict[str, Any]:
        spec = dict(self.case.get("manifest") or {})
        data = self._artifact_bytes()
        digest = hashlib.sha256(data).hexdigest()
        media = str((self.setup.get("artifact") or {}).get("media_type") or "text/plain")
        truncated = bool(spec.get("upstream_truncated", False))
        profile = str(spec.get("profile") or "native")

        if profile == "native":
            document: dict[str, Any] = {
                "import_contract": NATIVE_IMPORT_CONTRACT,
                "producer": {
                    "id": "synthetic-compactor",
                    "manifest_schema": NATIVE_IMPORT_CONTRACT,
                },
                "artifact": {
                    "path": self.declared_path(),
                    "bytes": len(data),
                    "sha256": digest,
                    "media_type": media,
                },
                "origin": {"tool": "synthetic_query", "upstream_truncated": truncated},
            }
            artifact_key = "artifact"
        elif profile == "foreign_tool_result_artifact":
            document = {
                "schema": FOREIGN_SCHEMA,
                "producer_id": "synthetic-compactor",
                "artifact": {
                    "uri": self.declared_path(),
                    "size_bytes": len(data),
                    "digest": f"sha256:{digest}",
                    "content_type": media,
                },
                "source": {"tool_name": "synthetic_query", "truncated": truncated},
            }
            artifact_key = "artifact"
        else:
            raise AssertionError(f"unknown profile: {profile}")

        for key, value in dict(spec.get("override_artifact") or {}).items():
            mapped = {
                "bytes": "size_bytes",
                "sha256": "digest",
                "media_type": "content_type",
            }
            field = key if profile == "native" else mapped.get(key, key)
            document[artifact_key][field] = value
        document.update(dict(spec.get("override") or {}))
        for key in spec.get("drop") or []:
            document.pop(key, None)
        return document

    def _write_manifest(self) -> Path:
        body = self.setup.get("manifest_body")
        if body is None:
            document = self._manifest_document()
            padding = int(self.setup.get("manifest_padding_bytes") or 0)
            if padding:
                # Padding goes in a field the schema would reject anyway; the size check
                # has to fire first, before the document is ever parsed as a contract.
                document["padding"] = "p" * padding
            body = json.dumps(document)
        target_dir = self.outside if self.setup.get("manifest_outside") else self.import_root
        path = target_dir / "artifact.manifest.json"
        path.write_text(str(body), encoding="utf-8")
        if self.setup.get("manifest_symlink"):
            link = self.import_root / "manifest-link.json"
            link.symlink_to(path)
            return link
        return path

    def _apply_post_manifest_setup(self) -> None:
        """Changes made *after* the manifest was written, i.e. what a swap looks like."""
        if self.setup.get("unlink_artifact"):
            self.artifact_path.unlink()
        rewritten = self.setup.get("rewrite_after_manifest")
        if rewritten is not None:
            self.artifact_path.write_bytes(str(rewritten).encode("utf-8"))

    # -- session -------------------------------------------------------------

    def session(self) -> ShuntSession:
        overrides = dict(self.case.get("config") or {})
        enabled = bool(overrides.get("enabled", True))
        roots = overrides.get("roots", [str(self.import_root)])
        schemas = overrides.get("accepted_manifest_schemas", list(ALL_SCHEMAS))
        raw = {
            "workspace_roots": [str(self.workspace)],
            "cache_dir": str(self.cache),
            "artifact_import": {
                "enabled": enabled,
                "roots": list(roots),
                "accepted_manifest_schemas": list(schemas),
            },
        }
        config = load_config(raw, default_spill_dir=self.cache)
        capability = make_capability(
            artifact_import=bool(overrides.get("capability_supported", True))
        )
        return ShuntSession("sess-import", config, capability)


# -- the corpus -------------------------------------------------------------


def test_the_corpus_is_complete_and_fixed():
    corpus = _corpus()
    cases = corpus["cases"]
    assert len(cases) >= 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert corpus["invariants"]["model_calls_for_import"] == 0
    assert corpus["invariants"]["raw_returned_on_any_failure"] is False
    assert corpus["invariants"]["translation_profile_grants_no_authorization"] is True
    imports = [c for c in cases if c["expect"]["action"] == "import"]
    refusals = [c for c in cases if c["expect"]["action"] == "error"]
    # The corpus is a security matrix, so refusals have to dominate it. A corpus that
    # drifted into mostly happy paths would still pass every assertion below while
    # proving much less.
    assert len(imports) >= 4 and len(refusals) >= 24


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_every_import_case_matches_the_contract(tmp_path, case):
    world = World(tmp_path, case)
    if case["id"] == "import_without_configured_roots_refused":
        pytest.skip("covered by the configuration test; the loader refuses it earlier")
    session = world.session()
    try:
        envelope = session.import_artifact("req_import", manifest_path=str(world.manifest_path))
        expect = case["expect"]
        assert validate_envelope(envelope), envelope
        assert envelope["schema_version"] == EMITTED_SCHEMA_VERSION

        if expect["action"] == "import":
            assert envelope["status"] == "ok"
            assert envelope["code"] == "IMPORTED"
            assert envelope["answer"] == "" and envelope["citations"] == []
            assert envelope["pointer"]["internal"] is True
            receipt = envelope["import_receipt"]
            # The receipt records the digest of the bytes read, and those bytes are what
            # the pointer addresses. If the two ever disagreed the receipt would be
            # describing a different artifact than the handle resolves to.
            assert f"sha256:{receipt['artifact_sha256']}" == envelope["pointer"]["snapshot_id"]
            assert receipt["bytes"] == envelope["pointer"]["bytes"]
            assert receipt["manifest_schema"] in ALL_SCHEMAS
            handle = session.registry.handle("sess-import", envelope["pointer"]["source_id"])
            assert handle.internal is True
            _assert_no_leak(envelope, world)
            return

        # A source the policy refuses is `blocked`; everything else is `error`. That
        # mapping is the envelope's, not the import path's, so it is derived here rather
        # than restated per case.
        assert envelope["status"] == ("blocked" if expect["code"] in BLOCKED_CODES else "error")
        assert envelope["code"] == expect["code"], envelope
        assert "pointer" not in envelope and "import_receipt" not in envelope
        assert envelope["answer"] == "" and envelope["citations"] == []
        # A refusal must leave nothing resolvable behind.
        assert session.store.stats().handles == 0
        _assert_no_leak(envelope, world)
    finally:
        session.close()


def _assert_no_leak(envelope: dict[str, Any], world: World) -> None:
    """No path and no artifact bytes may appear in the envelope.

    The receipt legitimately contains the word ``manifest_schema``, so this checks for
    the things that would actually be a leak: any directory or filename from the world,
    and any run of the artifact's own bytes.
    """
    wire = json.dumps(envelope)
    for forbidden in (
        str(world.root),
        str(world.import_root),
        str(world.outside),
        str(world.cache),
        world.artifact_path.name,
        world.manifest_path.name,
    ):
        assert forbidden not in wire, forbidden
    payload = world._artifact_bytes()
    if len(payload) >= 8:
        assert payload.decode("utf-8", "ignore")[:32] not in wire


# -- the detail every refusal is expected to carry --------------------------


@pytest.mark.parametrize(
    "case",
    [case for case in _cases() if case["expect"].get("detail")],
    ids=lambda case: case["id"],
)
def test_refusals_carry_the_contract_detail(tmp_path, case):
    """The code says what class of thing went wrong; the detail says which check fired.

    Asserted separately because the detail is not in the envelope - it is deliberately
    kept out of the wire format - so it has to be read from the raised error instead.
    """
    world = World(tmp_path, case)
    session = world.session()
    try:
        if case["expect"]["action"] == "import":
            pytest.skip("no detail on a successful import")
        if not session.artifact_import_enabled:
            # Disabled by config or capability: the session refuses before the importer
            # exists, and there is no importer to raise from.
            envelope = session.import_artifact(
                "req_import", manifest_path=str(world.manifest_path)
            )
            assert envelope["code"] == case["expect"]["code"]
            return
        importer = ArtifactImporter(
            session.registry,
            policy=session.config.import_path_policy(),
            accepted_schemas=session.config.artifact_import.accepted_manifest_schemas,
            limits=session.config.limits,
        )
        with pytest.raises(ShuntError) as raised:
            manifest = importer.read_manifest_file(str(world.manifest_path))
            importer.adopt("sess-import", manifest)
        assert raised.value.code == case["expect"]["code"]
        assert raised.value.detail == case["expect"]["detail"]
    finally:
        session.close()


# -- accounting -------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [case for case in _cases() if case["expect"].get("baseline_kind")],
    ids=lambda case: case["id"],
)
def test_the_import_baseline_matches_the_producers_own_claim(tmp_path, case):
    """A producer that already truncated only gets credited what we can observe."""
    world = World(tmp_path, case)
    session = world.session()
    try:
        envelope = session.import_artifact("req_import", manifest_path=str(world.manifest_path))
        assert envelope["code"] == "IMPORTED"
        page = session.store.operation_page(session.identity, page=1, page_size=8)
        record = next(r for r in page if r.operation_id == envelope["accounting_id"])
        assert record.kind == "capture"
        assert record.baseline_kind == case["expect"]["baseline_kind"]
    finally:
        session.close()


def test_an_import_is_accounted_as_a_capture_not_as_a_spill(tmp_path):
    """The accounting kind is where the producer distinction actually lives.

    ``handles.kind`` records the capture *category* and is shared with the local spill
    path, because ``contracts/store/v1.sql`` deliberately stores no producer identity.
    The distinction that a report needs - we adopted this rather than spilled it - is the
    ``capture`` operation kind plus the envelope receipt.
    """
    world = World(tmp_path, {"setup": {"artifact": {"content": "body\n"}}, "manifest": {}})
    session = world.session()
    try:
        envelope = session.import_artifact("req_import", manifest_path=str(world.manifest_path))
        page = session.store.operation_page(session.identity, page=1, page_size=8)
        kinds = {record.kind for record in page}
        assert kinds == {"capture"}
        assert "spill" not in kinds
        assert session.registry.handle("sess-import", envelope["pointer"]["source_id"]).kind == (
            "spilled_tool"
        )
    finally:
        session.close()


# -- the imported handle is an ordinary handle ------------------------------


IMPORTED_BODY = (
    "2026-09-01T00:00:01Z level=info msg=start\n"
    "2026-09-01T00:00:02Z level=error msg=upstream_timeout attempt=3\n"
    "2026-09-01T00:00:03Z level=info msg=retry_scheduled\n"
    "2026-09-01T00:00:04Z level=error msg=gave_up max_retries=3\n"
)


def _imported_session(tmp_path) -> tuple[ShuntSession, dict[str, Any], World]:
    world = World(
        tmp_path,
        {"setup": {"artifact": {"content": IMPORTED_BODY}}, "manifest": {}},
    )
    session = world.session()
    envelope = session.import_artifact("req_import", manifest_path=str(world.manifest_path))
    assert envelope["code"] == "IMPORTED", envelope
    return session, envelope, world


def test_an_imported_handle_answers_a_question_with_a_verified_citation(tmp_path):
    world = World(
        tmp_path,
        {"setup": {"artifact": {"content": IMPORTED_BODY}}, "manifest": {}},
    )
    quote = "msg=gave_up max_retries=3"
    luna = FakeLuna(
        replies=[
            answer_json(
                "The run stopped after three attempts [c1].",
                # The reply speaks the model-facing shape (`line_start`/`line_end`); the
                # reader rebuilds source, snapshot and `verified` from trusted chunk
                # metadata rather than believing any of it.
                [{"id": "c1", "line_start": 4, "line_end": 4, "quote": quote}],
            )
        ]
    )
    session = ShuntSession(
        "sess-import",
        load_config(
            {
                "workspace_roots": [str(world.workspace)],
                "cache_dir": str(world.cache),
                "artifact_import": {
                    "enabled": True,
                    "roots": [str(world.import_root)],
                    "accepted_manifest_schemas": list(ALL_SCHEMAS),
                },
            },
            default_spill_dir=world.cache,
        ),
        make_capability(artifact_import=True),
        # FakeLuna *is* a provider: it implements `complete`/`target` and returns a
        # ModelResponse, so wrapping it in a host bridge would be a second translation.
        provider=luna,
    )
    try:
        imported = session.import_artifact(
            "req_import", manifest_path=str(world.manifest_path)
        )
        pointer = imported["pointer"]
        answered = session.read(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": "req_read",
                "operation": "read",
                "question": "Why did the run stop?",
                "refined": True,
                "sources": [
                    {
                        "source_id": pointer["source_id"],
                        "snapshot_id": pointer["snapshot_id"],
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {
                    "max_chunks": 8,
                    "max_answer_bytes": 8192,
                    "deadline_ms": 60000,
                },
            }
        )
        assert answered["status"] == "ok", answered
        assert answered["code"] == "ANSWERED"
        assert answered["citations"][0]["verified"] is True
        assert answered["citations"][0]["quote"] == quote
        # The question reached the model, and the model saw only the excerpt.
        assert "Why did the run stop?" in luna.calls[0].user
    finally:
        session.close()


@pytest.mark.parametrize(
    "selector",
    [
        {"kind": "lines", "start": 2, "end": 2},
        {"kind": "bytes", "start": 0, "end": 40},
        {"kind": "search", "needle": "max_retries", "max_matches": 4},
    ],
    ids=["lines", "bytes", "search"],
)
def test_an_imported_handle_is_inspectable_with_zero_model_calls(tmp_path, selector):
    session, envelope, _world = _imported_session(tmp_path)
    try:
        pointer = envelope["pointer"]
        extracted = session.inspect(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": "req_inspect",
                "operation": "inspect",
                "source_id": pointer["source_id"],
                "snapshot_id": pointer["snapshot_id"],
                "selector": selector,
                "budgets": {"max_result_bytes": 16384, "max_scan_lines": 20000},
            }
        )
        assert extracted["code"] == "EXTRACTED", extracted
        assert extracted["result_kind"] == "deterministic_extraction"
        assert extracted["provenance"]["derived"] is False
        assert extracted["extraction"]["segments"]
        assert extracted["extraction"]["disclosed_bytes_source"] > 0
    finally:
        session.close()


def test_an_imported_handle_appears_in_session_stats(tmp_path):
    session, envelope, _world = _imported_session(tmp_path)
    try:
        stats = session.stats(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": "req_stats",
                "operation": "stats",
            }
        )
        assert stats["code"] == "STATS"
        totals = stats["stats"]["totals"]
        assert totals["raw_input_bytes"] >= len(IMPORTED_BODY.encode("utf-8"))
        assert any(
            record["operation_id"] == envelope["accounting_id"]
            for record in stats["stats"]["records"]
        )
    finally:
        session.close()


def test_an_imported_handle_stops_resolving_at_a_real_session_boundary(tmp_path):
    session, envelope, _world = _imported_session(tmp_path)
    pointer = envelope["pointer"]
    session.close()
    with pytest.raises(ShuntError) as raised:
        session.registry.resolve("sess-import", pointer["source_id"])
    assert raised.value.code == "SOURCE_EXPIRED"


def test_an_imported_pointer_cannot_be_spilled_again(tmp_path):
    """The internal flag is the recursion guard, and it is store-verified.

    An imported pointer is itself a small envelope, so it would never trip the size
    threshold - but the guard has to hold on identity rather than on size, because a
    payload that merely *claims* to be internal proves nothing.
    """
    session, envelope, _world = _imported_session(tmp_path)
    try:
        source_id = envelope["pointer"]["source_id"]
        assert session.registry.is_internal("sess-import", source_id) is True
        assert session.registry.is_internal("sess-import", "src_deadbeef") is False
    finally:
        session.close()


# -- manifest normalization, without a filesystem ---------------------------


def test_a_profile_cannot_launder_an_unallowlisted_schema():
    """A translator that rewrote the schema id must not be able to grant itself access."""
    document = {
        "import_contract": NATIVE_IMPORT_CONTRACT,
        "producer": {"id": "p", "manifest_schema": FOREIGN_SCHEMA},
        "artifact": {
            "path": "/tmp/x",
            "bytes": 1,
            "sha256": "0" * 64,
            "media_type": "text/plain",
        },
    }
    with pytest.raises(ShuntError) as raised:
        normalize_manifest(document, accepted_schemas=(NATIVE_IMPORT_CONTRACT,))
    assert raised.value.detail == "MANIFEST_SCHEMA_VIOLATION"


def test_a_manifest_that_is_not_an_object_is_refused():
    for document in (None, [], "text", 3):
        with pytest.raises(ShuntError) as raised:
            normalize_manifest(document, accepted_schemas=ALL_SCHEMAS)
        assert raised.value.code == "INVALID_REQUEST"


def test_an_unknown_schema_is_refused_before_the_allowlist_is_consulted():
    """A shape with no translator cannot be read at all, allowlisted or not."""
    document = {"import_contract": "some.unknown.schema.v9"}
    with pytest.raises(ShuntError) as raised:
        normalize_manifest(document, accepted_schemas=("some.unknown.schema.v9",))
    assert raised.value.detail == "MANIFEST_SCHEMA_UNKNOWN"


# -- configuration ----------------------------------------------------------


def test_enabling_the_import_boundary_without_roots_is_a_configuration_error(tmp_path):
    """An enabled boundary that trusts nothing in particular would be an allow-all."""
    for section in (
        {"enabled": True, "roots": [], "accepted_manifest_schemas": [NATIVE_IMPORT_CONTRACT]},
        {"enabled": True, "roots": [str(tmp_path / "imp")], "accepted_manifest_schemas": []},
    ):
        with pytest.raises(ShuntError) as raised:
            load_config(
                {
                    "workspace_roots": [str(tmp_path / "ws")],
                    "cache_dir": str(tmp_path / "cache"),
                    "artifact_import": section,
                },
                default_spill_dir=tmp_path / "cache",
            )
        assert raised.value.detail == "BAD_CONFIGURATION"


def test_an_import_root_may_not_contain_the_private_cache(tmp_path):
    """Otherwise a manifest could name one of our own immutable blobs as an artifact."""
    root = tmp_path / "imp"
    root.mkdir(parents=True)
    with pytest.raises(ShuntError) as raised:
        load_config(
            {
                "workspace_roots": [str(tmp_path / "ws")],
                "cache_dir": str(root / "cache"),
                "artifact_import": {
                    "enabled": True,
                    "roots": [str(root)],
                    "accepted_manifest_schemas": [NATIVE_IMPORT_CONTRACT],
                },
            },
            default_spill_dir=root / "cache",
        )
    assert raised.value.detail == "CACHE_INSIDE_IMPORT_ROOT"


def test_the_import_boundary_is_off_by_default(tmp_path):
    config = load_config(
        {
            "workspace_roots": [str(tmp_path / "ws")],
            "cache_dir": str(tmp_path / "cache"),
        },
        default_spill_dir=tmp_path / "cache",
    )
    assert config.artifact_import.enabled is False
    assert config.artifact_import.roots == ()
    assert config.artifact_import.accepted_manifest_schemas == ()
    session = ShuntSession("s", config, make_capability(artifact_import=True))
    try:
        assert session.artifact_import_enabled is False
        envelope = session.import_artifact("req", manifest={"import_contract": "x"})
        assert envelope["status"] == "error"
        assert envelope["code"] == "INVALID_REQUEST"
    finally:
        session.close()


def test_the_document_manifest_form_gets_the_same_content_check_as_the_file_form(tmp_path):
    """The two forms must not diverge on the secret policy.

    ``read_manifest_file`` runs the content marker check as a side effect of snapshotting
    the manifest. A document that never touched the filesystem skipped it, and the
    receipt's identifier patterns permit enough punctuation for a token-shaped producer id
    to be schema-valid - so a credential could have travelled into an envelope through the
    branch that did less work.
    """
    world = World(tmp_path, {"setup": {"artifact": {"content": "body\n"}}, "manifest": {}})
    session = world.session()
    try:
        importer = ArtifactImporter(
            session.registry,
            policy=session.config.import_path_policy(),
            accepted_schemas=ALL_SCHEMAS,
            limits=session.config.limits,
        )
        document = {
            "import_contract": NATIVE_IMPORT_CONTRACT,
            "producer": {
                # Schema-valid by the identifier pattern, and a credential marker.
                "id": "sk-ant-abcdef",
                "manifest_schema": NATIVE_IMPORT_CONTRACT,
            },
            "artifact": {
                "path": str(world.artifact_path),
                "bytes": 5,
                "sha256": "0" * 64,
                "media_type": "text/plain",
            },
        }
        with pytest.raises(ShuntError) as raised:
            importer.normalize(document)
        assert raised.value.code == "UNSAFE_SOURCE"
        assert raised.value.detail == "SECRET_IN_MANIFEST"
        # And the equivalent file form refuses it too, so neither branch is the weak one.
        manifest_file = world.import_root / "tainted.manifest.json"
        manifest_file.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ShuntError) as raised_file:
            importer.read_manifest_file(str(manifest_file))
        assert raised_file.value.code == "UNSAFE_SOURCE"
    finally:
        session.close()


def test_exactly_one_manifest_form_is_accepted(tmp_path):
    world = World(tmp_path, {"setup": {"artifact": {"content": "body\n"}}, "manifest": {}})
    session = world.session()
    try:
        for kwargs in ({}, {"manifest_path": "/tmp/a", "manifest": {"a": 1}}):
            envelope = session.import_artifact("req", **kwargs)
            assert envelope["status"] == "error"
            assert envelope["code"] == "INVALID_REQUEST"
    finally:
        session.close()


# -- the artifact swapped after authorization -------------------------------


def test_an_artifact_replaced_after_authorization_fails_closed(tmp_path):
    """The identity pinned at authorization time is re-checked before the bytes are used.

    This is the race the manifest hash check cannot see: the file that was stat'ed is not
    the file that gets read. ``snapshot_file`` re-checks the descriptor identity, so the
    import fails with ``SOURCE_CHANGED`` instead of snapshotting the substitute.
    """
    root = tmp_path / "imp"
    root.mkdir(parents=True)
    original = root / "artifact.log"
    original.write_text("original body\n", encoding="utf-8")

    policy = PathPolicy.from_config([str(root)])
    from context_shunt.paths import authorize

    authorized = authorize(str(original), policy)

    substitute = root / "substitute.log"
    substitute.write_text("a completely different body\n", encoding="utf-8")
    os.replace(substitute, original)

    from context_shunt.snapshot import snapshot_file

    with pytest.raises(ShuntError) as raised:
        snapshot_file(authorized)
    assert raised.value.code == "SOURCE_CHANGED"


# -- no model call on the import path ---------------------------------------


def test_the_import_path_never_calls_a_provider(tmp_path):
    world = World(tmp_path, {"setup": {"artifact": {"content": IMPORTED_BODY}}, "manifest": {}})

    def refuse(**_kwargs):
        raise AssertionError("the import path must not call a provider")

    from context_shunt.provider import HostBridgeProvider

    session = ShuntSession(
        "sess-import",
        load_config(
            {
                "workspace_roots": [str(world.workspace)],
                "cache_dir": str(world.cache),
                "artifact_import": {
                    "enabled": True,
                    "roots": [str(world.import_root)],
                    "accepted_manifest_schemas": list(ALL_SCHEMAS),
                },
            },
            default_spill_dir=world.cache,
        ),
        make_capability(artifact_import=True),
        provider=HostBridgeProvider(refuse),
    )
    try:
        envelope = session.import_artifact(
            "req_import", manifest_path=str(world.manifest_path)
        )
        assert envelope["code"] == "IMPORTED"
        assert envelope["provenance"]["derived"] is False
        assert envelope["provenance"]["label"] == "pointer_only"
        assert envelope["provenance"]["attribution_status"] == "not_applicable"
    finally:
        session.close()


# -- a store the import shares with an ordinary capture ---------------------


def test_an_imported_and_a_captured_handle_share_the_same_store(tmp_path):
    """Dedupe, TTL and quotas are the store's, not the import path's."""
    world = World(tmp_path, {"setup": {"artifact": {"content": IMPORTED_BODY}}, "manifest": {}})
    session = world.session()
    try:
        imported = session.import_artifact(
            "req_import", manifest_path=str(world.manifest_path)
        )
        source_file = world.workspace / "same.log"
        source_file.write_text(IMPORTED_BODY, encoding="utf-8")
        captured = session.register_path(str(source_file))
        # Identical bytes: one blob, two handles. The import did not get its own storage
        # path, which is the point of reusing the store rather than adding one.
        assert captured.snapshot.snapshot_id == imported["pointer"]["snapshot_id"]
        assert session.store.stats().handles == 2
        assert session.store.stats().blobs == 1
    finally:
        session.close()


def test_the_store_ddl_version_is_unchanged_by_this_feature():
    """The import boundary needed no schema migration, and this pins that.

    The DDL's ``handles.kind`` enum was deliberately not widened: the store holds no
    producer identity by design, so a new enum value would have bought a table rebuild in
    both cores and told an operator nothing the receipt does not already say.
    """
    from context_shunt.limits import DEFAULT_LIMITS, store_ddl

    assert DEFAULT_LIMITS.store_ddl_version == 2
    assert "CHECK (kind IN ('shunted_read', 'spilled_tool'))" in store_ddl()


def test_the_capture_accounting_kind_was_already_legal_in_the_ddl():
    """`capture` sat in the enum unused; the import path is the first thing that uses it.

    That is why the accounting side of this feature needed no migration either: the
    distinction an operator wants to read - adopted, not spilled - was already
    expressible.
    """
    from context_shunt.accounting import OperationKind
    from context_shunt.limits import store_ddl

    assert OperationKind.CAPTURE.value == "capture"
    assert "'capture'" in store_ddl()


# -- the registered tool surface -------------------------------------------


def test_the_import_tool_arguments_are_pinned_by_the_shared_contract():
    """The tool surface goes through the same validator as the other three.

    Nothing here is validated ad hoc in an adapter: `manifest_path` is the only accepted
    argument, and an extra one is refused rather than ignored. Both cores read this file,
    so a host cannot accept a shape the other would not.
    """
    from context_shunt.schema import validate_tool_args

    ok = {"tool": "context_shunt_import", "manifest_path": "/imports/a.manifest.json"}
    assert validate_tool_args(ok) == ok

    for bad in (
        {"tool": "context_shunt_import"},
        {"tool": "context_shunt_import", "manifest_path": ""},
        {"tool": "context_shunt_import", "manifest_path": "   "},
        {"tool": "context_shunt_import", "manifest_path": "/a", "extra": 1},
        {"tool": "context_shunt_import", "manifest_path": "/a" * 4096},
        # A manifest document is never passed through the tool surface: the tool takes a
        # path the path policy can authorize, not a document a caller composed.
        {"tool": "context_shunt_import", "manifest": {"import_contract": "x"}},
    ):
        with pytest.raises(ShuntError) as raised:
            validate_tool_args(bad)
        assert raised.value.code == "INVALID_REQUEST", bad


def test_the_hermes_adapter_only_registers_the_import_tool_when_configured(tmp_path):
    """A permanently-refusing surface in front of the model is worse than no surface."""
    import importlib.util

    module_path = REPO / "adapters" / "hermes" / "context-shunt" / "__init__.py"
    spec = importlib.util.spec_from_file_location("cs_hermes_import_probe", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    registered_modes = {mode for _schema, _handler, mode in module.TOOLS}
    assert "artifact_import" in registered_modes
    names = {schema["name"] for schema, _handler, _mode in module.TOOLS}
    assert "context_shunt_import" in names
    # And nothing in that surface writes.
    assert not any("write" in name or "patch" in name for name in names)
