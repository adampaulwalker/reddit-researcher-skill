# Reddit Researcher

A Claude Code skill for researching what people say on Reddit: pain points, the tools they use, what they pay for and what frustrates them. The scripts fetch real posts and comments through Reddit's application programming interface (API), and Claude Code reads the results and writes the analysis in the same session. You don't need a separate AI model account or key.

`SKILL.md` is what Claude reads. This file covers setup, written for a Mac.

## What you need

- Claude Code.
- Python 3.10 or newer. The built-in `python3` on a Mac is 3.9, which is too old. Install a newer one with [Homebrew](https://brew.sh): `brew install python@3.12`.
- Your own Reddit API credentials. Reddit reviews each request before granting access, so send yours before you install.

## Reddit API access

Since November 2025, Reddit approves new Data API access by request rather than letting anyone create an app. Submit a request under the [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) and describe how you plan to use the data, including any commercial use. Reddit's [Data API Terms](https://redditinc.com/policies/data-api-terms) apply to everything this skill fetches.

Approval gives you a client ID and a client secret. They belong to you alone, so keep them out of chat, email and git.

Access to this repository and approval from Reddit are separate. Everything below works without credentials except an actual fetch.

## Install

Accept the GitHub invitation to this repository first. The repository is private, so the terminal needs to sign in to GitHub once. Install the GitHub command-line tool and follow its browser prompt, choosing HTTPS when asked:

```bash
brew install gh
gh auth login
```

Then clone straight into your personal skills folder and build the Python environment in place:

```bash
mkdir -p "$HOME/.claude/skills"
gh repo clone adampaulwalker/reddit-researcher-skill "$HOME/.claude/skills/reddit-researcher"
cd "$HOME/.claude/skills/reddit-researcher"
python3.12 -m venv --clear .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Check the install with the offline tests. They need no credentials and make no network calls:

```bash
./.venv/bin/python test_fail_closed.py --prove
./.venv/bin/python -m unittest test_fetch_raw
```

## Add your credentials

Once Reddit approves your request, create a private credentials file from the example:

```bash
cd "$HOME/.claude/skills/reddit-researcher"
(umask 077; mkdir -p "$HOME/.config"; [ -e "$HOME/.config/reddit-researcher.env" ] || cp .env.example "$HOME/.config/reddit-researcher.env"; chmod 600 "$HOME/.config/reddit-researcher.env")
open -e "$HOME/.config/reddit-researcher.env"
```

That opens the file in TextEdit. Replace the three placeholder values inside the existing quotes, keep the quotes, and save. The user agent is the label your script shows Reddit, so put your Reddit username where it says `YOUR_REDDIT_USERNAME`.

Run a small fetch to confirm the credentials work:

```bash
cd "$HOME/.claude/skills/reddit-researcher"
set -a; . "$HOME/.config/reddit-researcher.env"; set +a
mkdir -p data
./.venv/bin/python fetch_raw.py --subreddits smallbusiness --sort new --days 7 \
  --scan-limit 25 --max-records 3 --max-comments-per-post 0 --out data/smoke.json
```

A working setup ends with a short summary showing `returned_posts` above zero. r/smallbusiness gets new posts many times a day, so zero posts or an error here points to a setup problem rather than a quiet week. `Missing REDDIT_CLIENT_ID` means the credentials file wasn't loaded in this terminal window. For any other error, paste it into Claude Code and ask what it means.

## Use it

Start a new Claude Code session in any project, since a session that was already open won't see the skill. Ask in plain language, for example "What are people on Reddit saying about bookkeeping automation for small agencies?" or run `/reddit-researcher` directly. Claude picks subreddits, runs the fetch, reads the results and writes up the findings with links to the posts behind each point.

## Updating

```bash
cd "$HOME/.claude/skills/reddit-researcher"
git pull --ff-only
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python test_fail_closed.py --prove && ./.venv/bin/python -m unittest test_fetch_raw
```
