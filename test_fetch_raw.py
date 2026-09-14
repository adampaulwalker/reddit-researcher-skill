#!/usr/bin/env python3
"""Offline tests for fetch_raw.py. No credentials, no network.

The raw fetcher has no per-subreddit catch, so it never publishes a partial
collection. What it must still guarantee:

  * a failure never leaves a stale --out where a resume check will trust it,
    and never destroys it either (quarantine, 2026-09-11);
  * two failures in the same second keep BOTH backups (review, 2026-09-14 -
    the timestamp-only name let the second overwrite the first);
  * missing credentials and a failed token refresh go through that same
    boundary (review, 2026-09-14 - both used to escape it);
  * a malformed response is an error, never an empty result;
  * success publishes atomically and leaves no temp files behind.

    ./.venv/bin/python -m unittest test_fetch_raw
"""
from __future__ import annotations

import contextlib
import datetime as real_dt
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import requests

import fetch_raw as R

FIXED_NOW = real_dt.datetime(2026, 9, 14, 12, 0, 0)


class FixedDateTime(real_dt.datetime):
    """Freezes now() so two runs provably share one quarantine timestamp."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.replace(tzinfo=tz)


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def post(pid, created, title="AI workflow", body="we use it daily"):
    return {"kind": "t3", "data": {
        "id": pid, "created_utc": created, "title": title, "selftext": body,
        "score": 5, "num_comments": 2, "permalink": f"/r/law/comments/{pid}/x/",
        "author": "someone"}}


def listing(children):
    return {"kind": "Listing", "data": {"children": children, "after": None}}


class FakeSession:
    """Stands in for RateLimitedSession; answers from a scripted route table."""

    requests_made = 0
    rate_limit_waits = 0

    def __init__(self, routes):
        self.routes = routes

    def get(self, path, params=None, tries=6):
        for prefix, answer in self.routes:
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected GET {path}")


class RawFetcherTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fetch-raw-test-")
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "sweep.json")
        now_ts = real_dt.datetime.now(real_dt.timezone.utc).timestamp()
        self.recent = now_ts - 3600
        # Any real network call is a test bug, not a flaky pass.
        for target in ("requests.post", "requests.Session.get"):
            patcher = mock.patch(target, side_effect=AssertionError(f"network call: {target}"))
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = mock.patch.object(R.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_main(self, argv, session=None):
        err = io.StringIO()
        ctx = (mock.patch.object(R, "RateLimitedSession", lambda ua: session)
               if session is not None else contextlib.nullcontext())
        with ctx, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = R.main(argv)
        return code, err.getvalue()

    def argv(self, *extra):
        return ["--subreddits", "law", "--out", self.out, *extra]

    def write_stale(self, note):
        with open(self.out, "w") as fh:
            json.dump({"meta": {"note": note}, "records": []}, fh)

    def assert_no_litter(self):
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".partial")]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    # ---- success ---------------------------------------------------------

    def test_success_publishes_atomically(self):
        sess = FakeSession([("/r/law/new", listing([post("a1", self.recent)]))])
        code, err = self.run_main(self.argv(), sess)
        self.assertEqual(code, 0, err)
        payload = read_json(self.out)
        self.assertEqual([r["id"] for r in payload["records"]], ["a1"])
        self.assertTrue(payload["meta"]["collection_complete"])
        self.assert_no_litter()

    def test_empty_listing_needs_allow_empty(self):
        sess = FakeSession([("/r/law/new", listing([]))])
        code, _ = self.run_main(self.argv(), sess)
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(self.out))
        code, err = self.run_main(self.argv("--allow-empty"), sess)
        self.assertEqual(code, 0, err)
        self.assertEqual(read_json(self.out)["records"], [])

    # ---- malformed responses ---------------------------------------------

    def test_malformed_listing_is_an_error_not_empty(self):
        self.write_stale("yesterday")
        sess = FakeSession([("/r/law/new", {"error": 500})])
        code, err = self.run_main(self.argv(), sess)
        self.assertEqual(code, 4, err)
        self.assertFalse(os.path.exists(self.out))
        moved = json.loads(err)["previous_output_moved_to"]
        self.assertEqual(read_json(moved)["meta"]["note"], "yesterday")

    def test_malformed_comment_response_is_an_error(self):
        sess = FakeSession([
            ("/r/law/new", listing([post("a1", self.recent)])),
            ("/r/law/comments/", {"not": "a list"}),
        ])
        code, err = self.run_main(self.argv("--max-comments-per-post", "2"), sess)
        self.assertEqual(code, 4, err)
        self.assertFalse(os.path.exists(self.out))

    def test_post_missing_fields_goes_through_the_boundary(self):
        # A KeyError/TypeError used to escape the BadResponse/RuntimeError catch.
        self.write_stale("yesterday")
        broken = {"kind": "t3", "data": {"created_utc": self.recent, "title": "t"}}
        sess = FakeSession([("/r/law/new", listing([broken]))])
        code, err = self.run_main(self.argv(), sess)
        self.assertEqual(code, 4, err)
        self.assertFalse(os.path.exists(self.out))

    # ---- quarantine --------------------------------------------------------

    def test_same_second_failures_keep_both_backups(self):
        sess = FakeSession([("/r/law/new", {"error": 500})])
        with mock.patch.object(R.dt, "datetime", FixedDateTime):
            self.write_stale("first")
            code1, err1 = self.run_main(self.argv(), sess)
            self.write_stale("second")
            code2, err2 = self.run_main(self.argv(), sess)
        self.assertEqual((code1, code2), (4, 4))
        first = json.loads(err1)["previous_output_moved_to"]
        second = json.loads(err2)["previous_output_moved_to"]
        self.assertNotEqual(first, second, "both failures picked the same backup path")
        self.assertEqual(read_json(first)["meta"]["note"], "first")
        self.assertEqual(read_json(second)["meta"]["note"], "second")

    def test_missing_credentials_quarantine_stale_output(self):
        self.write_stale("yesterday")
        env = {k: v for k, v in os.environ.items()
               if k not in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")}
        with mock.patch.dict(os.environ, env, clear=True):
            code, err = self.run_main(self.argv())
        self.assertEqual(code, 2, err)
        self.assertIn("Missing REDDIT_CLIENT_ID", err)
        self.assertFalse(os.path.exists(self.out))
        self.assertTrue(os.path.exists(json.loads(err)["previous_output_moved_to"]))

    def test_token_refresh_failure_quarantines_stale_output(self):
        # First token request succeeds, the listing answers 401, and the
        # re-authentication then fails at the transport layer.
        self.write_stale("yesterday")
        token = mock.Mock(status_code=200)
        token.json.return_value = {"access_token": "t"}
        token.raise_for_status.return_value = None
        unauthorized = mock.Mock(status_code=401, headers={})
        with mock.patch.dict(os.environ, {"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "s"}), \
             mock.patch("requests.post",
                        side_effect=[token, requests.ConnectionError("refresh down")]), \
             mock.patch("requests.Session.get", return_value=unauthorized):
            code, err = self.run_main(self.argv())
        self.assertEqual(code, 4, err)
        self.assertIn("ConnectionError", err)
        self.assertFalse(os.path.exists(self.out))
        self.assertTrue(os.path.exists(json.loads(err)["previous_output_moved_to"]))

    def test_publication_failure_cleans_up_and_quarantines(self):
        self.write_stale("yesterday")
        sess = FakeSession([("/r/law/new", listing([post("a1", self.recent)]))])
        real_replace = os.replace

        def replace(src, dst):
            if src.endswith(".partial"):
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(R.os, "replace", side_effect=replace):
            code, err = self.run_main(self.argv(), sess)
        self.assertEqual(code, 7, err)
        self.assertFalse(os.path.exists(self.out))
        self.assert_no_litter()

    # ---- offline validation --------------------------------------------

    def test_invalid_arguments_stop_before_the_network(self):
        def must_not_connect(ua):
            raise AssertionError("opened a session for invalid arguments")

        cases = [
            ["--subreddits", " , ", "--out", self.out],
            ["--subreddits", "law", "--out", os.path.join(self.tmp.name, "nope", "o.json")],
            ["--subreddits", "law", "--out", self.out, "--comment-posts", "-1"],
            ["--subreddits", "law", "--out", self.out, "--scan-limit", "0"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), \
                 mock.patch.object(R, "RateLimitedSession", must_not_connect), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    R.main(argv)

    def test_directory_out_is_refused_and_untouched(self):
        # Quarantine moves whatever sits at --out; a directory there would have
        # been moved wholesale on any failure (review, 2026-09-14 round 2).
        target = os.path.join(self.tmp.name, "data")
        os.makedirs(target)
        with open(os.path.join(target, "keep.txt"), "w") as fh:
            fh.write("corpus")

        def must_not_connect(ua):
            raise AssertionError("opened a session for a directory --out")

        with mock.patch.object(R, "RateLimitedSession", must_not_connect), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                R.main(["--subreddits", "law", "--out", target])
        with open(os.path.join(target, "keep.txt")) as fh:
            self.assertEqual(fh.read(), "corpus")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["data"])


if __name__ == "__main__":
    unittest.main()
