# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members


import gc
import platform
import random
import string

from twisted.internet import defer
from twisted.internet import reactor
from twisted.python import failure
from twisted.trial import unittest

from buildbot.util import lru

# construct weakref-able objects for particular keys


def short(k):
    return set([k.upper() * 3])


def long(k):
    return set([k.upper() * 6])


class LRUCacheTest(unittest.TestCase):
    def setUp(self):
        lru.inv_failed = False
        self.lru = lru.LRUCache(short, 3)

    def tearDown(self):
        self.assertFalse(lru.inv_failed, "invariant failed; see logs")

    def check_result(self, r, exp, exp_hits=None, exp_misses=None, exp_refhits=None):
        self.assertEqual(r, exp)
        if exp_hits is not None:
            self.assertEqual(self.lru.hits, exp_hits)
        if exp_misses is not None:
            self.assertEqual(self.lru.misses, exp_misses)
        if exp_refhits is not None:
            self.assertEqual(self.lru.refhits, exp_refhits)

    def test_single_key(self):
        # just get an item
        val = self.lru.get('a')
        self.check_result(val, short('a'), 0, 1)

        # second time, it should be cached..
        self.lru.miss_fn = long
        val = self.lru.get('a')
        self.check_result(val, short('a'), 1, 1)

    def test_simple_lru_expulsion(self):
        val = self.lru.get('a')
        self.check_result(val, short('a'), 0, 1)
        val = self.lru.get('b')
        self.check_result(val, short('b'), 0, 2)
        val = self.lru.get('c')
        self.check_result(val, short('c'), 0, 3)
        val = self.lru.get('d')
        self.check_result(val, short('d'), 0, 4)
        del val
        gc.collect()

        # now try 'a' again - it should be a miss
        self.lru.miss_fn = long
        val = self.lru.get('a')
        self.check_result(val, long('a'), 0, 5)

        # ..and that expelled B, but C is still in the cache
        val = self.lru.get('c')
        self.check_result(val, short('c'), 1, 5)

    @defer.inlineCallbacks
    def test_simple_lru_expulsion_maxsize_1(self):
        self.lru = lru.LRUCache(short, 1)
        val = yield self.lru.get('a')
        self.check_result(val, short('a'), 0, 1)
        val = yield self.lru.get('a')
        self.check_result(val, short('a'), 1, 1)
        val = yield self.lru.get('b')
        self.check_result(val, short('b'), 1, 2)
        del val
        gc.collect()

        # now try 'a' again - it should be a miss
        self.lru.miss_fn = long
        val = yield self.lru.get('a')
        self.check_result(val, long('a'), 1, 3)
        del val
        gc.collect()

        # ..and that expelled B
        val = yield self.lru.get('b')
        self.check_result(val, long('b'), 1, 4)

    def test_simple_lru_expulsion_maxsize_1_null_result(self):
        # a regression test for #2011
        def miss_fn(k):
            if k == 'b':
                return None
            return short(k)

        self.lru = lru.LRUCache(miss_fn, 1)
        val = self.lru.get('a')
        self.check_result(val, short('a'), 0, 1)
        val = self.lru.get('b')
        self.check_result(val, None, 0, 2)
        del val

        # 'a' was not expelled since 'b' was None
        self.lru.miss_fn = long
        val = self.lru.get('a')
        self.check_result(val, short('a'), 1, 2)

    def test_queue_collapsing(self):
        # just to check that we're practicing with the right queue size (so
        # QUEUE_SIZE_FACTOR is 10)
        self.assertEqual(self.lru.max_queue, 30)

        for c in 'a' + 'x' * 27 + 'ab':
            res = self.lru.get(c)
        self.check_result(res, short('b'), 27, 3)

        # at this point, we should have 'x', 'a', and 'b' in the cache, and
        # 'axx..xxab' in the queue.
        self.assertEqual(len(self.lru.queue), 30)

        # This 'get' operation for an existing key should cause compaction
        res = self.lru.get('b')
        self.check_result(res, short('b'), 28, 3)

        self.assertEqual(len(self.lru.queue), 3)

        # expect a cached short('a')
        self.lru.miss_fn = long
        res = self.lru.get('a')
        self.check_result(res, short('a'), 29, 3)

    def test_all_misses(self):
        for i, c in enumerate(string.ascii_lowercase + string.ascii_uppercase):
            res = self.lru.get(c)
            self.check_result(res, short(c), 0, i + 1)

    def test_get_exception(self):
        def fail_miss_fn(k):
            raise RuntimeError("oh noes")

        self.lru.miss_fn = fail_miss_fn

        got_exc = False
        try:
            self.lru.get('abc')
        except RuntimeError:
            got_exc = True

        self.assertEqual(got_exc, True)

    def test_all_hits(self):
        res = self.lru.get('a')
        self.check_result(res, short('a'), 0, 1)

        self.lru.miss_fn = long
        for i in range(100):
            res = self.lru.get('a')
            self.check_result(res, short('a'), i + 1, 1)

    def test_weakrefs(self):
        if platform.python_implementation() == 'PyPy':
            raise unittest.SkipTest('PyPy has different behavior with regards to weakref dicts')

        res_a = self.lru.get('a')
        self.check_result(res_a, short('a'))
        # note that res_a keeps a reference to this value

        res_b = self.lru.get('b')
        self.check_result(res_b, short('b'))
        del res_b  # discard reference to b

        # blow out the cache and the queue
        self.lru.miss_fn = long
        for c in string.ascii_lowercase[2:] * 5:
            self.lru.get(c)

        # and fetch a again, expecting the cached value
        res = self.lru.get('a')
        self.check_result(res, res_a, exp_refhits=1)

        # but 'b' should give us a new value
        res = self.lru.get('b')
        self.check_result(res, long('b'), exp_refhits=1)

    def test_fuzz(self):
        chars = list(string.ascii_lowercase * 40)
        random.shuffle(chars)
        for c in chars:
            res = self.lru.get(c)
            self.check_result(res, short(c))

    def test_set_max_size(self):
        # load up the cache with three items
        for c in 'abc':
            res = self.lru.get(c)
            self.check_result(res, short(c))
        del res

        # reset the size to 1
        self.lru.set_max_size(1)
        gc.collect()

        # and then expect that 'b' is no longer in the cache
        self.lru.miss_fn = long
        res = self.lru.get('b')
        self.check_result(res, long('b'))

    def test_miss_fn_kwargs(self):
        def keep_kwargs_miss_fn(k, **kwargs):
            return set(kwargs.keys())

        self.lru.miss_fn = keep_kwargs_miss_fn

        val = self.lru.get('a', a=1, b=2)
        self.check_result(val, set(['a', 'b']), 0, 1)

    def test_miss_fn_returns_none(self):
        calls = []

        def none_miss_fn(k):
            calls.append(k)
            return None

        self.lru.miss_fn = none_miss_fn

        for _ in range(2):
            self.assertEqual(self.lru.get('a'), None)

        # check that the miss_fn was called twice
        self.assertEqual(calls, ['a', 'a'])

    def test_put(self):
        self.assertEqual(self.lru.get('p'), short('p'))
        self.lru.put('p', set(['P2P2']))
        self.assertEqual(self.lru.get('p'), set(['P2P2']))

    def test_put_nonexistent_key(self):
        self.assertEqual(self.lru.get('p'), short('p'))
        self.lru.put('q', set(['new-q']))
        self.assertEqual(self.lru.get('p'), set(['PPP']))
        self.assertEqual(self.lru.get('q'), set(['new-q']))  # updated


class AsyncLRUCacheTest(unittest.TestCase):
    def setUp(self):
        lru.inv_failed = False
        self.lru = lru.AsyncLRUCache(self.short_miss_fn, 3)

    def tearDown(self):
        self.assertFalse(lru.inv_failed, "invariant failed; see logs")

    def short_miss_fn(self, key):
        return defer.succeed(short(key))

    def long_miss_fn(self, key):
        return defer.succeed(long(key))

    def failure_miss_fn(self, key):
        return defer.succeed(None)

    def check_result(self, r, exp, exp_hits=None, exp_misses=None, exp_refhits=None):
        self.assertEqual(r, exp)
        if exp_hits is not None:
            self.assertEqual(self.lru.hits, exp_hits)
        if exp_misses is not None:
            self.assertEqual(self.lru.misses, exp_misses)
        if exp_refhits is not None:
            self.assertEqual(self.lru.refhits, exp_refhits)

    # tests

    @defer.inlineCallbacks
    def test_single_key(self):
        # just get an item
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 0, 1)

        # second time, it should be cached..
        self.lru.miss_fn = self.long_miss_fn
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 1, 1)

    @defer.inlineCallbacks
    def test_simple_lru_expulsion(self):
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 0, 1)
        res = yield self.lru.get('b')
        self.check_result(res, short('b'), 0, 2)
        res = yield self.lru.get('c')
        self.check_result(res, short('c'), 0, 3)
        res = yield self.lru.get('d')
        self.check_result(res, short('d'), 0, 4)

        gc.collect()

        # now try 'a' again - it should be a miss
        self.lru.miss_fn = self.long_miss_fn
        res = yield self.lru.get('a')
        self.check_result(res, long('a'), 0, 5)

        # ..and that expelled B, but C is still in the cache
        res = yield self.lru.get('c')
        self.check_result(res, short('c'), 1, 5)

    @defer.inlineCallbacks
    def test_simple_lru_expulsion_maxsize_1(self):
        self.lru = lru.AsyncLRUCache(self.short_miss_fn, 1)

        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 0, 1)
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 1, 1)
        res = yield self.lru.get('b')
        self.check_result(res, short('b'), 1, 2)

        gc.collect()

        # now try 'a' again - it should be a miss
        self.lru.miss_fn = self.long_miss_fn
        res = yield self.lru.get('a')
        self.check_result(res, long('a'), 1, 3)

        gc.collect()

        # ..and that expelled B
        res = yield self.lru.get('b')
        self.check_result(res, long('b'), 1, 4)

    @defer.inlineCallbacks
    def test_simple_lru_expulsion_maxsize_1_null_result(self):
        # a regression test for #2011
        def miss_fn(k):
            if k == 'b':
                return defer.succeed(None)
            return defer.succeed(short(k))

        self.lru = lru.AsyncLRUCache(miss_fn, 1)

        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 0, 1)
        res = yield self.lru.get('b')
        self.check_result(res, None, 0, 2)

        # 'a' was not expelled since 'b' was None
        self.lru.miss_fn = self.long_miss_fn
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 1, 2)

    @defer.inlineCallbacks
    def test_queue_collapsing(self):
        # just to check that we're practicing with the right queue size (so
        # QUEUE_SIZE_FACTOR is 10)
        self.assertEqual(self.lru.max_queue, 30)

        for c in 'a' + 'x' * 27 + 'ab':
            res = yield self.lru.get(c)
        self.check_result(res, short('b'), 27, 3)

        # at this point, we should have 'x', 'a', and 'b' in the cache, and
        # 'axx..xxab' in the queue.
        self.assertEqual(len(self.lru.queue), 30)

        # This 'get' operation for an existing key should cause compaction
        res = yield self.lru.get('b')
        self.check_result(res, short('b'), 28, 3)

        self.assertEqual(len(self.lru.queue), 3)

        # expect a cached short('a')
        self.lru.miss_fn = self.long_miss_fn
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 29, 3)

    @defer.inlineCallbacks
    def test_all_misses(self):
        for i, c in enumerate(string.ascii_lowercase + string.ascii_uppercase):
            res = yield self.lru.get(c)
            self.check_result(res, short(c), 0, i + 1)

    @defer.inlineCallbacks
    def test_get_exception(self):
        def fail_miss_fn(k):
            return defer.fail(RuntimeError("oh noes"))

        self.lru.miss_fn = fail_miss_fn

        got_exc = False
        try:
            yield self.lru.get('abc')
        except RuntimeError:
            got_exc = True

        self.assertEqual(got_exc, True)

    @defer.inlineCallbacks
    def test_all_hits(self):
        res = yield self.lru.get('a')
        self.check_result(res, short('a'), 0, 1)

        self.lru.miss_fn = self.long_miss_fn
        for i in range(100):
            res = yield self.lru.get('a')
            self.check_result(res, short('a'), i + 1, 1)

    @defer.inlineCallbacks
    def test_weakrefs(self):
        if platform.python_implementation() == 'PyPy':
            raise unittest.SkipTest('PyPy has different behavior with regards to weakref dicts')

        res_a = yield self.lru.get('a')
        self.check_result(res_a, short('a'))
        # note that res_a keeps a reference to this value

        res_b = yield self.lru.get('b')
        self.check_result(res_b, short('b'))
        del res_b  # discard reference to b

        # blow out the cache and the queue
        self.lru.miss_fn = self.long_miss_fn
        for c in string.ascii_lowercase[2:] * 5:
            yield self.lru.get(c)

        # and fetch a again, expecting the cached value
        res = yield self.lru.get('a')
        self.check_result(res, res_a, exp_refhits=1)

        # but 'b' should give us a new value
        res = yield self.lru.get('b')
        self.check_result(res, long('b'), exp_refhits=1)

    @defer.inlineCallbacks
    def test_fuzz(self):
        chars = list(string.ascii_lowercase * 40)
        random.shuffle(chars)
        for c in chars:
            res = yield self.lru.get(c)
            self.check_result(res, short(c))

    @defer.inlineCallbacks
    def test_massively_parallel(self):
        chars = list(string.ascii_lowercase * 5)

        misses = [0]

        def slow_short_miss_fn(key):
            d = defer.Deferred()
            misses[0] += 1
            reactor.callLater(0, lambda: d.callback(short(key)))
            return d

        self.lru.miss_fn = slow_short_miss_fn

        def check(c, d):
            d.addCallback(self.check_result, short(c))
            return d

        yield defer.gatherResults([check(c, self.lru.get(c)) for c in chars], consumeErrors=True)

        self.assertEqual(misses[0], 26)
        self.assertEqual(self.lru.misses, 26)
        self.assertEqual(self.lru.hits, 4 * 26)

    @defer.inlineCallbacks
    def test_slow_fetch(self):
        def slower_miss_fn(k):
            d = defer.Deferred()
            reactor.callLater(0.05, lambda: d.callback(short(k)))
            return d

        self.lru.miss_fn = slower_miss_fn

        def do_get(test_d, k):
            d = self.lru.get(k)
            d.addCallback(self.check_result, short(k))
            d.addCallbacks(test_d.callback, test_d.errback)

        ds = []
        for i in range(8):
            d = defer.Deferred()
            reactor.callLater(0.02 * i, do_get, d, 'x')
            ds.append(d)

        yield defer.gatherResults(ds, consumeErrors=True)

        self.assertEqual((self.lru.hits, self.lru.misses), (7, 1))

    @defer.inlineCallbacks
    def test_slow_failure(self):
        def slow_fail_miss_fn(k):
            d = defer.Deferred()
            reactor.callLater(0.05, lambda: d.errback(failure.Failure(RuntimeError("oh noes"))))
            return d

        self.lru.miss_fn = slow_fail_miss_fn

        @defer.inlineCallbacks
        def do_get(test_d, k):
            try:
                with self.assertRaises(RuntimeError):
                    yield self.lru.get(k)
                test_d.callback(None)
            except Exception as e:
                test_d.errback(failure.Failure(e))

        ds = []
        for i in range(8):
            d = defer.Deferred()
            reactor.callLater(0.02 * i, do_get, d, 'x')
            ds.append(d)

        for d in ds:
            yield d

    @defer.inlineCallbacks
    def test_set_max_size(self):
        # load up the cache with three items
        for c in 'abc':
            res = yield self.lru.get(c)
            self.check_result(res, short(c))

        # reset the size to 1
        self.lru.set_max_size(1)
        gc.collect()

        # and then expect that 'b' is no longer in the cache
        self.lru.miss_fn = self.long_miss_fn
        res = yield self.lru.get('b')
        self.check_result(res, long('b'))

    @defer.inlineCallbacks
    def test_miss_fn_kwargs(self):
        def keep_kwargs_miss_fn(k, **kwargs):
            return defer.succeed(set(kwargs.keys()))

        self.lru.miss_fn = keep_kwargs_miss_fn

        res = yield self.lru.get('a', a=1, b=2)
        self.check_result(res, set(['a', 'b']), 0, 1)

    @defer.inlineCallbacks
    def test_miss_fn_returns_none(self):
        calls = []

        def none_miss_fn(k):
            calls.append(k)
            return defer.succeed(None)

        self.lru.miss_fn = none_miss_fn

        for _ in range(2):
            self.assertEqual((yield self.lru.get('a')), None)

        # check that the miss_fn was called twice
        self.assertEqual(calls, ['a', 'a'])

    @defer.inlineCallbacks
    def test_put(self):
        self.assertEqual((yield self.lru.get('p')), short('p'))
        self.lru.put('p', set(['P2P2']))
        self.assertEqual((yield self.lru.get('p')), set(['P2P2']))
