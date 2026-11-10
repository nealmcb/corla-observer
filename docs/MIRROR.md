# Mirroring the Audit Center

The public mirror of the Secretary of State's Audit Center,
[github.com/nealmcb/auditcenter](https://github.com/nealmcb/auditcenter), has kept the files the
Audit Center published since the 2017 audits, each at the path the Audit Center published it under,
so that every file stays available after the Audit Center moves on to the next election. Until now
it was updated by hand, with a scraper. This document describes how corla-observer's scheduler keeps
it up to date during an election instead: what it writes, where, when it is published, and what is
left to people.

Status: a design, not yet built.

## Terms

- **The scheduler** is the service that runs all the time on the server. It decides when to fetch,
  following the windows in `schedule.toml`: for the 2026 general, daily at noon until 12 November,
  hourly to the tabulation deadline on 13 November, every 3 hours over the weekend, every 3 minutes
  on roll day, and hourly during the audit.
- **A crawl** is one fetch of the Audit Center page and everything it links to under
  `/pubs/elections/RLA/`. The scheduler runs one in each cycle; a coordinator can also start one
  with **Fetch now** on `/coord`. The event log records every file a crawl finds new or changed.
- **The mirror update** is a step the scheduler takes after every crawl, writing what changed into
  the server's clone of the mirror. Nobody runs it by hand.
- **The server's clone** is the server's own git clone of the mirror (`~/auditcenter` on the
  sprite).
- **Publishing** is pushing the server's waiting mirror commits to GitHub.

## Where files go

The mirror's layout does not change:

- **Every file under `/pubs/elections/RLA/`** goes at its path relative to that directory: the
  2026 general's target contest list, published at
  `https://www.coloradosos.gov/pubs/elections/RLA/2026/general/files/2026GeneralRLAContestList.xlsx`,
  is `2026/general/files/2026GeneralRLAContestList.xlsx`.
- **The Audit Center page itself**, `https://www.coloradosos.gov/pubs/elections/auditCenter.html`,
  which lies outside that directory, is kept as dated snapshots in `auditCenters/`, named for the
  time it was fetched in Colorado time: `auditCenters/auditCenter_2026-11-06T17:24:08-07:00.html`.
  (Two snapshots from November 2022 at the top level predate that folder; they stay where they are.)
- **Nothing of the observers'** is written by the mirror update. Our files go in `observerfiles/`
  folders, written by `publish-evidence` (docs/TIMESTAMPING.md) or by people.

A file gets a new version in the mirror only when its content changed. For a page, content means
the page with the token Cloudflare puts in each response removed, so a page that differs only by
that token is never saved again; any other file is compared byte for byte. A file the Audit Center
stops publishing stays in the mirror: the mirror update never deletes, moves or renames anything,
and the event log records the file as missing.

## After each crawl

1. **Write.** Each Audit Center file whose content changed since the version in the server's clone
   is written there, at its path, from the copy the crawl stored in the workspace. The Audit Center
   page gets a new snapshot when its content differs from the latest snapshot.
2. **Commit.** If anything was written, the server's clone gets one commit for the crawl, headed
   `Audit Center files as fetched 2026-11-16 10:42 MST`. Its body lists each file as new or
   changed, and for a changed file the commit holding its previous version, so that the earlier
   version can be found.
3. **Wait.** The commit is not pushed yet. Commits from several crawls can wait together.
4. **Publish.** A coordinator sees the waiting commits on `/coord` and presses **Publish**; or, in
   windows where `schedule.toml` says so (on roll day, for instance), they are published
   automatically after each crawl. Publishing fetches GitHub's `main`, replays the waiting commits
   on top of it, each still its own commit and in the order of the crawls, and pushes. Only then
   does the event log record `mirror.updated`, naming the pushed commits and the files in them.

A crawl is never refused or held up by the state of the server's clone: if a commit or a push
fails, the crawl's own record is complete, the failure is shown on `/coord` and in the crawl's
summary, and the next mirror update tries again.

`publish-evidence` commits and pushes in the same clone. It publishes any waiting mirror commits
first, so the mirror's history stays in fetch order.

## Links outside the RLA directory

The mirror holds only what is under `/pubs/elections/RLA/` and the Audit Center page. A crawl also
lists the page's links that lead elsewhere and might matter to the audit, such as a notice of the
seed meeting on another page of the Secretary of State's site, or a video of it. Links that appear
on every page of the site (its navigation and footer) are left out. Each such link is shown on
`/coord` and in the crawl's summary once, when it first appears, for a person to decide whether it
belongs in the record; it is not fetched or mirrored automatically.

## The crawler's scope

- It follows every page under `/pubs/elections/RLA/` that the Audit Center links to, however many
  links away, as the scraper does; it currently stops two links from the Audit Center page.
- It ignores links to a part of the same page (`#...`).
- It still fetches, on every crawl, the addresses the setup expects (`probe_urls` in
  `election.toml`) before the Audit Center links them, recording each that is not there yet once.
- It fetches at most 2,000 addresses in one crawl. The cap keeps a link loop or a broken page from
  making a crawl run without end, or from sending the site thousands of requests; an election's
  Audit Center has a few hundred files at most. A crawl that reaches the cap is marked incomplete
  and flagged on `/coord`, and nothing it did not reach is treated as removed.

## Two clones

The server's clone and a person's own clone (such as one used with the scraper) both feed GitHub.

- **Before pushing,** the server fetches GitHub's `main` and replays its waiting commits on top.
  They are not public yet, so this is safe. Where someone has meanwhile pushed the same file, the
  server keeps GitHub's version if its content is the same (for a page, apart from Cloudflare's
  token), and otherwise commits its own newer version on top, naming both.
- **In a person's own clone,** run `git pull` before running the scraper or editing: otherwise it
  falls behind the server's pushes.
- **To avoid two writers for one election,** the scraper is used only for past elections, and the
  server mirrors the current one.
- The server's clone is sparse, holding only the folders it needs (`git sparse-checkout`); the
  mirror update adds an election's folder and `auditCenters/` to it when it first writes there.

## Pushing to GitHub

The server pushes with a **fine-grained GitHub token** for `nealmcb/auditcenter` only, with
permission to write its contents and nothing else, and an expiry date after the audit, so that
publishing works unattended, including on roll day.

- The token is kept in a file in the server's secrets directory, readable only by the server's
  user, outside the workspace and every repository. It is created by hand, never passed on a
  command line or written into a remote URL.
- The server's clone reads it through a git credential helper set for that one repository's
  address, and pushes over HTTPS. Its remote gets an explicit HTTPS push URL, since the sprite's
  global setting rewrites every HTTPS push to GitHub as an SSH one, which would bypass the token.
- Publishing records which way it pushed in `mirror.updated`.

**If the token is missing, expired or refused,** the waiting commits stay waiting and `/coord`
says so. They are then published from Neal's machine (`yl`): fetch the server's clone over SSH,
which brings the waiting commits with it, and push them from there with Neal's own key:

```
git -C /srv/voting/audit/corla/scrapy/mirror fetch corla-observer.sprite-app:auditcenter main
git -C /srv/voting/audit/corla/scrapy/mirror merge --ff-only FETCH_HEAD
git -C /srv/voting/audit/corla/scrapy/mirror push origin main
```

The next mirror update on the server sees its commits on GitHub's `main` and records them as
published.

## Timestamps are separate

The mirror update never signs or timestamps anything. Which files a timestamp lists, and when, is
described in docs/TIMESTAMPING.md: pages never, the upload status and the audit activity report
only when a coordinator asks, and every other Audit Center file. A timestamp names files at the
paths the mirror keeps them, so its check works in a copy of the mirror.

## The scraper

The scraper (`auditcenter/spiders/audit_data.py` in the scrapy project that writes to the mirror)
remains the tool for past elections. It differs from the mirror update in ways that matter for the
current one: it skips a file that already exists unless run with `--recheck`, so a changed file is
missed; it does not save the Audit Center page; and it fails on files at the top of the RLA
directory, such as `faqs.html`.
