# Software independence in corla-observer: a sketch for review

corla-observer supports independent public observation of Colorado's risk-limiting audits. It
replicates the random selection, prints or displays each selected ballot's expected votes (from the
county's redacted CVR) for observers to check as the audit board reads the ballot aloud, and
records what observers saw. It is in alpha testing for the November 2026 pilots.

We want the observers' findings to be **software-independent**: an undetected error or compromise
in corla-observer should not be able to cause an undetected change in what observers report about
the audit. We are not counting independent implementations. There is one implementation and a set
of procedures, and the aim is that every claim rests on evidence that anyone *could* check. What we
most want to learn is which of those checks are the most valuable, and which are sufficient. Below
are the mechanisms we have built in, how we think they should be used, and our arguments. We would
be grateful for validation or refutation, especially of the claims marked **Claim**.

The scope is the observers' findings, not the audit's outcome: the paper ballots and CORLA's own
process are outside it. We take SHA-256, the signature schemes used, the public timestamping
services, the public seed ceremony, and the observers' own eyes, ears and paper as trusted.

## Three things the tool could get wrong

1. **The reference:** which ballots are selected, and the votes the CVR records for each. A wrong
   reference misleads observers toward false matches or false discrepancies.
2. **The record:** what observers entered. Entries could be lost, altered or invented.
3. **The report:** what the record is said to show: how many ballots, how many discrepancies, and
   in which contests.

## The central check: the audit's report against the observers'

At the end of an audit, CORLA publishes a discrepancy report: for each contest on each sampled
ballot where the audit board's interpretation differs from the CVR, the county, the contest, the
ballot's imprinted ID, both choices and a reason (audit board error, wrong ballot, voter intent).
Observers compare the same two things, what the board reads aloud against what the CVR records, and
corla-observer produces their findings as an **observation report** in the same terms: per ballot, a
match or a discrepancy, and, where the observer entered contest by contest, which contests.

`corla-observer report corla` compares the two, ballot by ballot:
- **observer only:** the observer heard a discrepancy that CORLA does not report;
- **CORLA only:** CORLA reports a discrepancy on a ballot the observer heard match;
- **contests differ:** both found one, in different contests;
- **not observed:** CORLA reports a discrepancy on a ballot of the observer's list that the
  observer did not record.

**Claim:** an error in the reference or the record that changes an observer's finding on a ballot
the board read aloud shows up as one of these disagreements. A wrong expected vote turns a match
into a reported discrepancy that CORLA lacks, or hides one that CORLA reports. A lost, altered or
invented entry does the same. The disagreement is then resolved from the evidence: the committed
CVR, and if need be the paper ballot.

A disagreement is not necessarily a software fault. A board can rightly settle on a different
reading of a voter's intent than the one first read aloud, and the observers are there to notice
when what is entered differs from what was said. Either way it is a ballot to look at.

**Weaknesses:**
- **Shared inputs.** CORLA compares against the county's CVR, and our reference comes from the
  redacted version of the same CVR. An error already in the county's CVR is in both and does not
  show here. That is what the audit itself is for.
- **Redaction.** Votes the redaction hides cannot be checked by observers at all.
- **Timing.** The discrepancy report comes with CORLA's final reports, possibly days after the
  audit and possibly only as a PDF. Until then the comparison waits; observers' reports can
  meanwhile be compared with each other.
- **Which ballots.** Ballots selected but never observed are outside this check, so the selection
  needs its own check (section 1).

## 1. The reference

**Built in:**
- **Inputs committed before the seed.** Every input file (the audit center's files, the counties'
  redacted CVRs and manifests) is stored by SHA-256. Before the seed is rolled, a versioned
  `SHA256SUMS` listing is signed (Sigstore), entered in a public transparency log (Rekor), and
  timestamped (OpenTimestamps) in a public git repository.
- **Deterministic derivation.** The selection and the extract are a deterministic function of the
  committed inputs, the public seed and the county's pull list, computed by published, versioned
  code. The commit of the running code is recorded at each server start and in each extract built.
- **The extract identifies itself.** Every printed page and every screen shows the seed, the
  extract's hash and its position, and a QR code of the page's own hash.

**Checks that could be made, roughly in order of value:**
1. **The seed.** Observers note the seed at the public ceremony and compare it with the seed on
   their printout or screen.
2. **The selection.** corla-observer compares its prediction of the selection (a reimplementation
   of CORLA's) with CORLA's posted selections and the county's pull list, and records any
   disagreement. Anyone can rerun it from the committed inputs. A separately written implementation
   exists too, and is a useful extra check, but we don't make it a requirement.
3. **The expected votes.** For an RLA it is enough that the CVR's outcomes are the unofficial
   outcomes being audited: the same winners. The totals check compares, for each targeted contest
   and county, the CVR's tally of the last winner and first loser with the results, allowing for
   the redacted rows, and flags any other difference. Stricter checking is good, but need not be
   exact: redaction blanks some votes.
4. **The extract each observer follows.** The browser extension (section 2) checks every extract it
   shows against the published commitments, automatically. A printed page can be checked by
   photographing it: the photo check reads the page and its QR code, and finds it in the committed
   extract. Checking every page before the audit is not practical, so pages are spot-checked, and
   errors that matter show in the central check anyway.

**Weakness:** the argument covers the computation, not whether the inputs are the right ones.
Committing the inputs before the seed stops them being changed afterwards to fit, but not their
being wrong to begin with.

## 2. The record and its report, by mode of observation

Several observers may cover one audit board, each in their own way. Each mode yields the same
observation report, computed by one definition (`corla_observer.obsreport`), so the reports can be
compared with each other (`corla-observer report compare`) and with CORLA's.

### Paper (implemented)

The observer marks the printed extract by hand, then initials and dates it. The marked pages are
photographed at the end of the day; the photos are stored by hash and recorded in the event log,
which is anchored daily. For the report, the marks are transcribed into the extract's spreadsheet,
below.

**Claim:** the marks themselves never pass through the software. What does is the transcription,
and its report is checkable against the paper and against CORLA's report.

**Weaknesses:**
- The transcription is a step where errors can enter. Doing it twice, independently, and comparing
  the two reports catches them.
- A photo proves only that the marked page existed when its hash was committed. It doesn't prove
  when the marks were made.

### Spreadsheet (implemented)

The extract is available as a spreadsheet, with its provenance (the seed, the input hashes, the
extract's hash) and escaped against formula injection. It has two blank columns per ballot, `match`
and `heard instead`, which the observer fills in. `corla-observer report show` reads the marked file
(CSV or workbook) as an observation report, the same report the same observations entered online
would give. A mark that can't be read is an error naming the row, never a guess.

**Procedure:** at the end of the session the observer hashes the file and commits the hash outside
the deployment, for example in a timestamped email or an OpenTimestamps proof. The report is then
compared with the board's other observers' and with CORLA's.

**Weaknesses:**
- We don't cover errors in the spreadsheet software, the operating system or the observer's
  computer. Spreadsheets autocorrect.
- The file can be edited until its hash is committed.

### Online entry (implemented, piloting)

The observer's browser makes a signing key pair (Web Crypto, private key non-extractable). Every
entry is signed on the device, over the observer, county, board, ballot, content, the device's time
and sequence number, and the hash of the device's previous entry. Entries are kept on the device,
including while offline. The server answers each with a signed receipt: the entry's hash, its place
in the server's hash-chained log, and the log's current head. The observer can download the whole
record, and a published checker compares it with the published log.

**The report, three ways.** The server signs each device's observation report, computed from the
entries its log holds for that device. The extension (below) computes the same report from the
device's own records and compares the two, raising an alert if they differ. And from a downloaded
record, `corla-observer report` computes it again with no browser at all, to compare with the
server's signed report.

**Claim:** the server cannot forge or alter an observer's entry undetectably, because it doesn't
hold the device's key. It cannot drop or reorder entries without contradicting receipts the
observer holds. It cannot report the observer's findings differently from their own entries
without contradicting its own signed report or the device's. It cannot show different observers
different logs without the heads in their receipts, or the published anchors, disagreeing.

**Weaknesses:**
- **The page's code comes from the server**, unless the observer uses the extension.
- **The browser and device are trusted.** We don't cover errors in the browser or the device.
- **Receipts protect only those who check them.** With the extension every observer checks theirs
  as they go. Without it, only those who run the checker do.

### The observer client as a browser extension (built, in testing)

To take the page's code away from the server altogether, the observer client is packaged as a
browser extension. It is built and tested in Chromium and Firefox, on a development branch, and is
not yet in use at an audit.

- **Its page** is rendered at build time from the server's own template, so the two cannot drift
  apart. The device's key lives in the extension's own storage, which no web page can reach. The
  server only supplies data and accepts signed entries; our test confirms it is never asked for the
  page or its code.
- **As entries are sent, it checks** each receipt's signature (with the server's receipt key,
  pinned the first time it sees that server), that the receipt is for the payload the device
  signed, and that the log heads the receipts state are consistent, across sessions.
- **Against the published commitments**, whose folder the observer is given by their coordinator,
  never by the server, it checks that the extract shown is one the commitments list, that the
  receipt key is the committed one, and that every head its receipts stated lies on one log with
  the anchored heads. It fetches the log's lines between them and recomputes each line's hash.
- **It compares the reports**, as above.
- **It can be inspected.** Its source is in the repository. Its build is reproducible, byte for
  byte, and it shows its identity (the hash of its file list) for comparison with a published
  release. It can be installed from unpacked files that never update themselves. It asks for access
  only to the server an observer link names.

Any failure raises an alert asking the observer to keep their record and tell their coordinator.
Our tests check that each check passes on an honest server, and that a forged receipt and a false
anchor are each caught.

The checks' results go into the observer's downloadable record, and the record checker reports any
alert the extension raised.

**Not yet done:** testing on Android; signed releases, published like the evidence commitments.

**Limits:** Chrome on Android doesn't support extensions, and Safari on iPhone supports them only
inside an App Store app. Observers on those phones keep the served page, and paper, the spreadsheet
and the comparison with CORLA's report carry the argument for them. The checks need a current
browser (Ed25519 in Web Crypto).

## 3. Operations

- **Recomputable.** The dashboard's figures are recomputable from the published event log, which
  has a standalone verifier anyone can run without this software.
- **Restarts and updates are recorded.** Every start and stop of the server and the scheduler is
  recorded, with the software's commit, so a restart, an update or a crash shows in the log.
- **Resets are linked, and are for testing only.** A practice workspace's reset starts its new log
  with an event naming the old log's head and the archive's hash.

## Which checks matter most

Our proposal, for review, in order:
1. **Each board's observation reports compared with CORLA's discrepancy report**, ballot by ballot,
   with every disagreement resolved. This is the check that catches errors in the reference or the
   record that change a finding.
2. **The seed, and the selection against the county's pull list and CORLA's posted selections.**
   Otherwise the right ballots might not be observed at all.
3. **The CVR's outcomes against the unofficial outcomes.**
4. **The inputs committed before the seed, and the log anchored daily**, so nothing can be changed
   afterwards to fit.
5. **For online entry, the extension or the receipt checker.** This matters most when online entry
   is a board's only observation, because then no paper or spreadsheet report can contradict it.

**Claim:** with 1 to 4, a board's findings are software-independent when it has at least one
observer whose report was compared with CORLA's. Five is needed only when that observer's report
comes from online entry alone.

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
  and kept in a client that CORLA's server does not supply. Each entry would get a receipt carrying
  the log's head, and the board's own record could be checked against the published log.
- **Interpretations published per ballot, during the audit.** Observers could then run the central
  check above as each ballot is entered rather than after the final reports.

**The claims these might support:**
1. CORLA's recorded interpretation of each sampled ballot is the one its audit board entered and
   read aloud. This rests on the board's signatures, on the observers' reports, and on the paper
   ballots themselves, which remain available.
2. The sample, and the risk measured from the interpretations, follow from published inputs and the
   public seed, whatever the software did. This rests on recomputation from the published log.

**Procedures and costs:** key management for the audit boards, and a client that state IT would
have to distribute and support; observers or independent parties who actually run the checks; care
about ballot privacy in what is published per ballot, though the redacted CVRs are already public.

None of this replaces the paper ballots. It would make the software's part of the audit checkable,
as the paper makes the outcome checkable.

## Questions for review

1. Is comparing the observers' reports with CORLA's discrepancy report the right central check? Is
   it sufficient, with the selection and outcome checks, for the findings it covers?
2. Which of the checks listed above are necessary, and which are only valuable? Is our ordering
   right?
3. For the expected votes, is agreement of the CVR's outcomes with the unofficial outcomes enough,
   given that the central check would catch a wrong expected vote on any ballot read aloud?
4. For paper, is an independent double transcription of the marks enough, or is something closer
   to the marking needed?
5. For online entry, does the extension, with its report comparison, answer the problem of the
   server supplying the page's code? What would you require of how it is built, distributed and
   checked? What is a practical answer for phones that cannot run extensions?
6. Spreadsheets and browsers are trusted here. Is that acceptable when a second observer, or CORLA's
   report, can contradict the result?
7. Are we missing a failure mode, or relying on something we haven't listed as trusted?
8. Would any of the techniques sketched for CORLA itself be worth proposing, and with what claims?
