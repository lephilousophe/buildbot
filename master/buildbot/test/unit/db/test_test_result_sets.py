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

from twisted.internet import defer
from twisted.trial import unittest

from buildbot.db import test_result_sets
from buildbot.test import fakedb
from buildbot.test.fake import fakemaster
from buildbot.test.reactor import TestReactorMixin


class Tests(TestReactorMixin, unittest.TestCase):
    common_data = [
        fakedb.Worker(id=47, name='linux'),
        fakedb.Buildset(id=20),
        fakedb.Builder(id=88, name='b1'),
        fakedb.Builder(id=89, name='b2'),
        fakedb.BuildRequest(id=41, buildsetid=20, builderid=88),
        fakedb.BuildRequest(id=42, buildsetid=20, builderid=88),
        fakedb.BuildRequest(id=43, buildsetid=20, builderid=88),
        fakedb.Master(id=88),
        fakedb.Build(id=30, buildrequestid=41, number=7, masterid=88, builderid=88, workerid=47),
        fakedb.Build(id=31, buildrequestid=42, number=8, masterid=88, builderid=88, workerid=47),
        fakedb.Build(id=40, buildrequestid=43, number=9, masterid=88, builderid=89, workerid=47),
        fakedb.Step(id=131, number=231, name='step231', buildid=30),
        fakedb.Step(id=132, number=232, name='step232', buildid=30),
        fakedb.Step(id=141, number=241, name='step241', buildid=31),
        fakedb.Step(id=142, number=242, name='step242', buildid=40),
    ]

    common_test_result_set_data = [
        fakedb.TestResultSet(
            id=91,
            builderid=88,
            buildid=30,
            stepid=131,
            description='desc1',
            category='cat',
            value_unit='ms',
            tests_failed=None,
            tests_passed=None,
            complete=0,
        ),
        fakedb.TestResultSet(
            id=92,
            builderid=88,
            buildid=30,
            stepid=131,
            description='desc2',
            category='cat',
            value_unit='ms',
            tests_failed=None,
            tests_passed=None,
            complete=1,
        ),
    ]

    @defer.inlineCallbacks
    def setUp(self):
        self.setup_test_reactor()
        self.master = yield fakemaster.make_master(self, wantDb=True)
        self.db = self.master.db

    @defer.inlineCallbacks
    def test_add_set_get_set(self):
        yield self.db.insert_test_data(self.common_data)
        set_id = yield self.db.test_result_sets.addTestResultSet(
            builderid=88,
            buildid=30,
            stepid=131,
            description='desc',
            category='cat',
            value_unit='ms',
        )
        set_dict = yield self.db.test_result_sets.getTestResultSet(set_id)
        self.assertIsInstance(set_dict, test_result_sets.TestResultSetModel)
        self.assertEqual(
            set_dict,
            test_result_sets.TestResultSetModel(
                id=set_id,
                builderid=88,
                buildid=30,
                stepid=131,
                description='desc',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=False,
            ),
        )

    @defer.inlineCallbacks
    def test_get_sets(self):
        yield self.db.insert_test_data([
            *self.common_data,
            fakedb.TestResultSet(
                id=91,
                builderid=88,
                buildid=30,
                stepid=131,
                description='desc1',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=0,
            ),
            fakedb.TestResultSet(
                id=92,
                builderid=89,
                buildid=40,
                stepid=142,
                description='desc2',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=1,
            ),
            fakedb.TestResultSet(
                id=93,
                builderid=88,
                buildid=31,
                stepid=141,
                description='desc3',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=1,
            ),
            fakedb.TestResultSet(
                id=94,
                builderid=88,
                buildid=30,
                stepid=132,
                description='desc4',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=1,
            ),
            fakedb.TestResultSet(
                id=95,
                builderid=88,
                buildid=30,
                stepid=131,
                description='desc4',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=0,
            ),
        ])

        set_dicts = yield self.db.test_result_sets.getTestResultSets()
        self.assertEqual([d.id for d in set_dicts], [91, 92, 93, 94, 95])
        for d in set_dicts:
            self.assertIsInstance(d, test_result_sets.TestResultSetModel)

        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88)
        self.assertEqual([d.id for d in set_dicts], [91, 93, 94, 95])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=89)
        self.assertEqual([d.id for d in set_dicts], [92])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88, buildid=30)
        self.assertEqual([d.id for d in set_dicts], [91, 94, 95])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88, buildid=31)
        self.assertEqual([d.id for d in set_dicts], [93])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88, stepid=131)
        self.assertEqual([d.id for d in set_dicts], [91, 95])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88, stepid=132)
        self.assertEqual([d.id for d in set_dicts], [94])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88, complete=True)
        self.assertEqual([d.id for d in set_dicts], [93, 94])
        set_dicts = yield self.db.test_result_sets.getTestResultSets(builderid=88, complete=False)
        self.assertEqual([d.id for d in set_dicts], [91, 95])

    @defer.inlineCallbacks
    def test_get_set_from_data(self):
        yield self.db.insert_test_data(self.common_data + self.common_test_result_set_data)

        set_dict = yield self.db.test_result_sets.getTestResultSet(91)
        self.assertEqual(
            set_dict,
            test_result_sets.TestResultSetModel(
                id=91,
                builderid=88,
                buildid=30,
                stepid=131,
                description='desc1',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=False,
            ),
        )

    @defer.inlineCallbacks
    def test_get_non_existing_set(self):
        set_dict = yield self.db.test_result_sets.getTestResultSet(91)
        self.assertEqual(set_dict, None)

    @defer.inlineCallbacks
    def test_complete_already_completed_set(self):
        yield self.db.insert_test_data(self.common_data + self.common_test_result_set_data)
        with self.assertRaises(test_result_sets.TestResultSetAlreadyCompleted):
            yield self.db.test_result_sets.completeTestResultSet(92)
        self.flushLoggedErrors(test_result_sets.TestResultSetAlreadyCompleted)

    @defer.inlineCallbacks
    def test_complete_set_with_test_counts(self):
        yield self.db.insert_test_data(self.common_data + self.common_test_result_set_data)

        yield self.db.test_result_sets.completeTestResultSet(91, tests_passed=12, tests_failed=2)

        set_dict = yield self.db.test_result_sets.getTestResultSet(91)
        self.assertEqual(
            set_dict,
            test_result_sets.TestResultSetModel(
                id=91,
                builderid=88,
                buildid=30,
                stepid=131,
                description='desc1',
                category='cat',
                value_unit='ms',
                tests_failed=2,
                tests_passed=12,
                complete=True,
            ),
        )

    @defer.inlineCallbacks
    def test_complete_set_without_test_counts(self):
        yield self.db.insert_test_data(self.common_data + self.common_test_result_set_data)

        yield self.db.test_result_sets.completeTestResultSet(91)

        set_dict = yield self.db.test_result_sets.getTestResultSet(91)
        self.assertEqual(
            set_dict,
            test_result_sets.TestResultSetModel(
                id=91,
                builderid=88,
                buildid=30,
                stepid=131,
                description='desc1',
                category='cat',
                value_unit='ms',
                tests_failed=None,
                tests_passed=None,
                complete=True,
            ),
        )
