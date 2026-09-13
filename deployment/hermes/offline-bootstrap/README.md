# Hermes pinned offline bootstrap

This directory owns the release-specific correction for Hermes startup. `ensure_core.py`
keeps the host's supported offline-only, no-dependency installer flow and adds unconditional
fail-closed checks so `PYTHONOPTIMIZE` cannot remove an integrity gate. The release manifest
and the path inside the marked shunt hook block advance to
`ffa218cb27a33d99659b36b1214c49c166cffa73`.

`prepare_candidate.py` is non-mutating with respect to Hermes. It requires the exact
inspected hook preimage and the exact verified release wheel, refuses hook drift, preserves
all bytes outside the marked shunt block, and produces a review directory containing both
hook images, the one-line diff, and the complete pinned release directory.
`SHA256SUMS` covers every generated candidate file so the transferred inner bundle can be
checked for corruption before any live path is touched. Authentication is rooted outside
the candidate: the exact reviewed archive and `bundle.sha256` are committed with the
reviewed deployment source, and CI checks the archive against both that outer digest and
the source/inner-checksum contract. Resolve that digest from the trusted deployment commit,
verify the local archive, transfer it over
SSH, and compare the host's independently computed archive digest with the committed
literal before extracting or executing `activate_bootstrap.py`. Do not trust a checksum
file obtained only from inside the archive.

`activate_bootstrap.py` enforces the file transaction. Run `preflight`, then `apply` with a
fresh backup path after both profiles are drained. It snapshots the hook into a root-owned
`0700` backup, repeats the live drift check, stages the root-owned/read-only release on the
same filesystem, atomically renames release first and hook second, and writes a receipt. On
any failed pre-start or startup gate, run `rollback` while the service is stopped; it
atomically restores the hook first and moves the complete candidate release into the same
backup. Never restore one without the other.
The durable preimage/state records and actual live bytes make both `apply` and `rollback`
resumable after interruption between the two atomic renames.

The production service remains stopped or drained while replacing or rolling back this
transaction. No site-packages file is copied directly: the init hook invokes the offline
installer, which verifies the wheel, installed dependencies, every packaged
`context_shunt/` file, and the unchanged set of all other installed distributions.

The virtualenv lives in the container image, not the data volume. A full rollout rollback
must discard and recreate the candidate container from the prior image after `rollback`;
that restores package state as well as the host-mounted files.
