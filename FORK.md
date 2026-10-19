# This fork

`shbernal/linkedin-mcp-server` is
[`stickerdaniel/linkedin-mcp-server`](https://github.com/stickerdaniel/linkedin-mcp-server)
plus tools for running a job search. Everything in the README holds here. This
file covers what differs.

## What only the fork has

Most of these wait in an upstream pull request. Once one merges, it drops off
this list.

| Tool or parameter | What it does | Upstream PR |
| --- | --- | --- |
| `unsave_job`, `archive_job` | Unsave or archive a job from the job tracker | - |
| `get_saved_jobs` | Walks every page of a tracker tab, not just the first ten jobs | - |
| `get_easy_apply_form` | Reads an Easy Apply form's questions without sending anything | [#1343](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1343) |
| `submit_easy_apply` | Answers and sends an Easy Apply form, in the old and the redesigned dialog | - |
| `reply_to_conversation` | Replies on an existing message thread | [#1342](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1342) |
| `archive_conversation` | Archives a message thread | [#1354](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1354) |
| `dismiss_job` | Dismisses a job from LinkedIn job search | [#1326](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1326) |
| `search_jobs` `pagination` | Says why a search stopped paging | [#1323](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1323) |
| `search_jobs` `start` | Resumes a search from a result offset | - |
| `search_jobs` `dismiss_ids` | Dismisses known jobs while searching | - |
| `search_jobs` on the redesigned page | Detects a search with no matches | [#1363](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1363) |
| Throttle evidence | A failed call names the 429 or 999 responses LinkedIn sent | [#1264](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1264) |
| `LINKEDIN_DEBUG_HOLD_MESSAGE_SEND` | Delays or aborts message sends, to test an unconfirmed send | never |

## Branches

- `main` mirrors upstream `main`. Nothing is committed on it.
- `jobsearch` is `main` plus a merge of each branch in
  [`fork/branches.txt`](fork/branches.txt), in that order. Every rebuild resets
  it to `main` and force-pushes it, so never base work on it.
- Topic branches hold one feature or fix each. They are force-pushed when
  rebased onto a newer `main`.
- `local/` branches hold changes that never go upstream, this file among them.

`python scripts/fork/check.py check` verifies that `jobsearch` matches the
manifest.

## Releases

A `jobsearch-v<n>` tag with a GitHub release is the only stable pin. Install
one with:

```bash
uvx --from git+https://github.com/shbernal/linkedin-mcp-server@jobsearch-v<n> mcp-server-linkedin
```

A `jobsearch-v<n>-rc<k>` tag is an untested candidate and has no release.
Each release's notes name the upstream commit it is built on and the branches
it carries.

## Contributing

Send fixes and features upstream to
[`stickerdaniel/linkedin-mcp-server`](https://github.com/stickerdaniel/linkedin-mcp-server).
That includes bugs in any tool upstream ships, even when you hit them here.

Open a pull request on this fork only against a fork-only topic branch or
`local/fork-meta`. Open an issue here only for what the fork alone has: the
tools and parameters above, their live checks, the `jobsearch` integration
and its releases.
