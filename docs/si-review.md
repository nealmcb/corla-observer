# Software independence in corla-observer: a sketch for review

corla-observer supports independent public observation of Colorado's risk-limiting audits. It
replicates the random selection, prints or displays each selected ballot's expected votes (from the
county's redacted CVR) for observers to check as the audit board reads the ballot aloud, and
records what observers saw. It is in alpha testing for the November 2026 pilots.

We want the observers' findings to be **software-independent**: an undetected error or compromise
in corla-observer should not be able to cause an undetected change in what observers report about
the audit. Below are the mechanisms we have engineered in, the procedures we think are needed to
use them, and our arguments. We would be grateful for validation or refutation, especially of the
claims marked **Claim**.

The scope is the observers' findings, not the audit's outcome: the paper ballots and CORLA's own
process are outside it. We take SHA-256, the signature schemes used, the public timestamping
services, the public seed ceremony, and the observers' own eyes, ears and paper as trusted.

## Three things the tool could get wrong

1. **The reference:** which ballots are selected, and the votes the CVR records for each. A wrong
   reference misleads observers toward false matches or false discrepancies.
2. **The record:** what observers entered. Entries could be lost, altered or invented.
3. **The summary:** the dashboard and reports drawn from the record.

## 1. The reference

**Built in:**
- **Inputs committed before the seed.** Every input file (the audit center's files, the counties'
  redacted CVRs and manifests) is stored by SHA-256. Before the seed is rolled, a versioned
  `SHA256SUMS` listing is signed (Sigstore), entered in a public transparency log (Rekor), and
  timestamped (OpenTimestamps) in a public git repository.
- **Deterministic derivation.** The selection and the extract are a deterministic function of the
  committed inputs, the public seed and the county's pull list, computed by published, versioned
  code. The commit of the running code is recorded at each server start and in each extract built.
- **Independent replication.** Before the first ballot is read, corla-observer compares its own
  prediction of the selection (a reimplementation of CORLA's) with CORLA's posted selections and
  with the county's pull list, and records any disagreement. A separately written implementation in
  another project recomputes the selection independently; it is run on its own, not by
  corla-observer.
- **The extract identifies itself.** Every printed page and every screen shows the seed, the
  inputs' hashes, the extract's hash and its position, plus a QR code of the page's own hash. A
  photo of a printed page can be checked against the committed extract.

**Procedures:**
- Observers note the seed at the public ceremony and compare it with the seed on their printout or
  screen.
- Someone outside the deployment recomputes the selection from the committed inputs, with the
  separately written implementation, and checks it against the county's pull list and
  corla-observer's extract.
- Before the audit, each printed page, or each screen's extract hash, is checked against the
  published commitment.

**Claim:** for an error in the reference to go undetected, it must appear identically in two
independently written implementations and agree with CORLA's own pull list, or the commitments
must be broken.

**Weaknesses:**
- **Shared inputs.** All three results read the same input files, so the argument covers the
  computation, not whether the inputs are the right ones. Committing the inputs before the seed
  stops them being changed afterwards to fit, but not their being wrong to begin with.
- **The expected votes need checking too.** They come from the redacted CVR. They are verifiable
  only by someone who recomputes the extract from the committed CVR, which the procedure above
  includes but which is easy to skip.

## 2. The record, by mode of observation

Several observers may cover one audit board, each in their own way, and their records are compared
afterwards. The modes differ in how much they depend on the software.

### Paper (implemented)

The observer marks the printed extract by hand, then initials and dates it. Their marked pages are
photographed at the end of the day; the photos are stored by hash and recorded in the event log,
which is anchored daily.

**Claim:** paper observation is software-independent by construction, provided the printed
reference is checked as in section 1. The software only produced the reference, and the observer's
marks never pass through it.

**Weaknesses:**
- A photo proves only that the marked page existed when its hash was committed. It doesn't prove
  when the marks were made.
- Any transcription of paper marks into a digital summary is outside this argument unless it is
  done twice, independently.

### Spreadsheet (partly implemented)

The extract is available as a CSV file, with its provenance (seed, input hashes, the extract's
hash) and escaped against formula injection. The observer can mark it in their own spreadsheet. An
online observer can also download their own record as CSV.

**Not yet implemented:**
- importing an observer's completed spreadsheet;
- signing it;
- comparing it with the other records of the same board.

**Proposed procedure:**
- The completed file is the observer's record. At the end of the session the observer hashes it
  and commits the hash outside the deployment, for example in a timestamped email or an
  OpenTimestamps proof.
- The file then joins the comparison with the board's other records.

**Weaknesses:**
- Spreadsheets autocorrect, and the file can be edited until its hash is committed.
- The observer's own computer is trusted.

### Online entry (implemented, piloting)

The observer's browser makes a signing key pair (Web Crypto, private key non-extractable) when the
observer link is first opened.

Every entry is signed on the device: each confirmation, contest outcome, note or correction. The
signature covers:
- the observer, county, board and ballot;
- the content;
- the device's time and a per-device sequence number;
- the hash of the device's previous entry.

Entries are kept on the device, including while it is offline, and sent when it is online. The
server answers each entry with a receipt that it signs: the entry's hash, its place in the server's
hash-chained log, and the log's current head. The device keeps every receipt. The observer can
download the whole record at any time.

A published checker compares a downloaded record with the published log. It reports:
- entries missing from the log or altered;
- entries in the log, under that observer's device, that the device did not sign;
- breaks in the chain.

The log's head is anchored at the end of each audit day.

When one observer uses several devices, each device's entries are kept and differences between
devices are shown to the observer.

**Claim:** with these records, the server cannot forge or alter an observer's entry undetectably,
because it doesn't hold the device's key. It cannot drop or reorder entries without contradicting
receipts the observer holds. It cannot show different observers different logs (equivocation)
without the heads in their receipts, or the published anchors, disagreeing.

**Procedures:**
- Observers keep their device record and run the checker against the published log.
- Observers, or someone collecting their records, compare the log heads in their receipts with the
  published anchors.

**Weaknesses (the ones we most want reviewed):**
- **The page's code comes from the server.** A compromised server could serve JavaScript that signs
  something other than what the observer entered, or shows them a wrong reference. The code is
  published with a version and hash, and served with subresource integrity. But that helps only if
  someone checks that the code served matches the published code. The observer's own device and
  browser are trusted.
- **Receipts protect only those who check them.** We don't know how many observers must run the
  checker for the detection probability to be meaningful.
- **The expected votes are shown by the same software.** The screen shows the extract's hash, which
  can be compared with the published commitment, but in practice few observers will compare it.

### The observer client as a browser extension (built, in testing)

Our answer to the first weakness is to take the page's code away from the server altogether. The
extension is built and tested in Chromium, on a development branch; it is not yet in use at an
audit.

**What changes:**
- The observer client (the same code as the served page) is packaged as a browser extension the
  observer installs. Its page is rendered from the server's own template at build time, so the
  two cannot drift apart.
- The device's signing key is generated and kept in the extension's own storage, which no web page
  can reach.
- The server only supplies data (the ballots and the extract) and accepts signed entries. Our test
  confirms the server is never asked for the page or its code.

**What the extension checks, as entries are sent:**
- **Each receipt's signature,** with the server's receipt key, which the extension pins the first
  time it sees that server. A different key later is an alert.
- **That each receipt is for the payload this device signed.**
- **The log heads the receipts state,** for consistency with each other, kept across sessions.

So every observer who uses it checks their receipts as they go, which also bears on the second
weakness.

**And against the published commitments,** whose public folder the observer is given by their
coordinator, on paper or by email, never by the server:
- **The extract shown** must be one the commitments list.
- **The receipt key** must be the one the commitments list. The key is now committed with the
  evidence.
- **One log.** Every head its receipts stated, and every anchored head in the commitments, must lie
  on one log. The extension fetches the log's lines between them, recomputes each line's hash from
  its bytes, and follows the chain. The server cannot fake a chain through points it has already
  committed to.

Any failure is an alert asking the observer to keep their record and tell their coordinator. Our
tests check each check passes on an honest server, and that a forged receipt and a false anchor are
each caught.

**Inspecting and managing it:**
- The extension's source is in the repository.
- Its build is reproducible: two builds of a commit are byte-identical. It lists every file's
  SHA-256, and shows its identity (the hash of that list) for comparison with a published release.
- An observer can install it from the unpacked files, which they can read and which never update
  themselves.
- It asks for access only to the server an observer link names (and to GitHub, when given the
  commitments' folder).

**Claim:** with the extension:
- A compromised server can no longer sign anything in the observer's name.
- It cannot show an extract or use a receipt key that differs from the commitments without the
  extension saying so.
- It cannot show this observer a log inconsistent with the published anchors without the extension
  saying so.

It can still withhold or delay data, which the observer sees. It can also show different observers
different logs between two anchors, which shows only once the next anchor is published and checked,
or when observers compare the heads in their receipts.

**Not yet done:**
- testing in Firefox and on Android;
- signed releases, published like the evidence commitments;
- carrying the checks' results into the observer's downloadable record.

**Limits:**
- **Phones.** Extensions work in Firefox on Android. Chrome on Android doesn't support them, and
  Safari on iPhone supports them only inside an App Store app. Observers on those phones keep the
  served page, and paper and receipt checking carry the argument for them.
- **The device is trusted.** The observer's browser and device remain trusted.
- **The checks need a current browser.** The receipt checks need Ed25519 in Web Crypto, in
  current Chrome and Firefox; an older browser says it cannot check them.

**A lighter alternative we considered:** an extension that only checks the served page.
- In Chrome it cannot see the bytes the page actually ran, so the check is best effort.
- In any browser it can only detect tampered code after the code has run and may have signed in the
  observer's name.

We therefore moved the client into the extension.

## 3. The summary, and operations

- **Recomputable.** The dashboard's figures are recomputable from the published event log, which
  has a standalone verifier anyone can run without this software.
- **Restarts and updates are recorded.** Every start and stop of the server and the scheduler is
  recorded, with the software's commit, so a restart, an update or a crash shows in the log.
- **Resets are linked, and are for testing only.** A practice workspace's reset starts its new log
  with an event naming the old log's head and the archive's hash. Resets exist only on testing
  deployments.

## When the observers' findings should be trustworthy

**Claim:** the findings are software-independent when all of the following hold:
1. The inputs were committed, and the commitments anchored, before the seed was rolled.
2. Someone recomputed the selection and the extract from the committed inputs with an independent
   implementation, and they agree with each other and with the county's pull list.
3. Each audit board has at least one paper observer whose printout was checked against the
   commitment, or its online observers' records were checked against the published log by the
   checker.
4. The event log is published and anchored at least daily, and someone has verified it.
5. For online entry, the observers used the extension client, or someone confirmed that the code
   served matched the published version.

If online entry alone covers a board, and nobody checks the receipts or the served code, the
findings for that board rest on trusting the server.

## Speculation: could CORLA itself use these techniques?

CORLA's own record is the audit boards' interpretations of the sampled ballots, entered in a web
application the state serves. The same techniques could apply to it. This is speculation, offered
for discussion.

**What could be added:**
- **Commit to inputs before the seed.** CORLA already records hashes of the counties' uploaded
  files. Signing and timestamping a list of them in public, before the seed is rolled, would fix the
  inputs as our commitments do.
- **A published, hash-chained log of the audit.** Every upload, draw, interpretation and risk
  calculation, published as it happens and anchored at least daily. Anyone could then recompute the
  sample and the risk measurement from the log.
- **Signed interpretations.** Each audit board's entries signed with a key the board holds, made
  and kept in a client that CORLA's server does not supply (an extension or an installed client, as
  above). Each entry would get a receipt carrying the log's head, and the board's own record could
  be checked against the published log.
- **Interpretations published per ballot, during the audit.** Observers could then compare each
  sampled ballot's recorded interpretation with what they heard read aloud, as soon as it is
  entered rather than in the final reports.

**The claims these might support:**
1. CORLA's recorded interpretation of each sampled ballot is the one its audit board entered and
   read aloud. This rests on the board's signatures, on the observers' records, and on the paper
   ballots themselves, which remain available.
2. The sample, and the risk measured from the interpretations, follow from published inputs and the
   public seed, whatever the software did. This rests on independent recomputation from the
   published log.

**Procedures and costs:**
- Key management for the audit boards, and a client that state IT would have to distribute and
  support.
- Observers or independent parties who actually run the checks.
- Care about ballot privacy in what is published per ballot, though the redacted CVRs are already
  public.

None of this replaces the paper ballots. It would make the software's part of the audit checkable,
as the paper makes the outcome checkable.

## Questions for review

1. Is agreement between two independent implementations and CORLA's pull list enough for the
   reference, given that all three read the same inputs? What else would you require?
2. For paper, is photographing marked pages at the end of the day enough? Or is a commitment closer
   to the marking needed?
3. For online entry, does moving the client into a browser extension, as built above, answer
   the problem of the server supplying the page's code? What would you require of how it is built,
   distributed and checked? And what is a practical answer for phones that cannot run extensions?
4. What fraction of online observers must check their receipts for detection to be meaningful?
   Does publishing the heads in observers' receipts, or anchoring more often, change that?
5. What is the minimal procedure that makes spreadsheet observation software-independent?
6. Are we missing a failure mode, or relying on something we haven't listed as trusted?
7. Would any of the techniques sketched for CORLA itself be worth proposing, and with what claims?
