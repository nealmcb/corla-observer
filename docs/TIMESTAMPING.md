# Timestamping the evidence

Observers can only show that the evidence they checked the audit against was not adjusted afterwards if it was publicly committed to, with a time, before it mattered. `corla-observer commit-evidence` makes that commitment, and `corla-observer publish-evidence` publishes it, following the procedure first used for the July 2026 primary and described in the public audit center mirror's [evidence-timestamping.md](https://github.com/nealmcb/auditcenter/blob/main/observerfiles/evidence-timestamping.md), which also explains, for newcomers, why this matters and how to check a commitment in a browser.

## What it does

```
corla-observer commit-evidence --workspace DIR --election 2026/general --out-repo MIRROR \
    [--subdir 2026/general/observerfiles] [--actor coordinator:ID] \
    [--include-files] [--cosign-key cosign.key] [--no-ots] [--no-cosign] [--tool-timeout 300]
```

1. **Collects the evidence set** from the workspace: every file the data map uses (fetched audit center files, the counties' redacted CVRs, manifests and pull lists, files supplied by an override, adapter outputs), the overrides file last applied, the source code of every adapter whose output is used, the latest prediction of the selections, and each county's latest extract with its stored page index (the hashes printed on each page, recorded as `page_index_sha256` in the `extract.built` event). Each stored file is re-hashed on the way; a missing or corrupt one stops the command rather than being left out. Files the data map ignores are not listed.
2. **Writes a new SHA256SUMS file** in MIRROR/SUBDIR (by default `ELECTION/observerfiles`): named for its version and the time it was written, in UTC, without colons so that it can be checked out on Windows: `SHA256SUMS-v1-20261106T193720Z.txt`, then `SHA256SUMS-v2-…` and so on. The time in the name is the one in its header, by the writer's clock; it proves nothing on its own, and the proof of time is the OpenTimestamps proof and the Sigstore entry. Files named in the earlier form, `SHA256SUMS.txt` and `SHA256SUMS.v2.txt` (as for the July 2026 primary), are still read. A version number that exists, that ever existed in the repository's history, or that the event log records, is never reused, and the file is created exclusively, so no SUMS file is ever overwritten. It has one `<sha256>  <path>` line per file in sorted path order, the format `sha256sum` reads, after a header of `#` lines: the election, the time (UTC), the event log's head (its last sequence number and hash, which through the hash chain commits to every event before it), the software (`corla-observer 0.1.0 (f855b1a1c2d3)`: the version and the git commit it runs from, `+dirty` when the checkout has changes not in that commit, or `(commit unknown)`; `corla_observer.version`) and the number of files. Paths are `auditcenter/...` for fetched files (their path under the Audit Center's RLA directory), `uploads/COUNTY/KIND/NAME` for uploads, `supplied/`, `adapter-output/`, `adapters/`, `overrides/` and `derived/` (the prediction, the extracts and their page indexes, `extract-COUNTY-pages.json`) for the rest.
3. **Stamps it with OpenTimestamps** (`ots stamp`), making `SHA256SUMS*.txt.ots`, if the `ots` command is installed. The proof is pending until a calendar server's Bitcoin transaction confirms, usually within an hour or two.
4. **Signs it with Sigstore** (`cosign sign-blob --yes --bundle`), making `SHA256SUMS*.txt.cosign-bundle`, if `cosign` is installed: keyless by default, which binds the signature to an OIDC identity (a Google, GitHub or Microsoft login) and records it in the public Rekor transparency log; or with `--cosign-key`, a key file (whose public key must then be published for anyone to check it). Keyless signing uses Sigstore's device flow: the output shows a link and a code ("Enter the verification code … in your browser at: https://oauth2.sigstore.dev/auth/device…"); the signer opens the link within five minutes, from any browser, checks the code and logs in, and nothing is typed back. So it can run on a server with no browser, and the signer need not be at its terminal; an identity token in the environment also works.
5. **Copies the evidence files themselves** next to it, only with `--include-files`, and even then never an uploaded CVR without a passing redaction check on record (one that failed is never stored, so cannot be copied).
6. **Records** an `evidence.committed` event in the workspace's event log: the SUMS file's hash and version, each step's outcome, the proofs made, the Rekor log index if cosign reported one, the copied files, and the event log head written in the header. That is everything fixed from this moment on. It makes no git commit, and records none: the files wait in MIRROR's working tree to be published. A SUMS file written whose stamping, signing or copying then failed is recorded as `evidence.commit_failed`.

Every step's outcome is printed: DONE, PENDING (an OpenTimestamps proof awaiting Bitcoin), SKIPPED (the tool is not installed, or was turned off) or FAILED (with the tool's output). Nothing is skipped silently. The command exits 0 when everything that ran succeeded, 1 when a step failed (the commitment is still recorded with whatever proofs succeeded), and 2 when nothing was written. No local paths are written into the SUMS file or the event.

## Publishing it

Once the OpenTimestamps proof has a Bitcoin confirmation, usually an hour or two later:

```
corla-observer publish-evidence --out-repo MIRROR --workspace DIR \
    [--title "2026 General: Auditcenter risk limit with digital timestamp"] [--sums NAME] \
    [--dry-run] [--allow-pending] [--actor coordinator:ID]
```

publishes the oldest commitment not yet published (or the one `--sums` names). It checks that the SUMS file in MIRROR has the hash the log recorded, completes its OpenTimestamps proof (`ots upgrade`; it refuses while the proof is still pending, unless `--allow-pending`), and makes **one commit** on MIRROR's current branch holding the SUMS file, its proofs and any copied files. The commit's message is headed by `--title` (or "Observer evidence for ELECTION: NAME (N files)") and gives the log head, the software, the Bitcoin block, the Rekor log index, for a keyless signature its signer and the `cosign verify-blob` command that checks it, and the number of the `evidence.committed` event. It pushes the commit, and only then records an `evidence.published` event: the SUMS file's hash and the committed event's number, the repository's address, the branch, the pushed commit, the files, the proof's state and the Rekor index.

So the log names a git commit only once it is public, and a pushed commit is never rewritten. `--dry-run` shows the message and files and commits, pushes and records nothing. If the push fails, nothing is recorded; run it again once the cause is fixed, and it pushes the commit already made.

Commitments recorded before publishing was its own step name a git commit made at the time. If such a commit was never pushed, the `evidence.published` event names it in `replaces_unpublished_commits`.

`upgrade-proofs` remains for proofs committed with a pending proof, as for the July 2026 primary:

```
corla-observer upgrade-proofs --out-repo MIRROR --workspace DIR [--subdir SUBDIR] [--no-push] [--all]
```

runs `ots upgrade` on every tracked `.ots` file that does not yet hold a Bitcoin attestation (`--all`: on every one), commits the upgraded proofs, pushes them, and records an `evidence.proofs_upgraded` event listing what was upgraded and what is still pending, with the Bitcoin block height of each completed proof (shown by the photo check, docs/PHOTO_CHECK.md). It exits 1 if `ots` is not installed or an upgrade failed.

## When to run it

- **Before the seed is rolled,** for the inputs: once every county's manifest and redacted CVR has arrived and the data map is complete, and in any case well before the roll, so that the OpenTimestamps proof's Bitcoin block (not only the calendar's receipt) predates the dice. A file that arrives late gets a new version; the earlier versions stay valid commitments to what was there then.
- **When the extract is ready,** for the predicted selections and each county's extract, before the first ballot is read.
- **At the end of each audit day,** which also anchors the event log (observers' entries included) through the head in the header.
- **When the audit closes.**

Each run is cheap, so when in doubt, run it again: a new version is better than a gap.

## How anyone can check it

Anyone with a copy of the mirror can check a commitment without this software, as the mirror's evidence-timestamping.md explains step by step on Mac, Windows and Linux: `sha256sum --check --ignore-missing SHA256SUMS-v2-20261116T160000Z.txt` for the files they have (lines starting with `#` are ignored), `ots verify SHA256SUMS-v2-20261116T160000Z.txt.ots` or opentimestamps.org for the Bitcoin timestamp, and `cosign verify-blob --bundle SHA256SUMS-v2-20261116T160000Z.txt.cosign-bundle --certificate-identity EMAIL --certificate-oidc-issuer ISSUER SHA256SUMS-v2-20261116T160000Z.txt` (or `--key cosign.pub`) or search.sigstore.dev for the Rekor entry.

With this software:

```
corla-observer verify-evidence MIRROR/SUBDIR/SHA256SUMS-v2-20261116T160000Z.txt [--base DIR] [--workspace DIR] \
    [--certificate-identity EMAIL --certificate-oidc-issuer ISSUER | --cosign-key cosign.pub] [--quiet]
```

recomputes the hash of every listed file found under `--base` (by default the SUMS file's directory) or, by hash, in a workspace, and runs `ots verify` and `cosign verify-blob` where those tools are installed. It reports each file as OK, MISMATCH or MISSING (not available locally), and each proof as OK, PENDING, SKIPPED (no tool, or no Bitcoin node for `ots verify`) or FAILED. Without an expected identity, a keyless signature is checked against the Rekor log but its signer is not, and the report says so. It exits 1 if a file differs, a line is malformed or a proof fails. Paths in a SUMS file that are absolute or contain `..` are refused, not read.

To check that the commitment covers what was used, compare its header's event log head with the published event log (`corla-observer log verify`, and `docs/EVENT_LOG.md` for doing so independently), and recompute the selections from the committed inputs and the seed.

## Limits

- A commitment proves that the listed bytes existed by the time of the Bitcoin block or Rekor entry. It does not prove they are the right files, that the county's files are genuine, or that nothing else existed: that is what the data map, the change flags and the three-way comparison of selections are for.
- Without `--include-files` only hashes are published. Anyone can then confirm a file they obtain later, but not obtain the files from the mirror. Fetched audit center files are already public, and the mirror usually holds them; the counties' redacted CVRs are published only when the project decides to.
- An OpenTimestamps proof is only as early as the Bitcoin block that confirms it, not the moment of stamping, so stamp early. `ots verify` needs a Bitcoin node (or use opentimestamps.org).
- The Sigstore signature says who signed only with keyless signing and only if the verifier checks the expected identity. A key file proves only that the same key signed each version.
- The git commit's date is set by the committer and proves nothing on its own; the push to GitHub adds a public copy, not a trusted time.
- The event log head commits to the log as it was; events after it are anchored by the next commitment.
- If `ots` or `cosign` is not installed on the server, the command still commits the SUMS file and reports the step as skipped. Install both before an audit (`pip install opentimestamps-client`; cosign from its releases page or the distribution's package) and run a commitment to a scratch repository to check them.
