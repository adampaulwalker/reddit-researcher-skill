#!/usr/bin/env python3
"""Prove the fetchers fail closed instead of writing a confidently-empty file.

The defect being guarded, 2026-09-11: praw returned TooManyRequests for 37 of 39
subreddits. Every failure was swallowed per-subreddit (correctly - one banned
subreddit must not kill a run), and the script then wrote a well-formed file
holding zero records and exited 0. A throttled pull and a quiet subreddit
produced byte-identical output.

This runs offline. No credentials, no network: `collect_posts` and `make_client`
are replaced with stubs so each failure shape can be forced on demand.

A guard that has never been watched failing is decoration, so `--prove` reverts
the guard in memory and asserts the bad cases go GREEN without it - which is how
we know the test is testing something.

This file covers fetch_reddit.py only. fetch_raw.py has its own offline suite in
test_fetch_raw.py.

    ./.venv/bin/python test_fail_closed.py          # every case must pass
    ./.venv/bin/python test_fail_closed.py --prove  # plus one proof-of-failure pass
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

import fetch_reddit as F

PASS, FAIL = [], []


def case(name):
    def deco(fn):
        try:
            fn()
            PASS.append(name)
            print(f"  PASS  {name}")
        except AssertionError as exc:
            FAIL.append((name, str(exc)))
            print(f"  FAIL  {name}\n          {exc}")
        return fn
    return deco


def run(subs, records_returned, errors_to_add, out, extra_argv=(), client=None, stdout=None):
    """Call main() with collect_posts stubbed to a chosen outcome.

    `out=None` exercises the stdout path; `client` replaces make_client.
    """
    def fake_collect(reddit, args, pattern, use_signal, cutoff, errors):
        errors.extend(errors_to_add)
        return list(records_returned)

    orig_collect, orig_client, orig_attach = F.collect_posts, F.make_client, F.attach_comments
    F.collect_posts = fake_collect
    F.make_client = client or (lambda: object())
    F.attach_comments = lambda *a, **k: None
    argv = ["--subreddits", subs, *(["--out", out] if out else []), *extra_argv]
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout or io.StringIO()), contextlib.redirect_stderr(err):
            code = F.main(argv)
    finally:
        F.collect_posts, F.make_client, F.attach_comments = orig_collect, orig_client, orig_attach
    return code, err.getvalue()


# Shaped like a real fetch_reddit.py record, so a schema change shows up here.
REC = {"type": "post", "subreddit": "law", "id": "x1",
       "created_utc": "2026-09-01T12:00:00Z", "title": "t", "body_excerpt": "b",
       "body_truncated": False, "score": 1, "num_comments": 0,
       "permalink": "https://reddit.com/r/law/comments/x1/t/", "author": "a",
       "matched_terms": [], "comments": []}

# Every case works inside this directory, so outputs AND quarantine directories
# are cleaned up at exit rather than left in the system temp folder.
_WORKDIR = tempfile.TemporaryDirectory(prefix="reddit-fail-closed-")


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def read_text(path):
    with open(path) as fh:
        return fh.read()


def tmp_out():
    fd, path = tempfile.mkstemp(suffix=".json", dir=_WORKDIR.name)
    os.close(fd)
    os.remove(path)
    return path


print("fail-closed guard, fetch_reddit.py")


@case("every subreddit errored -> non-zero exit, no file")
def _():
    out = tmp_out()
    code, err = run("law,accounting", [], [{"subreddit": "law", "error": "TooManyRequests"},
                                           {"subreddit": "accounting", "error": "TooManyRequests"}], out)
    assert code != 0, f"expected non-zero, got {code}"
    assert not os.path.exists(out), "wrote a file for a wholly failed pull"
    assert "broken pull" in err, f"error text did not name the cause: {err}"


@case("the 37-of-39 shape -> non-zero, no file")
def _():
    out = tmp_out()
    errs = [{"subreddit": f"s{i}", "error": "TooManyRequests"} for i in range(37)]
    subs = ",".join(f"s{i}" for i in range(39))
    code, _ = run(subs, [], errs, out)
    assert code != 0, "37 rate-limited subreddits and zero records exited 0"
    assert not os.path.exists(out), "wrote a confidently-empty file"


@case("zero records with no errors -> still non-zero without --allow-empty")
def _():
    out = tmp_out()
    code, _ = run("law", [], [], out)
    assert code != 0, "a zero result succeeded silently"
    assert not os.path.exists(out)


@case("--allow-empty makes a COMPLETE true zero succeed")
def _():
    out = tmp_out()
    code, _ = run("law", [], [], out, extra_argv=["--allow-empty"])
    assert code == 0, f"explicit --allow-empty should succeed, got {code}"
    assert os.path.exists(out), "--allow-empty should still publish"
    assert read_json(out)["meta"]["collection_complete"] is True
    os.remove(out)


@case("--allow-empty ALONE cannot publish the 37-of-39 failure  [review 2026-09-11]")
def _():
    # The hole the review found. 37 of 39 failed, so the all-failed guard does
    # not fire; a single --allow-empty flag then disabled the zero-records guard
    # and republished the exact defect this script exists to catch.
    out = tmp_out()
    errs = [{"subreddit": f"s{i}", "error": "TooManyRequests"} for i in range(37)]
    subs = ",".join(f"s{i}" for i in range(39))
    code, err = run(subs, [], errs, out, extra_argv=["--allow-empty"])
    assert code != 0, "--allow-empty alone republished an incomplete zero-record pull"
    assert not os.path.exists(out)
    assert "incomplete" in err, f"error should name incompleteness, got: {err}"


@case("a partial pull WITH records still publishes, labelled incomplete")
def _():
    # The original promise: one private or banned subreddit must not kill a run.
    # Blocking this was tried and broke the good-run case, so the disclosure is
    # the metadata rather than a refusal.
    out = tmp_out()
    errs = [{"subreddit": "s0", "error": "Forbidden"}]
    code, _ = run("s0,s1", [REC], errs, out)
    assert code == 0, f"one banned subreddit killed a run that returned records, got {code}"
    meta = read_json(out)["meta"]
    assert meta["collection_complete"] is False, "partial corpus was not labelled partial"
    assert meta["failed_subreddits"] == ["s0"]
    os.remove(out)


@case("both overrides together CAN publish the 37-of-39 pull, deliberately")
def _():
    out = tmp_out()
    errs = [{"subreddit": f"s{i}", "error": "TooManyRequests"} for i in range(37)]
    subs = ",".join(f"s{i}" for i in range(39))
    code, _ = run(subs, [], errs, out, extra_argv=["--allow-empty", "--allow-incomplete"])
    assert code == 0, f"two explicit overrides should publish, got {code}"
    assert read_json(out)["meta"]["collection_complete"] is False
    os.remove(out)


@case("a stale destination is QUARANTINED, not left and not destroyed")
def _():
    out = tmp_out()
    with open(out, "w") as fh:
        json.dump({"records": [REC], "meta": {"note": "yesterday's good pull"}}, fh)
    code, err = run("law", [], [{"subreddit": "law", "error": "TooManyRequests"}], out)
    assert code != 0
    assert not os.path.exists(out), "left a stale file a resume check would trust forever"
    moved = json.loads(err)["previous_output_moved_to"]
    assert moved and os.path.exists(moved), f"yesterday's corpus was destroyed, not moved: {moved}"
    # and the bytes survived intact - this is the whole point of quarantine
    assert read_json(moved)["meta"]["note"] == "yesterday's good pull"


@case("missing credentials also quarantine a stale destination  [review 2026-09-14]")
def _():
    # make_client used to raise SystemExit outside the failure boundary, so a
    # run with no credentials left yesterday's file where a resume check trusts it.
    out = tmp_out()
    with open(out, "w") as fh:
        json.dump({"records": [REC], "meta": {"note": "old"}}, fh)

    # Raise whatever make_client raises in this version, so the case reports a
    # FAIL against the old SystemExit behavior instead of crashing the suite.
    exc_type = getattr(F, "MissingCredentials", SystemExit)

    def no_creds():
        raise exc_type("Missing environment variables: REDDIT_CLIENT_ID")

    try:
        code, err = run("law", [REC], [], out, client=no_creds)
    except SystemExit:
        raise AssertionError("missing credentials escaped the failure boundary")
    assert code != 0, "missing credentials exited 0"
    assert not os.path.exists(out), "missing credentials left the stale file in place"
    moved = json.loads(err)["previous_output_moved_to"]
    assert moved and os.path.exists(moved), f"stale file was not preserved: {moved}"


@case("stdout budget cannot trim every record and still exit 0  [review 2026-09-14]")
def _():
    # The empty-result guard ran BEFORE budgeting, so a budget that fit the
    # metadata but no record printed records: [] and exited 0.
    big = dict(REC, body_excerpt="x" * 5000)
    buf = io.StringIO()
    code, err = run("law", [big], [], None, extra_argv=["--max-output-chars", "1500"], stdout=buf)
    assert code != 0, f"budget trimmed to zero records and exited {code}"
    assert buf.getvalue() == "", "printed a confidently-empty result"
    assert "left room for 0" in err, f"error did not name the cause: {err}"


@case("an INCOMPLETE pull trimmed to zero needs BOTH overrides  [review 2026-09-14 round 2]")
def _():
    # The first budget fix re-checked only --allow-empty, so a partial pull
    # trimmed to nothing printed records: [] with --allow-empty alone.
    big = dict(REC, body_excerpt="x" * 5000)
    errs = [{"subreddit": "s0", "error": "TooManyRequests"}]
    for flags, want_ok in (([], False), (["--allow-empty"], False),
                           (["--allow-incomplete"], False),
                           (["--allow-empty", "--allow-incomplete"], True)):
        buf = io.StringIO()
        code, _ = run("s0,law", [big], errs, None,
                      extra_argv=["--max-output-chars", "1500", *flags], stdout=buf)
        if want_ok:
            assert code == 0, f"both overrides should print, got {code}"
            assert json.loads(buf.getvalue())["meta"]["collection_complete"] is False
        else:
            assert code != 0, f"{flags or 'no flags'} printed an incomplete empty result"
            assert buf.getvalue() == "", f"{flags} printed output"


@case("--out pointing at a directory is refused and the directory is untouched  [review 2026-09-14 round 2]")
def _():
    # Quarantine moves whatever sits at --out, so a directory there would have
    # been moved wholesale on any failure.
    target = os.path.join(_WORKDIR.name, "data-dir")
    os.makedirs(target, exist_ok=True)
    sentinel = os.path.join(target, "keep.txt")
    with open(sentinel, "w") as fh:
        fh.write("corpus")

    def must_not_run():
        raise AssertionError("created a client for a directory --out")

    try:
        run("law", [REC], [], target, client=must_not_run)
    except SystemExit:
        pass
    else:
        raise AssertionError("a directory --out was accepted")
    assert read_text(sentinel) == "corpus", "directory contents changed"
    assert os.listdir(_WORKDIR.name).count("data-dir") == 1
    assert not [n for n in os.listdir(_WORKDIR.name) if n.startswith("data-dir.superseded")], \
        "the directory was quarantined"


@case("a budget smaller than the metadata is refused, never exceeded")
def _():
    buf = io.StringIO()
    code, err = run("law", [REC], [], None, extra_argv=["--max-output-chars", "10"], stdout=buf)
    assert code != 0, "printed past a ceiling the caller set"
    assert buf.getvalue() == "", f"wrote {len(buf.getvalue())} chars against a 10-char ceiling"


@case("stdout output fits the ceiling including the trailing newline")
def _():
    recs = [dict(REC, id=f"x{i}", body_excerpt="y" * 300) for i in range(20)]
    buf = io.StringIO()
    code, _ = run("law", recs, [], None, extra_argv=["--max-output-chars", "3000"], stdout=buf)
    assert code == 0, f"a trimmable result should publish, got {code}"
    assert len(buf.getvalue()) <= 3000, f"{len(buf.getvalue())} chars exceeds 3000"
    meta = json.loads(buf.getvalue())["meta"]
    assert meta["truncated"] is True and 0 < meta["returned_posts"] < 20


@case("a real pull still succeeds and publishes")
def _():
    out = tmp_out()
    code, _ = run("law", [REC], [{"subreddit": "other", "error": "Forbidden"}], out)
    assert code == 0, f"one bad subreddit killed a good run, got {code}"
    assert os.path.exists(out), "did not publish a successful pull"
    payload = read_json(out)
    assert len(payload["records"]) == 1
    os.remove(out)


@case("offline argument errors stop before any client is created")
def _():
    def must_not_run():
        raise AssertionError("created a client for invalid arguments")

    for bad in (["--scan-limit", "0"], ["--comment-posts", "-1"]):
        try:
            run("law", [REC], [], tmp_out(), extra_argv=bad, client=must_not_run)
        except SystemExit:
            continue
        raise AssertionError(f"{bad} was accepted")
    try:
        run(" , ", [REC], [], tmp_out(), client=must_not_run)
    except SystemExit:
        pass
    else:
        raise AssertionError("an empty --subreddits list was accepted")
    try:
        run("law", [REC], [], os.path.join(_WORKDIR.name, "missing-dir", "o.json"),
            client=must_not_run)
    except SystemExit:
        pass
    else:
        raise AssertionError("a missing --out directory was accepted")


if "--prove" in sys.argv:
    print("\nprove the guard is load-bearing: revert it and watch the bad cases pass")
    src = read_text(F.__file__)
    assert "return fail(" in src, "guard not present in source"
    # Neutralise the guard exactly as it existed before the fix.
    # EVERY guard must be neutralised, or the revert is partial and the harness
    # reports a false failure. That happened on 2026-09-11 when a third guard was
    # added and only two were reverted, so each target is asserted individually.
    targets = [
        ("    if requested and failed_subs >= set(requested):",
         "    if False and requested and failed_subs >= set(requested):"),
        ("    if not records and not complete and not (args.allow_empty and args.allow_incomplete):",
         "    if False and not records and not complete:"),
        ("    if not records and not args.allow_empty:",
         "    if False and not records and not args.allow_empty:"),
    ]
    patched = src
    for old, new in targets:
        assert old in patched, f"PATCH-TARGET-NOT-FOUND: {old.strip()[:60]}"
        patched = patched.replace(old, new)
    assert patched != src, "PATCH-TARGET-NOT-FOUND - the prove harness edited nothing"
    ns = {"__name__": "reverted", "__file__": F.__file__}
    exec(compile(patched, F.__file__, "exec"), ns)

    class NsProxy:
        """Attribute access that reads AND WRITES the exec'd globals.

        The first version of this harness set attributes on a plain object, so
        the stubs never reached the reverted module - it resolves `make_client`
        from its own globals, not from the proxy. The result looked like a
        failure to reproduce the defect when nothing had been stubbed at all.
        """
        def __getattr__(self, k):
            try:
                return ns[k]
            except KeyError as exc:
                raise AttributeError(k) from exc
        def __setattr__(self, k, v):
            ns[k] = v

    saved = F
    globals()["F"] = NsProxy()
    out = tmp_out()
    errs = [{"subreddit": f"s{i}", "error": "TooManyRequests"} for i in range(37)]
    subs = ",".join(f"s{i}" for i in range(39))
    code, _ = run(subs, [], errs, out)
    globals()["F"] = saved
    if code == 0 and os.path.exists(out):
        print("  PASS  without the guard, 37 rate-limited subs exit 0 and write an empty file")
        print("        -> the guard is what stops it. The test tests something.")
        os.remove(out)
        PASS.append("prove-red")
    else:
        print(f"  FAIL  reverted code did not reproduce the defect (code={code})")
        FAIL.append(("prove-red", "defect not reproduced"))

_WORKDIR.cleanup()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
