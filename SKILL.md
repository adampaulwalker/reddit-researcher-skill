---
name: reddit-researcher
description: Research any topic on Reddit - pain points, tool adoption, real user experiences, pricing complaints. Fetches real posts and comments via the Reddit API, then you analyze them in-session. Triggers on "what are people saying about", "reddit research", "search reddit", "pain points with", "find discussions about".
---

# Reddit Researcher

Pulls real Reddit discussions and hands them to you as structured data. **You do
the analysis yourself, in this session, on whatever model you are running.**

There is no second agent, no model API key, and no model name anywhere in this
skill. The scripts only fetch data. They do need **Reddit API credentials** - see
Credentials below.

An earlier version shelled out to a standalone app that made its own model API
calls against a hardcoded model. That pinned model was retired and every run
started returning HTTP 404, and the nested calls billed per-token. Fetch-only
removes both problems by construction.

## Run it

**Use `fetch_raw.py`.** It speaks to `oauth.reddit.com` directly and is the
default for any sweep wider than a couple of subreddits. Always source the
credentials in the same shell invocation, always use the skill's own venv, and
create the output directory first - neither fetcher creates it.

```bash
set -a; . "$HOME/.config/reddit-researcher.env"; set +a
mkdir -p data

"$HOME/.claude/skills/reddit-researcher/.venv/bin/python" \
  "$HOME/.claude/skills/reddit-researcher/fetch_raw.py" \
  --subreddits "accounting,lawfirm,recruiting" \
  --keywords "AI,ChatGPT,Claude,Copilot,automation" \
  --min-signals 2 \
  --use-signal default \
  --days 30 \
  --max-comments-per-post 0 \
  --out data/sweep.json
```

With `--out`, stdout is only a summary (`meta` plus `output_file`). The full
`{"meta": {...}, "records": [...]}` object is in the file: read it selectively
and write the synthesis yourself.

`fetch_reddit.py` is the alternative. It goes through praw, is much slower, and
its defaults and failure behavior differ - see the table below. Records share
the same core fields in both, but `meta` and some record fields differ.

Several subreddits in one call are accepted, but `--max-records` applies after
ranking across all of them, so the busiest can crowd quieter ones out. Run one
subreddit per invocation when balanced coverage matters.

### Why there are two fetchers, and why speed was the smaller problem

Measured 2026-09-11 on a real 39-subreddit sweep. praw returned
`TooManyRequests` on **37 of 39 pulls**, then self-throttled to **125 seconds for
a single 100-page**. The same requests made directly finish 1,000 posts in 17
seconds using 10 of the window's 1,000 requests. The rate limit was never the
constraint; praw's pacing was.

The worse half: **each of those 37 failures wrote a well-formed file containing
zero posts, and exited 0.** A throttled pull and a genuinely quiet subreddit
produced byte-identical output. Downstream that reads as "no discussion exists"
and is believed.

Both scripts now fail closed:

- zero records is a **non-zero exit** unless you pass `--allow-empty`
- every-subreddit-failed is **always** an error, never an empty result
- a failure **quarantines** a stale `--out`, moving it into a uniquely named
  `<out>.superseded-YYYYMMDD-HHMMSS.<random>/` directory beside it (a timestamp
  alone let two failures in one second overwrite the first backup). The path is
  gone, so a resume check cannot
  trust yesterday's file; the bytes are not, so a transient failure on a refresh
  cannot destroy a corpus that took hours to collect. An earlier version deleted
  it, and two independent reviewers called that out.
- success publishes **atomically** through a unique temp file created with
  `tempfile.mkstemp` in the destination directory. A shared `.partial` name let
  two concurrent runs on the same `--out` clobber each other and publish an empty
  result; that was reproduced in review, not theorised.

**Two overrides, deliberately separate.** `--allow-empty` accepts a zero-record
result from a COMPLETE collection. `--allow-incomplete` accepts a run where some
subreddits failed. The dangerous combination is both at once, because that is the
shape the original defect wore, so publishing an empty result from a failed pull
needs BOTH flags. A single override was tried first and review found it re-opened
the bug: 37 of 39 failing does not trip the all-failed guard, so `--allow-empty`
alone waved the original defect straight back through.

A partial pull that still returned records DOES publish, labelled
`collection_complete: false` with `failed_subreddits` in `meta`. One private or
banned subreddit must not kill a run; the disclosure is the metadata, and a
downstream check can read it rather than guess.

**The two fetchers differ here, and it is worth knowing which you are running.**
`fetch_reddit.py` catches per subreddit, so it can be partially complete and
computes those two fields. `fetch_raw.py` has no per-subreddit catch: any
subreddit failure aborts the whole run, so it is complete by construction and
harsher - one banned subreddit in a 40-subreddit sweep fails the sweep.

Fatal failures go through one boundary: non-zero exit, and an attempt to
quarantine a stale `--out` file, with the outcome printed to stderr as
`previous_output_moved_to`. In `fetch_raw.py` that covers missing credentials, a
failed token refresh, a malformed response and a failed write. In
`fetch_reddit.py` it covers missing credentials and a failed write, while a
per-subreddit failure is recorded in `errors[]` and the run can still publish
partial records. Argument errors are rejected before any network call and touch
nothing. Always check the exit status and `meta`, not just whether the file
exists.

Proof the guards are load-bearing, not decoration (offline, no credentials, no
network):

```bash
cd "$HOME/.claude/skills/reddit-researcher"
./.venv/bin/python test_fail_closed.py --prove   # fetch_reddit.py
./.venv/bin/python -m unittest test_fetch_raw    # fetch_raw.py
```

`--prove` reverts the empty-result guard in memory and asserts the 37-of-39
shape goes back to exiting 0 with an empty file, so the suite is shown to test
something.

## Setup

Python 3.10 or newer (the pinned praw and requests releases require it). Create
the venv at the final install location - never copy one from another machine:

```bash
cd "$HOME/.claude/skills/reddit-researcher"
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

## Credentials

Read from the environment only, never passed as flags (flags leak into shell
history and process listings): `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`,
`REDDIT_USER_AGENT` (optional, but Reddit asks for a descriptive one naming your
Reddit username).

Since November 2025 Reddit requires approval before any new Data API access, so
credentials come from an approved request under Reddit's Responsible Builder
Policy, not from creating an app on the spot. Describe the real intended use in
that request, including commercial use if it applies. Credentials are personal:
never share them or use someone else's.

Keep them in `~/.config/reddit-researcher.env` with `chmod 600`, starting from
`.env.example` here. The scripts do not load that file themselves; source it in
the same shell invocation as the fetch. Never commit real credentials.

## Options that matter

Defaults differ between the fetchers. `raw` is `fetch_raw.py`, `praw` is
`fetch_reddit.py`.

| Flag | raw | praw | Why you would change it |
|---|---|---|---|
| `--subreddits` | required | required | Comma-separated, no `r/` prefix. |
| `--keywords` | none | none | Matched on **word boundaries**. Empty keeps everything. |
| `--min-signals` | 1 | 1 | How many distinct keywords a post must hit. |
| `--use-signal` | off | off | Extra regex a post must match. `default` = built-in usage pattern. |
| `--days` | 365 | 30 | Look-back window, UTC. |
| `--sort` | new | top | `new` plus local filtering has better recall than search. |
| `--scan-limit` | 1000 | 100 | Posts examined per subreddit **before** filtering. |
| `--rank` | score | score | Ordering, and therefore what `--max-records` throws away. |
| `--max-records` | 1000 | 40 | Posts returned. |
| `--max-comments-per-post` | 0 | 0 | Off by default. Comments are where volume explodes. |
| `--comment-posts` | 0 = **all** kept posts | 10 (0 = none) | How many top posts get comments. Always set both comment flags explicitly. |
| `--body-chars` | 100000 | 800 | Post body cap. |
| `--comment-chars` | 8000 | 400 | Comment body cap. |
| `--max-output-chars` | n/a | 120000 | praw only, and only without `--out`. Stdout ceiling including the newline; records drop past it, and a ceiling that leaves no record is an error unless the empty-result overrides allow it. |
| `--out FILE` | **required** | optional | Write full JSON to a file, print only a summary. Must be a file path, not a directory. |
| `--allow-empty` | yes | yes | Accept a zero-record result from a complete collection. |
| `--allow-incomplete` | n/a | yes | praw only. Needed together with `--allow-empty` to publish zero records when some subreddits failed. |

## Filtering: the trap to avoid

**Never filter Reddit text with naive substring matching.** A bare `ai` matches
"r**ai**ses", "ag**ai**nst", "em**ai**l" and floods results with irrelevant
threads. This was measured, not theoretical: a substring filter returned 419
posts that were mostly generic career complaints, and word-boundary matching plus
one corroborating signal cut the same pull to 20 genuinely relevant posts.

The script matches on word boundaries already. Two knobs tune precision:

- `--min-signals 2` for broad terms like `AI`, so a post must mention two
  distinct terms to qualify.
- `--min-signals 1` for niche product names, where two signals would suppress
  valid hits.
- `--use-signal default` to require language describing actual use ("I use",
  "we switched to", "saves me", "rolled out") rather than a passing mention.

Start broad, look at what comes back, then tighten. Say what you filtered out.

## Score is not relevance

**Upvotes measure agreement, not usefulness.** On Reddit the posts that describe
what someone actually built score low, because few people have done the same
thing. The posts that score high are opinion threads everyone can react to.

This was measured on a real pull across 12 professional-services subreddits:

| Post | Score | Value |
|---|---|---|
| "People who say ai will takeover accounting do not know accounting" | 375 | Debate. No implementation detail. |
| "Claude Connection for multiple QBO companies" | 23 | Names the exact product limit and ships a fix. |
| "Half of my screening is still phone calls..." | 13 | An unmet need stated by the person who has it. |
| "Clio Work Vs. Claude" | 11 | A firm mid-decision between two named tools. |
| "AI notetaker... useless for hiring decisions" | 0 | Specific, reproducible failure of a tool category. |

Ranked by score with `--max-records 6`, **all four useful posts are dropped and
the debate thread is kept.** So:

- Set `--max-records` generously and read down the list. Do not assume the top
  of the list is the useful part.
- Use `--rank comments` to surface contested threads. A post with 0 points and
  35 comments is a live argument, which is usually where the real detail sits.
- When you cite something, cite it for what it says, not for its score. Say
  plainly when a finding rests on one person's account.

## Managing volume

Comments are the main risk to the context window, so they are **off by default**.
The script fetches in two passes: rank posts first, then pull comments only for
the top `--comment-posts`, and it never expands full comment trees.

For a wide sweep, write to a file and read it selectively:

```bash
set -a; . "$HOME/.config/reddit-researcher.env"; set +a
mkdir -p data
"$HOME/.claude/skills/reddit-researcher/.venv/bin/python" \
  "$HOME/.claude/skills/reddit-researcher/fetch_reddit.py" \
  --subreddits "smallbusiness,Entrepreneur,agency" \
  --keywords "AI,automation" --min-signals 2 \
  --max-comments-per-post 5 --comment-posts 10 \
  --max-records 100 --out data/reddit-pull.json
```

## Reading the output

`meta` carries the honesty fields. Always check them before drawing conclusions:

- `matched_posts` vs `returned_posts` and `truncated` - was anything cut?
- `collection_complete` and `failed_subreddits` (praw) - did every subreddit
  listing finish? A subreddit can return some posts before a later page fails,
  so a failed subreddit may be **partially** represented. Say which subreddits
  failed rather than implying full coverage.
- `errors[]` (praw) - subreddit listing failures and comment-fetch failures.
  Comment failures do not change `collection_complete`, which describes the
  listings only.
- `cutoff_utc` - the actual window boundary.

Each record has `matched_terms`, so you can see why a post qualified.

## Writing the synthesis

- Group findings by theme, not by subreddit.
- Quote directly and link the `permalink` for every claim.
- Give frequency honestly: "4 of 20 posts raised confidentiality."
- Separate what people *did* from what they *speculate* about.
- Note when a thread is one person's anecdote rather than a pattern.
- If the data is thin, say so. Do not pad it out.

## Failure modes

| Symptom | Cause |
|---|---|
| Exit 2, `Missing ...` credentials | Credentials not sourced in this shell invocation. |
| `--out directory does not exist` | Create it first (`mkdir -p`). Checked before any network call. |
| A subreddit in `errors[]` | Private, banned, quarantined, or misspelled. |
| `429` in `errors[]` | Rate limited. Switch to `fetch_raw.py`, which paces off the response headers instead of guessing. |
| Non-zero exit, `kept 0 records` | Working as designed. The pull returned nothing and the script refuses to publish a file that would read as "no discussion exists". Check `errors[]` in stderr before assuming the topic is quiet. |
| Non-zero exit, `every requested subreddit failed` | A broken pull, not an empty result. Usually credentials or rate limiting. |
| Exit 4 (raw) | A request or response failed. The whole run aborted; nothing was published. |
| Exit 7 | The result could not be written. |
| Exit 5, `left room for 0 of N records` (praw) | `--max-output-chars` left no room for any record. Raise it or use `--out`. |
| Exit 8, `metadata alone exceeds` (praw) | `--max-output-chars` is smaller than the metadata. Raise it or use `--out`. |
| `--out must be a file path` | `--out` names a directory. Give a file name inside it. |
| Few results on a broad topic | `--min-signals` too high, or window too short. |
| Junk results | Add `--use-signal default`, or raise `--min-signals`. |

On a fatal failure a stale `--out` file is quarantined, not deleted: stderr
reports the attempt as `previous_output_moved_to`, which names the new location
or says the move failed.

Reddit listings cap out around 1000 items per endpoint, so a very wide sweep is
sampled rather than exhaustive. Deleted and removed content is skipped, and
crossposted duplicates are collapsed.
