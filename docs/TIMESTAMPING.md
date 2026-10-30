# Timestamping the evidence

Observers can only show that the evidence they checked the audit against was not adjusted afterwards if it was publicly committed to, with a time, before it mattered. `corla-observer commit-evidence` makes that commitment in one command, following the procedure first used for the July 2026 primary and described in the public audit center mirror's [evidence-timestamping.md](https://github.com/nealmcb/auditcenter/blob/main/observerfiles/evidence-timestamping.md), which also explains, for newcomers, why this matters and how to check a commitment in a browser.

## What it does

```
corla-observer commit-evidence --workspace DIR --election 2026/general --out-repo MIRROR \
    [--subdir 2026/general/observerfiles/evidence] [--actor coordinator:ID] [--no-push] \
    [--include-files] [--cosign-key cosign.key] [--no-ots] [--no-cosign] [--tool-timeout 300]
```

1. **Collects the evidence set** from the workspace: every file the data map uses (fetched audit center files, the counties' redacted CVRs, manifests and pull lists, files supplied by an override, adapter outputs), the overrides file last applied, the source code of every adapter whose output is used, the latest prediction of the selections, and each county's latest extract with its stored page index (the hashes printed on each page, recorded as `page_index_sha256` in the `extract.built` event). Each stored file is re-hashed on the way; a missing or corrupt one stops the command rather than being left out. Files the data map ignores are not listed.
2. **Writes a new SHA256SUMS file** in MIRROR/SUBDIR (by default `ELECTION/observerfiles/evidence`): `SHA256SUMS.txt` the first time, then `SHA256SUMS.v2.txt`, `SHA256SUMS.v3.txt` and so on. A version number that exists, or ever existed in the repository's history, is never reused, and the file is created exclusively, so no SUMS file is ever overwritten. It has one `<sha256>  <path>` line per file in sorted path order, the format `sha256sum` reads, after a header of `#` lines: the election, the time (UTC), the event log's head (its last sequence number and hash, which through the hash chain commits to every event before it), the software (`corla-observer 0.1.0 (f855b1a1c2d3)`: the version and the git commit it runs from, `+dirty` when the checkout has changes not in that commit, or `(commit unknown)`; `corla_observer.version`) and the number of files. Paths are `auditcenter/...` for fetched files (their path under the Audit Center's RLA directory), `uploads/COUNTY/KIND/NAME` for uploads, `supplied/`, `adapter-output/`, `adapters/`, `overrides/` and `derived/` (the prediction, the extracts and their page indexes, `extract-COUNTY-pages.json`) for the rest.
3. **Stamps it with OpenTimestamps** (`ots stamp`), making `SHA256SUMS*.txt.ots`, if the `ots` command is installed. The proof is pending until a calendar server's Bitcoin transaction confirms, usually within an hour or two.
4. **Signs it with Sigstore** (`cosign sign-blob --yes --bundle`), making `SHA256SUMS*.txt.cosign-bundle`, if `cosign` is installed: keyless by default, which binds the signature to an OIDC identity (a Google, GitHub or Microsoft login) and records it in the public Rekor transparency log; or with `--cosign-key`, a key file (whose public key must then be published for anyone to check it). Keyless signing needs a browser or device-code login, or an identity token in the environment, so run it where one is possible; the tool's output, including any login URL, is shown as it runs.
5. **Commits** the SUMS file and its proofs in MIRROR with a message giving the event log head, the software version and each step's outcome, and **pushes** the current branch unless `--no-push` is given or the repository has no remote. The evidence files themselves are copied and committed only with `--include-files`, and even then never an uploaded CVR without a passing redaction check on record (one that failed is never stored, so cannot be copied).
6. **Records** an `evidence.committed` event in the workspace's event log, with the SUMS file's hash and version, each step's outcome, the proofs made, the Rekor log index if cosign reported one, the commit hash, whether it was pushed, and the event log head written in the header. A SUMS file that was written but could not be committed is recorded as `evidence.commit_failed`.

Every step's outcome is printed: DONE, PENDING (an OpenTimestamps proof awaiting Bitcoin), SKIPPED (the tool is not installed, or was turned off) or FAILED (with the tool's output). Nothing is skipped silently. The command exits 0 when everything that ran succeeded, 1 when a step failed (the commit is still made with whatever proofs succeeded), and 2 when nothing was committed. No local paths are written into the SUMS file, the commit message or the event.

Later, once the calendars have confirmed:

```
corla-observer upgrade-proofs --out-repo MIRROR --workspace DIR [--subdir SUBDIR] [--no-push] [--all]
```

runs `ots upgrade` on every tracked `.ots` file that does not yet hold a Bitcoin attestation (`--all`: on every one), commits the upgraded proofs, pushes as above, and records an `evidence.proofs_upgraded` event listing what was upgraded and what is still pending, with the Bitcoin block height of each completed proof (shown by the photo check, docs/PHOTO_CHECK.md). Run it an hour or more after each commitment, and again until nothing is pending. It exits 1 if `ots` is not installed or an upgrade failed.

## When to run it

- **Before the seed is rolled,** for the inputs: once every county's manifest and redacted CVR has arrived and the data map is complete, and in any case well before the roll, so that the OpenTimestamps proof's Bitcoin block (not only the calendar's receipt) predates the dice. A file that arrives late gets a new version; the earlier versions stay valid commitments to what was there then.
- **When the extract is ready,** for the predicted selections and each county's extract, before the first ballot is read.
- **At the end of each audit day,** which also anchors the event log (observers' entries included) through the head in the header.
- **When the audit closes.**

Each run is cheap, so when in doubt, run it again: a new version is better than a gap.

## How anyone can check it

Anyone with a copy of the mirror can check a commitment without this software, as the mirror's evidence-timestamping.md explains step by step on Mac, Windows and Linux: `sha256sum --check --ignore-missing SHA256SUMS.v2.txt` for the files they have (lines starting with `#` are ignored), `ots verify SHA256SUMS.v2.txt.ots` or opentimestamps.org for the Bitcoin timestamp, and `cosign verify-blob --bundle SHA256SUMS.v2.txt.cosign-bundle --certificate-identity EMAIL --certificate-oidc-issuer ISSUER SHA256SUMS.v2.txt` (or `--key cosign.pub`) or search.sigstore.dev for the Rekor entry.

With this software:

```
corla-observer verify-evidence MIRROR/SUBDIR/SHA256SUMS.v2.txt [--base DIR] [--workspace DIR] \
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
