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

from __future__ import annotations

import inspect
import sys
from typing import TYPE_CHECKING
from typing import Callable
from typing import ClassVar
from typing import Sequence

from twisted.internet import defer
from twisted.internet import error
from twisted.python import deprecate
from twisted.python import log
from twisted.python import versions
from twisted.python.failure import Failure
from twisted.python.reflect import accumulateClassList
from twisted.web.util import formatFailure
from zope.interface import implementer

from buildbot import config
from buildbot import interfaces
from buildbot import util
from buildbot.config.checks import check_param_bool
from buildbot.config.checks import check_param_length
from buildbot.config.checks import check_param_number_none
from buildbot.config.checks import check_param_str
from buildbot.config.checks import check_param_str_none
from buildbot.db.model import Model
from buildbot.interfaces import IRenderable
from buildbot.interfaces import WorkerSetupError
from buildbot.locks import BaseLock
from buildbot.process import log as plog
from buildbot.process import properties
from buildbot.process import remotecommand
from buildbot.process import results
from buildbot.process.locks import get_real_locks_from_accesses

# (WithProperties used to be available in this module)
from buildbot.process.properties import WithProperties
from buildbot.process.results import ALL_RESULTS
from buildbot.process.results import CANCELLED
from buildbot.process.results import EXCEPTION
from buildbot.process.results import FAILURE
from buildbot.process.results import RETRY
from buildbot.process.results import SKIPPED
from buildbot.process.results import SUCCESS
from buildbot.process.results import WARNINGS
from buildbot.process.results import statusToString
from buildbot.util import bytes2unicode
from buildbot.util import debounce
from buildbot.util import deferwaiter
from buildbot.util import flatten
from buildbot.util.test_result_submitter import TestResultSubmitter

if TYPE_CHECKING:
    from buildbot.process.build import Build
    from buildbot.worker.base import AbstractWorker


class BuildStepFailed(Exception):
    pass


class BuildStepCancelled(Exception):
    # used internally for signalling
    pass


class CallableAttributeError(Exception):
    # attribute error raised from a callable run inside a property
    pass


@implementer(interfaces.IBuildStepFactory)
class _BuildStepFactory(util.ComparableMixin):
    """
    This is a wrapper to record the arguments passed to as BuildStep subclass.
    We use an instance of this class, rather than a closure mostly to make it
    easier to test that the right factories are getting created.
    """

    compare_attrs: ClassVar[Sequence[str]] = ('factory', 'args', 'kwargs')

    def __init__(self, step_class, *args, **kwargs):
        self.step_class = step_class
        self.args = args
        self.kwargs = kwargs

    def buildStep(self):
        try:
            step = object.__new__(self.step_class)
            step._factory = self
            step.__init__(*self.args, **self.kwargs)
            return step
        except Exception:
            log.msg(
                f"error while creating step, step_class={self.step_class}, args={self.args}, "
                f"kwargs={self.kwargs}"
            )
            raise


class BuildStepStatus:
    # used only for old-style steps
    pass


def get_factory_from_step_or_factory(step_or_factory: BuildStep | interfaces.IBuildStepFactory):
    if hasattr(step_or_factory, 'get_step_factory'):
        factory = step_or_factory.get_step_factory()
    else:
        factory = step_or_factory
    # make sure the returned value actually implements IBuildStepFactory
    return interfaces.IBuildStepFactory(factory)


def create_step_from_step_or_factory(step_or_factory):
    return get_factory_from_step_or_factory(step_or_factory).buildStep()


class BuildStepWrapperMixin:
    __init_completed: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__init_completed = True

    def __setattr__(self, name, value):
        if self.__init_completed:
            config.error(
                "Changes to attributes of a BuildStep instance are ignored, this is a bug. "
                "Use set_step_arg(name, value) for that."
            )
        super().__setattr__(name, value)


# This is also needed for comparisons to work because ComparableMixin requires type(x) and
# x.__class__ to be equal in order to perform comparison at all.
_buildstep_wrapper_cache: dict[int, type[BuildStep]] = {}


def _create_buildstep_wrapper_class(klass):
    class_id = id(klass)
    cached = _buildstep_wrapper_cache.get(class_id, None)
    if cached is not None:
        return cached

    wrapper = type(klass.__qualname__, (BuildStepWrapperMixin, klass), {})
    _buildstep_wrapper_cache[class_id] = wrapper
    return wrapper


@implementer(interfaces.IBuildStep)
class BuildStep(
    results.ResultComputingConfigMixin, properties.PropertiesMixin, util.ComparableMixin
):
    # Note that the BuildStep is at the same time a template from which per-build steps are
    # constructed. This works by creating a new IBuildStepFactory in __new__, retrieving it via
    # get_step_factory() and then calling buildStep() on that factory.

    alwaysRun: bool = False
    doStepIf: bool | Callable[[BuildStep], bool] = True
    hideStepIf: bool | Callable[[int, BuildStep], bool] = False
    compare_attrs: ClassVar[Sequence[str]] = ("_factory",)
    # properties set on a build step are, by nature, always runtime properties
    set_runtime_properties: bool = True

    renderables: Sequence[str] = [
        *results.ResultComputingConfigMixin.resultConfig,
        'alwaysRun',
        'description',
        'descriptionDone',
        'descriptionSuffix',
        'doStepIf',
        'hideStepIf',
        'workdir',
    ]

    # '_params_names' holds a list of all the parameters we care about, to allow
    # users to instantiate a subclass of BuildStep with a mixture of
    # arguments, some of which are for us, some of which are for the subclass
    # (or a delegate of the subclass, like how ShellCommand delivers many
    # arguments to the RemoteShellCommand that it creates). Such delegating
    # subclasses will use this list to figure out which arguments are meant
    # for us and which should be given to someone else.
    _params_config: list[tuple[str, Callable | None]] = [
        ('alwaysRun', check_param_bool),
        ('description', None),
        ('descriptionDone', None),
        ('descriptionSuffix', None),
        ('doStepIf', None),
        ('flunkOnFailure', check_param_bool),
        ('flunkOnWarnings', check_param_bool),
        ('haltOnFailure', check_param_bool),
        ('updateBuildSummaryPolicy', None),
        ('hideStepIf', None),
        ('locks', None),
        ('logEncoding', None),
        ('name', check_param_str),
        ('progressMetrics', None),
        ('useProgress', None),
        ('warnOnFailure', check_param_bool),
        ('warnOnWarnings', check_param_bool),
        ('workdir', check_param_str_none),
    ]

    _params_names: list[str] = [arg for arg, _ in _params_config]

    name: str = "generic"
    description: str | list[str] | None = None  # set this to a list of short strings to override
    descriptionDone: str | list[str] | None = (
        None  # alternate description when the step is complete
    )
    descriptionSuffix: str | list[str] | None = None  # extra information to append to suffix
    updateBuildSummaryPolicy: list[int] | None | bool = None
    locks: list[str] | None = None
    _locks_to_acquire: list[BaseLock] = []
    progressMetrics: tuple[str, ...] = ()  # 'time' is implicit
    useProgress: bool = True  # set to False if step is really unpredictable
    build: Build | None = None
    step_status: None = None
    progress: None = None
    logEncoding: str | None = None
    cmd: remotecommand.RemoteCommand | None = None
    rendered: bool = False  # true if attributes are rendered
    _workdir: str | None = None
    _waitingForLocks: bool = False

    def __init__(self, **kwargs):
        self.worker = None

        for p, check in self.__class__._params_config:
            if p in kwargs:
                value = kwargs.pop(p)
                if check is not None and not IRenderable.providedBy(value):
                    check(value, self.__class__, p)
                setattr(self, p, value)

        if kwargs:
            config.error(
                f"{self.__class__}.__init__ got unexpected keyword argument(s) {list(kwargs)}"
            )
        self._pendingLogObservers = []

        check_param_length(
            self.name, f'Step {self.__class__.__name__} name', Model.steps.c.name.type.length
        )

        if isinstance(self.description, str):
            self.description = [self.description]
        if isinstance(self.descriptionDone, str):
            self.descriptionDone = [self.descriptionDone]
        if isinstance(self.descriptionSuffix, str):
            self.descriptionSuffix = [self.descriptionSuffix]

        if self.updateBuildSummaryPolicy is None:
            # compute default value for updateBuildSummaryPolicy
            self.updateBuildSummaryPolicy = [EXCEPTION, RETRY, CANCELLED]
            if self.flunkOnFailure or self.haltOnFailure or self.warnOnFailure:
                self.updateBuildSummaryPolicy.append(FAILURE)
            if self.warnOnWarnings or self.flunkOnWarnings:
                self.updateBuildSummaryPolicy.append(WARNINGS)
        if self.updateBuildSummaryPolicy is False:
            self.updateBuildSummaryPolicy = []
        if self.updateBuildSummaryPolicy is True:
            self.updateBuildSummaryPolicy = ALL_RESULTS
        if not isinstance(self.updateBuildSummaryPolicy, list):
            config.error(
                "BuildStep updateBuildSummaryPolicy must be "
                "a list of result ids or boolean but it is "
                f"{self.updateBuildSummaryPolicy!r}"
            )
        self._acquiringLocks = []
        self.stopped = False
        self.timed_out = False
        self.max_lines_reached = False
        self.master = None
        self.statistics = {}
        self.logs = {}
        self._running = False
        self.stepid = None
        self.results = None
        self._start_unhandled_deferreds = None
        self._interrupt_deferwaiter = deferwaiter.DeferWaiter()
        self._update_summary_debouncer = debounce.Debouncer(
            1.0, self._update_summary_impl, lambda: self.master.reactor, until_idle=False
        )
        self._test_result_submitters = {}

    def __new__(klass, *args, **kwargs):
        # The following code prevents changing BuildStep attributes after an instance
        # is created during config time. Such attribute changes don't affect the factory,
        # so they will be lost when actual build step is created.
        #
        # This is implemented by dynamically creating a subclass that disallows attribute
        # writes after __init__ completes.
        self = object.__new__(_create_buildstep_wrapper_class(klass))
        self._factory = _BuildStepFactory(klass, *args, **kwargs)
        return self

    def is_exact_step_class(self, klass):
        # Due to wrapping BuildStep in __new__, it's not possible to compare self.__class__ to
        # check if self is an instance of some class (but not subclass).
        if self.__class__ is klass:
            return True
        mro = self.__class__.mro()
        if len(mro) >= 3 and mro[1] is BuildStepWrapperMixin and mro[2] is klass:
            return True
        return False

    def __str__(self):
        args = [repr(x) for x in self._factory.args]
        args.extend([str(k) + "=" + repr(v) for k, v in self._factory.kwargs.items()])
        return f'{self.__class__.__name__}({", ".join(args)})'

    __repr__ = __str__

    def setBuild(self, build: Build) -> None:
        self.build = build
        self.master = self.build.master

    def setWorker(self, worker: AbstractWorker):
        self.worker = worker

    @deprecate.deprecated(versions.Version("buildbot", 0, 9, 0))
    def setDefaultWorkdir(self, workdir):
        if self._workdir is None:
            self._workdir = workdir

    @property
    def workdir(self):
        # default the workdir appropriately
        if self._workdir is not None or self.build is None:
            return self._workdir
        else:
            # see :ref:`Factory-Workdir-Functions` for details on how to
            # customize this
            if callable(self.build.workdir):
                try:
                    return self.build.workdir(self.build.sources)
                except AttributeError as e:
                    # if the callable raises an AttributeError
                    # python thinks it is actually workdir that is not existing.
                    # python will then swallow the attribute error and call
                    # __getattr__ from worker_transition
                    _, _, traceback = sys.exc_info()
                    raise CallableAttributeError(e).with_traceback(traceback) from e
                    # we re-raise the original exception by changing its type,
                    # but keeping its stacktrace
            else:
                return self.build.workdir

    @workdir.setter
    def workdir(self, workdir):
        self._workdir = workdir

    def getProperties(self):
        return self.build.getProperties()

    def get_step_factory(self):
        return self._factory

    def set_step_arg(self, name, value):
        self._factory.kwargs[name] = value
        # check if buildstep can still be constructed with the new arguments
        try:
            self._factory.buildStep()
        except Exception:
            log.msg(f"Cannot set step factory attribute {name} to {value}: step creation fails")
            raise

    def setupProgress(self):
        # this function temporarily does nothing
        pass

    def setProgress(self, metric, value):
        # this function temporarily does nothing
        pass

    def getCurrentSummary(self):
        if self.description is not None:
            stepsumm = util.join_list(self.description)
            if self.descriptionSuffix:
                stepsumm += ' ' + util.join_list(self.descriptionSuffix)
        else:
            stepsumm = 'running'
        return {'step': stepsumm}

    def getResultSummary(self):
        if self.descriptionDone is not None or self.description is not None:
            stepsumm = util.join_list(self.descriptionDone or self.description)
            if self.descriptionSuffix:
                stepsumm += ' ' + util.join_list(self.descriptionSuffix)
        else:
            stepsumm = 'finished'

        if self.results != SUCCESS:
            stepsumm += f' ({statusToString(self.results)})'
            if self.timed_out:
                stepsumm += " (timed out)"
            elif self.max_lines_reached:
                stepsumm += " (max lines reached)"

        if self.build is not None:
            stepsumm = self.build.properties.cleanupTextFromSecrets(stepsumm)
        return {'step': stepsumm}

    @defer.inlineCallbacks
    def getBuildResultSummary(self):
        summary = yield self.getResultSummary()
        if (
            self.results in self.updateBuildSummaryPolicy
            and 'build' not in summary
            and 'step' in summary
        ):
            summary['build'] = summary['step']
        return summary

    def updateSummary(self):
        self._update_summary_debouncer()

    @defer.inlineCallbacks
    def _update_summary_impl(self):
        def methodInfo(m):
            lines = inspect.getsourcelines(m)
            return "\nat {}:{}:\n {}".format(
                inspect.getsourcefile(m), lines[1], "\n".join(lines[0])
            )

        if not self._running:
            summary = yield self.getResultSummary()
            if not isinstance(summary, dict):
                raise TypeError(
                    'getResultSummary must return a dictionary: '
                    + methodInfo(self.getResultSummary)
                )
        else:
            summary = yield self.getCurrentSummary()
            if not isinstance(summary, dict):
                raise TypeError(
                    'getCurrentSummary must return a dictionary: '
                    + methodInfo(self.getCurrentSummary)
                )

        stepResult = summary.get('step', 'finished')
        if not isinstance(stepResult, str):
            raise TypeError(f"step result string must be unicode (got {stepResult!r})")
        if self.stepid is not None:
            stepResult = self.build.properties.cleanupTextFromSecrets(stepResult)
            yield self.master.data.updates.setStepStateString(self.stepid, stepResult)

        if not self._running:
            buildResult = summary.get('build', None)
            if buildResult and not isinstance(buildResult, str):
                raise TypeError("build result string must be unicode")

    @defer.inlineCallbacks
    def addStep(self):
        # create and start the step, noting that the name may be altered to
        # ensure uniqueness
        self.name = yield self.build.render(self.name)
        self.build.setUniqueStepName(self)
        self.stepid, self.number, self.name = yield self.master.data.updates.addStep(
            buildid=self.build.buildid, name=util.bytes2unicode(self.name)
        )

    @defer.inlineCallbacks
    def startStep(self, remote):
        self.remote = remote

        yield self.addStep()
        started_at = int(self.master.reactor.seconds())
        yield self.master.data.updates.startStep(self.stepid, started_at=started_at)

        try:
            yield self._render_renderables()
            # we describe ourselves only when renderables are interpolated
            self.updateSummary()

            # check doStepIf (after rendering)
            if isinstance(self.doStepIf, bool):
                doStep = self.doStepIf
            else:
                doStep = yield self.doStepIf(self)

            if doStep:
                yield self._setup_locks()

                # set up locks
                if self._locks_to_acquire:
                    yield self.acquireLocks()

                    if self.stopped:
                        raise BuildStepCancelled

                    locks_acquired_at = int(self.master.reactor.seconds())
                    yield defer.DeferredList(
                        [
                            self.master.data.updates.set_step_locks_acquired_at(
                                self.stepid, locks_acquired_at=locks_acquired_at
                            ),
                            self.master.data.updates.add_build_locks_duration(
                                self.build.buildid, duration_s=locks_acquired_at - started_at
                            ),
                        ],
                        consumeErrors=True,
                    )
                else:
                    yield self.master.data.updates.set_step_locks_acquired_at(
                        self.stepid, locks_acquired_at=started_at
                    )

                    if self.stopped:
                        raise BuildStepCancelled

                yield self.addTestResultSets()
                try:
                    self._running = True
                    self.results = yield self.run()
                finally:
                    self._running = False
            else:
                self.results = SKIPPED

        # NOTE: all of these `except` blocks must set self.results immediately!
        except BuildStepCancelled:
            self.results = CANCELLED

        except BuildStepFailed:
            self.results = FAILURE

        except error.ConnectionLost:
            self.results = RETRY

        except Exception:
            self.results = EXCEPTION
            why = Failure()
            log.err(why, "BuildStep.failed; traceback follows")
            yield self.addLogWithFailure(why)

        if self.stopped and self.results != RETRY:
            # We handle this specially because we don't care about
            # the return code of an interrupted command; we know
            # that this should just be exception due to interrupt
            # At the same time we must respect RETRY status because it's used
            # to retry interrupted build due to some other issues for example
            # due to worker lost
            if self.results != CANCELLED:
                self.results = EXCEPTION

        # determine whether we should hide this step
        hidden = self.hideStepIf
        if callable(hidden):
            try:
                hidden = hidden(self.results, self)
            except Exception:
                why = Failure()
                log.err(why, "hidden callback failed; traceback follows")
                yield self.addLogWithFailure(why)
                self.results = EXCEPTION
                hidden = False

        # perform final clean ups
        success = yield self._cleanup_logs()
        if not success:
            self.results = EXCEPTION

        # update the summary one last time, make sure that completes,
        # and then don't update it any more.
        self.updateSummary()
        yield self._update_summary_debouncer.stop()

        for sub in self._test_result_submitters.values():
            yield sub.finish()

        self.releaseLocks()

        yield self.master.data.updates.finishStep(self.stepid, self.results, hidden)

        return self.results

    @defer.inlineCallbacks
    def _setup_locks(self):
        self._locks_to_acquire = yield get_real_locks_from_accesses(self.locks, self.build)

        if self.build._locks_to_acquire:
            build_locks = [l for l, _ in self.build._locks_to_acquire]
            for l, _ in self._locks_to_acquire:
                if l in build_locks:
                    log.err(
                        f"{self}: lock {l} is claimed by both a Step ({self}) and the"
                        f" parent Build ({self.build})"
                    )
                    raise RuntimeError(f"lock claimed by both Step and Build ({l})")

    @defer.inlineCallbacks
    def _render_renderables(self):
        # render renderables in parallel
        renderables = []
        accumulateClassList(self.__class__, 'renderables', renderables)

        def setRenderable(res, attr):
            setattr(self, attr, res)

        dl = []
        for renderable in renderables:
            d = self.build.render(getattr(self, renderable))
            d.addCallback(setRenderable, renderable)
            dl.append(d)
        yield defer.gatherResults(dl, consumeErrors=True)
        self.rendered = True

    def setBuildData(self, name, value, source):
        # returns a Deferred that yields nothing
        return self.master.data.updates.setBuildData(self.build.buildid, name, value, source)

    @defer.inlineCallbacks
    def _cleanup_logs(self):
        # Wait until any in-progress interrupt() to finish (that function may add new logs)
        yield self._interrupt_deferwaiter.wait()

        all_success = True
        not_finished_logs = [v for (k, v) in self.logs.items() if not v.finished]
        finish_logs = yield defer.DeferredList(
            [v.finish() for v in not_finished_logs], consumeErrors=True
        )
        for success, res in finish_logs:
            if not success:
                log.err(res, "when trying to finish a log")
                all_success = False

        for log_ in self.logs.values():
            if log_.had_errors():
                all_success = False

        return all_success

    def addTestResultSets(self):
        return defer.succeed(None)

    @defer.inlineCallbacks
    def addTestResultSet(self, description, category, value_unit):
        sub = TestResultSubmitter()
        yield sub.setup(self, description, category, value_unit)
        setid = sub.get_test_result_set_id()
        self._test_result_submitters[setid] = sub
        return setid

    def addTestResult(
        self, setid, value, test_name=None, test_code_path=None, line=None, duration_ns=None
    ):
        self._test_result_submitters[setid].add_test_result(
            value,
            test_name=test_name,
            test_code_path=test_code_path,
            line=line,
            duration_ns=duration_ns,
        )

    def acquireLocks(self, res=None):
        if not self._locks_to_acquire:
            return defer.succeed(None)
        if self.stopped:
            return defer.succeed(None)
        log.msg(f"acquireLocks(step {self}, locks {self._locks_to_acquire})")
        for lock, access in self._locks_to_acquire:
            for waited_lock, _, _ in self._acquiringLocks:
                if lock is waited_lock:
                    continue

            if not lock.isAvailable(self, access):
                self._waitingForLocks = True
                log.msg(f"step {self} waiting for lock {lock}")
                d = lock.waitUntilMaybeAvailable(self, access)
                self._acquiringLocks.append((lock, access, d))
                d.addCallback(self.acquireLocks)
                return d
        # all locks are available, claim them all
        for lock, access in self._locks_to_acquire:
            lock.claim(self, access)
        self._acquiringLocks = []
        self._waitingForLocks = False
        return defer.succeed(None)

    def run(self):
        raise NotImplementedError("A custom build step must implement run()")

    @defer.inlineCallbacks
    def _maybe_interrupt_cmd(self, reason):
        if not self.cmd:
            return

        try:
            yield self.cmd.interrupt(reason)
        except Exception as e:
            log.err(e, 'while cancelling command')

    def interrupt(self, reason):
        # Note that this method may be run outside usual step lifecycle (e.g. after run() has
        # already completed), so extra care needs to be taken to prevent race conditions.
        return self._interrupt_deferwaiter.add(self._interrupt_impl(reason))

    @defer.inlineCallbacks
    def _interrupt_impl(self, reason):
        if self.stopped:
            # If we are in the process of interruption and connection is lost then we must tell
            # the command not to wait for the interruption to complete.
            if isinstance(reason, Failure) and reason.check(error.ConnectionLost):
                yield self._maybe_interrupt_cmd(reason)
            return

        self.stopped = True
        if self._acquiringLocks:
            for lock, access, d in self._acquiringLocks:
                lock.stopWaitingUntilAvailable(self, access, d)
            self._acquiringLocks = []

        log_name = "cancelled while waiting for locks" if self._waitingForLocks else "cancelled"
        yield self.addCompleteLog(log_name, str(reason))
        yield self._maybe_interrupt_cmd(reason)

    def releaseLocks(self):
        log.msg(f"releaseLocks({self}): {self._locks_to_acquire}")
        for lock, access in self._locks_to_acquire:
            if lock.isOwner(self, access):
                lock.release(self, access)
            else:
                # This should only happen if we've been interrupted
                assert self.stopped

    # utility methods that BuildSteps may find useful

    def workerVersion(self, command, oldversion=None):
        return self.build.getWorkerCommandVersion(command, oldversion)

    def workerVersionIsOlderThan(self, command, minversion):
        sv = self.build.getWorkerCommandVersion(command, None)
        if sv is None:
            return True
        if [int(s) for s in sv.split(".")] < [int(m) for m in minversion.split(".")]:
            return True
        return False

    def checkWorkerHasCommand(self, command):
        if not self.workerVersion(command):
            message = f"worker is too old, does not know about {command}"
            raise WorkerSetupError(message)

    def getWorkerName(self):
        return self.build.getWorkerName()

    def addLog(self, name, type='s', logEncoding=None):
        if self.stepid is None:
            raise BuildStepCancelled
        d = self.master.data.updates.addLog(self.stepid, util.bytes2unicode(name), str(type))

        @d.addCallback
        def newLog(logid):
            return self._newLog(name, type, logid, logEncoding)

        return d

    def getLog(self, name):
        return self.logs[name]

    @defer.inlineCallbacks
    def addCompleteLog(self, name, text):
        if self.stepid is None:
            raise BuildStepCancelled
        logid = yield self.master.data.updates.addLog(self.stepid, util.bytes2unicode(name), 't')
        _log = self._newLog(name, 't', logid)
        yield _log.addContent(text)
        yield _log.finish()

    @defer.inlineCallbacks
    def addHTMLLog(self, name, html):
        if self.stepid is None:
            raise BuildStepCancelled
        logid = yield self.master.data.updates.addLog(self.stepid, util.bytes2unicode(name), 'h')
        _log = self._newLog(name, 'h', logid)
        html = bytes2unicode(html)
        yield _log.addContent(html)
        yield _log.finish()

    @defer.inlineCallbacks
    def addLogWithFailure(self, why, logprefix=""):
        # helper for showing exceptions to the users
        try:
            yield self.addCompleteLog(logprefix + "err.text", why.getTraceback())
            yield self.addHTMLLog(logprefix + "err.html", formatFailure(why))
        except Exception:
            log.err(Failure(), "error while formatting exceptions")

    def addLogWithException(self, why, logprefix=""):
        return self.addLogWithFailure(Failure(why), logprefix)

    def addLogObserver(self, logname, observer):
        assert interfaces.ILogObserver.providedBy(observer)
        observer.setStep(self)
        self._pendingLogObservers.append((logname, observer))
        self._connectPendingLogObservers()

    def _newLog(self, name, type, logid, logEncoding=None):
        if not logEncoding:
            logEncoding = self.logEncoding
        if not logEncoding:
            logEncoding = self.master.config.logEncoding
        log = plog.Log.new(self.master, name, type, logid, logEncoding)
        self.logs[name] = log
        self._connectPendingLogObservers()
        return log

    def _connectPendingLogObservers(self):
        for logname, observer in self._pendingLogObservers[:]:
            if logname in self.logs:
                observer.setLog(self.logs[logname])
                self._pendingLogObservers.remove((logname, observer))

    @defer.inlineCallbacks
    def addURL(self, name, url):
        yield self.master.data.updates.addStepURL(self.stepid, str(name), str(url))
        return None

    @defer.inlineCallbacks
    def runCommand(self, command: remotecommand.RemoteCommand):
        if self.stopped:
            return CANCELLED

        self.cmd = command
        command.worker = self.worker
        try:
            assert self.build
            assert self.build.builder.name
            res = yield command.run(self, self.remote, self.build.builder.name)
            if command.remote_failure_reason in ("timeout", "timeout_without_output"):
                self.timed_out = True
            elif command.remote_failure_reason in ("max_lines_failure",):
                self.max_lines_reached = True
        finally:
            self.cmd = None
        return res

    def hasStatistic(self, name):
        return name in self.statistics

    def getStatistic(self, name, default=None):
        return self.statistics.get(name, default)

    def getStatistics(self):
        return self.statistics.copy()

    def setStatistic(self, name, value):
        self.statistics[name] = value


class CommandMixin:
    @defer.inlineCallbacks
    def _runRemoteCommand(self, cmd, abandonOnFailure, args, makeResult=None):
        cmd = remotecommand.RemoteCommand(cmd, args)
        try:
            log = self.getLog('stdio')
        except Exception:
            log = yield self.addLog('stdio')
        cmd.useLog(log, False)
        yield self.runCommand(cmd)
        if abandonOnFailure and cmd.didFail():
            raise BuildStepFailed()
        if makeResult:
            return makeResult(cmd)
        else:
            return not cmd.didFail()

    def runRmdir(self, dir, log=None, abandonOnFailure=True):
        return self._runRemoteCommand('rmdir', abandonOnFailure, {'dir': dir, 'logEnviron': False})

    def pathExists(self, path, log=None):
        return self._runRemoteCommand('stat', False, {'file': path, 'logEnviron': False})

    def runMkdir(self, dir, log=None, abandonOnFailure=True):
        return self._runRemoteCommand('mkdir', abandonOnFailure, {'dir': dir, 'logEnviron': False})

    def runGlob(self, path):
        return self._runRemoteCommand(
            'glob',
            True,
            {'path': path, 'logEnviron': False},
            makeResult=lambda cmd: cmd.updates['files'][0],
        )


class ShellMixin:
    command: list[str] | None = None
    env: dict[str, str] = {}
    want_stdout = True
    want_stderr = True
    usePTY: bool | None = None
    logfiles: dict[str, str] = {}
    lazylogfiles: bool = False
    timeout = 1200
    maxTime: float | None = None
    max_lines: int | None = None
    logEnviron = True
    interruptSignal = 'KILL'
    sigtermTime: int | None = None
    initialStdin: str | None = None
    decodeRC = {0: SUCCESS}

    _shell_mixin_arg_config = [
        ('command', None),
        ('workdir', check_param_str),
        ('env', None),
        ('want_stdout', check_param_bool),
        ('want_stderr', check_param_bool),
        ('usePTY', check_param_bool),
        ('logfiles', None),
        ('lazylogfiles', check_param_bool),
        ('timeout', check_param_number_none),
        ('maxTime', check_param_number_none),
        ('max_lines', check_param_number_none),
        ('logEnviron', check_param_bool),
        ('interruptSignal', check_param_str_none),
        ('sigtermTime', check_param_number_none),
        ('initialStdin', check_param_str_none),
        ('decodeRC', None),
    ]
    renderables: Sequence[str] = [arg for arg, _ in _shell_mixin_arg_config]

    def setupShellMixin(self, constructorArgs, prohibitArgs=None):
        constructorArgs = constructorArgs.copy()

        if prohibitArgs is None:
            prohibitArgs = []

        def bad(arg):
            config.error(f"invalid {self.__class__.__name__} argument {arg}")

        for arg, check in self._shell_mixin_arg_config:
            if arg not in constructorArgs:
                continue
            if arg in prohibitArgs:
                bad(arg)
            else:
                value = constructorArgs[arg]
                if check is not None and not IRenderable.providedBy(value):
                    check(value, self.__class__, arg)

                setattr(self, arg, constructorArgs[arg])
            del constructorArgs[arg]
        for arg in list(constructorArgs):
            if arg not in BuildStep._params_names:
                bad(arg)
                del constructorArgs[arg]
        return constructorArgs

    @defer.inlineCallbacks
    def makeRemoteShellCommand(
        self, collectStdout=False, collectStderr=False, stdioLogName='stdio', **overrides
    ):
        kwargs = {arg: getattr(self, arg) for arg, _ in self._shell_mixin_arg_config}
        kwargs.update(overrides)
        stdio = None
        if stdioLogName is not None:
            # Reuse an existing log if possible; otherwise, create one.
            try:
                stdio = yield self.getLog(stdioLogName)
            except KeyError:
                stdio = yield self.addLog(stdioLogName)

        kwargs['command'] = flatten(kwargs['command'], (list, tuple))

        # store command away for display
        self.command = kwargs['command']

        # check for the usePTY flag
        if kwargs['usePTY'] is not None:
            if self.workerVersionIsOlderThan("shell", "2.7"):
                if stdio is not None:
                    yield stdio.addHeader("NOTE: worker does not allow master to override usePTY\n")
                del kwargs['usePTY']

        # check for the interruptSignal flag
        if kwargs["interruptSignal"] and self.workerVersionIsOlderThan("shell", "2.15"):
            if stdio is not None:
                yield stdio.addHeader(
                    "NOTE: worker does not allow master to specify interruptSignal\n"
                )
            del kwargs['interruptSignal']

        # lazylogfiles are handled below
        del kwargs['lazylogfiles']

        # merge the builder's environment with that supplied here
        builderEnv = self.build.builder.config.env
        kwargs['env'] = {
            **(yield self.build.render(builderEnv)),
            **kwargs['env'],
        }
        kwargs['stdioLogName'] = stdioLogName

        # default the workdir appropriately
        if not kwargs.get('workdir') and not self.workdir:
            if callable(self.build.workdir):
                kwargs['workdir'] = self.build.workdir(self.build.sources)
            else:
                kwargs['workdir'] = self.build.workdir

        # the rest of the args go to RemoteShellCommand
        cmd = remotecommand.RemoteShellCommand(
            collectStdout=collectStdout, collectStderr=collectStderr, **kwargs
        )

        # set up logging
        if stdio is not None:
            cmd.useLog(stdio, False)
        for logname in self.logfiles:
            if self.lazylogfiles:
                # it's OK if this does, or does not, return a Deferred
                def callback(cmd_arg, local_logname=logname):
                    return self.addLog(local_logname)

                cmd.useLogDelayed(logname, callback, True)
            else:
                # add a LogFile
                newlog = yield self.addLog(logname)
                # and tell the RemoteCommand to feed it
                cmd.useLog(newlog, False)

        return cmd

    def getResultSummary(self):
        if self.descriptionDone is not None:
            return super().getResultSummary()
        summary = util.command_to_string(self.command)
        if summary:
            if self.results != SUCCESS:
                summary += f' ({statusToString(self.results)})'
                if self.timed_out:
                    summary += " (timed out)"
                elif self.max_lines_reached:
                    summary += " (max lines)"

            if self.build is not None:
                summary = self.build.properties.cleanupTextFromSecrets(summary)
            return {'step': summary}
        return super().getResultSummary()


_hush_pyflakes = [WithProperties]
del _hush_pyflakes
