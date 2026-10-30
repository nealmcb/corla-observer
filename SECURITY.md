# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's private vulnerability reporting for this repository: on the repository's **Security** tab, choose **Report a vulnerability**. (This needs private vulnerability reporting to be enabled in the repository's settings, under Security.) If that is not available, contact the maintainer listed on the GitHub profile of the repository's owner, without including the details in a public place.

Please do not open a public issue, pull request or discussion about a vulnerability.

## What to report

Anything that would let someone:

- alter or forge the event log, observer entries, receipts, evidence commitments or extracts without detection;
- read or change data without authorization, including through coordinator access or observer and reader links;
- bypass the redaction check, or make it pass a file that exposes voters;
- show expected votes to an audit board;
- deny service on audit days;
- compromise the software through its dependencies (supply-chain issues).

Privacy problems in data the tool handles or publishes, such as a published cast vote record that exposes voters, are reported the same way: privately, never in a public issue.

## What to expect

Reports are acknowledged, and answered with a timeline for a fix or a mitigation, on a best-effort basis: this is a volunteer project. Disclosure is coordinated with the reporter, and reporters are credited if they wish.

## Supported versions

The `main` branch, and the latest deployed commit, which every instance shows on its `/version` page.

## Scope

The operators of a hosted instance, not this project, are responsible for their own deployment. Problems with third-party sites, such as a county's website or the Colorado Secretary of State's, should be reported to them directly.
