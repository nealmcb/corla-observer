# The photo check

Anyone holding a printed extract page (docs/EXTRACT.md), or a photo or scan of one, or a screenshot or photo of the observer screen showing a ballot, can confirm that it matches the committed extract; and a coordinator can confirm that printouts are genuine before observers rely on them. The check reads the page's QR code and its printed text, and compares every imprinted ID and every expected choice on it with the page as committed.

The code is `corla_observer.photocheck` (the check) and `corla_observer.photoimage` (reading the image); the command is `corla-observer photo check`, and the web page is "Check a page".

## Using it

```
corla-observer photo check IMAGE... --workspace W [--actor coordinator:ID] [--json] [--no-record] [--require-committed]
```

prints, for each image, which page it is ("Weld · Board 1 · page 1 of 28 · two-up · extract acbe1a4f"), how it was located, the content verdict with the counts of lines matched, differing and unread, the commitment status (the evidence commitments covering it with their proofs, or NOT YET COMMITTED), and every line that differs or could not be read. The exit status follows the content verdict: 0 if every image is MATCH, 1 if any is MISMATCH or UNKNOWN PAGE (or is not a readable image), otherwise 2 (MATCH WITH UNREAD LINES), and 3 if the check cannot run (a tool or the workspace missing). With `--require-committed`, a page that matches but that no commitment covers yet exits 1.

On the web:

- **Coordinators:** `/coord/check` ("Check a page" in the menu) checks a page of any extract the server has built.
- **Observers and practice readers:** `/observe/TOKEN/check` and `/read/TOKEN/check`, linked from their pages, check pages of their own county's extracts only. A paper observer's photos of their marked pages go through the same page and are recorded against their link.
- **Anyone:** `/check`, only when the server runs with `corla-observer serve --public-check`, which is off by default: during the audit the pages' content is to be kept out of sight of the audit boards. The public page never keeps the photo.

Each page takes a photo, with two controls: "Take a photo", which opens the camera on a phone, and "Choose a photo or file", which opens the gallery or the files for a photo already taken (a single control with the camera hint would, on Android, allow only a new picture). Or it takes the two short hashes typed in from the identification line ("extract 2350aed6 · page e83a5d34"). For a photo it shows, at the top, which page it is, how it was located, the content verdict with the counts and what "unread" means, and the commitment status; then the photo beside the committed page as text with each line's result, and the commitment's proofs. For each line unread or differing it shows what was read ("read: “1”", or "read: nothing found"), why it was not accepted (nothing found; no reading clear enough to judge; read as something else only once; the readings disagree; two or more readings agree on something else), each pass's reading and how it was judged, and, to whoever may see the stored photo, a small crop of the line as photographed, straightened, to judge by eye (made for the page, inline, never stored or recorded). The command line prints the same, and its `--json` carries each such line's `reason`, `reads` (pass, text, judgement) and `box`. For typed short hashes it shows the page they locate, as committed, with its proofs, to compare by eye: the short hashes only locate a page; the QR code's full hash is the check.

For a good photo: one page per photo, the whole page in the frame and filling most of it, held square to the page, in even light without glare. A scan at 200 dots per inch or more is better still. A photo of the print view on a screen works too: on screen (not when printed) the QR code is drawn larger, 30 mm, so that a phone photo of the screen can still decode it.

## How it works

1. **Find the page.** The QR code (`COX2:` and the page's SHA-256, or on a provenance sheet `COX2E:` and the extract's) is decoded with zbar. The code may be small in the photo (it is 22 mm on the PDF printout, docs/EXTRACT.md, but 10 to 12 mm on earlier printouts, and a photo of the whole sheet or of a screen gives it few pixels), so first the small square windows most like a QR code (about half dark, changing often from dark to light both across and down; text is far less dark) are cut out, contrast-stretched and enlarged (with Lanczos resampling, which zbar reads where bicubic fails), each tried in turn; this reads a code only 50 or 60 pixels across, as in a phone photo of a screen. Then a few preparations of the whole image are tried until one reads: as it is, grey with stretched contrast, flattened against glare and shadow, black and white, enlarged or reduced, and each corner enlarged. Its hash is looked up in the stored page index of every extract the workspace has built, every layout, audit and practice, superseded extracts included (the result says if the extract has been superseded). Without a readable code, the identification line is found (both of its lines, where it is printed on two beside the code), wherever it is in the photo (read from the whole page, then smoothed, then from the top of the page alone), and its short hashes looked up instead: exactly, or else allowing for characters OCR commonly misreads ("a" for "4", "l" for "1"), within three edits for each hash, among the pages whose board, page number and page count agree with what was read, and only if no other page comes within two edits of the best (two random 8-digit hashes are that close about once in hundreds of millions). The page is then located but not confirmed. The page's content is recomputed from the stored extract and must reproduce the indexed hash.
2. **Find the commitment.** Every recorded evidence commitment (`evidence.committed`, docs/TIMESTAMPING.md) whose SHA256SUMS file lists the extract's page index or the extract is listed, earliest first, with its proofs: the OpenTimestamps proof's Bitcoin block once upgraded (`upgrade-proofs` records the block's height), or pending; the Sigstore signature and its Rekor log index; the git commit, and whether it was pushed.
3. **Read the printed text.** The image is turned upright (by the QR code's orientation, or tesseract's orientation detection). A photo of a screen is recognized by its moiré (the banding a phone camera records off a screen's pixels: the paper's brightness, with the text taken out, rising and falling across the image with one period, the same in a dozen thin strips across the page) and evened out (divided by its own local background). The image is levelled (the small rotation that best separates the text lines from the spaces between them), then straightened: the line angle is measured in a grid of tiles across the page, fitted by a plane (one per column of a page of two or three columns, split at the gaps between them), and where it varies by 0.3 degrees or more, as when the page or screen was photographed at an angle, every line is made level by a mesh transform. It is then enlarged to about 300 dots per inch. tesseract reads it twice: as photographed, and flattened against glare and shadow, each column of a page of several columns on its own (between the white gaps found where the columns should divide). Where tesseract has run lines of neighbouring columns together, they are split at the widest space near each boundary between columns, line by line, so that a photo taken at an angle still splits correctly.
4. **Align.** The page's expected lines are known exactly: its identification line, and for each ballot its "ImprintedId" line, its marks line (POSSIBLE, BOUNDARY) and its contest lines, with the shortened names actually printed (`corla_observer.printnames`). Each pass's lines are paired with the expected ones by a sequence alignment that keeps both orders and pairs only similar lines, so a line that was not read is skipped, not confused with its neighbour.
5. **Judge each line.** Every reading of a line is a vote: for an imprinted ID, the ID read (look-alike letters such as O and l taken as digits when the IDs are all digits); for a contest line, which of the contest's possible printed values (its choices, NO VOTE, the expected value) the text read is closest to, by a clear margin, or, if it is close to none and was read confidently, the text itself; for the identification line, each of its parts. A line still unsettled is cut out on its own and read again, enlarged (an imprinted ID clear of the rule beside it, a short hash as hexadecimal); these re-readings count one and a half times. Then:
   - **matched** only if the readings showing exactly what should be printed outweigh those showing anything else;
   - **differs** only if no reading shows what should be printed and readings worth at least two agree on the same other content;
   - otherwise **unread**.

   A reading that is the expected ID with a stray character at an end (a rule read as a "1"), a part of the expected value (one name of an overvote), or within a character or so of the expected value (one character of an imprinted ID, two of a short hash, four of a full hash), is counted neither for nor against the line: OCR could have made that difference, and the same misreading made twice must not add up to a contradiction. Lines read that should not be there, another ballot's imprinted ID or an extra contest line, read alike by both passes, differ too. If contest lines that seem to differ are more than a quarter of those that matched, and nothing firmer differs (an imprinted ID, the identification line, a line not on the page), the photo was read too poorly to tell lines apart, and they are reported as unread.
6. **Marks.** A check mark written in a box is tolerated: the box's column is skipped when the line is read. A mark that overruns the box into the choices is read as junk, and never counts against the page: a value read with fewer than 6 letters or digits is not taken for another value, and a line read with one of the page's contests and that row's choice at its end, or nothing but junk after the contest, is a reading of that row, not an extra line; such a line is unread at worst. A box holding much more ink than the page's typical box is reported as marked. This is only a hint for now (for paper observers' uploaded pages later); it does not affect the verdict.

## The verdict

A check answers two questions separately.

**Does the printed content match?** For any page found in a stored page index, committed or not:

- **MATCH:** the QR code's full hash located the page, and every line matched.
- **MATCH WITH UNREAD LINES:** nothing printed contradicts the page, but some lines (or the QR code, when the page was located by its short hashes) could not be read. "Unread" means the photo did not show a line clearly enough to tell: it is counted neither as matching nor as differing. The unread lines are listed: check them by eye.
- **MISMATCH:** some printed content contradicts the page: each differing line is listed with what is printed and what the page should show.
- **UNKNOWN PAGE:** only when the page cannot be found at all: its hash is in no page index the checker may search (for an observer, their own county's), or neither the QR code nor the short hashes could be read.

**Is the page committed?** Shown beside the content verdict:

- **committed** in evidence version N at a given time, with its proofs: the Bitcoin block of its OpenTimestamps proof (or pending), its Rekor log entry, its git commit. The earliest covering commitment is named, and every one is listed.
- **NOT YET COMMITTED:** the page is in the workspace's extract, but no published, timestamped commitment covers it yet, so the check shows only that the page matches the tool's own copy. This is normal before the evidence for an extract is committed, and during practice.

OCR uncertainty can turn a difference into an unread line, but never into a match: a line counts as matched only when the text read is what was committed, and an unreadable line is reported as unread, not matched.

## What is recorded

Each check is a `photo.checked` event with the actor (the coordinator, the link's observer or reader, or `system` for the command line by default and the public page), the photo's hash, the page's and extract's hashes, the layout and page number, the content verdict (`verdict` and `content_verdict`), the commitment status (`commitment_status`: `committed`, `not_committed`, or null when no page was found; `committed` as a boolean; and `commitment`, the earliest covering commitment's version, time, SUMS hash, Bitcoin block, Rekor log index and commit), the photo's size and upright dimensions as received (`received_size`, `received_dimensions`) and whether the photo kept is the reduced one (`stored_reduced`), the counts of lines matched, differing and unread and of boxes marked, and for each differing line its kind, its ballot's place on the board and its contest. It never holds the printed or expected values, nor imprinted IDs, so the event log carries none of the page's content.

The photo is kept in the workspace under its SHA-256 (except from the public page), with its metadata removed: a JPEG already upright loses its EXIF, GPS, camera and comment segments with its image data untouched; any other image, or a JPEG that EXIF says to rotate, is re-encoded upright without metadata. Photos are accepted only as JPEG, PNG, WebP or TIFF, recognized by content, up to 40 MB and 200 megapixels (a phone's full-resolution photo is 50 to 200 megapixels, 15 to 40 MB); the pixel count is checked from the image's header before any of it is decoded, so a small file claiming a huge image is refused unread. A photo whose longer side is over 4800 pixels is reduced to 4800 (Lanczos) before it is checked, which is more than the reading needs and much quicker. It is kept as received (without its metadata, as above) unless it was received larger than 15 MB, when the reduced image, as checked, is kept instead (a JPEG of quality 92), and the event says so. On the check page, the browser itself shrinks a photo whose longer side is over 4000 pixels or whose size is over 8 MB before sending it (drawn upright by its EXIF orientation, sent as a JPEG of quality 92), showing the size chosen and the size sent; without JavaScript the photo is sent as it is. A request larger than the limit is refused with a page saying so. A stored photo is shown only to coordinators and to the link that sent it, never publicly. While being checked, a photo is written only to a private temporary directory under the workspace's `staging/`, removed afterwards.

## The observer screen

A screenshot of the observer page, or a photo of the screen, showing a ballot, in either entry mode, on a phone or a laptop, is checked the same way, against the committed extract. The screen's ballot header carries its identification (docs/EXTRACT.md, "On screen"): the imprinted ID, "Ballot n of N", the county, board and extract's short hash, and a QR code `COX2S:` with the first 32 hex digits of the extract's hash, the board and the ballot's place on it.

1. **Find the ballot.** The QR code is decoded as for a page (the same small-code windows; on a phone screenshot it is about 64 pixels across), and the extract whose hash starts with it found among the workspace's extracts, then the board's ballot at that place. Without a readable QR code, an identification line with an extract's short hash but no page names a screen: the ballot is then found from that short hash, the board and "Ballot n of N" read on the screen, and is only located, not confirmed.
2. **Read it.** The image is enlarged as a page is, and read twice by tesseract (as it is, and flattened). The header's lines give the imprinted ID, "Ballot n of N", and the identification line (joined across a wrap before "extract" on a phone; its short hash read again as hexadecimal if needed). Below it, the words of the contest-by-contest status cells and buttons (the readings' numbers, the outcome symbols, the pencil and "?") are dropped; the lines of one row (a contest's name or its choices wrapped in their cell) are joined, since rows are further apart than the lines within one; and each row's words are split at the channel down the middle where the contest names, right-aligned, meet the choices, left-aligned.
3. **Compare.** The expected rows are the ballot's contests with the screen's names (`corla_observer.printnames`, layout `screen`) and its choices together; each is aligned with the rows read and judged exactly as a printed contest line is (matched, differs, unread). A long ballot scrolls, so only the stretch of contests read is checked: the result says which ("contests 1 to 12 of 60 are on this screenshot and were checked; the others are not shown"), and contests inside that stretch not read are unread. The header's parts are judged like an identification line's.

The verdicts are those of a page: MATCH (located by the QR code, everything shown matched), MATCH WITH UNREAD LINES, MISMATCH (a contest shown differs from the extract, as a forged screen would) or UNKNOWN PAGE; with the commitment status of the extract. It is recorded as `photo.checked` with `kind` `screen` and the ballot's place (`ballot`), never its content. `tests/test_browser_per_contest.py` checks real screenshots made by the browser (phone and desktop, both modes, and one with a choice changed in the page).

## Requirements

Two system tools, run as commands:

- `tesseract` with its English data: Debian and Ubuntu packages `tesseract-ocr` and `tesseract-ocr-eng`;
- `zbarimg`: package `zbar-tools`.

```
sudo apt-get install tesseract-ocr tesseract-ocr-eng zbar-tools
```

Pillow (a Python dependency) prepares the images. Without the tools, the command exits 3 and the web page says the check cannot run on the server; the typed short hashes still work.

## How well it works

`scripts/photo_check_rates.py` measures it: it prints board 1 of a synthetic but realistic extract (`tests/photofixtures.py`: Colorado primary contest and candidate names, long names the print shortens, overvotes, no votes, redacted and missing ballots, POSSIBLE and BOUNDARY marks) through the real printout, its PDF (`corla_observer.pdfextract`) rasterized by `pdftoppm` at 200 dots per inch, in the layouts chosen; makes pen-ticked copies and tampered printouts (a choice changed, an imprinted ID swapped for another ballot's, another page's QR code pasted on); degrades each like a photo (`tests/photoimages.py`); and checks every image.

The conditions (`tests/photoimages.py`) are a clean 200 dpi scan; the sheet photographed on a desk; turned 3 and -6 degrees, or upside down; a perspective that moves each corner inwards by up to 4 or 8 percent of the image; a Gaussian blur of 1 or 2 pixels; JPEG quality 30; reduced to 150 or 100 dpi; a lamp's glare with a shadow across a corner; a photo of a screen (moiré bands 16 pixels apart, slightly tilted, 30 percent deep); and a "phone photo" combining moderate amounts of most of these.

By default it measures a reduced set, in a few minutes: `portrait-2` under six conditions (a clean scan, on a desk, turned 3 degrees, a 4 percent perspective, JPEG quality 30 and the combined "phone photo"), 7 sheets each (two genuine pages, the provenance sheet, a pen-ticked page and the three tampered printouts), 42 checks; `--all` measures every layout under every condition (420 checks, an hour or more). tesseract runs single-threaded, so checks run side by side without oversubscribing the processors, and each result is printed and saved as it finishes.

### The current version

The reduced set, measured on 26 October 2026 (about 30 seconds per check, six at once):

| Printout | Genuine: MATCH | unread lines | MISMATCH | unknown | Tampered: MISMATCH | unread lines | MATCH | unknown |
|---|---|---|---|---|---|---|---|---|
| PDF, `portrait-2` | 17 | 7 | 0 | 0 | 18 | 0 | 0 | 0 |
| the HTML print view of page format 4, `portrait`, the same six conditions (25 October 2026, before the QR code windows, the rectification and the descreening) | 15 | 9 | 0 | 0 | 17 | 1 | 0 | 0 |

Genuine pages are never MISMATCH and tampered ones never MATCH. The unread lines on genuine pages are mostly on the provenance sheet (a 64-digit hash read with a character or two wrong is unread) and on the pen-ticked page.

### The full sets

The full set on the PDF printout (`--all`, 26 October 2026, with the photo check before the QR code windows, the rectification, the descreening and the misread-hash lookup below): 14 conditions (all but the photo of a screen); for each of the four layouts, two genuine pages, the provenance sheet and a pen-ticked page (16 genuine images per condition), and the three tampered printouts (12 per condition), under 14 conditions, 392 images in all. The conditions are a clean 200 dpi scan; the sheet photographed on a desk; turned 3 and -6 degrees, or upside down; a perspective that moves each corner inwards by up to 4 or 8 percent of the image; a Gaussian blur of 1 or 2 pixels; JPEG quality 30; reduced to 150 or 100 dpi; a lamp's glare with a shadow across a corner; and a "phone photo" combining moderate amounts of most of these.

| Condition | Genuine: MATCH | unread lines | MISMATCH | unknown | Tampered: MISMATCH | unread lines | MATCH | unknown |
|---|---|---|---|---|---|---|---|---|
| clean scan | 10 | 6 | 0 | 0 | 12 | 0 | 0 | 0 |
| on a desk | 10 | 6 | 0 | 0 | 12 | 0 | 0 | 0 |
| rotated 3 degrees | 12 | 4 | 0 | 0 | 11 | 1 | 0 | 0 |
| rotated -6 degrees | 8 | 8 | 0 | 0 | 11 | 1 | 0 | 0 |
| upside down | 10 | 6 | 0 | 0 | 12 | 0 | 0 | 0 |
| perspective 4% | 11 | 5 | 0 | 0 | 12 | 0 | 0 | 0 |
| perspective 8% | 4 | 12 | 0 | 0 | 11 | 1 | 0 | 0 |
| blur 1px | 8 | 8 | 0 | 0 | 12 | 0 | 0 | 0 |
| blur 2px | 0 | 16 | 0 | 0 | 6 | 6 | 0 | 0 |
| JPEG quality 30 | 8 | 8 | 0 | 0 | 12 | 0 | 0 | 0 |
| 150 dpi | 12 | 4 | 0 | 0 | 11 | 1 | 0 | 0 |
| 100 dpi | 8 | 8 | 0 | 0 | 12 | 0 | 0 | 0 |
| glare | 10 | 6 | 0 | 0 | 12 | 0 | 0 | 0 |
| phone photo (all moderate) | 10 | 6 | 0 | 0 | 12 | 0 | 0 | 0 |

- **Genuine pages:** never MISMATCH (0 of 224). MATCH 121, MATCH WITH UNREAD LINES 103, UNKNOWN PAGE 0. Every page was found, in every condition. In good conditions most are MATCH; with perspective, blur, 100 dpi or the combined "phone photo" about half are MATCH WITH UNREAD LINES, with a few unread lines (at most 10 of a page's 77 to 188, apart from a 2-pixel blur, where the page is still found but many lines, sometimes all, are unread). By layout, MATCH was 32 of 56 genuine images in `portrait-1` and `portrait-2`, 30 in `landscape-2` and 27 in `landscape-3`.
- **Tampered pages:** never MATCH (0 of 168). A changed choice or an imprinted ID swapped for another ballot's is MISMATCH in 102 of 112 images, and MATCH WITH UNREAD LINES in the other 10 (mostly a 2-pixel blur, where the changed line is unread). Another page's QR code pasted on is MISMATCH in all 56.
- **Pen ticks** never caused a MISMATCH. Marked boxes were reported in 53 of the 56 ticked images, about 13 per image; 26 of the 168 unticked genuine images had boxes wrongly reported as marked (111 boxes, most in blurred or warped photos).
- **Against the HTML print view** (25 October 2026, `portrait` and `two-up`, the same 14 conditions, 196 images): genuine MATCH 51 of 112, MATCH WITH UNREAD LINES 51, UNKNOWN PAGE 10; tampered MISMATCH 69 of 84, MATCH WITH UNREAD LINES 5, UNKNOWN PAGE 10. The PDF's crisper text and its 22 mm QR code brought UNKNOWN PAGE to none (the code decodes even at a 2-pixel blur and at 100 dpi, `scripts/qr_size_rates.py`, docs/EXTRACT.md), and tampering is caught more often (94 percent MISMATCH against 82).

The full set on the HTML print view of page format 4 (26 October 2026, `portrait` and `two-up`, 12 mm and 10 mm QR codes): for each layout, two genuine pages, the provenance sheet and a pen-ticked page (8 genuine images per condition), and the three tampered printouts (6 per condition), under 15 conditions, 210 images in all. The conditions are a clean 200 dpi scan; the sheet photographed on a desk; turned 3 and -6 degrees, or upside down; a perspective that moves each corner inwards by up to 4 or 8 percent of the image; a Gaussian blur of 1 or 2 pixels; JPEG quality 30; reduced to 150 or 100 dpi; a lamp's glare with a shadow across a corner; a photo of a screen (moiré bands 16 pixels apart, slightly tilted, 30 percent deep); and a "phone photo" combining moderate amounts of most of these. In parentheses, where different, the same images checked by the version before the QR code windows, the rectification, the descreening and the misread-hash lookup.

| Condition | Genuine: MATCH | unread lines | MISMATCH | unknown | Tampered: MISMATCH | unread lines | MATCH | unknown |
|---|---|---|---|---|---|---|---|---|
| clean scan | 7 | 1 | 0 | 0 | 6 | 0 | 0 | 0 |
| on a desk | 8 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| rotated 3 degrees | 7 | 1 | 0 | 0 | 6 | 0 | 0 | 0 |
| rotated -6 degrees | 4 | 4 | 0 | 0 | 6 | 0 | 0 | 0 |
| upside down | 7 | 1 | 0 | 0 | 6 | 0 | 0 | 0 |
| perspective 4% | 7 (2) | 1 (6) | 0 | 0 | 6 | 0 | 0 | 0 |
| perspective 8% | 6 (1) | 2 (7) | 0 | 0 | 6 | 0 | 0 | 0 |
| blur 1px | 0 | 8 | 0 | 0 | 4 | 2 | 0 | 0 |
| blur 2px | 0 | 4 (0) | 0 | 4 (8) | 0 | 3 (0) | 0 | 3 (6) |
| JPEG quality 30 | 5 | 3 | 0 | 0 | 6 | 0 | 0 | 0 |
| 150 dpi | 6 (5) | 2 (3) | 0 | 0 | 6 (5) | 0 | 0 | 0 (1) |
| 100 dpi | 0 | 8 (6) | 0 | 0 (2) | 4 (2) | 2 (1) | 0 | 0 (3) |
| glare | 5 | 3 | 0 | 0 | 6 | 0 | 0 | 0 |
| photo of a screen (moiré) | 7 (5) | 1 (3) | 0 | 0 | 6 | 0 | 0 | 0 |
| phone photo (all moderate) | 2 (0) | 6 (8) | 0 | 0 | 5 (4) | 1 (2) | 0 | 0 |

- **Genuine pages** (HTML print view): never MISMATCH (0 of 120). MATCH 71 (56 before), MATCH WITH UNREAD LINES 45, UNKNOWN PAGE 4 (10 before). In good conditions (a scan, a photo square to the page, turned a little or upside down, compressed, 150 dpi, glare) and now also in perspective and in a photo of a screen, most are MATCH; with blur, 100 dpi or the combined "phone photo" most are MATCH WITH UNREAD LINES: a few unread lines, or every line matched but the QR code too blurred to decode, so that the page was located by its short hashes only. A provenance sheet is often MATCH WITH UNREAD LINES: a 64-digit hash read with a character or two wrong is unread. At a 2-pixel blur half the pages are still located by their short hashes, read with a character or two wrong; the others are UNKNOWN PAGE.
- **Tampered pages** (HTML print view): never MATCH (0 of 90). A changed choice or an imprinted ID swapped for another ballot's is MISMATCH in 56 of 60 images (54 before); 2 are photos too blurred to read the changed line (MATCH WITH UNREAD LINES, the line listed as unread) and 2 too poor to locate the page (UNKNOWN PAGE). Another page's QR code pasted on is MISMATCH in 23 of 30 (21 before); in 6 more the pasted code could not be decoded, so the page was located by its own printed short hashes, whose text is genuine, and the verdict is MATCH WITH UNREAD LINES (the QR code unread); 1 is UNKNOWN PAGE.
- **Pen ticks** (HTML print view) in every third box never caused a MISMATCH. Marked boxes were reported in all 30 ticked images; 11 of the 60 unticked genuine images had a box wrongly reported as marked (29 boxes in all). Mark detection is a rough hint.
- **Time:** a check takes about 15 to 30 seconds on a laptop, single-threaded.

**Real phone photos.** Two real phone photos of the HTML print view (page format 4) of a two-up page of 136 lines shown on a laptop screen, not printed, both of them taken at an angle and with the screen's moiré, have been checked. Before these changes, the first, tilted, was located by its QR code with 122 lines matched, 14 unread and none differing; the second, square to the screen but with strong moiré and its QR code only 55 pixels across, could not be located at all (UNKNOWN PAGE, nothing read). Now the first has 124 matched and 12 unread, and the second is located by its QR code with 130 matched and 6 unread; with the QR code ignored, both are still located from their identification lines' short hashes, read with a few characters wrong, with the same counts. Neither has a line wrongly reported as differing. These pages are of page format 4, which this version no longer reproduces: checked now, each is found by its QR code in the stored page index and reported UNKNOWN PAGE, saying that it is in a layout of an earlier page format, to be built again and printed from the PDF.

These are synthetic printouts photographed synthetically, and two real photos of a screen; real phone photos of paper printouts (curled paper, uneven light, motion blur, a hand-held angle) should be measured as soon as there are printouts to photograph.

## Limits

- **The photo must be of one page, readable.** A blurry, distant or glare-covered photo gives unread lines, and a QR code too small to decode leaves the page located only by its short hashes, or not at all. Neither is ever a match.
- **Very small changes go unread rather than differing.** A single changed character in an imprinted ID, up to two in a printed short hash or four in a provenance sheet's hash, or a choice changed to a name that differs from the expected one by a letter or two, cannot be told from an OCR error, and is reported unread, not differing. It is never reported as matched, so the verdict is at best MATCH WITH UNREAD LINES, listing the line to check by eye.
- **What is checked is what is printed in the page's text:** the identification line's parts, each imprinted ID, each contest line's choices and the marks line. The ballot's place on the board ("41/127") and location ("loc Bin 1") are not compared, nor are the contest names themselves beyond pairing lines (a contest line under the wrong contest name pairs poorly and is unread).
- **The page index is this server's.** The check finds pages in the extracts this workspace built, and their commitments as this workspace recorded them. The independent route is in docs/EXTRACT.md ("Checking an extract"): recompute the extract and its pages from the published evidence and compare by hand.
- **Hand-made marks are only detected roughly,** as boxes with much more ink than usual; a faint mark may be missed, and a smudge may count.
- **The provenance sheet** is checked for its county, seed, extract hash, every input's hash and the page index's hash; its board, layout and page counts are not compared.
