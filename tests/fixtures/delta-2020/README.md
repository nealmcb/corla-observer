# Delta County, 2020 general election: test data

The two files a county coordinator uploads in the Delta demonstration
([docs/walkthroughs/demo-delta-2020-general.md](../../../docs/walkthroughs/demo-delta-2020-general.md)),
and that the demo and its browser check use. Each is byte-identical to the file Delta County uploaded
to CORLA for the 2020 general election's risk-limiting audit, as CORLA's own upload log records it
([`2020/general/upload_status.csv`](https://github.com/nealmcb/auditcenter/blob/main/2020/general/upload_status.csv)
in the public mirror of the Audit Center):

| File | Bytes | SHA-256 | CORLA imported |
|---|---|---|---|
| `Modified CVR_Export_20201113093409.csv`, the redacted cast vote record | 4,801,433 | `70ae835ca04b289f65db7eba1054ba3e954daaf6685f93050c326e7f34304e19` | 2020-11-13 10:22:42 MST |
| `Modified 2020 General Ballot Manifest.csv`, the ballot manifest | 7,700 | `4fdbfda9ba46d3aaaff9ad33f639127b2b61a5282be2331370f7883f9b87ff31` | 2020-11-13 10:24:03 MST |

The names are the ones in CORLA's log. Check a copy with `sha256sum`: the hashes above are CORLA's
`computed_hash` for each upload.

## Where they come from

**The manifest** is the one the Audit Center posted, byte for byte:
[`2020/general/round_1/manifest-Delta.csv`](https://github.com/nealmcb/auditcenter/blob/main/2020/general/round_1/manifest-Delta.csv)
in the public mirror.

**The CVR** was rebuilt from the copy of Delta's 2020 general CVR obtained by a Colorado Open
Records Act request and compiled at votedatabase.com, `co/2020/Delta/Delta CVR_Export.csv`
(4,664,562 bytes, SHA-256 `50012f3703d140b0…`). It holds the same 19,553 ballot rows as the file
CORLA imported, with the same CRLF line endings, and differs in one way only: each `ImprintedId` is
plain text, `1-1-34`, where the Dominion export, and the file CORLA imported, write it as an Excel
formula, `="1-1-34"` (in the CSV, `"=""1-1-34"""`). Those are 7 more bytes per ballot row, 136,871
in all, exactly the difference in size. Writing every `ImprintedId` back in that form reproduces
CORLA's SHA-256.

So the file here is the one CORLA used, and its SHA-256 says so independently of how it was
obtained.

**Redaction.** Delta's CVR needs none: no group of ballots with the same contests and precinct has
fewer than 10 ballots, by Lori Stevens's anonymizer (`anonymize_cvr.py --redact-on-precinct`) and by
corla-observer's own redaction check, which accepts the file on upload.

Git keeps these files' bytes, line endings included (`.gitattributes`), so the hashes hold in any
checkout.
