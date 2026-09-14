#!/usr/bin/env python3
"""Fetch Reddit posts and comments for analysis. Collects data only - no LLM calls.

The calling Claude Code session does the tagging and synthesis in-context, on
whatever model that session is running. Nothing here pins a model or spends API
credit, which is what broke the previous version of this skill.

Credentials come from the environment only, never from flags (flags leak into
shell history and process listings):

    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT (optional)

Output is a single bounded JSON object on stdout: {"meta": {...}, "records": [...]}.
Every cap is explicit so a wide pull cannot blow up the session context window.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile

try:
    import praw
except ImportError:
    print(
        json.dumps({
            "error": "praw is not installed in this interpreter.",
            "hint": "From the skill folder: python3 -m venv .venv && "
                    "./.venv/bin/python -m pip install -r requirements.txt, "
                    "then run this script with ./.venv/bin/python",
        }),
        file=sys.stderr,
    )
    sys.exit(2)


# Signals that a post describes real usage rather than just naming a tool.
# Callers can replace this with --use-signal.
DEFAULT_USE_SIGNAL = (
    r"\b(I use|I've been using|we use|switched to|set ?up|built|workflow|"
    r"saves? me|cut my|replaced|tried|testing|implement\w*|stack|rolled out)\b"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch Reddit data for in-session analysis (no LLM calls).",
    )
    p.add_argument(
        "--subreddits",
        required=True,
        help="Comma-separated subreddit names, without the r/ prefix.",
    )
    p.add_argument(
        "--keywords",
        default="",
        help=(
            "Comma-separated terms. Matched with word boundaries, so 'ai' will "
            "not match 'raises' or 'email'. Empty means keep everything."
        ),
    )
    p.add_argument(
        "--min-signals",
        type=int,
        default=1,
        help=(
            "How many DISTINCT keywords a post must match. Use 2+ for broad terms "
            "like 'ai'; keep at 1 for niche product names (default: 1)."
        ),
    )
    p.add_argument(
        "--use-signal",
        default="",
        help=(
            "Optional regex a post must also match, to keep real usage reports and "
            "drop passing mentions. Pass 'default' for the built-in usage pattern."
        ),
    )
    p.add_argument("--days", type=int, default=30, help="Look-back window (default: 30).")
    p.add_argument(
        "--sort",
        default="top",
        choices=["top", "new", "hot"],
        help=(
            "Listing to crawl. 'new' plus local filtering has more predictable "
            "recall than search (default: top)."
        ),
    )
    p.add_argument(
        "--scan-limit",
        type=int,
        default=100,
        help="Posts to examine per subreddit before filtering (default: 100).",
    )
    p.add_argument(
        "--rank",
        default="score",
        choices=["score", "comments", "recency"],
        help=(
            "Ordering, which also decides what --max-records DROPS. Score favours "
            "popular debate threads; 'comments' surfaces contested practitioner "
            "threads that score near zero (default: score)."
        ),
    )
    p.add_argument(
        "--max-records", type=int, default=40, help="Total posts returned (default: 40)."
    )
    p.add_argument(
        "--max-comments-per-post",
        type=int,
        default=0,
        help=(
            "Top-level comments per post. Second pass, applied only to the "
            "highest-ranked posts. 0 disables comment fetching (default: 0)."
        ),
    )
    p.add_argument(
        "--comment-posts",
        type=int,
        default=10,
        help="How many top-ranked posts get comments fetched (default: 10).",
    )
    p.add_argument(
        "--body-chars", type=int, default=800, help="Post body cap (default: 800)."
    )
    p.add_argument(
        "--comment-chars", type=int, default=400, help="Comment body cap (default: 400)."
    )
    p.add_argument(
        "--max-output-chars",
        type=int,
        default=120_000,
        help=(
            "Hard ceiling on stdout, including the trailing newline, when --out "
            "is NOT used. Records are dropped past it; a ceiling that leaves no "
            "record is an error unless the empty-result overrides allow it "
            "(default: 120000)."
        ),
    )
    p.add_argument(
        "--out",
        default="",
        help="Write full JSON here and print only a summary plus this path.",
    )
    p.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "Accept a zero-record result from a COMPLETE collection - every "
            "requested subreddit was read without error and genuinely had "
            "nothing. Does NOT accept an incomplete pull; see --allow-incomplete."
        ),
    )
    p.add_argument(
        "--allow-incomplete",
        action="store_true",
        help=(
            "Only matters together with --allow-empty: publishing ZERO records "
            "when some subreddits failed needs both flags. A partial pull that "
            "kept records publishes without it, labelled collection_complete: "
            "false. Separate on purpose: one flag must not switch off both "
            "safeguards, because 37-of-39-failed plus zero records is exactly "
            "the defect this script exists to catch."
        ),
    )
    return p.parse_args(argv)


def _umask() -> int:
    """Read the process umask without leaving it changed.

    There is no getter, so the only way to read it is to set it and restore it.
    """
    current = os.umask(0)
    os.umask(current)
    return current


def build_pattern(keywords: str) -> re.Pattern | None:
    """Word-boundary alternation. Substring matching is the bug this avoids."""
    terms = [k.strip() for k in keywords.split(",") if k.strip()]
    if not terms:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean, False
    return clean[:limit].rstrip() + "...", True


def time_filter_for(days: int) -> str:
    """Map the look-back window onto Reddit's coarse `top` buckets.

    Asking for top-of-all-time and filtering locally returns nothing: the
    all-time winners are years old and never fall inside a recent window.
    Pick the smallest bucket that still covers the window.

    Buckets are approximate, so the thresholds round DOWN to the next wider
    bucket. Over-covering is harmless because the cutoff is enforced locally;
    under-covering would silently lose posts near the edge of the window.
    """
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 30:
        return "month"
    if days <= 365:
        return "year"
    return "all"


class MissingCredentials(RuntimeError):
    """Raised instead of SystemExit so a stale --out is still quarantined."""


def make_client() -> praw.Reddit:
    missing = [
        v for v in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET") if not os.environ.get(v)
    ]
    if missing:
        raise MissingCredentials(
            f"Missing environment variables: {', '.join(missing)}. "
            "See .env.example in this skill folder."
        )
    client = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "reddit-researcher-skill/1.0"),
    )
    client.read_only = True
    return client


def collect_posts(reddit, args, pattern, use_signal, cutoff, errors):
    """First pass: posts only. Comments are a separate pass over the winners."""
    seen_ids: set[str] = set()
    records = []

    for name in [s.strip() for s in args.subreddits.split(",") if s.strip()]:
        try:
            sub = reddit.subreddit(name)
            if args.sort == "top":
                listing = sub.top(
                    time_filter=time_filter_for(args.days), limit=args.scan_limit
                )
            elif args.sort == "hot":
                listing = sub.hot(limit=args.scan_limit)
            else:
                listing = sub.new(limit=args.scan_limit)

            for post in listing:
                created = dt.datetime.fromtimestamp(post.created_utc, dt.timezone.utc)
                if created < cutoff:
                    continue
                if post.id in seen_ids:
                    continue

                body = post.selftext or ""
                # Skip removed/deleted shells - they carry no analyzable content.
                if body.strip() in ("[deleted]", "[removed]"):
                    continue

                blob = f"{post.title} {body}"
                matched = []
                if pattern is not None:
                    matched = sorted({m.group(0).lower() for m in pattern.finditer(blob)})
                    if len(matched) < args.min_signals:
                        continue
                if use_signal is not None and not use_signal.search(blob):
                    continue

                # Only true crossposts are redundant. Two separate discussions of
                # the same article are NOT duplicates - different subreddits argue
                # about it differently, which is exactly the signal we want.
                if getattr(post, "crosspost_parent", None):
                    continue

                seen_ids.add(post.id)

                excerpt, truncated = truncate(body, args.body_chars)
                records.append(
                    {
                        "type": "post",
                        "subreddit": name,
                        "id": post.id,
                        "created_utc": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "title": post.title,
                        "body_excerpt": excerpt,
                        "body_truncated": truncated,
                        "score": post.score,
                        "num_comments": post.num_comments,
                        "permalink": f"https://reddit.com{post.permalink}",
                        "author": str(post.author) if post.author else "[deleted]",
                        "matched_terms": matched,
                        "comments": [],
                    }
                )
        except Exception as exc:
            # One private, banned, or rate-limited subreddit must not kill the run.
            errors.append({"subreddit": name, "error": f"{type(exc).__name__}: {exc}"})

    # Ranking is not cosmetic: --max-records truncates from the bottom, so this
    # decides which posts are silently discarded. Measured on a real pull, score
    # ordering put a 375-point "will AI replace us" debate first and buried every
    # concrete how-they-did-it post below 25 points.
    if args.rank == "comments":
        records.sort(key=lambda r: r["num_comments"], reverse=True)
    elif args.rank == "recency":
        records.sort(key=lambda r: r["created_utc"], reverse=True)
    else:
        records.sort(key=lambda r: r["score"], reverse=True)
    return records


def attach_comments(reddit, records, args, errors):
    """Second pass: comments for the top-ranked posts only, never full trees."""
    if args.max_comments_per_post <= 0:
        return
    for record in records[: args.comment_posts]:
        try:
            submission = reddit.submission(id=record["id"])
            # Cap BEFORE touching .comments. Slicing afterwards still downloads
            # the whole listing first, which is the volume risk we are avoiding.
            submission.comment_limit = args.max_comments_per_post
            submission.comments.replace_more(limit=0)  # never expand the full tree
            picked = []
            for comment in submission.comments[: args.max_comments_per_post]:
                text = getattr(comment, "body", "") or ""
                if text.strip() in ("[deleted]", "[removed]"):
                    continue
                excerpt, truncated = truncate(text, args.comment_chars)
                picked.append(
                    {
                        "score": getattr(comment, "score", 0),
                        "author": str(comment.author) if comment.author else "[deleted]",
                        "body_excerpt": excerpt,
                        "body_truncated": truncated,
                    }
                )
            record["comments"] = picked
        except Exception as exc:
            errors.append(
                {"post_id": record["id"], "error": f"{type(exc).__name__}: {exc}"}
            )


class OverBudget(RuntimeError):
    """Even the metadata alone does not fit under --max-output-chars."""


def render(payload: dict, keep: int) -> str:
    trimmed = dict(payload)
    trimmed["records"] = payload["records"][:keep]
    meta = dict(payload["meta"])
    meta["returned_posts"] = keep
    meta["truncated"] = meta["truncated"] or keep < len(payload["records"])
    trimmed["meta"] = meta
    return json.dumps(trimmed, indent=2)


def fit_to_budget(payload: dict, budget: int) -> tuple[str, int]:
    """Return the largest prefix of records that fits under `budget` chars,
    and how many records that prefix holds.

    Binary search rather than pop-and-reserialize: dropping one record at a time
    re-serializes the whole payload on every iteration, which goes quadratic
    once someone raises --max-records.

    The count is returned because trimming can drop EVERY record. Review found
    that path printed records: [] and exited 0 without --allow-empty, which is
    the silent-zero defect arriving by a side door, so the caller re-checks it.
    """
    total = len(payload["records"])
    full = render(payload, total)
    if len(full) <= budget:
        return full, total

    lo, hi, best, best_keep = 0, total, None, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render(payload, mid)
        if len(candidate) <= budget:
            best, best_keep = candidate, mid
            lo = mid + 1
        else:
            hi = mid - 1

    # Even zero records can exceed a tiny budget. Emitting meta anyway would
    # blow past a ceiling the caller asked us to respect, so refuse instead.
    if best is None:
        raise OverBudget(f"metadata alone exceeds --max-output-chars {budget + 1}")
    return best, best_keep


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Validate everything that can be checked offline BEFORE any network call.
    for name, val in (("--scan-limit", args.scan_limit), ("--max-records", args.max_records),
                      ("--days", args.days), ("--max-output-chars", args.max_output_chars)):
        if val <= 0:
            raise SystemExit(f"{name} must be positive; {val} only manufactures an empty result")
    for name, val in (("--max-comments-per-post", args.max_comments_per_post),
                      ("--comment-posts", args.comment_posts),
                      ("--body-chars", args.body_chars), ("--comment-chars", args.comment_chars)):
        if val < 0:
            raise SystemExit(f"{name} must not be negative, got {val}")
    if not [s for s in args.subreddits.split(",") if s.strip()]:
        raise SystemExit("--subreddits named no subreddit")
    if args.out and not os.path.isdir(os.path.dirname(os.path.abspath(args.out))):
        raise SystemExit(
            f"--out directory does not exist: {os.path.dirname(os.path.abspath(args.out))} "
            "(create it first)"
        )
    if args.out and os.path.isdir(args.out):
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
    errors: list[dict] = []

    def fail(msg: str, code: int) -> int:
        # A stale destination must not stay where a resume check will trust it.
        # But two independent reviewers converged on deletion being too
        # destructive: one malformed listing on a refresh would destroy a corpus
        # collected over hours. So quarantine instead - the path is gone, which
        # is what the resume check needs, and the bytes are not.
        quarantined = None
        # Files only. Never move a directory, even if one appeared after the
        # up-front check.
        if args.out and os.path.isfile(args.out):
            try:
                # A timestamp alone is NOT unique: quarantine A, publish B, then
                # quarantine B in the same second, and os.replace silently
                # overwrites A - destroying the backup quarantine exists to keep.
                # mkdtemp gives an exclusively-created directory, so no second
                # run can land on the same path.
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                holder = tempfile.mkdtemp(
                    prefix=f"{os.path.basename(args.out)}.superseded-{stamp}.",
                    dir=os.path.dirname(os.path.abspath(args.out)) or ".",
                )
                quarantined = os.path.join(holder, os.path.basename(args.out))
                os.replace(args.out, quarantined)
            except OSError as exc:                  # never mask the real error
                quarantined = f"(quarantine failed: {exc})"
        print(
            json.dumps({"error": msg, "errors": errors, "previous_output_moved_to": quarantined}),
            file=sys.stderr,
        )
        return code

    # Client creation sits inside the failure boundary, so missing credentials
    # quarantine a stale --out like every other failure (found in review).
    try:
        reddit = make_client()
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}" if not isinstance(exc, MissingCredentials)
                    else str(exc), 2)
    records = collect_posts(reddit, args, pattern, use_signal, cutoff, errors)
    scanned = len(records)
    records = records[: args.max_records]
    attach_comments(reddit, records, args, errors)

    payload = {
        "meta": {
            "fetched_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cutoff_utc": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": args.days,
            "sort": args.sort,
            "subreddits": [s.strip() for s in args.subreddits.split(",") if s.strip()],
            "keywords": [k.strip() for k in args.keywords.split(",") if k.strip()],
            "min_signals": args.min_signals,
            "rank": args.rank,
            "matched_posts": scanned,
            "returned_posts": len(records),
            "truncated": scanned > len(records),
            "errors": errors,
        },
        "records": records,
    }

    # ---- FAIL CLOSED -------------------------------------------------------
    # 2026-09-11. The defect this guards, found on a real run: praw returned
    # TooManyRequests for 37 of 39 subreddits. Each one was swallowed into
    # `errors` by the per-subreddit `except Exception` above - correctly, so one
    # private or banned subreddit cannot kill a run - and then this function
    # wrote a well-formed file holding zero records and returned 0. A throttled
    # pull and a quiet subreddit produced byte-identical output, and the caller
    # had no way to tell them apart. Downstream that reads as "no discussion
    # exists", which is the silent-zero class: the analysis is not wrong, it is
    # confidently empty.
    #
    # So: a run that kept nothing is an error unless the caller passed
    # --allow-empty, and a run where EVERY subreddit errored is always an error.
    requested = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    failed_subs = {e["subreddit"] for e in errors if "subreddit" in e}

    # COMPLETENESS AND EMPTINESS ARE SEPARATE QUESTIONS, and conflating them
    # re-opened this very bug. Review, 2026-09-11: with a single --allow-empty
    # flag, the original 37-of-39 failure publishes zero records and exits 0
    # again - the all-failed guard does not fire (37 < 39) and the flag disables
    # the zero-records guard. One override must not switch off both.
    complete = not failed_subs
    payload["meta"]["collection_complete"] = complete
    payload["meta"]["failed_subreddits"] = sorted(failed_subs)

    if requested and failed_subs >= set(requested):
        return fail(
            f"every requested subreddit failed ({len(failed_subs)} of "
            f"{len(requested)}); this is a broken pull, not an empty result",
            3,
        )
    # A partial pull that still RETURNED RECORDS is published, labelled incomplete
    # in meta. That preserves the original promise - one private or banned
    # subreddit must not kill a run - and a downstream check can read
    # collection_complete rather than guess. Blocking here instead was tried and
    # it broke the good-run case in the test suite, which is why the gate is
    # narrower than the review's first suggestion.
    #
    # The dangerous combination is failures AND an empty result, because that is
    # the shape the original defect wore. It needs BOTH overrides, so no single
    # flag can wave the 37-of-39 pull through.
    if not records and not complete and not (args.allow_empty and args.allow_incomplete):
        return fail(
            f"kept 0 records AND {len(failed_subs)} of {len(requested)} "
            f"subreddit(s) failed: {sorted(failed_subs)}. This is the shape a "
            f"throttled pull wears. Pass BOTH --allow-empty and "
            f"--allow-incomplete if you really mean to publish it",
            6,
        )
    if not records and not args.allow_empty:
        return fail(
            f"kept 0 records from {len(requested)} subreddit(s); pass "
            f"--allow-empty if these are genuinely expected to be quiet",
            5,
        )

    if args.out:
        # Publish atomically so a killed process never leaves a half-written
        # file that parses as a short one. A UNIQUE temp name, because a shared
        # ".partial" let two concurrent runs on the same --out clobber each
        # other and publish an empty result (reproduced in review).
        out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                prefix=os.path.basename(args.out) + ".", suffix=".partial", dir=out_dir
            )
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            # mkstemp creates 0600. Publishing that would silently narrow the
            # permissions of a corpus other tooling already reads, so carry the
            # existing file's mode over, or fall back to the process umask.
            try:
                os.chmod(tmp, os.stat(args.out).st_mode & 0o7777)
            except FileNotFoundError:
                os.chmod(tmp, 0o666 & ~_umask())
            os.replace(tmp, args.out)
            tmp = None                      # published; nothing to clean up
        except OSError as exc:
            return fail(f"could not publish {args.out}: {exc}", 7)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        summary = dict(payload["meta"])
        print(json.dumps({"meta": summary, "output_file": args.out}, indent=2))
        return 0

    # print() appends a newline, so the JSON itself gets one character less.
    try:
        text, kept = fit_to_budget(payload, args.max_output_chars - 1)
    except OverBudget as exc:
        return fail(str(exc), 8)
    # Re-apply BOTH emptiness rules to what will actually be printed. Checking
    # only --allow-empty here let an incomplete pull trimmed to zero records
    # print with --allow-empty alone - the 37-of-39 shape again (review).
    if kept == 0 and records:
        allowed = args.allow_empty and (complete or args.allow_incomplete)
        if not allowed:
            return fail(
                f"--max-output-chars {args.max_output_chars} left room for 0 of "
                f"{len(records)} records"
                + ("" if complete else f" and {len(failed_subs)} subreddit(s) failed")
                + "; raise it or use --out",
                5,
            )
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
