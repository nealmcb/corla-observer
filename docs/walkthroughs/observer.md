# Walkthrough: following an audit as an observer

A script for a training video for observers. It uses the links made in [coordinator-single-county.md](coordinator-single-county.md) for the fictional Testland county (random votes, safe to show): **ann** follows on her phone, **ben** on his laptop, and **rita**, with a practice reader link, plays the audit board, reading each ballot aloud twice. Every step was done as written, against a local server; the screenshots named in each step were taken then by `scripts/walkthrough_screenshots.py`, ann's at phone size (390 x 844), ben's and rita's at desktop size (1366 x 900). They are not in the repository; that script makes them again.

To record this walkthrough on its own, without the coordinator's steps:

```
scripts/demo_setup.sh ~/demo/testland --through links
~/demo/testland/serve.sh
```

prints the three links (each shown only once).

Total: about 14 minutes of video. Times are rough.

There are two ways to enter what you hear: contest by contest (the default, step 17) and two taps per ballot (steps 4 to 14). Each observer chooses on the notice before the first ballot, and can change at any time with the switch at the top of the page. In this walkthrough ann and ben choose two taps on the notice, and ben switches to contest by contest in step 17.

## Steps

### 1. Open your link: the notice first (45 s)

Open the link your coordinator sent you, on the device you'll use for the whole session, in one browser. There is no login and nothing to install. The first time, the page makes a signing key for this device (it never leaves the device) and registers it with the server; there is nothing to tap for it.

Before the first ballot, a short notice (`00-ann-notice.png`): "Before you start. Keep this screen out of sight of the audit board and the public. The board must never see the votes on it: hold the phone or turn the laptop away from the table.", then the choice of how you enter what you hear (**Contest by contest**, chosen unless you chose otherwise before, or **Two taps per ballot**), typing the end of an imprinted ID when the board reads a different ballot, and not interrupting the board. Ann taps **Two taps per ballot**, then **I understand, start**; her tap is recorded, signed like an entry, and so is the mode she starts in. The notice comes again the next time you open the link in a new browser session, and on each new day. **More about how this works** opens the full help.

Narration: "Open your link on the phone or laptop you'll use all day. It first reminds you of the one rule above all: the board must never see your screen."

### 2. The ballot on your screen (45 s)

Screenshot: `01-ann-phone-first-view.png`. At the top: "ann · Testland · board 1", the status **Live**, "0 of 21 done", **More**, and the reminder "Keep this screen out of sight of the audit board." Then the first ballot. Its header, on one line: the imprinted ID `101-1-8` at the left, "Ballot 1 of 21" at the right; under it `Testland · Board 1 · extract a7898e7d`; and beside them a small QR code. The header stays at the top as you scroll a long ballot. A screenshot or photo of your screen can be checked against the committed extract with these (step 16).

Then the expected votes, laid out as on the printout: the contest's name at the right of the left half, a box in the middle, the choice at the left of the right half; a contest with several choices (a multi-seat contest, or an overvote) has each on its own line. At the bottom, the big **First reading** button, the box "Heard something else? Type it" with **Note**, and **Different ballot...**.

Hold the phone or turn the laptop so that no board member (and no member of the public near the table) can see the screen.

Narration: "It shows one ballot at a time: its ID and its place, then the votes the county's records say it has, as on the paper printout."

### 3. More (30 s)

Everything else is under **More** (`02-ann-more.png`): your record (**Download my record**, **As a spreadsheet (CSV)**), **Check a printed page, or send photos of your marked pages**, the provenance, the printouts (PDF, four layouts), **Dim the screen** (a dark mode for a dim room, not a privacy screen), **Show the instructions again**, and **How this works** (`02-ann-how-this-works.png`, opened). **Close** returns.

### 4. A match takes two taps: First reading, Confirmed match (1 min)

The board reads each ballot twice, once by each member. Rita's reader page shows her what to read: "First reading: read it aloud", the ID and every vote (`03-reader-start.png` before **Next ballot**, `04-reader-first-reading.png` after).

- When the first member has read the ballot and it matches your screen, tap **First reading**. It turns into **Confirmed match** and says "First reading heard. Tap "Confirmed match" after the second reading, or type what was heard instead." (`05-ann-first-reading-tapped.png`).
- Rita taps **Second reading** and reads it again (`06-reader-second-reading.png`). When it still matches, tap **Confirmed match**: the match is recorded and the next ballot appears from its top (`07-ann-confirmed-next-ballot.png`, "1 of 21 done"); the ballot just done moves into the list of ballots entered, above it (scroll up), with "✓ match" and its receipt code (such as `246-1b1b99`: the entry's number in the public log and the start of its hash, signed by the server).

Narration: "Tap once after the first reading, once after the second. Two taps keep a slip of the finger from recording a match."

### 5. On a laptop (30 s)

Ben's laptop shows the same (`08-ben-laptop-first-view.png`). On a keyboard, **Enter** (or the space bar) does each tap: Enter, Enter for a match (`09-ben-two-matches.png`, two ballots done, each with "✓ match" and a receipt code).

Narration: "On a laptop, press Enter after each reading."

### 6. When you hear something different: a note (45 s)

If a vote read aloud differs from the screen, or anything else happens, type what you heard, in your own words, and press Enter (or **Note**). Typing anywhere goes to the box. Ann taps **First reading**, then hears Yes on Proposition 115 where the screen says No, and types "Prop 115: heard Yes/For" (`10-ann-note-typed.png`). The note is recorded instead of a match and the next ballot appears. In the list, the ballot shows "▲ Prop 115: heard Yes/For" with an orange edge (`11-ann-history-after-note.png`, after one more match).

Don't interrupt the board: observers note, they don't overrule.

Narration: "Heard something different? Just type what you heard and press Enter. That records a difference instead of a match."

### 7. Correcting an earlier ballot (1 min)

Tap any ballot in the list to change its entry. Ann realizes she missed the Senate vote on ballot 1: she taps it. It opens again, marked "changing an earlier entry", saying what was entered ("Entered: match (receipt 29-b8b7d4). A new entry is added; nothing is overwritten."), with **Back to the next ballot** to leave it unchanged (`12-ann-changing-earlier-entry.png`). She types "US Senator: heard Cory Gardner" and presses Enter.

The change is a new entry; nothing is overwritten. The list now shows ballot 1 as "▲ US Senator: heard Cory Gardner · corrected (tap to see the earlier entry)" with a new receipt code, and the page goes back to the next ballot not yet done (`13-ann-after-correction.png`). Tapping ballot 1 again shows both: what is entered now, and "Before that: match", each with its receipt. The earlier entry is also in ann's downloaded record and in the coordinator's statistics ("Changed later: 1").

Narration: "Made a mistake? Tap the ballot and enter it again. Your earlier entry isn't erased; the new one is added after it, and the list shows the ballot was corrected."

### 8. Losing the connection: entries wait on the device (45 s)

If the network drops, keep going. Each entry is signed and kept on the device, and sent as soon as the connection is back. (To show this, the phone's connection was cut with Chrome's network emulation.) Ann matches two more ballots: the status turns to "Offline 4", four entries waiting (a match is two signed taps; holding the pointer over the status says "Offline: 2 ballots (4 entries) waiting") and each of the two ballots shows "queued" instead of a receipt code (`14-ann-offline-queued.png`).

Narration: "No signal? Carry on. Your entries are kept on your phone and marked queued."

### 9. Reconnecting: entries resynchronize, with receipts (30 s)

When the connection is back, the waiting entries are sent at once; within a few seconds the status is **Live** again and each queued ballot shows its receipt code (`15-ann-back-online-receipts.png`).

Narration: "When you're back online, everything queued is sent, and each entry gets a receipt from the server."

### 10. Reloading the page mid-audit (30 s)

Reloading the page (or the phone reloading it after you switch apps) keeps your place, and a first reading you had tapped: ann tapped **First reading** on ballot 6 and reloaded; the page is back at "Ballot 6 of 21", "5 of 21 done", showing **Confirmed match** and "First reading already heard (before the page was reloaded)." (`16-ann-after-reload.png`). Tap **Confirmed match** after the second reading as usual; the first reading is not recorded twice.

### 11. Closing the browser and opening the link again (30 s)

Closing the browser, or restarting the phone, loses nothing: open the same link in the same browser, read the notice again and tap **I understand, start**, and you are back where you were, with the same device key and every receipt (`17-ann-browser-reopened.png`, "Ballot 6 of 21", **Live**).

Narration: "Closed the browser by accident? Open your link again in the same browser and pick up where you left off."

### 12. A second device for the same observer (45 s)

It works, but each device is separate. Ben opened his link on a phone as well: it made its own key, registered it, and shows his progress from the server: "2 of 21 done", his two matches, and the next ballot (`18-ben-second-device.png`). Each device's downloaded record holds only the entries made on that device, so download from both. Better: use one device, in one browser, for the whole session.

### 13. The board reads a ballot out of order (45 s)

If the board reads a different ballot than the one on your screen, type its imprinted ID in the box, or tap **Different ballot...** and type it there; the end of the ID is enough when only one ballot on this board ends that way. Ann types `1-22` (`19-ann-different-ballot-typed.png`) and presses Enter: the page jumps to ballot 10, `102-1-22`, and says "The board read 102-1-22 out of order. 101-4-8 and any ballots skipped stay marked not read yet." (`20-ann-jumped-out-of-order.png`). After she enters it, the page goes back to the first ballot not yet done, marked "not read yet: skipped earlier", and the skipped ones stay in the list as "○ not read yet".

Narration: "If the board reads a different ballot, type the end of its ID. The page jumps to it, and comes back afterwards."

### 14. An ID that isn't on the list (30 s)

If the board reads an ID that isn't on this board's list, type it the same way. Ann types `103-9-99`: the page says "“103-9-99” is not on this board's list. Your note is recorded, and listed above; the page stays on 101-4-8." (`21-ann-id-not-on-list.png`). The note stays in her list, after the ballot she was on, as "≠ 103-9-99 not on this board's list: noted", with its receipt code (see `25-ann-all-done.png`).

Narration: "An ID that isn't on your list is recorded as a note, kept in your list, and you stay where you were."

### 15. Download your record (45 s)

At any time, and at the end of the session, open **More** and tap **Download my record** (a JSON file) and **As a spreadsheet (CSV)** (`22-ann-download-record.png`). The files are named in Colorado time, with its offset from UTC, like `corla-observer-record-Testland-board1-ann-2026-10-27T1124-0600.json`, and the spreadsheet's times are Colorado times too. The record holds every entry this device made, with its signature and the server's receipt. Keep it: with it, you or anyone you give it to can check later that the published event log holds exactly what you entered and nothing else under your name:

```
corla-observer check-record corla-observer-record-Testland-board1-ann-2026-10-27T1124-0600.json events.jsonl --server-key receipt-key.json
```

(`receipt-key.json` is what the server publishes at `/receipt-key`; the server's own `receipt-key.pem` is accepted too.) For ann's record it printed the record's entries, all with receipts, "Log: intact", and a note for each later entry made after the download.

Narration: "Download your record: it's your own copy of everything you entered, with the server's receipts, so you can check the published log later."

### 16. The practice reader's view (45 s)

Rita's reader link opens "Practice reader: Testland, board 1" (`03-reader-start.png`): instructions, **Next ballot**, a setting for **Deliberate differences** (a changed vote, a wrong ID or a skipped ballot, about 1 in 10, 5 or 3, recorded for scoring), **Start this board over**, and the printouts. For each ballot it shows "First reading: read it aloud", the ID and every vote (`04-reader-first-reading.png`), then after **Second reading**, "Second reading: read it all aloud again" (`06-reader-second-reading.png`), then **Next ballot**. It doesn't show what observers entered; the reader keeps her own pace (`26-reader-later.png`, at the end).

Narration: "For practice, someone plays the audit board with a reader link. It can slip in deliberate mistakes, to see whether observers catch them."

### 17. Contest by contest (1 min)

Instead of two taps per ballot, an observer can confirm each contest as it is read. Ben taps **Contest by contest** in the switch at the top (`27-ben-contest-by-contest.png`): the same ballot header, the reading (**1 First reading**), "contest 1 of 42", and the contests as rows, each with the contest's name, the two readings' outcomes in the middle (○ not yet), and the choice; the focused row is outlined. Enter (or a tap on the row) confirms a contest and moves on (`28-ben-two-contests-confirmed.png`, two contests ✓); typing a letter opens a box for what was heard instead, with the contest's choices suggested; "?" marks it not heard clearly. The **Keys** button lists every key.

Narration: "If you prefer, follow contest by contest: confirm each one as it's read, or type what you heard instead."

### 18. Checking a printed page, or your screen, with your phone (1 min)

Under **More**, **Check a printed page, or send photos of your marked pages** opens "Check a printed page" for your own county's extract (`23-ann-check-page.png`), with your name and board at the top and **Back to your observation page**. Tap **Take a photo** (it opens the camera) or **Choose a photo or file**, then **Check this page**. Hold the phone square to the page, fill the frame, avoid glare. For page 1 the answer is "Testland · Board 1 · page 1 of 8 portrait-2 · extract a7898e7d", **MATCH**, "120 lines matched, 0 differ, 0 unread", then "this page is from a practice extract" and "not yet committed" (a Testland page is never committed) (`24-ann-check-result.png`). A screenshot of your own screen is checked the same way. At the end of the day, paper observers photograph each of their marked pages here, one photo per page: each is checked and kept for the coordinator.

Narration: "If you're using paper, photograph a page and the tool confirms every ID and vote on it is what was committed."

### 18a. Switching to a targeted list (30 s)

When the county also has targeted ballots (ballots the observers chose, which the county pulls at their request), a list appears in the header beside **More**: "RLA sample (127)", each sent list, "Targeted list 1 (20)", and "Look up a ballot (from a box)". Choose a targeted list: the page reloads on it, with "TARGETED (NOT RLA)" above and on each ballot's header, its own "0 of 20 done", and its own printouts under **More**. Your RLA sample entries and place are kept; choose "RLA sample" to go back. A ballot on the list that is also in the RLA sample says so: enter it on the RLA sample, where it counts for both. **Look up a ballot** (in that list, or under **More** when the board has the RLA sample alone) is for a ballot the board pulls from a storage box at the observers' request: type its imprinted ID, its expected votes come from the county's redacted CVR, and you enter it contest by contest; these are recorded apart from every list.

Narration: "Targeted ballots are a separate list, never mixed with the state's sample: switch at the top, and switch back."

### 19. The end of the session (30 s)

When every ballot has an entry, the page says "All of this board's ballots have an entry. Tap any ballot above to change it." and "Keep your own copy of what you entered, with the server's receipts:", with **Download my record** and **As a spreadsheet (CSV)** (`25-ann-all-done.png`, the whole page). Download both, then close the page. Paper observers: initial and date each marked page, and hand them to the coordinator, or photograph them on the check page.

Narration: "When the board is done, save your record. Thank you for observing."

## Friction found

What still doesn't work well, or is confusing, in these steps.

1. **A second device for the same observer** works and resumes from the server's progress, but the earlier entries show no receipt codes there, and its record holds only its own entries, so the observer has two records to keep.
2. **The reader and the observers aren't linked.** The reader's page doesn't show what observers entered, and observers can be ahead of or behind the reader; for a video, the reader must lead.
3. **The ballot list on a phone is long:** each ballot entered takes three lines, so the current ballot is far down the page after many entries (the page scrolls to it on each new ballot).
