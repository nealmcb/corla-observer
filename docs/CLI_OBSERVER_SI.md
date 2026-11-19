# Is an observer's record from the command-line client software-independent?

Draft for review, November 2026. Please raise anything you find as an
[issue](https://github.com/nealmcb/corla-observer/issues), quoting the claim or gap you're
responding to (for example "Claim 2" or "Gap 1").

On 17 November 2026, at one county's risk-limiting audit in Colorado's November pilots (round one,
149 ballot cards, one audit board), an observer entered what they heard against the county's
redacted cast vote record (CVR), using the command-line client [`scripts/corla-observe.py`](CLI_OBSERVER.md).
This document sets out how the client is meant to make that record software-independent, the
evidence from that day, and the gaps we know of.

**The aim:** an undetected error or compromise in corla-observer's server should not be able to
cause an undetected change in what an observer reports about the audit. The scope is the observer's
findings, not the audit's outcome. Trusted: SHA-256, ECDSA P-256 and Ed25519, the public
timestamping services (Sigstore/Rekor, OpenTimestamps), the public seed ceremony, and the
observer's own eyes and ears.

The idea is to take the code an observer relies on away from the server: the server supplies
data and accepts signed entries, and everything that checks them runs on the observer's own
machine.

## How the client works

1. **The code doesn't come from the server.** The client is one readable Python file, run from a
   git checkout on the observer's own machine, using the standard library and the `cryptography`
   package. It records its own SHA-256 at the start of each session.
2. **The key stays with the observer.** The client makes an ECDSA P-256 key pair in the observer's
   record folder. It signs every entry over the observer, county, board, ballot, content, the
   device's time and sequence number, and the hash of the device's previous entry.
3. **Every entry gets a receipt, and the client checks it.** The server answers each entry with an
   Ed25519-signed receipt ([RECEIPTS.md](RECEIPTS.md)): the entry's hash, its place in the
   hash-chained event log ([EVENT_LOG.md](EVENT_LOG.md)), and the log's head. The client checks
   the signature, that the receipt is for the payload it signed, and that successive heads are
   consistent.
4. **It checks against the published commitments, not the server's word.** The commitments'
   location is given on the command line (`--commitments`), never by the server
   ([TIMESTAMPING.md](TIMESTAMPING.md)). Against them the client checks that the extract shown
   (the expected votes for each selected ballot) is one the commitments list, that the receipt key
   is the committed one, and that every log head in its receipts lies on one hash chain with the
   anchored heads, fetching and rehashing the log lines between them.
5. **Everything is kept.** The record folder holds every signed entry and receipt and the log as
   fetched. `corla-observer check-record` repeats the checks without the client.
6. **Alarms.** Any failed check raises a banner the observer has to acknowledge, and is recorded.

## Claims

1. The server could not have forged or altered the observer's entries undetectably, since it
   doesn't hold the observer's key.
2. It could not have dropped or reordered them without contradicting receipts the observer holds.
3. It could not have shown the observer an extract other than the committed one without the
   client's check failing.
4. It could not have shown different observers different logs without the heads in their receipts,
   or the published anchors, disagreeing.

## The evidence from 17 November

The client's final check, when the observer quit:

```
149 of 149 ballots entered; 306 signed entries: 306 with receipts (306 checked), 0 queued,
0 refused; log verified to event 1850; 0 alarm(s)
The receipt key is the one in the published commitments (SHA256SUMS-v6-20261117T160726Z.txt).
The extract shown: listed in SHA256SUMS-v6-20261117T160726Z.txt.
```

Every receipt's signature was verified, and every log head the receipts stated lies on one hash
chain with the six published anchors.

## The central check: CORLA's published comparison

What ties the record to the audit is comparing the observer's findings with what CORLA
publishes after the round: the audit board's interpretation of every contest on every sampled card,
against the CVR (the Audit Center's `round1/contestComparison.csv`). A wrong expected vote, or a
lost, altered or invented entry, shows up there as a disagreement, which is resolved from the
committed CVR and if need be the paper ballot.

For this audit, CORLA's comparison shows the audit board's entries differing from the CVR on one
card, in one contest: a statewide targeted contest, but on a card drawn for a county contest, so
outside that contest's own sample, and CORLA recorded no discrepancy for the statewide contest. The
observer recorded a match there. Photographs of that ballot
show its marks agree with the CVR, so the difference is in the audit board's data entry, not in the
CVR or the observer's record. On every other card the observer's record and CORLA's agree, apart from
one selected card that was not in the redacted CVR, so had nothing to compare against (gap 8).

## Gaps

1. **The CVR was committed after the seed.** The county's redacted CVR reached us the morning after
   the seed was rolled. The selection depends only on the manifest and the seed, which were
   committed before the roll, and our prediction of it matched CORLA's list. But the expected votes
   come from a CVR that could in principle have been altered with the sample known. What would
   close this: CORLA's own hash of the county's upload, published before the seed, or a check of the
   CVR's totals against the results.
2. **The first entries came before the day's commitment.** Three of 149 entries were made before
   the commitment listing the extract, which was published, and so externally timestamped, about
   two hours later.
3. **The later entries are not yet anchored.** The day's log needs its own commitment, with its
   head published and timestamped.
4. **Two sessions ran an uncommitted version of the client.** Two of the four sessions ran a
   version that is a commit on `main`; two ran a working copy not yet committed. That exact file
   has to be committed so anyone can match its recorded hash.
5. **The client's display is trusted.** An error in how it draws the expected votes could mislead
   the observer even with the right extract. Mitigations: it is one readable file, the committed
   extract can be rendered any other way, and the central check catches such an error on any ballot
   read aloud.
6. **The observer's attention is trusted.** "Match" means "I heard nothing different". The record
   keeps the timing of each confirmation, so very fast confirmations can be seen.
7. **The device and Python are trusted.**
8. **Redacted ballots.** When a selected ballot's CVR row was redacted, there is nothing to compare
   against, and anything the observer records risks revealing votes the redaction protects. The log
   is hash-chained and anchored, so such an entry can't simply be deleted; publishing the record and
   log needs a way to withhold its text while keeping the chain checkable, such as publishing only
   the entry's hash. Observers should not record what they hear for such ballots at all.

## Questions

1. With these gaps closed, is it fair to call this observer's findings software-independent?
2. Is gap 1 fatal for the expected votes, or are the central check and a check of the CVR's totals
   enough?
3. Is a single-file client run from a git checkout, with its hash recorded per session, an adequate
   answer to "the code comes from the server"? What more would you want: a signed release,
   reproducible distribution, or a second implementation?
4. Are we missing a failure mode, or relying on something we haven't listed as trusted?
