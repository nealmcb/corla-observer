# Relaying election night results to a server Clarity refuses

Clarity's election night reporting site, https://results.enr.clarityelections.com/CO/, sits behind
a bot challenge that refuses some addresses outright: it answers every request with HTTP 202 and an
empty body. Since 6 November 2026 every request from the corla-observer sprite's cloud address has
been refused this way, whatever the request's user agent, while a home connection is served at
once, with corla-observer's own user agent. The Audit Center (www.coloradosos.gov) serves the
sprite normally.

The server records each refusal as a `results.fetch_failed` event with `blocked` true, and
`/coord/estimates` says "Clarity refuses this server", with when the refusals began. The results
then have to come from somewhere Clarity does serve.

## The relay

`scripts/relay_results.sh`, run on a machine Clarity serves (Neal's `yl`), does it in two steps:

1. `corla-observer results download` saves the state election's current `detailxml.zip`, and the
   named counties' own `summary.json`, into a local directory, skipping any version already
   relayed.
2. For each new file, it copies it to the server (`~/results-relay/`) and records it there with
   `corla-observer results add`, under the coordinator running the relay, with its source URL, its
   scope and a note saying it was relayed and why. A county's `summary.json` does not say when it
   was generated, so it is recorded with the time Clarity's state settings list for that version of
   the county's results (`--file-time`), and ranks by that rather than by when it was relayed. That
   checks the totals and updates the estimates at once, as an upload by hand does.

A file is marked relayed (in `relayed.tsv`, in the relay's state directory) only after the server
has recorded it, so a failed run is simply repeated by the next one. It needs the local checkout's
`.venv` (or `CORLA_OBSERVER` naming the command) and ssh access to the server.

For the 2026 general, with the server's workspace and the pilot counties:

```
scripts/relay_results.sh corla-observer.sprite-app /home/sprite/workspaces/2026-general 127407 "La Plata" Weld Morgan
```

From cron, every 30 minutes during the canvass (the files change a few times a day at most, and a
run with nothing new makes two small requests to Clarity):

```
*/30 * * * * cd /srv/s/electionaudits/corla-observer && scripts/relay_results.sh corla-observer.sprite-app /home/sprite/workspaces/2026-general 127407 "La Plata" Weld Morgan >> ~/.local/state/corla-observer-relay/cron.log 2>&1
```

`RELAY_ACTOR` sets who is recorded as responsible (default `coordinator:` then `$USER`, else `$LOGNAME`, which cron sets), and `RELAY_STATE`
where the files and `relayed.tsv` are kept (default
`~/.local/state/corla-observer-relay/HOST`).

## What relayed results change

They count exactly like fetched ones: per scope, the newest file by the time Clarity generated it.
In the estimates (`corla_observer.estimate`), a targeted contest's margin comes from the latest
results, ahead of the target contest list's, until CORLA's own contest list is posted; its ballot
cards come from each county's ballots cast in the results times its cards per ballot, until the
manifests arrive. A relayed file also feeds the totals check, which compares the results with the
target list, the CVRs and CORLA's tallies.

The server keeps trying Clarity itself on its schedule. Once Clarity serves it again, the notice on
`/coord/estimates` clears, and the relay can stop.
