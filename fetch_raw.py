#!/usr/bin/env python3
"""Fetch Reddit posts and comments over raw OAuth HTTP.

Why this exists, since the reddit-researcher skill already ships a fetcher.
That fetcher uses praw, and on this client praw returned `TooManyRequests` on
37 of 39 pulls and then self-throttled to **125 seconds for a single 100-post
page**, measured. The same requests made directly finish in under a second each:
1,000 posts in 17.4 seconds with 645 of the window's 1,000 requests still unused.
The limit was never the problem; praw's pacing was. So this speaks to
`oauth.reddit.com` directly, paces itself off the response headers, and writes
the JSON shape `fetch_reddit.py` writes, so the analysis scripts are unchanged.

The other reason it exists: a throttled pull must never look like an empty
subreddit. Both fetchers now refuse to publish a result they cannot vouch for.

Code review found six real defects in the first version. All are fixed here and
each fix is commented where it lives:
  * a scan that returned posts but ended with zero records wrote a clean-looking
    empty file - now a non-zero exit unless --allow-empty is passed;
  * a malformed or error-shaped response became an empty listing and published
    partial data as if complete - now raises;
  * the pacing clamp of 5s defeated quota pacing, and remaining==0 updated
    nothing - now uncapped, and an exhausted window sleeps until reset;
  * the 429 branch capped its wait at 120s, far short of the advertised window;
  * "no file is written on failure" was false when --out already existed - now a
    failure quarantines a stale destination, and success publishes atomically;
  * raw_json=1 was missing, so Reddit's legacy HTML escaping (&amp;, &lt;) would
    have gone straight into a vocabulary frequency count.

Credentials come from the environment only:
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT (optional)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time

import requests

API = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

DEFAULT_USE_SIGNAL = (
    r"\b(I use|I've been using|we use|switched to|set ?up|built|workflow|"
    r"saves? me|cut my|replaced|tried|testing|implement\w*|stack|rolled out)\b"
)


class BadResponse(RuntimeError):
    """A response that is not the shape Reddit documents. Never swallowed."""


class MissingCredentials(RuntimeError):
    """Raised instead of SystemExit so a stale --out is still quarantined."""


class RateLimitedSession:
    """One session, paced off Reddit's own headers.

    Reddit answers every call with x-ratelimit-remaining and x-ratelimit-reset.
    Spreading what is left over the time that is left keeps the window from ever
    emptying, which is what turns a fast run into a throttled one.
    """

    def __init__(self, ua: str, min_gap: float = 0.6):
        self.ua = ua
        self.min_gap = min_gap
        self.session = requests.Session()
        self.token = None
        self.next_allowed = 0.0
        self.requests_made = 0
        self.rate_limit_waits = 0
        self._authenticate()

    def _authenticate(self):
        cid = os.environ.get("REDDIT_CLIENT_ID")
        sec = os.environ.get("REDDIT_CLIENT_SECRET")
        if not cid or not sec:
            raise MissingCredentials("Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
        r = requests.post(
            TOKEN_URL, auth=(cid, sec), data={"grant_type": "client_credentials"},
            headers={"User-Agent": self.ua}, timeout=30,
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self.session.headers.update(
            {"Authorization": f"bearer {self.token}", "User-Agent": self.ua}
        )

    @staticmethod
    def _header_float(headers, name, default):
        try:
            v = float(headers.get(name, default))
            return v if v == v and v not in (float("inf"), float("-inf")) else default
        except (TypeError, ValueError):
            return default

    def _pace(self, headers):
        """Spread the remaining budget over the remaining window.

        No upper clamp. From review: clamping to 5 seconds meant that with one
        request left and ten minutes on the clock the next call went out five
        seconds later and got a 429 - which is precisely the state praw sat in.
        """
        remaining = self._header_float(headers, "x-ratelimit-remaining", 100.0)
        reset = self._header_float(headers, "x-ratelimit-reset", 60.0)
        if remaining > 0:
            self.min_gap = max(0.6, reset / remaining)
            self.next_allowed = time.monotonic() + self.min_gap
        else:
            # The window is spent. Waiting it out is the only correct move.
            self.rate_limit_waits += 1
            self.next_allowed = time.monotonic() + reset + 2

    def get(self, path: str, params: dict | None = None, tries: int = 6):
        last_error = None
        params = dict(params or {})
        params["raw_json"] = 1          # or &amp; lands in the word counts
        for attempt in range(tries):
            gap = self.next_allowed - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            try:
                r = self.session.get(f"{API}{path}", params=params, timeout=45)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(min(2 ** attempt, 30))
                continue
            self.requests_made += 1
            self.next_allowed = time.monotonic() + self.min_gap

            if r.status_code == 401:
                self._authenticate()
                last_error = "401, re-authenticated"
                continue
            if r.status_code == 429:
                # Wait the FULL advertised window. This still consumes one of
                # the bounded attempts; the first version capped each wait at
                # 120 seconds against a 600-second reset and ran out of tries.
                retry_after = self._header_float(r.headers, "retry-after", 0.0)
                reset = self._header_float(r.headers, "x-ratelimit-reset", 60.0)
                wait = max(retry_after, reset) + 2
                last_error = f"429, waiting {wait:.0f}s for the window"
                self.rate_limit_waits += 1
                time.sleep(wait)
                self.next_allowed = time.monotonic()
                continue
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                time.sleep(min(2 ** attempt, 30))
                continue

            self._pace(r.headers)
            try:
                return r.json()
            except ValueError as exc:
                raise BadResponse(f"GET {path} returned non-JSON: {exc}") from exc
        raise RuntimeError(f"GET {path} failed after {tries} tries: {last_error}")


def build_pattern(keywords: str):
    terms = [k.strip() for k in keywords.split(",") if k.strip()]
    if not terms:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)


def truncate(text: str, limit: int):
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean, False
    return clean[:limit].rstrip() + "...", True


def top_bucket(days: int) -> str:
    """Smallest bucket that still covers the window; the cutoff is local."""
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 366:
        return "year"
    return "all"


def listing(sess, subreddit, sort, scan_limit, days):
    """Page a listing. Reddit caps every listing near 1,000 items.

    Validates the envelope rather than defaulting a bad response to an empty
    page, and refuses to loop on a repeated cursor.
    """
    path = f"/r/{subreddit}/{sort}"
    params = {"limit": 100}
    if sort == "top":
        params["t"] = top_bucket(days)
    after, seen, seen_cursors, stop = None, [], set(), "listing exhausted"
    while len(seen) < scan_limit:
        if after:
            params["after"] = after
        payload = sess.get(path, params)
        if not isinstance(payload, dict) or payload.get("kind") != "Listing":
            raise BadResponse(f"r/{subreddit}/{sort}: not a Listing: {str(payload)[:200]}")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("children"), list):
            raise BadResponse(f"r/{subreddit}/{sort}: Listing without children")
        children = data["children"]
        if not children:
            break
        for c in children:
            if c.get("kind") != "t3" or not isinstance(c.get("data"), dict):
                raise BadResponse(f"r/{subreddit}/{sort}: unexpected child kind {c.get('kind')}")
            seen.append(c["data"])
        after = data.get("after")
        if not after:
            break
        if after in seen_cursors:
            raise BadResponse(f"r/{subreddit}/{sort}: cursor {after} repeated")
        seen_cursors.add(after)
    if len(seen) >= scan_limit:
        stop = "hit --scan-limit"
    return seen[:scan_limit], stop


def fetch_comments(sess, subreddit, post_id, want, char_cap):
    payload = sess.get(f"/r/{subreddit}/comments/{post_id}",
                       {"limit": want, "depth": 1, "sort": "top"})
    if not isinstance(payload, list) or len(payload) < 2:
        raise BadResponse(f"comments/{post_id}: expected two listings, got {type(payload).__name__}")
    body = payload[1]
    if not isinstance(body, dict) or not isinstance(body.get("data", {}).get("children"), list):
        raise BadResponse(f"comments/{post_id}: comment listing malformed")
    picked, skipped = [], 0
    for child in body["data"]["children"]:
        if child.get("kind") != "t1":      # `more` placeholders, not comments
            skipped += 1
            continue
        c = child.get("data", {})
        text = c.get("body") or ""
        if text.strip() in ("[deleted]", "[removed]"):
            skipped += 1
            continue
        excerpt, truncated = truncate(text, char_cap)
        picked.append({
            "score": c.get("score", 0),
            "author": c.get("author") or "[deleted]",
            "body_excerpt": excerpt,
            "body_truncated": truncated,
        })
        if len(picked) >= want:
            break
    return picked, skipped


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--subreddits", required=True,
                   help="Comma-separated, no r/ prefix. Several are accepted, but "
                        "--max-records is applied AFTER ranking across all of "
                        "them, so the busiest can crowd quieter ones out of the "
                        "cap. Run one subreddit per invocation when balanced "
                        "coverage matters.")
    p.add_argument("--keywords", default="")
    p.add_argument("--min-signals", type=int, default=1)
    p.add_argument("--use-signal", default="")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--sort", default="new", choices=["new", "top", "hot"])
    p.add_argument("--scan-limit", type=int, default=1000)
    p.add_argument("--rank", default="score", choices=["score", "comments", "recency"])
    p.add_argument("--max-records", type=int, default=1000)
    p.add_argument("--max-comments-per-post", type=int, default=0)
    p.add_argument("--comment-posts", type=int, default=0)
    p.add_argument("--body-chars", type=int, default=100_000,
                   help="Effectively no truncation by default. A body clipped at "
                        "4,000 characters can drop the one jargon mention the "
                        "study is counting.")
    p.add_argument("--comment-chars", type=int, default=8_000)
    p.add_argument("--allow-empty", action="store_true",
                   help="Permit a successful run that yields zero records. Only "
                        "correct when a keyword filter is expected to reject "
                        "everything; otherwise zero means a broken pull.")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    # Validate everything that can be checked offline BEFORE any network call, so
    # a typo does not cost a full sweep and then fail at publication.
    for name, val in (("--scan-limit", args.scan_limit), ("--max-records", args.max_records),
                      ("--days", args.days)):
        if val <= 0:
            raise SystemExit(f"{name} must be positive; {val} only manufactures an empty result")
    for name, val in (("--max-comments-per-post", args.max_comments_per_post),
                      ("--comment-posts", args.comment_posts),
                      ("--body-chars", args.body_chars), ("--comment-chars", args.comment_chars)):
        if val < 0:
            raise SystemExit(f"{name} must not be negative, got {val}")
    subs = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    if not subs:
        raise SystemExit("--subreddits named no subreddit")
    out_parent = os.path.dirname(os.path.abspath(args.out))
    if not os.path.isdir(out_parent):
        raise SystemExit(f"--out directory does not exist: {out_parent} (create it first)")
    if os.path.isdir(args.out):
        # Quarantine moves whatever sits at --out. Pointed at a directory, a
        # failure would have moved the whole directory (review).
        raise SystemExit(f"--out must be a file path, not a directory: {args.out}")

    pattern = build_pattern(args.keywords)
    if args.use_signal == "default":
        use_signal = re.compile(DEFAULT_USE_SIGNAL, re.I)
    elif args.use_signal:
        use_signal = re.compile(args.use_signal, re.I)
    else:
        use_signal = None

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=args.days)
    ua = os.environ.get("REDDIT_USER_AGENT", "reddit-researcher-skill/1.0")

    def fail(msg, code):
        # A stale destination must not stay where a resume check will trust it -
        # that is the poisoned-file class and the reason this guard exists.
        #
        # But 2026-09-11 review, converged across two reviewers: DELETING it is
        # destructive and reachable on a transient failure. A single malformed
        # listing on a refresh would have destroyed a good corpus collected over
        # hours. So the prior file is QUARANTINED, not removed: moved out of the
        # way, which keeps the resume check honest (the path is gone) without
        # losing data (the bytes are not).
        quarantined = None
        # Files only. Never move a directory, even if one appeared after the
        # up-front check.
        if os.path.isfile(args.out):
            try:
                # A timestamp alone is NOT unique: two fail/publish/fail cycles in
                # the same second made os.replace overwrite the first backup
                # (reproduced in review). mkdtemp creates the directory
                # exclusively, so no second run can land on the same path.
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                holder = tempfile.mkdtemp(
                    prefix=f"{os.path.basename(args.out)}.superseded-{stamp}.",
                    dir=out_parent,
                )
                quarantined = os.path.join(holder, os.path.basename(args.out))
                os.replace(args.out, quarantined)
            except OSError as exc:                       # never mask the real error
                quarantined = f"(quarantine failed: {exc})"
        print(
            json.dumps({"error": msg, "previous_output_moved_to": quarantined}),
            file=sys.stderr,
        )
        return code

    # Authentication sits INSIDE the failure boundary. It used to run before it,
    # so a token-endpoint timeout or a 401 exited without quarantining a stale
    # destination - the one case where the resume check is most likely to trust
    # yesterday's file forever (reproduced in review with a mocked ConnectionError).
    # Missing credentials go through the same boundary for the same reason.
    try:
        sess = RateLimitedSession(ua)
    except MissingCredentials as exc:
        return fail(str(exc), 2)
    except Exception as exc:
        return fail(f"authentication failed: {type(exc).__name__}: {exc}", 2)

    records, seen_ids = [], set()
    drops = {"older_than_cutoff": 0, "duplicate_id": 0, "deleted_body": 0,
             "failed_keywords": 0, "failed_use_signal": 0, "crosspost": 0}
    scanned, stops = 0, {}

    try:
        for name in subs:
            posts, stop = listing(sess, name, args.sort, args.scan_limit, args.days)
            stops[name] = stop
            scanned += len(posts)
            for post in posts:
                ts = post.get("created_utc")
                if not isinstance(ts, (int, float)):
                    raise BadResponse(f"r/{name}: post {post.get('id')} has no numeric created_utc")
                created = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
                if created < cutoff:
                    drops["older_than_cutoff"] += 1
                    continue
                if post.get("id") in seen_ids:
                    drops["duplicate_id"] += 1
                    continue
                body = post.get("selftext") or ""
                if body.strip() in ("[deleted]", "[removed]"):
                    drops["deleted_body"] += 1
                    continue
                blob = f"{post.get('title', '')} {body}"
                matched = []
                if pattern is not None:
                    matched = sorted({m.group(0).lower() for m in pattern.finditer(blob)})
                    if len(matched) < args.min_signals:
                        drops["failed_keywords"] += 1
                        continue
                if use_signal is not None and not use_signal.search(blob):
                    drops["failed_use_signal"] += 1
                    continue
                if post.get("crosspost_parent"):
                    drops["crosspost"] += 1
                    continue
                seen_ids.add(post["id"])
                excerpt, truncated = truncate(body, args.body_chars)
                records.append({
                    "type": "post",
                    "subreddit": name,
                    "id": post["id"],
                    "created_utc": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "title": post.get("title", ""),
                    "body_excerpt": excerpt,
                    "body_truncated": truncated,
                    "score": post.get("score", 0),
                    "num_comments": post.get("num_comments", 0),
                    "permalink": f"https://reddit.com{post.get('permalink', '')}",
                    "author": post.get("author") or "[deleted]",
                    "matched_terms": matched,
                    "comments": [],
                    "comments_fetched": False,
                })

        if args.rank == "comments":
            records.sort(key=lambda r: r["num_comments"], reverse=True)
        elif args.rank == "recency":
            records.sort(key=lambda r: r["created_utc"], reverse=True)
        else:
            records.sort(key=lambda r: r["score"], reverse=True)

        matched_posts = len(records)
        records = records[: args.max_records]

        comment_stats = {"posts_attempted": 0, "comments_kept": 0, "children_skipped": 0}
        if args.max_comments_per_post > 0:
            targets = records[: args.comment_posts or len(records)]
            for rec in targets:
                picked, skipped = fetch_comments(
                    sess, rec["subreddit"], rec["id"],
                    args.max_comments_per_post, args.comment_chars)
                rec["comments"] = picked
                rec["comments_fetched"] = True
                comment_stats["posts_attempted"] += 1
                comment_stats["comments_kept"] += len(picked)
                comment_stats["children_skipped"] += skipped
    except Exception as exc:
        # Not just BadResponse/RuntimeError: a token refresh after a 401 can
        # raise a requests error, and a malformed post can raise KeyError or
        # TypeError. Any of those escaping here skipped the quarantine. Exception
        # (not BaseException) so Ctrl-C still interrupts.
        return fail(f"{type(exc).__name__}: {exc}", 4)

    # 2026-09-11, review round 2. This used to fail on scanned == 0 BEFORE
    # consulting --allow-empty, so a subreddit that legitimately returned an empty
    # listing exited 3 and deleted the destination even when the caller had
    # explicitly said an empty result was expected. An empty listing is a valid
    # answer; only a listing we never successfully READ is a broken pull, and that
    # already raises BadResponse above.
    if scanned == 0 and not args.allow_empty:
        return fail(
            f"scanned 0 posts across {subs}; pass --allow-empty if these "
            f"subreddits are genuinely expected to be empty",
            3,
        )
    if not records and not args.allow_empty:
        # The first defect review flagged: a scan that saw posts and kept none
        # used to write records: [] with errors: [] and exit 0.
        return fail(f"scanned {scanned} posts across {subs} and kept none; drops={drops}", 5)

    payload = {
        "meta": {
            "fetched_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cutoff_utc": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": args.days,
            "sort": args.sort,
            "subreddits": subs,
            "keywords": [k.strip() for k in args.keywords.split(",") if k.strip()],
            "min_signals": args.min_signals,
            "rank": args.rank,
            "scanned_posts": scanned,
            "listing_stop_reason": stops,
            "dropped": drops,
            "matched_posts": matched_posts,
            "returned_posts": len(records),
            "truncated": matched_posts > len(records),
            "comment_stats": comment_stats,
            "api_requests": sess.requests_made,
            "rate_limit_waits": sess.rate_limit_waits,
            "errors": [],
            # Always true here, and true BY CONSTRUCTION rather than by luck:
            # this script has no per-subreddit except, so any subreddit failure
            # propagates to the outer handler and aborts the whole run. There is
            # no partial state to label. fetch_reddit.py DOES swallow per
            # subreddit, so it computes this field instead of asserting it.
            "collection_complete": True,
            "failed_subreddits": [],
            "fetcher": "fetch_raw.py",
        },
        "records": records,
    }
    # Publish atomically. A shared ".partial" name meant two concurrent runs on
    # the same --out clobbered each other's temp file and one published an empty
    # result (reproduced in review). A unique temp in the destination directory
    # fixes that, and the finally block stops a crash leaving litter behind.
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(args.out) + ".",
                                   suffix=".partial", dir=out_parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, args.out)
        tmp = None                     # published; nothing left to clean up
    except OSError as exc:
        return fail(f"could not publish {args.out}: {exc}", 7)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    print(json.dumps({"meta": payload["meta"], "output_file": args.out}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
