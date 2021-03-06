# Copyright (C) 2015 UCSC Computational Genomics Lab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, print_function

import base64
import cPickle
import collections
import copy_reg
import errno
import importlib
import inspect
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from abc import ABCMeta, abstractmethod
from argparse import ArgumentParser
from contextlib import contextmanager
from fcntl import flock, LOCK_EX, LOCK_UN
from functools import partial
from hashlib import sha1
from io import BytesIO
from Queue import Queue, Empty
from threading import Thread, Semaphore, Event

from bd2k.util.expando import Expando
from bd2k.util.humanize import human2bytes
from toil.common import Toil, addOptions, cacheDirName
from toil.leader import mainLoop
from toil.lib.bioio import (setLoggingFromOptions,
                            getTotalCpuTimeAndMemoryUsage,
                            getTotalCpuTime,
                            makePublicDir)
from toil.realtimeLogger import RealtimeLogger
from toil.resource import ModuleDescriptor

logger = logging.getLogger( __name__ )


class Job(object):
    """
    Class represents a unit of work in toil.
    """
    def __init__(self, memory=None, cores=None, disk=None, preemptable=None, cache=None, checkpoint=False):
        """
        This method must be called by any overriding constructor.
        
        :param memory: the maximum number of bytes of memory the job will \
        require to run.
        :param cores: the number of CPU cores required.
        :param disk: the amount of local disk space required by the job, \
        expressed in bytes.
        :param preemptable: if the job can be run on a preemptable node.
        :param cache: the amount of disk (so that cache <= disk), expressed in bytes, \
        for storing files from previous jobs so that they can be accessed from a local copy. 
        :param checkpoint: if any of this job's successor jobs completely fails,
        exhausting all their retries, remove any successor jobs and rerun this job to restart the subtree. \
        Job must be a leaf vertex in the job graph when initially defined, \
        see :func:`toil.job.Job.checkNewCheckpointsAreCutVertices`.
        :type cores: int or string convertable by bd2k.util.humanize.human2bytes to an int
        :type disk: int or string convertable by bd2k.util.humanize.human2bytes to an int
        :type preemptable: boolean
        :type cache: int or string convertable by bd2k.util.humanize.human2bytes to an int
        :type memory: int or string convertable by bd2k.util.humanize.human2bytes to an int
        """
        self.cores = cores
        parse = lambda x : x if x is None else human2bytes(str(x))
        self.memory = parse(memory)
        self.disk = parse(disk)
        self.cache = parse(cache)
        self.checkpoint = checkpoint
        self.preemptable = preemptable
        #Private class variables

        #See Job.addChild
        self._children = []
        #See Job.addFollowOn
        self._followOns = []
        #See Job.addService
        self._services = []
        #A follow-on, service or child of a job A, is a "direct successor" of A, if B
        #is a direct successor of A, then A is a "direct predecessor" of B.
        self._directPredecessors = set()
        # Note that self.__module__ is not necessarily this module, i.e. job.py. It is the module
        # defining the class self is an instance of, which may be a subclass of Job that may be
        # defined in a different module.
        self.userModule = ModuleDescriptor.forModule(self.__module__)
        # Maps indices into composite return values to lists of IDs of files containing promised
        # values for those return value items. The special key None represents the entire return
        # value.
        self._rvs = collections.defaultdict(list)
        self._promiseJobStore = None


    def run(self, fileStore):
        """
        Override this function to perform work and dynamically create successor jobs.

        :param toil.job.Job.FileStore fileStore: Used to create local and globally \
        sharable temporary files and to send log messages to the leader process.

        :return: The return value of the function can be passed to other jobs \
        by means of :func:`toil.job.Job.rv`.
        """
        pass

    def addChild(self, childJob):
        """
        Adds childJob to be run as child of this job. Child jobs will be run \
        directly after this job's :func:`toil.job.Job.run` method has completed.

        :param toil.job.Job childJob:
        :return: childJob
        :rtype: toil.job.Job
        """
        self._children.append(childJob)
        childJob._addPredecessor(self)
        return childJob

    def hasChild(self, childJob):
        """
        Check if childJob is already a child of this job.

        :param toil.job.Job childJob:
        :return: True if childJob is a child of the job, else False.
        :rtype: Boolean
        """
        return childJob in self._children

    def addFollowOn(self, followOnJob):
        """
        Adds a follow-on job, follow-on jobs will be run after the child jobs and \
        their successors have been run.

        :param toil.job.Job followOnJob:
        :return: followOnJob
        :rtype: toil.job.Job
        """
        self._followOns.append(followOnJob)
        followOnJob._addPredecessor(self)
        return followOnJob

    def addService(self, service, parentService=None):
        """
        Add a service.

        The :func:`toil.job.Job.Service.start` method of the service will be called \
        after the run method has completed but before any successors are run. \
        The service's :func:`toil.job.Job.Service.stop` method will be called once \
        the successors of the job have been run.

        Services allow things like databases and servers to be started and accessed \
        by jobs in a workflow.

        :raises toil.job.JobException: If service has already been made the child of a job or another service.
        :param toil.job.Job.Service service: Service to add.
        :param toil.job.Job.Service parentService: Service that will be started before 'service' is
               started. Allows trees of services to be established. parentService must be a service
               of this job.
        :return: a promise that will be replaced with the return value from
                 :func:`toil.job.Job.Service.start` of service in any successor of the job.
        :rtype:toil.job.Promise
        """
        if parentService is not None:
            # Do check to ensure that parentService is a service of this job
            def check(services):
                for jS in services:
                    if jS.service == parentService or check(jS.service._childServices):
                        return True
                return False
            if not check(self._services):
                raise JobException("Parent service is not a service of the given job")
            return parentService._addChild(service)
        else:
            if service._hasParent:
                raise JobException("The service already has a parent service")
            service._hasParent = True
            jobService = ServiceJob(service)
            self._services.append(jobService)
            return jobService.rv()

    ##Convenience functions for creating jobs

    def addChildFn(self, fn, *args, **kwargs):
        """
        Adds a function as a child job.

        :param fn: Function to be run as a child job with ``*args`` and ``**kwargs`` as \
        arguments to this function. See toil.job.FunctionWrappingJob for reserved \
        keyword arguments used to specify resource requirements.
        :return: The new child job that wraps fn.
        :rtype: toil.job.FunctionWrappingJob
        """
        return self.addChild(FunctionWrappingJob(fn, *args, **kwargs))

    def addFollowOnFn(self, fn, *args, **kwargs):
        """
        Adds a function as a follow-on job.

        :param fn: Function to be run as a follow-on job with ``*args`` and ``**kwargs`` as \
        arguments to this function. See toil.job.FunctionWrappingJob for reserved \
        keyword arguments used to specify resource requirements.
        :return: The new follow-on job that wraps fn.
        :rtype: toil.job.FunctionWrappingJob
        """
        return self.addFollowOn(FunctionWrappingJob(fn, *args, **kwargs))

    def addChildJobFn(self, fn, *args, **kwargs):
        """
        Adds a job function as a child job. See :class:`toil.job.JobFunctionWrappingJob`
        for a definition of a job function.

        :param fn: Job function to be run as a child job with ``*args`` and ``**kwargs`` as \
        arguments to this function. See toil.job.JobFunctionWrappingJob for reserved \
        keyword arguments used to specify resource requirements.
        :return: The new child job that wraps fn.
        :rtype: toil.job.JobFunctionWrappingJob
        """
        return self.addChild(JobFunctionWrappingJob(fn, *args, **kwargs))

    def addFollowOnJobFn(self, fn, *args, **kwargs):
        """
        Add a follow-on job function. See :class:`toil.job.JobFunctionWrappingJob`
        for a definition of a job function.

        :param fn: Job function to be run as a follow-on job with ``*args`` and ``**kwargs`` as \
        arguments to this function. See toil.job.JobFunctionWrappingJob for reserved \
        keyword arguments used to specify resource requirements.
        :return: The new follow-on job that wraps fn.
        :rtype: toil.job.JobFunctionWrappingJob
        """
        return self.addFollowOn(JobFunctionWrappingJob(fn, *args, **kwargs))

    @staticmethod
    def wrapFn(fn, *args, **kwargs):
        """
        Makes a Job out of a function. \
        Convenience function for constructor of :class:`toil.job.FunctionWrappingJob`.

        :param fn: Function to be run with ``*args`` and ``**kwargs`` as arguments. \
        See toil.job.JobFunctionWrappingJob for reserved keyword arguments used \
        to specify resource requirements.
        :return: The new function that wraps fn.
        :rtype: toil.job.FunctionWrappingJob
        """
        return FunctionWrappingJob(fn, *args, **kwargs)

    @staticmethod
    def wrapJobFn(fn, *args, **kwargs):
        """
        Makes a Job out of a job function. \
        Convenience function for constructor of :class:`toil.job.JobFunctionWrappingJob`.

        :param fn: Job function to be run with ``*args`` and ``**kwargs`` as arguments. \
        See toil.job.JobFunctionWrappingJob for reserved keyword arguments used \
        to specify resource requirements.
        :return: The new job function that wraps fn.
        :rtype: toil.job.JobFunctionWrappingJob
        """
        return JobFunctionWrappingJob(fn, *args, **kwargs)

    def encapsulate(self):
        """
        Encapsulates the job, see :class:`toil.job.EncapsulatedJob`.
        Convenience function for constructor of :class:`toil.job.EncapsulatedJob`.

        :return: an encapsulated version of this job.
        :rtype: toil.job.EncapsulatedJob.
        """
        return EncapsulatedJob(self)

    ####################################################
    #The following function is used for passing return values between
    #job run functions
    ####################################################

    def rv(self, index=None):
        """
        Creates a *promise* (:class:`toil.job.Promise`) representing a return value of the job's
        run method, or, in case of a function-wrapping job, the wrapped function's return value.

        :param int|None index: If None the complete return value will be used, otherwise an
               index to select an individual item from the return value in which case the return
               value must be of a type that implements the __getitem__ magic method, e.g. dict,
               list or tuple.

        :return: A promise representing the return value of this jobs :meth:`toil.job.Job.run`
                 method.

        :rtype: toil.job.Promise
        """
        return Promise(self, index)

    def allocatePromiseFile(self, index):
        if self._promiseJobStore is None:
            raise RuntimeError('Trying to pass a promise from a promising job that is not a '
                               'predecessor of the job receiving the promise')
        jobStoreFileID = self._promiseJobStore.getEmptyFileStoreID()
        self._rvs[index].append(jobStoreFileID)
        return self._promiseJobStore.config.jobStore, jobStoreFileID

    ####################################################
    #Cycle/connectivity checking
    ####################################################

    def checkJobGraphForDeadlocks(self):
        """
        :raises toil.job.JobGraphDeadlockException: if the job graph \
        is cyclic, contains multiple roots or contains checkpoint jobs that are
        not leaf vertices when defined (see :func:`toil.job.Job.checkNewCheckpointsAreLeaves`).

        See :func:`toil.job.Job.checkJobGraphConnected`, \
        :func:`toil.job.Job.checkJobGraphAcyclic` and \
        :func:`toil.job.Job.checkNewCheckpointsAreLeafVertices` for more info.
        """
        self.checkJobGraphConnected()
        self.checkJobGraphAcylic()
        self.checkNewCheckpointsAreLeafVertices()

    def getRootJobs(self):
        """
        :return: The roots of the connected component of jobs that contains this job. \
        A root is a job with no predecessors.

        :rtype : set of toil.job.Job instances
        """
        roots = set()
        visited = set()
        #Function to get the roots of a job
        def getRoots(job):
            if job not in visited:
                visited.add(job)
                if len(job._directPredecessors) > 0:
                    map(lambda p : getRoots(p), job._directPredecessors)
                else:
                    roots.add(job)
                #The following call ensures we explore all successor edges.
                map(lambda c : getRoots(c), job._children +
                    job._followOns)
        getRoots(self)
        return roots

    def checkJobGraphConnected(self):
        """
        :raises toil.job.JobGraphDeadlockException: if :func:`toil.job.Job.getRootJobs` does \
        not contain exactly one root job.

        As execution always starts from one root job, having multiple root jobs will \
        cause a deadlock to occur.
        """
        rootJobs = self.getRootJobs()
        if len(rootJobs) != 1:
            raise JobGraphDeadlockException("Graph does not contain exactly one"
                                            " root job: %s" % rootJobs)

    def checkJobGraphAcylic(self):
        """
        :raises toil.job.JobGraphDeadlockException: if the connected component \
        of jobs containing this job contains any cycles of child/followOn dependencies \
        in the *augmented job graph* (see below). Such cycles are not allowed \
        in valid job graphs.

        A follow-on edge (A, B) between two jobs A and B is equivalent \
        to adding a child edge to B from (1) A, (2) from each child of A, \
        and (3) from the successors of each child of A. We call each such edge \
        an edge an "implied" edge. The augmented job graph is a job graph including \
        all the implied edges.

        For a job graph G = (V, E) the algorithm is ``O(|V|^2)``. It is ``O(|V| + |E|)`` for \
        a graph with no follow-ons. The former follow-on case could be improved!
        """
        #Get the root jobs
        roots = self.getRootJobs()
        if len(roots) == 0:
            raise JobGraphDeadlockException("Graph contains no root jobs due to cycles")

        #Get implied edges
        extraEdges = self._getImpliedEdges(roots)

        #Check for directed cycles in the augmented graph
        visited = set()
        for root in roots:
            root._checkJobGraphAcylicDFS([], visited, extraEdges)

    def checkNewCheckpointsAreLeafVertices(self):
        """
        A checkpoint job is a job that is restarted if either it fails, or if any of \
        its successors completely fails, exhausting their retries.

        A job is a leaf it is has no successors.

        A checkpoint job must be a leaf when initially added to the job graph. When its \
        run method is invoked it can then create direct successors. This restriction is made
        to simplify implementation.

        :raises toil.job.JobGraphDeadlockException: if there exists a job being added to the graph for which \
        checkpoint=True and which is not a leaf.
        """
        roots = self.getRootJobs() # Roots jobs of component, these are preexisting jobs in the graph

        # All jobs in the component of the job graph containing self
        jobs = set()
        map(lambda x : x._dfs(jobs), roots)

        # Check for each job for which checkpoint is true that it is a cut vertex or leaf
        for y in filter(lambda x : x.checkpoint, jobs):
            if y not in roots: # The roots are the prexisting jobs
                if len(y._children) != 0 and len(y._followOns) != 0 and len(y._services) != 0:
                    raise JobGraphDeadlockException("New checkpoint job %s is not a leaf in the job graph" % y)

    ####################################################
    #The following nested classes are used for
    #creating jobtrees (Job.Runner),
    #managing temporary files (Job.FileStore),
    #and defining a service (Job.Service)
    ####################################################

    class Runner(object):
        """
        Used to setup and run Toil workflow.
        """
        @staticmethod
        def getDefaultArgumentParser():
            """
            Get argument parser with added toil workflow options.

            :returns: The argument parser used by a toil workflow with added Toil options.
            :rtype: :class:`argparse.ArgumentParser`
            """
            parser = ArgumentParser()
            Job.Runner.addToilOptions(parser)
            return parser

        @staticmethod
        def getDefaultOptions(jobStore):
            """
            Get default options for a toil workflow.

            :param string jobStore: A string describing the jobStore \
            for the workflow.
            :returns: The options used by a toil workflow.
            :rtype: argparse.ArgumentParser values object
            """
            parser = Job.Runner.getDefaultArgumentParser()
            return parser.parse_args(args=[jobStore])

        @staticmethod
        def addToilOptions(parser):
            """
            Adds the default toil options to an :mod:`optparse` or :mod:`argparse`
            parser object.

            :param parser: Options object to add toil options to.
            :type parser: optparse.OptionParser or argparse.ArgumentParser
            """
            addOptions(parser)

        @staticmethod
        def startToil(job, options):
            """
            Deprecated by toil.common.Toil.run. Runs the toil workflow using the given options
            (see Job.Runner.getDefaultOptions and Job.Runner.addToilOptions) starting with this
            job.
            :param toil.job.Job job: root job of the workflow
            :raises: toil.leader.FailedJobsException if at the end of function \
            their remain failed jobs.
            :return: The return value of the root job's run function.
            :rtype: Any
            """
            setLoggingFromOptions(options)
            with Toil(options) as toil:
                if not options.restart:
                    return toil.start(job)
                else:
                    return toil.restart()

    class FileStore( object ):
        """
        Class used to manage temporary files, read and write files from the job store\
        and log messages, passed as argument to the :func:`toil.job.Job.run` method.
        """
        #Variables used for synching reads/writes
        _pendingFileWritesLock = Semaphore()
        _pendingFileWrites = set()
        # For files in jobStore that are on the local disk,
        # map of jobStoreFileIDs to locations in localTempDir.
        _jobStoreFileIDToCacheLocation = {}
        _terminateEvent = Event() #Used to signify crashes in threads

        def __init__(self, jobStore, jobWrapper, localTempDir, inputBlockFn):
            """
            This constructor should not be called by the user, \
            FileStore instances are only provided as arguments to the run function.

            :param toil.jobStores.abstractJobStore.JobStore jobStore: The job store \
            for the workflow.
            :param toil.jobWrapper.JobWrapper jobWrapper: The jobWrapper for the job.
            :param string localTempDir: A temporary directory in which local temporary \
            files will be placed.
            :param method inputBlockFn: A function which blocks and which is called before \
            the fileStore completes atomically updating the jobs files in the job store.
            """
            self.jobStore = jobStore
            self.jobWrapper = jobWrapper
            self.localTempDir = os.path.abspath(localTempDir)
            self.loggingMessages = []
            self.filesToDelete = set()
            self.jobsToDelete = set()
            #Asynchronous writes stuff
            self.workerNumber = 2
            self.queue = Queue()
            self.updateSemaphore = Semaphore()
            self.mutable = self.jobStore.config.readGlobalFileMutableByDefault
            #Function to write files asynchronously to job store
            def asyncWrite():
                try:
                    while True:
                        try:
                            #Block for up to two seconds waiting for a file
                            args = self.queue.get(timeout=2)
                        except Empty:
                            #Check if termination event is signaled
                            #(set in the event of an exception in the worker)
                            if self._terminateEvent.isSet():
                                raise RuntimeError("The termination flag is set, exiting")
                            continue
                        #Normal termination condition is getting None from queue
                        if args is None:
                            break
                        inputFileHandle, jobStoreFileID = args
                        #We pass in a fileHandle, rather than the file-name, in case
                        #the file itself is deleted. The fileHandle itself should persist
                        #while we maintain the open file handle
                        with jobStore.updateFileStream(jobStoreFileID) as outputFileHandle:
                            bufferSize=1000000 #TODO: This buffer number probably needs to be modified/tuned
                            while 1:
                                copyBuffer = inputFileHandle.read(bufferSize)
                                if not copyBuffer:
                                    break
                                outputFileHandle.write(copyBuffer)
                        inputFileHandle.close()
                        #Remove the file from the lock files
                        with self._pendingFileWritesLock:
                            self._pendingFileWrites.remove(jobStoreFileID)
                except:
                    self._terminateEvent.set()
                    raise

            self.workers = map(lambda i : Thread(target=asyncWrite),
                               range(self.workerNumber))
            for worker in self.workers:
                worker.start()
            self.inputBlockFn = inputBlockFn

        @contextmanager
        def open(self, job):
            '''
            This is a dummy context manager that has a true purpose in Job.CachedFileStore where the
            __enter__ and __exit__ methods carry out cache eviction, and cache cleanup operations.
            :param job:
            :return:
            '''
            # Create a working directory for the job
            startingDir = os.getcwd()
            self.localTempDir = makePublicDir(os.path.join(self.localTempDir, str(uuid.uuid4())))
            try:
                # chdir to the working directory so all files created without any path are created
                # in the scope of Toil's working folder.
                os.chdir(self.localTempDir)
                yield
            finally:
                # chdir back to the starting point for sanity
                os.chdir(startingDir)

        def getLocalTempDir(self):
            """
            Get a new local temporary directory in which to write files that persist \
            for the duration of the job.

            :return: The absolute path to a new local temporary directory. \
            This directory will exist for the duration of the job only, and is \
            guaranteed to be deleted once the job terminates, removing all files \
            it contains recursively.
            :rtype: string
            """
            return os.path.abspath(tempfile.mkdtemp(prefix="t", dir=self.localTempDir))

        def getLocalTempFile(self):
            """
            Get a new local temporary file that will persist for the duration of the job.

            :return: The absolute path to a local temporary file. \
            This file will exist for the duration of the job only, and \
            is guaranteed to be deleted once the job terminates.
            :rtype: string
            """
            handle, tmpFile = tempfile.mkstemp(prefix="tmp",
                                               suffix=".tmp", dir=self.localTempDir)
            os.close(handle)
            return os.path.abspath(tmpFile)

        def getLocalTempFileName(self):
            '''
            Get a valid name for a new local file. Do it in a really stupid way by creating and then
            deleting a temp file (haha).
            :return: Path to valid file
            '''
            tempFile = self.getLocalTempFile()
            os.remove(tempFile)
            return tempFile

        def writeGlobalFile(self, localFileName, cleanup=False):
            """
            Takes a file (as a path) and uploads it to the job store.

            If the local file is a file returned by :func:`toil.job.Job.FileStore.getLocalTempFile`
            or is in a directory, or, recursively, a subdirectory, returned by
            :func:`toil.job.Job.FileStore.getLocalTempDir` then the write is asynchronous,
            so further modifications during execution to the file pointed by localFileName will
            result in undetermined behavior. Otherwise, the method will block until the file is
            written to the file store.

            :param string localFileName: The path to the local file to upload.

            :param Boolean cleanup: if True then the copy of the global file will \
            be deleted once the job and all its successors have completed running. \
            If not the global file must be deleted manually.

            :returns: an ID that can be used to retrieve the file.
            """
            #Put the file into the cache if it is a path within localTempDir
            absLocalFileName = os.path.abspath(localFileName)
            cleanupID = None if not cleanup else self.jobWrapper.jobStoreID
            if absLocalFileName.startswith(self.localTempDir):
                jobStoreFileID = self.jobStore.getEmptyFileStoreID(cleanupID)
                fileHandle = open(absLocalFileName, 'r')
                if os.stat(absLocalFileName).st_uid == os.getuid():
                    #Chmod if permitted to make file read only to try to prevent accidental user modification
                    os.chmod(absLocalFileName, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                with self._pendingFileWritesLock:
                    self._pendingFileWrites.add(jobStoreFileID)
                # A file handle added to the queue allows the asyncWrite threads to remove their jobID from _pendingFileWrites.
                # Therefore, a file should only be added after its fileID is added to _pendingFileWrites
                self.queue.put((fileHandle, jobStoreFileID))
                self._jobStoreFileIDToCacheLocation[jobStoreFileID] = absLocalFileName
            else:
                #Write the file directly to the file store
                jobStoreFileID = self.jobStore.writeFile(localFileName, cleanupID)
            return jobStoreFileID

        def writeGlobalFileStream(self, cleanup=False):
            """
            Similar to writeGlobalFile, but allows the writing of a stream to the job store.

            :param Boolean cleanup: is as in :func:`toil.job.Job.FileStore.writeGlobalFile`.

            :returns: a context manager yielding a tuple of 1) a file handle which \
            can be written to and 2) the ID of the resulting file in the job store. \
            The yielded file handle does not need to and should not be closed explicitly.
            """
            #TODO: Make this work with the caching??
            return self.jobStore.writeFileStream(None if not cleanup else self.jobWrapper.jobStoreID)

        def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=None):
            """
            Get a copy of a file in the job store.

            :param string userPath: a path to the name of file to which the global \
            file will be copied or hard-linked (see below).

            :param boolean cache: If True will use caching (see below). Caching will \
            attempt to keep copies of files between sequences of jobs run on the same \
            worker.

            :param boolean mutable: If True, the file path returned points to a file that is
            modifiable by the user. The value defaults to the False unless backwards compatibility
            was requested.

            If cache=True and userPath is either: (1) a file path contained within \
            a directory or, recursively, a subdirectory of a temporary directory \
            returned by Job.FileStore.getLocalTempDir(), or (2) a file path returned by \
            Job.FileStore.getLocalTempFile() then the file will be cached and returned file \
            will be read only (have permissions 444).

            If userPath is specified and the file is already cached, the userPath file \
            will be a hard link to the actual location, else it will be an actual copy \
            of the file.

            If the cache=False or userPath is not either of the above the file will not \
            be cached and will have default permissions. Note, if the file is already cached \
            this will result in two copies of the file on the system.

            :return: an absolute path to a local, temporary copy of the file keyed \
            by fileStoreID.

            :rtype : string
            """
            if fileStoreID in self.filesToDelete:
                raise RuntimeError("Trying to access a file in the jobStore you've deleted: %s" % fileStoreID)
            # Set up the modifiable variable if it wasn't provided by the user in the function call.
            if mutable is None:
                mutable = self.mutable
            if userPath != None:
                userPath = os.path.abspath(userPath) #Make an absolute path
                #Turn off caching if user file is not in localTempDir
                if cache and not userPath.startswith(self.localTempDir):
                    cache = False
            #When requesting a new file from the jobStore first check if fileStoreID
            #is a key in _jobStoreFileIDToCacheLocation.
            if fileStoreID in self._jobStoreFileIDToCacheLocation:
                cachedAbsFilePath = self._jobStoreFileIDToCacheLocation[fileStoreID]
                if cache and not mutable:
                    #If the user specifies a location and it is not the current location
                    #return a hardlink to the location, else return the original location
                    if userPath == None or userPath == cachedAbsFilePath:
                        return cachedAbsFilePath
                    #Chmod to make file read only
                    if os.path.exists(userPath):
                        os.remove(userPath)
                    os.link(cachedAbsFilePath, userPath)
                    os.chmod(userPath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                    return userPath
                else:
                    #If caching is not true then make a copy of the file
                    localFilePath = userPath if userPath != None else self.getLocalTempFile()
                    shutil.copyfile(cachedAbsFilePath, localFilePath)
                    return localFilePath
            else:
                #If it is not in the cache read it from the jobStore to the
                #desired location
                localFilePath = userPath if userPath != None else self.getLocalTempFile()
                self.jobStore.readFile(fileStoreID, localFilePath)
                if mutable:
                    # FIXME Can't do this at the top because of loopy (circular) import errors
                    from toil.jobStores.fileJobStore import FileJobStore
                    if isinstance(self.jobStore, FileJobStore):
                        # If readFile created a hard-link, we need to undo the link and copy. 
                        if os.stat(localFilePath).st_nlink==2:
                            shutil.copyfile(localFilePath, localFilePath+'.tmp')
                            os.rename(localFilePath+'.tmp', localFilePath)
                else:
                    os.chmod(localFilePath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                #If caching is enabled and the file is in local temp dir then
                #add to cache and make read only
                if cache:
                    assert localFilePath.startswith(self.localTempDir)
                    self._jobStoreFileIDToCacheLocation[fileStoreID] = localFilePath
                    os.chmod(localFilePath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                return localFilePath

        def readGlobalFileStream(self, fileStoreID):
            """
            Similar to readGlobalFile, but allows a stream to be read from the job \
            store.

            :returns: a context manager yielding a file handle which can be read from. \
            The yielded file handle does not need to and should not be closed explicitly.
            """
            if fileStoreID in self.filesToDelete:
                raise RuntimeError("Trying to access a file in the jobStore you've deleted: %s" % fileStoreID)

            #If fileStoreID is in the cache provide a handle from the local cache
            if fileStoreID in self._jobStoreFileIDToCacheLocation:
                #This leaks file handles (but the commented out code does not work properly)
                return open(self._jobStoreFileIDToCacheLocation[fileStoreID], 'r')
                #with open(self._jobStoreFileIDToCacheLocation[fileStoreID], 'r') as fH:
                #        yield fH
            else:
                #TODO: Progressively add the file to the cache
                return self.jobStore.readFileStream(fileStoreID)
                #with self.jobStore.readFileStream(fileStoreID) as fH:
                #    yield fH

        def deleteGlobalFile(self, fileStoreID):
            """
            Deletes a global file with the given job store ID.

            To ensure that the job can be restarted if necessary, the delete \
            will not happen until after the job's run method has completed.

            :param fileStoreID: the job store ID of the file to be deleted.
            """
            self.filesToDelete.add(fileStoreID)
            #If the fileStoreID is in the cache:
            if fileStoreID in self._jobStoreFileIDToCacheLocation:
                #This will result in the files removal from the cache at the end of the current job
                self._jobStoreFileIDToCacheLocation.pop(fileStoreID)

        def importFile(self, srcUrl):
            return self.jobStore.importFile(srcUrl)

        def exportFile(self, jobStoreFileID, dstUrl):
            self.jobStore.exportFile(jobStoreFileID, dstUrl)

        def logToMaster(self, text, level=logging.INFO):
            """
            Send a logging message to the leader. The message will also be \
            logged by the worker at the same level.

            :param text: The string to log.
            :param int level: The logging level.
            """
            logger.log(level=level, msg=("LOG-TO-MASTER: " + text))
            self.loggingMessages.append(dict(text=text, level=level))

        def _updateJobWhenDone(self):
            """
            Asynchronously update the status of the job on the disk, first waiting \
            until the writing threads have finished and the input blockFn has stopped \
            blocking.
            """
            def asyncUpdate():
                try:
                    #Wait till all file writes have completed
                    for i in xrange(len(self.workers)):
                        self.queue.put(None)

                    for thread in self.workers:
                        thread.join()

                    #Wait till input block-fn returns - in the event of an exception
                    #this will eventually terminate
                    self.inputBlockFn()

                    #Check the terminate event, if set we can not guarantee
                    #that the workers ended correctly, therefore we exit without
                    #completing the update
                    if self._terminateEvent.isSet():
                        raise RuntimeError("The termination flag is set, exiting before update")

                    #Indicate any files that should be deleted once the update of
                    #the job wrapper is completed.
                    self.jobWrapper.filesToDelete = list(self.filesToDelete)

                    #Complete the job
                    self.jobStore.update(self.jobWrapper)

                    #Delete any remnant jobs
                    map(self.jobStore.delete, self.jobsToDelete)

                    #Delete any remnant files
                    map(self.jobStore.deleteFile, self.filesToDelete)

                    #Remove the files to delete list, having successfully removed the files
                    if len(self.filesToDelete) > 0:
                        self.jobWrapper.filesToDelete = []
                        #Update, removing emptying files to delete
                        self.jobStore.update(self.jobWrapper)
                except:
                    self._terminateEvent.set()
                    raise
                finally:
                    #Indicate that _blockFn can return
                    #This code will always run
                    self.updateSemaphore.release()
            #The update semaphore is held while the jobWrapper is written to disk
            try:
                self.updateSemaphore.acquire()
                t = Thread(target=asyncUpdate)
                t.start()
            except: #This is to ensure that the semaphore is released in a crash to stop a deadlock scenario
                self.updateSemaphore.release()
                raise

        def _cleanLocalTempDir(self, cacheSize):
            """
            At the end of the job, remove all localTempDir files except those whose \
            value is in _jobStoreFileIDToCacheLocation.

            :param int cacheSize: the total number of bytes of files allowed in the cache.
            """
            #Remove files so that the total cached files are smaller than a cacheSize

            #List of pairs of (fileCreateTime, fileStoreID) for cached files
            with self._pendingFileWritesLock:
                deletableCacheFiles = set(
                        self._jobStoreFileIDToCacheLocation.keys()) - self._pendingFileWrites
            cachedFileCreateTimes = map(
                    lambda x: (os.stat(self._jobStoreFileIDToCacheLocation[x]).st_ctime, x),
                    deletableCacheFiles)
            # Total number of bytes stored in cached files
            totalCachedFileSizes = sum(
                    [os.stat(self._jobStoreFileIDToCacheLocation[x]).st_size for x in
                     self._jobStoreFileIDToCacheLocation.keys()])
            # Remove earliest created files first - this is in place of 'Remove smallest files first'.  Again, might
            # not be the best strategy.
            cachedFileCreateTimes.sort()
            cachedFileCreateTimes.reverse()
            #Now do the actual file removal
            while totalCachedFileSizes > cacheSize and len(cachedFileCreateTimes) > 0:
                fileCreateTime, fileStoreID = cachedFileCreateTimes.pop()
                fileSize = os.stat(self._jobStoreFileIDToCacheLocation[fileStoreID]).st_size
                filePath = self._jobStoreFileIDToCacheLocation[fileStoreID]
                self._jobStoreFileIDToCacheLocation.pop(fileStoreID)
                os.remove(filePath)
                totalCachedFileSizes -= fileSize
                assert totalCachedFileSizes >= 0

            #Iterate from the base of localTempDir and remove all
            #files/empty directories, recursively
            cachedFiles = set(self._jobStoreFileIDToCacheLocation.values())

            def clean(dirOrFile, remove=True):
                canRemove = True
                if os.path.isdir(dirOrFile):
                    for f in os.listdir(dirOrFile):
                        canRemove = canRemove and clean(os.path.join(dirOrFile, f))
                    if canRemove and remove:
                        os.rmdir(dirOrFile) #Dir should be empty if canRemove is true
                    return canRemove
                if dirOrFile in cachedFiles:
                    return False
                os.remove(dirOrFile)
                return True
            clean(self.localTempDir, False)

        def _blockFn(self):
            """
            Blocks while _updateJobWhenDone is running.
            """
            self.updateSemaphore.acquire()
            self.updateSemaphore.release() #Release so that the block function can be recalled
            #This works, because once acquired the semaphore will not be acquired
            #by _updateJobWhenDone again.
            return

        def __del__(self):
            """Cleanup function that is run when destroying the class instance \
            that ensures that all the file writing threads exit.
            """
            self.updateSemaphore.acquire()
            for i in xrange(len(self.workers)):
                self.queue.put(None)
            for thread in self.workers:
                thread.join()
            self.updateSemaphore.release()

    class CachedFileStore(FileStore):
        '''
        A cache-enabled version of Filestore. Basically FileStore on Adderall(R)
        '''
        def __init__(self, jobStore, jobWrapper, localTempDir, inputBlockFn):
            super(Job.CachedFileStore, self).__init__(jobStore, jobWrapper, localTempDir,
                                                      inputBlockFn)
            # cacheDir has to be 1 levels above local worker tempdir, at the same level as the
            # worker dirs. At this point, localTempDir is the worker directory, not the jobwrapper
            # directory.
            self.localTempDir = localTempDir
            self.localCacheDir = os.path.join(os.path.dirname(localTempDir),
                                              cacheDirName(self.jobStore.config.workflowID))
            self.cacheLockFile = os.path.join(self.localCacheDir, '.cacheLock')
            self.cacheStateFile = os.path.join(self.localCacheDir, '_cacheState')
            # Since each worker has it's own unique FileStore instance, and only one Job can run at
            # a time on a worker, we can bookkeep the job's file store operated files here.
            self.jobSpecificFiles = {}
            self.nlinkThreshold = None
            self.workflowAttemptNumber = self.jobStore.config.workflowAttemptNumber
            self.hashedJobCommand = sha1(self.jobWrapper.command.split()[1]).hexdigest()
            # This is a flag to better resolve cache equation imbalances at cleanup time.
            self.cleanupInProgress = False

            self._setupCache()

        @contextmanager
        def open(self,job):
            '''
            This context manager decorated method allows cache-specific operations to be conducted
            before and after the execution of a job in worker.py
            :param job:
            :return:
            '''
            # Create a working directory for the job
            startingDir = os.getcwd()
            self.localTempDir = makePublicDir(os.path.join(self.localTempDir, str(uuid.uuid4())))
            # Figure out if this operation job already went through the process of writing it's info
            # into the cache lock file.  If it has, restore the cache file to a state where the job
            # doesn't exist.
            with self._CacheState.open(self) as cacheInfo:
                if cacheInfo.jobState[self.hashedJobCommand]:
                    # Delete the old work directory if it still exists, to remove unwanted nlinks.
                    jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
                    assert jobState.jobDir != self.localTempDir
                    if os.path.exists(jobState.jobDir):
                        shutil.rmtree(jobState.jobDir)  # Ignore_errors?
                    cacheInfo.sigmaJob -= jobState.jobReqs
                    cacheInfo.jobState.pop(self.hashedJobCommand)
            # Get the requirements for the job and clean the cache if necessary. cleanCache will
            # ensure that the requirements for this job are stored in the state file.
            jobReqs = job.effectiveRequirements(self.jobStore.config).disk
            # Cleanup the cache to free up enough space for this job (if needed)
            self.cleanCache(jobReqs)
            try:
                os.chdir(self.localTempDir)
                yield
            finally:
                os.chdir(startingDir)
                self.cleanupInProgress = True
                self.returnJobReqs(jobReqs)

        # Overridden FileStore methods
        def writeGlobalFile(self, localFileName, cleanup=False):
            """
            Takes a file (as a path) and uploads it to the job store.  Depending on the jobstore
            used, carry out the appropriate cache functions.

            :param string localFileName: The path to the local file to upload.

            :param Boolean cleanup: if True then the copy of the global file will \
            be deleted once the job and all its successors have completed running. \
            If not the global file must be deleted manually.

            :returns: an ID that can be used to retrieve the file.
            """
            absLocalFileName = self._abspath(localFileName)
            # What does this do?
            cleanupID = None if not cleanup else self.jobWrapper.jobStoreID
            # If the file is from the scope of local temp dir
            if absLocalFileName.startswith(self.localTempDir):
                # If the job store is of type FileJobStore and the job store and the local temp dir
                # are on the same file system, then we want to hard link the files istead of copying
                # barring the case where the file being written was one that was previously read
                # from the file store. In that case, you want to copy to the file store so that
                # the two have distinct nlink counts.
                # Can read without a lock because we're only reading job-specific info.
                jobSpecificFiles = self._CacheState._load(self.cacheStateFile).jobState[
                    self.hashedJobCommand]['filesToFSIDs'].keys()
                # Saying nlink is 2 implicitly means we are using the job file store, and it is on
                # the same device as the work dir.
                if self.nlinkThreshold == 2 and absLocalFileName not in jobSpecificFiles:
                    jobStoreFileID = self.jobStore.getEmptyFileStoreID(cleanupID)
                    # getEmptyFileStoreID creates the file in the scope of the job store hence we
                    # need to delete it before linking.
                    os.remove(self.jobStore._getAbsPath(jobStoreFileID))
                    os.link(absLocalFileName, self.jobStore._getAbsPath(jobStoreFileID))
                # If they're not on the file system, or if the file is already linked with an
                # existing file, we need to copy to the job store.
                # Check if the user allows asynchronous file writes
                elif self.jobStore.config.useAsync:
                    jobStoreFileID = self.jobStore.getEmptyFileStoreID(cleanupID)
                    fileHandle = open(absLocalFileName, 'r')
                    with self._pendingFileWritesLock:
                        self._pendingFileWrites.add(jobStoreFileID)
                    # A file handle added to the queue allows the asyncWrite threads to remove their
                    # jobID from _pendingFileWrites. Therefore, a file should only be added after
                    # its fileID is added to _pendingFileWrites
                    self.queue.put((fileHandle, jobStoreFileID))
                # Else write directly to the job store.
                else:
                    jobStoreFileID = self.jobStore.writeFile(absLocalFileName, cleanupID)
                # Local files are cached by default, unless they were written from previously read
                # files.
                if absLocalFileName not in jobSpecificFiles:
                    self.addToCache(absLocalFileName, jobStoreFileID, 'write')
                else:
                    self._JobState.updateJobSpecificFiles(self, jobStoreFileID, absLocalFileName,
                                                          0.0, False)
            # Else write directly to the job store.
            else:
                jobStoreFileID = self.jobStore.writeFile(absLocalFileName, cleanupID)
                # Non local files are NOT cached by default, but they are tracked as local files.
                self._JobState.updateJobSpecificFiles(self, jobStoreFileID, None,
                                                      0.0, False)
            return jobStoreFileID

        def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=None):
            """
            Downloads a file described by fileStoreID from the file store to the local directory.
            The function first looks for the file in the cache and if found, it hardlinks to the
            cached copy instead of downloading.

            If a user path is specified, it is used as the destination. If a user path isn't
            specified, the file is stored in the local temp directory with an encoded name.

            The cache parameter will be used only if the file isn't already in the cache, and
            provided user path (if specified) is in the scope of local temp dir.

            :param fileStoreID: file store id for the file

            :param string userPath: a path to the name of file to which the global \
            file will be copied or hard-linked (see below).

            :param boolean cache: If True, a copy of the file will be saved into a cache that can be
            used by other workers. caching supports multiple concurrent workers requesting the same
            file by allowing only one to download the file while the others wait for it to complete.

            :param boolean mutable: If True, the file path returned points to a file that is
            modifiable by the user. Using False is recommended as it saves disk by making multiple
            workers share a file via hard links. The value defaults to False unless backwards
            compatibility was requested.

            :return: an absolute path to a local, temporary copy of the file keyed \
            by fileStoreID.
            :rtype : string
            """
            # Check that the file hasn't been deleted by the user
            if fileStoreID in self.filesToDelete:
                raise RuntimeError('Trying to access a file in the jobStore you\'ve deleted: ' + \
                                   '%s' % fileStoreID)
            # Set up the modifiable variable if it wasn't provided by the user in the function call.
            if mutable is None:
                mutable = self.mutable
            # Get the name of the file as it would be in the cache
            cachedFileName = self.encodedFileID(fileStoreID)
            # setup the harbinger variable for the file.  This is an identifier that the file is
            # currently being downloaded by another job and will be in the cache shortly. It is used
            # to prevent multiple jobs from simultaneously downloading the same file from the file
            # store.
            harbingerFileName = ''.join(['/.'.join(os.path.split(cachedFileName)), '.harbinger'])
            # setup the output filename.  If a name is provided, use it - This makes it a Named
            # Local File. If a name isn't provided, use the base64 encoded name such that we can
            # easily identify the files later on.
            if userPath is not None:
                localFilePath = self._abspath(userPath)
                if os.path.exists(localFilePath):
                    # yes, this is illegal now.
                    raise RuntimeError(' File %s ' % localFilePath + ' exists. Cannot Overwrite.' )
                fileIsLocal = True if localFilePath.startswith(self.localTempDir) else False
            else:
                localFilePath = self.getLocalTempFileName()
                fileIsLocal = True
            # First check whether the file is in cache.  If it is, then hardlink the file to
            # userPath. Cache operations can only occur on local files.
            with self.cacheLock() as lockFileHandle:
                if fileIsLocal and self._fileIsCached(fileStoreID):
                    logger.info('CACHE: Cache hit on file with ID \'%s\'.' % fileStoreID)
                    assert not os.path.exists(localFilePath)
                    if mutable:
                        shutil.copyfile(cachedFileName, localFilePath)
                        cacheInfo = self._CacheState._load(self.cacheStateFile)
                        jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
                        jobState.addToJobSpecFiles(fileStoreID, localFilePath, -1, None)
                        cacheInfo.jobState[self.hashedJobCommand] = jobState.__dict__
                        cacheInfo.write(self.cacheStateFile)
                    else:
                        os.link(cachedFileName, localFilePath)
                        self.returnFileSize(fileStoreID, localFilePath, lockFileHandle,
                                            fileAlreadyCached=True)
                # If the file is not in cache, check whether the .harbinger file for the given
                # FileStoreID exists. If it does, the wait and periodically check for the removal of
                # the file and the addition of the completed download into cache of the file by the
                # other job. Then we link to it.
                elif fileIsLocal and os.path.exists(harbingerFileName):
                    logger.info('CACHE: Waiting for another worker to download file with ID %s.'
                                % fileStoreID)
                    while os.path.exists(harbingerFileName):
                        # Release the file lock and then periodically check for completed download
                        flock(lockFileHandle, LOCK_UN)
                        time.sleep(20)  # What should this value be?
                        flock(lockFileHandle, LOCK_EX)
                    # If the code reaches here, the partial lock file has been removed. This means
                    # either the file was successfully downloaded and added to cache, or something
                    # failed. To prevent code duplication, we recursively call readGlobalFile.
                    flock(lockFileHandle, LOCK_UN)
                    return self.readGlobalFile(fileStoreID, userPath, cache)
                # If the file is not in cache, then download it to the userPath and then add to
                # cache if specified.
                else:
                    logger.debug('CACHE: Cache miss on file with ID \'%s\'.' % fileStoreID)
                    if fileIsLocal and cache:
                        # If caching of the downloaded file is desired, First create the .partial
                        # file so other jobs know not to redundantly download the same file.
                        open(harbingerFileName, 'w').close()  # This emulates the system 'touch'
                        # Now release the file lock while the file is downloaded as download could
                        # take a while.
                        flock(lockFileHandle, LOCK_UN)
                        # Use try:finally: so that the .harbinger file is removed whether the
                        # download succeeds or not.
                        try:
                            self.jobStore.readFile(fileStoreID,
                                                   '/.'.join(os.path.split(cachedFileName)))
                        except: #TODO
                            if os.path.exists('/.'.join(os.path.split(cachedFileName))):
                                os.remove('/.'.join(os.path.split(cachedFileName)))
                        else:
                            # If the download succeded, officially add the file to cache (by
                            # recording it in the cache lock file) if possible. Possibly use better
                            # function name.
                            if os.path.exists('/.'.join(os.path.split(cachedFileName))):
                                os.rename('/.'.join(os.path.split(cachedFileName)), cachedFileName)
                                self.addToCache(localFilePath, fileStoreID, 'read', mutable)
                                # We don't need to return the file size here because addToCache
                                # already does it for us
                        finally:
                            # Reacquire the file lock and delete the partial file.
                            flock(lockFileHandle, LOCK_EX)
                            os.remove(harbingerFileName)
                    else:
                        # Release the cache lock since the remaining stuff is not cache related.
                        flock(lockFileHandle, LOCK_UN)
                        self.jobStore.readFile(fileStoreID, localFilePath)
                        os.chmod(localFilePath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                        # Now that we have the file, we have 2 options. It's modifiable or not.
                        # Either way, we need to account for FileJobStore making links instead of
                        # copies.
                        if mutable:
                            if self.nlinkThreshold == 2:
                                # nlinkThreshold can only be 1 or 2 and it can only be 2 iff the
                                # job store is FilejobStore, and the job store and local temp dir
                                # are on the same device. An atomic rename removes the nlink on the
                                # file handle linked from the job store.
                                shutil.copyfile(localFilePath, localFilePath+'.tmp')
                                os.rename(localFilePath+'.tmp', localFilePath)
                            self._JobState.updateJobSpecificFiles(self, fileStoreID, localFilePath,
                                                                  -1, False)
                        # If it was immutable
                        else:
                            if self.nlinkThreshold == 2:
                                self._accountForNlinkEquals2(localFilePath)
                            self._JobState.updateJobSpecificFiles(self, fileStoreID, localFilePath,
                                                                  0.0, False)
            return localFilePath

        def readGlobalFileStream(self, fileStoreID):
            """
            Similar to readGlobalFile, but allows a stream to be read from the job \
            store.

            :returns: a context manager yielding a file handle which can be read from. \
            The yielded file handle does not need to and should not be closed explicitly.
            """
            if fileStoreID in self.filesToDelete:
                raise RuntimeError(
                        "Trying to access a file in the jobStore you've deleted: %s" % fileStoreID)

            # If fileStoreID is in the cache provide a handle from the local cache
            if self._fileIsCached(fileStoreID):
                logger.info('CACHE: Cache hit on file with ID \'%s\'.' % fileStoreID)
                return open(self.encodedFileID(fileStoreID), 'r')
            else:
                logger.info('CACHE: Cache miss on file with ID \'%s\'.' % fileStoreID)
                return self.jobStore.readFileStream(fileStoreID)

        def deleteLocalFile(self, fileStoreID):
            '''
            Deletes a local file with the given job store ID.
            :param str fileStoreID: File Store ID of the file to be deleted.
            :return: None
            '''
            # The local file may or may not have been cached. If it was, we need to do some
            # bookkeeping. If it wasn't, we just delete the file and continue with no might need
            # some bookkeeping if the file store and cache live on the same filesystem. We can know
            # if a file was cached or not based on the value held in the third tuple value for the
            # dict item having key = fileStoreID. If it was cached, it holds the value True else
            # False.
            with self._CacheState.open(self) as cacheInfo:
                jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
                assert fileStoreID in jobState.jobSpecificFiles.keys(), 'Attempting to delete ' + \
                    'a non-local file'
                # filesToDelete is a dictionary of file: fileSize
                filesToDelete = jobState.jobSpecificFiles[fileStoreID]
                allOwnedFiles = jobState.filesToFSIDs
                for (fileToDelete, fileSize) in filesToDelete.items():
                    # Handle the case where a file not in the local temp dir was written to
                    # filestore
                    if fileToDelete is None:
                        filesToDelete.pop(fileToDelete)
                        allOwnedFiles[fileToDelete].remove(fileStoreID)
                        cacheInfo.jobState[self.hashedJobCommand] = jobState.__dict__
                        cacheInfo.write(self.cacheStateFile)
                        continue
                    # If the file size is zero (copied into the local temp dir) or -1 (mutable), we
                    # can safely delete without any bookkeeping
                    if fileSize in (0, -1):
                        # Only remove the file if there is only one FSID associated with it.
                        if len(allOwnedFiles[fileToDelete]) == 1:
                            try:
                                os.remove(fileToDelete)
                            except OSError as err:
                                if err.errno == errno.ENOENT and fileSize == -1:
                                    logger.debug(fileToDelete,  'was read mutably and deleted by '
                                                 'the user')
                                else:
                                    raise CacheError('Illegal operation detected. Cache tracked'
                                                     'file deleted explicitly by user. Use'
                                                     'deleteLocalFile to delete such files.')
                        allOwnedFiles[fileToDelete].remove(fileStoreID)
                        filesToDelete.pop(fileToDelete)
                        cacheInfo.jobState[self.hashedJobCommand] = jobState.__dict__
                        cacheInfo.write(self.cacheStateFile)
                        continue
                    # If not, we need to do bookkeeping
                    # Get the size of the file to be deleted, and the number of jobs using the file
                    # at the moment.
                    if not os.path.exists(fileToDelete):
                        raise CacheError('Illegal operation detected. Cache tracked file deleted '
                                         'explicitly by user. Use deleteLocalFile to delete such '
                                         'files.')
                    fileStats = os.stat(fileToDelete)
                    if fileSize != fileStats.st_size:
                        logger.warn("the size on record differed from the real size by " +
                                    "%s bytes" % str(fileSize-fileStats.st_size))
                    # Remove the file and return file size to the job
                    if len(allOwnedFiles[fileToDelete]) == 1:
                        os.remove(fileToDelete)
                    cacheInfo.sigmaJob += fileSize
                    filesToDelete.pop(fileToDelete)
                    allOwnedFiles[fileToDelete].remove(fileStoreID)
                    jobState.updateJobReqs(fileSize, 'remove')
                    cacheInfo.jobState[self.hashedJobCommand] = jobState.__dict__
                # If the job is not in the process of cleaning up, then we may need to remove the
                # cached copy of the file as well.
                if not self.cleanupInProgress:
                    # If the file is cached and if other jobs are using the cached copy of the file,
                    # or if retaining the file in the cache doesn't affect the cache equation, then
                    # don't remove it from cache.
                    if self._fileIsCached(fileStoreID):
                        cachedFile = self.encodedFileID(fileStoreID)
                        jobsUsingFile = os.stat(cachedFile).st_nlink
                        if not cacheInfo.isBalanced() and jobsUsingFile == self.nlinkThreshold:
                            os.remove(cachedFile)
                            cacheInfo.cached -= fileSize
                    self.logToMaster('Successfully deleted cached copy of file with ID '
                                     '\'%s\'.' % fileStoreID)
                self.logToMaster('Successfully deleted local copies of file with ID '
                                 '\'%s\'.' % fileStoreID)

        def deleteGlobalFile(self, fileStoreID):
            """
            Deletes a global file with the given job store ID.

            To ensure that the job can be restarted if necessary, the delete will not happen until
            after the job's run method has completed.

            :param fileStoreID: the job store ID of the file to be deleted.
            """
            with self._CacheState.open(self) as cacheInfo:
                jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
            if fileStoreID in jobState.jobSpecificFiles.keys():
                # Use deleteLocalFile in the backend to delete the local copy of the file.
                self.deleteLocalFile(fileStoreID)
                # At this point, the local file has been deleted, and possibly the cached copy. If
                # the cached copy exists, it is either because another job is using the file, or
                # because retaining the file in cache doesn't unbalance the caching equation. The
                # first case is unacceptable for deleteGlobalFile and the second requires explicit
                # deletion of the cached copy.
            # Check if the fileStoreID is in the cache. If it is, ensure only the current job is
            # using it.
            cachedFile = self.encodedFileID(fileStoreID)
            if os.path.exists(cachedFile):
                self.removeSingleCachedFile(fileStoreID)
            # Add the file to the list of files to be deleted once the run method completes.
            self.filesToDelete.add(fileStoreID)
            self.logToMaster('Added file with ID \'%s\' to the list of files to be' % fileStoreID +
                             ' globally deleted.')

        # Cache related methods
        @contextmanager
        def cacheLock(self):
            '''
            This is a context manager to acquire a lock on the Lock file that will be used to
            prevent synchronous cache operations between workers.
            :yield: File descriptor for cache lock file in r+ mode
            '''
            cacheLockFile = open(self.cacheLockFile, 'w')
            try:
                flock(cacheLockFile, LOCK_EX)
                logger.debug("CACHE: Obtained lock on file %s" % self.cacheLockFile)
                yield cacheLockFile
            except IOError:
                logger.critical('CACHE: Unable to acquire lock on %s' % self.cacheLockFile)
                raise
            finally:
                cacheLockFile.close()
                logger.debug("CACHE: Released lock")

        def _setupCache(self):
            '''
            Setup the cache based on the provided values for localCacheDir.
            :return: None
            '''
            # we first check whether the cache directory exists. If it doesn't, create it.
            if not os.path.exists(self.localCacheDir):
                # Create a temporary directory as this worker's private cache. If all goes well, it
                # will be renamed into the cache for this node.
                personalCacheDir = ''.join([os.path.dirname(self.localCacheDir), '/.ctmp-',
                                            str(uuid.uuid4())])
                os.mkdir(personalCacheDir, 0755)
                self._createCacheLockFile(personalCacheDir)
                try:
                    os.rename(personalCacheDir, self.localCacheDir)
                except OSError as err:
                    # The only acceptable FAIL case is that the destination is a non-empty directory
                    # directory.  Assuming (it's ambiguous) atomic renaming of directories, if the
                    # dst is non-empty, it only means that another worker has beaten this one to the
                    # rename.
                    if err.errno == errno.ENOTEMPTY:
                        # Cleanup your own mess.  It's only polite.
                        shutil.rmtree(personalCacheDir)
                    else:
                        raise
            # You can't reach here unless a local cache directory has been created successfully
            with self._CacheState.open(self) as cacheInfo:
                # Ensure this cache is from the correct attempt at the workflow!  If it isn't, we
                # need to reset the cache lock file
                if cacheInfo.attemptNumber != self.workflowAttemptNumber:
                    if cacheInfo.nlink == 2:
                        cacheInfo.cached = 0 # cached file sizes are accounted for by job store
                    else:
                        allCachedFiles = [os.path.join(self.localCacheDir, x)
                                          for x in os.listdir(self.localCacheDir)
                                          if not self._isHidden(x)]
                        cacheInfo.cached = sum([os.stat(cachedFile).st_size
                                                for cachedFile in allCachedFiles])
                        # TODO: Delete the working directories
                    cacheInfo.sigmaJob = 0
                    cacheInfo.attemptNumber = self.workflowAttemptNumber
                self.nlinkThreshold = cacheInfo.nlink

        def _createCacheLockFile(self, tempCacheDir):
            '''
            Create the cache lock file file to contain the state of the cache on the node.
            :param file setupLockFileHandle: Open file handle for the setup lock file. Released on
            successful setup.
            :return: None
            '''
            # The nlink threshold is setup along with the first instance of the cache class on the
            # node.
            self.setNlinkThreshold()
            # Get the free space on the device
            diskStats = os.statvfs(tempCacheDir)
            freeSpace = diskStats.f_frsize * diskStats.f_bavail
            # Create the cache lock file.
            open(os.path.join(tempCacheDir, os.path.basename(self.cacheLockFile)), 'w').close()
            # Setup the cache state file
            personalCacheStateFile = os.path.join(tempCacheDir,
                                                  os.path.basename(self.cacheStateFile))
            # Setup the initial values for the cache state file in a dict
            cacheInfo = self._CacheState({
                'nlink': self.nlinkThreshold,
                'attemptNumber': self.workflowAttemptNumber,
                'total': freeSpace,
                'cached': 0,
                'sigmaJob': 0,
                'cacheDir': self.localCacheDir,
                'jobState': collections.defaultdict(int)})
            cacheInfo.write(personalCacheStateFile)

        def encodedFileID(self, JobStoreFileID):
            '''
            Uses a url safe base64 encoding to encode the jobStoreFileID into a unique identifier to
            use as filename within the cache folder.  jobstore IDs are essentially urls/paths to
            files and thus cannot be used as is. Base64 encoding is used since it is reversible.

            :param jobStoreFileID: string representing a job store file ID
            :return: str outCachedFile: A path to the hashed file in localCacheDir
            '''
            outCachedFile = os.path.join(self.localCacheDir,
                                         base64.urlsafe_b64encode(JobStoreFileID))
            return outCachedFile

        def _fileIsCached(self, jobStoreFileID):
            '''
            Is the file identified by jobStoreFileID in cache or not.
            '''
            return os.path.exists(self.encodedFileID(jobStoreFileID))

        def decodedFileID(self, cachedFilePath):
            '''
            Decode a cached fileName back to a job store file ID.

            :param str cachedFilePath: Path to the cached file
            :return: The jobstore file ID associated with the file
            :rtype: str
            '''
            fileDir, fileName = os.path.split(cachedFilePath)
            assert fileDir == self.localCacheDir, 'Can\'t decode uncached file names'
            return base64.urlsafe_b64decode(fileName)

        def addToCache(self, localFilePath, jobStoreFileID, callingFunc, mutable=None):
            '''
            Used to process the caching of a file. This depends on whether a file is being written
            to file store, or read from it.
            WRITING
            The file is in localTempDir. It needs to be linked into cache if possible.
            READING
            The file is already in the cache dir. Depending on whether it is modifiable or not, does
            it need to be linked to the required location, or copied. If it is copied, can the file
            still be retained in cache?

            :param str localFilePath: Path to the Source file
            :param jobStoreFileID: jobStoreID for the file
            :param str callingFunc: Who called this function, 'write' or 'read'
            :param boolean mutable: See modifiable in readGlobalFile
            :return: None
            '''
            assert callingFunc in ('read', 'write')
            # Set up the modifiable variable if it wasn't provided by the user in the function call.
            if mutable is None:
                mutable = self.mutable
            assert isinstance(mutable, bool)
            with self.cacheLock() as lockFileHandle:
                cachedFile = self.encodedFileID(jobStoreFileID)
                # The file to be cached MUST originate in the environment of the TOIL temp directory
                if (os.stat(self.localCacheDir).st_dev !=
                        os.stat(os.path.dirname(localFilePath)).st_dev):
                    raise CacheInvalidSrcError('Attempting to cache a file across file systems '
                                               'cachedir = %s, file = %s.' % (self.localCacheDir,
                                                                              localFilePath))
                if not localFilePath.startswith(self.localTempDir):
                    raise CacheInvalidSrcError('Attempting a cache operation on a non-local file '
                                               '%s.' % localFilePath)
                if callingFunc == 'read' and mutable:
                    shutil.copyfile(cachedFile, localFilePath)
                    fileSize = os.stat(cachedFile).st_size
                    cacheInfo = self._CacheState._load(self.cacheStateFile)
                    cacheInfo.cached += fileSize if cacheInfo.nlink != 2 else 0
                    if not cacheInfo.isBalanced():
                        os.remove(cachedFile)
                        cacheInfo.cached -= fileSize if cacheInfo.nlink != 2 else 0
                        logger.debug('Could not download both download ' +
                                     '%s as mutable and add to ' % os.path.basename(localFilePath) +
                                     'cache. Hence only mutable copy retained.')
                    else:
                        logger.info('CACHE: Added file with ID \'%s\' to the cache.' %
                                     jobStoreFileID)
                    jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
                    jobState.addToJobSpecFiles(jobStoreFileID, localFilePath, -1, False)
                    cacheInfo.jobState[self.hashedJobCommand] = jobState.__dict__
                    cacheInfo.write(self.cacheStateFile)
                else:
                    # There are two possibilities, read and immutable, and write. both cases do
                    # almost the same thing except for the direction of the os.link hence we're
                    # writing them together.
                    if callingFunc == 'read': # and mutable is inherently False
                        src = cachedFile
                        dest = localFilePath
                        # To mirror behaviour of shutil.copyfile
                        if os.path.exists(dest):
                            os.remove(dest)
                    else: # write
                        src = localFilePath
                        dest = cachedFile
                    try:
                        os.link(src, dest)
                    except OSError as err:
                        if err.errno != errno.EEXIST:
                            raise
                        # If we get the EEXIST error, it can only be from write since in read we are
                        # explicitly deleting the file.  This shouldn't happen with the .partial
                        # logic hence we raise a cache error.
                        raise CacheError('Attempting to recache a file %s.' % src)
                    else:
                        # Chmod the cached file. Cached files can never be modified.
                        os.chmod(cachedFile, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                        # Return the filesize of cachedFile to the job and increase the cached size
                        # The values passed here don't matter since rFS looks at the file only for
                        # the stat
                        self.returnFileSize(jobStoreFileID, localFilePath, lockFileHandle,
                                            fileAlreadyCached=False)
                    if callingFunc == 'read':
                        logger.info('CACHE: Read file with ID \'%s\' from the cache.' %
                                    jobStoreFileID)
                    else:
                        logger.info('CACHE: Added file with ID \'%s\' to the cache.' %
                                    jobStoreFileID)

        def returnFileSize(self, fileStoreID, cachedFileSource, lockFileHandle,
                           fileAlreadyCached=False):
            '''
            Returns the fileSize of the file described by fileStoreID to the job requirements pool
            if the file was recently added to, or read from cache (A job that reads n bytes from
            cache doesn't really use those n bytes as a part of it's job disk since cache is already
            accounting for that disk space).

            :param fileStoreID: fileStore ID of the file bein added to cache
            :param str cachedFileSource: File being added to cache
            :param file lockFileHandle: Open file handle to the cache lock file
            :param bool fileAlreadyCached: A flag to indicate whether the file was already cached or
            not. If it was, then it means that you don't need to add the filesize to cache again.
            :return: None
            '''
            fileSize = os.stat(cachedFileSource).st_size
            cacheInfo = self._CacheState._load(self.cacheStateFile)
            # If the file isn't cached, add the size of the file to the cache pool. However, if the
            # nlink threshold is not 1 -  i.e. it is 2 (it can only be 1 or 2), then don't do this
            # since the size of the file is accounted for by the file store copy.
            if not fileAlreadyCached and self.nlinkThreshold == 1:
                cacheInfo.cached += fileSize
            cacheInfo.sigmaJob -= fileSize
            if not cacheInfo.isBalanced():
                self.logToMaster('CACHE: The cache was not balanced on returning file size',
                                 logging.WARN)
            # Add the info to the job specific cache info
            jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
            jobState.addToJobSpecFiles(fileStoreID, cachedFileSource, fileSize, True)
            cacheInfo.jobState[self.hashedJobCommand] = jobState.__dict__
            cacheInfo.write(self.cacheStateFile)

        @staticmethod
        def _isHidden(filePath):
            '''
            This is a function that checks whether filePath is hidden
            :param str filePath: Path to the file under consideration
            :return:
            '''
            #>>> timeit.timeit('x = a[0] in {".", "_"}', setup='a=".abc"', number=1000000)
            #0.14043903350830078
            #>>> timeit.timeit('x = (a.startswith(".") or a.startswith("_"))', ...)
            #0.216400146484375
            #>>> timeit.timeit('x = a.startswith((".", "_"))', ...)
            #0.21130609512329102
            assert isinstance(filePath, str)
            # I can safely assume i will never see an empty string because this is always called on
            # the results of an os.listdir()
            return filePath[0] in {'.', '_'}

        def cleanCache(self, newJobReqs):
            """
            Cleanup all files in the cache directory to ensure that at lead newJobReqs are available
            for use.
            :param float newJobReqs: the total number of bytes of files allowed in the cache.
            """
            with self._CacheState.open(self) as cacheInfo:
                # Add the new job's disk requirements to the sigmaJobDisk variable
                cacheInfo.sigmaJob += newJobReqs
                # Initialize the job state here. we use a partial in the jobSpecificFiles call so
                # that this entire thing is pickleable. Based on answer by user Nathaniel Gentile at
                # http://stackoverflow.com/questions/2600790
                assert not cacheInfo.jobState[self.hashedJobCommand]
                cacheInfo.jobState[self.hashedJobCommand] = {
                    'jobReqs': newJobReqs,
                    'jobDir': self.localTempDir,
                    'jobSpecificFiles': collections.defaultdict(partial(collections.defaultdict,
                                                                        int)),
                    'filesToFSIDs': collections.defaultdict(set)}
                # If the caching equation is balanced, do nothing.
                if cacheInfo.isBalanced():
                    return None

                # List of deletable cached files.  A deletable cache file is one
                #  that is not in use by any other worker (identified by the number of symlinks to
                # the file)
                allCacheFiles = [os.path.join(self.localCacheDir, x)
                                 for x in os.listdir(self.localCacheDir)
                                 if not self._isHidden(x)]
                allCacheFiles = [(path, os.stat(path)) for path in allCacheFiles]
                #TODO mtime vs ctime
                deletableCacheFiles = {(path, inode.st_mtime, inode.st_size)
                                       for path, inode in allCacheFiles
                                       if inode.st_nlink == self.nlinkThreshold}

                # Sort in descending order of mtime so the first items to be popped from the list
                # are the least recently created.
                deletableCacheFiles = sorted(deletableCacheFiles, key=lambda x: (-x[1], -x[2]))
                logger.debug('CACHE: Need %s bytes for new job. Have %s' %
                             (newJobReqs, cacheInfo.cached + cacheInfo.sigmaJob - newJobReqs))
                logger.debug('CACHE: Evicting files to make room for the new job.')

                # Now do the actual file removal
                while not cacheInfo.isBalanced() and len(deletableCacheFiles) > 0:
                    cachedFile, fileCreateTime, cachedFileSize = deletableCacheFiles.pop()
                    os.remove(cachedFile)
                    cacheInfo.cached -= cachedFileSize if self.nlinkThreshold != 2 else 0
                    assert cacheInfo.cached >= 0
                    # self.logToMaster('CACHE: Evicted  file with ID \'%s\' (%s bytes)' %
                    #                  (self.decodedFileID(cachedFile), cachedFileSize))
                    logger.debug('CACHE: Evicted  file with ID \'%s\' (%s bytes)' %
                                 (self.decodedFileID(cachedFile), cachedFileSize))
                assert cacheInfo.isBalanced(), 'Unable to free up enough space for caching.'
                logger.debug('CACHE: After Evictions, ended up with %s.' %
                             (cacheInfo.cached + cacheInfo.sigmaJob))
                logger.debug('CACHE: Unable to free up enough space for caching.')

        def removeSingleCachedFile(self, fileStoreID):
            '''
            Removes a single file described by the fileStoreID from the cache forcibly.
            :return:
            '''
            with self._CacheState.open(self) as cacheInfo:
                cachedFile = self.encodedFileID(fileStoreID)
                cachedFileStats = os.stat(cachedFile)
                # We know the file exists because this function was called in the if block.  So we
                # have to ensure nothing has changed since then.
                assert cachedFileStats.st_nlink == self.nlinkThreshold, 'Attempting to delete ' + \
                    'a global file that is in use by another job.'
                # Remove the file size from the cached file size if the jobstore is not fileJobStore
                # and then delete the file
                os.remove(cachedFile)
                if self.nlinkThreshold != 2:
                    cacheInfo.cached -= cachedFileStats.st_size
                if not cacheInfo.isBalanced():
                    self.logToMaster('CACHE: The cache was not balanced on removing single file',
                                     logging.WARN)
                self.logToMaster('CACHE: Successfully removed file with ID \'%s\'.' % fileStoreID)
            return None

        def setNlinkThreshold(self):
            #FIXME Can't do this at the top because of loopy (circular) import errors
            from toil.jobStores.fileJobStore import FileJobStore
            if (isinstance(self.jobStore, FileJobStore) and
                        os.stat(os.path.dirname(self.localCacheDir)).st_dev == os.stat(
                            self.jobStore.jobStoreDir).st_dev):
                self.nlinkThreshold = 2
            else:
                self.nlinkThreshold = 1

        def returnJobReqs(self, jobReqs):
            '''
            This function returns the effective job requirements back to the pool after the job
            completes. It also deletes the local copies of files with the cache lock held.

            :param float jobReqs: Original size requirement of the job
            :return: None
            '''
            # Since we are only reading this job's specific values from the state file, we don't
            # need a lock
            jobState = self._JobState(self._CacheState._load(self.cacheStateFile
                                                             ).jobState[self.hashedJobCommand])
            for x in jobState.jobSpecificFiles.keys():
                self.deleteLocalFile(x)
            with self._CacheState.open(self) as cacheInfo:
                cacheInfo.sigmaJob -= jobReqs
                cacheInfo.jobState.pop(self.hashedJobCommand)
                #assert cacheInfo.isBalanced() # commenting this out for now. God speed

        def _abspath(self, key):
            """
            Return the absolute path to key.  This is a wrapepr for os.path.abspath because mac OS
            symlinks /tmp and /var (the most common places for a default tempdir) to
            private/<tmp or var>.
            :param str key: The absolute or relative path to the file. If relative, it must be
            relative to the local temp working dir
            :return: Absolute path to key
            """
            if key.startswith('/'):
                return os.path.abspath(key)
            else:
                return os.path.join(self.localTempDir, key)

        class _CacheState(object):
            '''
            Utility class to read and write the cache lock file. Also for checking whether the
            caching equation is balanced or not.
            '''
            def __init__(self, stateDict):
                # Should i assert the dictionary contains the required elements? This class is
                # instantiated often {nlink, attemptNumber, total, cached, sigmaJob}
                assert isinstance(stateDict, dict)
                self.__dict__.update(stateDict)

            @classmethod
            @contextmanager
            def open(cls, outer):
                '''
                This is a context manager that basically opens the cache state file and reads it
                into an object that is returned to the user in the yield
                :param outer: instance of the CachedFileStore class (to use the cachelock method)
                '''
                with outer.cacheLock():
                    cacheInfo = cls._load(outer.cacheStateFile)
                    yield cacheInfo
                    cacheInfo.write(outer.cacheStateFile)

            @classmethod
            def _load(cls, fileName):
                '''
                Load the state of the cache from the cache state file (via the file handle provided)
                :param str fileName: Path to the cache state file
                '''
                # Read the value from the cache state file then initialize and instance of
                # _CacheState with it.
                with open(fileName, 'r') as fH:
                    cacheInfoDict = cPickle.load(fH)
                return cls(cacheInfoDict)

            def write(self, fileName):
                '''
                Write the current state of the cache into a temporary file then atomically rename it
                to the main cache state file.
                :param str fileName: Path to the cache state file
                :return:
                '''
                with open(fileName + '.tmp', 'w') as fH:
                    # Based on answer by user "Mark" at:
                    # http://stackoverflow.com/questions/2709800/how-to-pickle-yourself
                    # We can't pickle nested classes. So we have to pickle the variables of the class
                    cPickle.dump(self.__dict__, fH, cPickle.HIGHEST_PROTOCOL)
                os.rename(fileName + '.tmp', fileName)

            def isBalanced(self):
                '''
                Checks for the inequality of the caching equation, i.e.
                                cachedSpace + sigmaJobDisk <= totalFreeSpace
                Essentially, the sum of all cached file + disk requirements of all running jobs
                should always be less than the available space on the system
                :return: Boolean for equation is balanced (T) or not (F)
                '''
                return self.cached + self.sigmaJob <= self.total

            def purgeRequired(self, jobReqs):
                '''
                Similar to isBalanced, however it looks at the actual state of the system and
                decides whether an eviction is required.
                :return bool: Is a purge required(T) or no(F)
                '''
                return not self.isBalanced()
                #totalStats = os.statvfs(self.cacheDir)
                #totalFree = totalStats.f_bavail * totalStats.f_frsize
                #return totalFree < jobReqs

        def _accountForNlinkEquals2(self, localFilePath):
            '''
            This is a utility function that accounts for the fact that if nlinkThreshold == 2, the
            size of the file is accounted for by the file store copy of the file and thus the file
            size shouldn't be added to the cached file sizes.
            :param str localFilePath: Path to the local file that was linked to the file store copy.
            :return: None
            '''
            fileStats = os.stat(localFilePath)
            assert fileStats.st_nlink >= self.nlinkThreshold
            with self._CacheState.open(self) as cacheInfo:
                cacheInfo.sigmaJob -= fileStats.st_size
                jobState = self._JobState(cacheInfo.jobState[self.hashedJobCommand])
                jobState.updateJobReqs(fileStats.st_size, 'remove')

        class _JobState(object):
            '''
            This is a utility class to handle the state of a job in terms of it's current disk
            requirements, working directory, and job specific files.
            '''
            def __init__(self, dictObj):
                assert isinstance(dictObj, dict)
                self.__dict__.update(dictObj)

            @classmethod
            def updateJobSpecificFiles(cls, outer, jobStoreFileID, filePath, fileSize, cached):
                '''
                This method will update the job specifc files in the job state object. It deals with
                opening a cache lock file, etc.
                :param outer: An instance of job.CachedFileStore
                :param jobStoreFileID: job store Identifier for the file
                :param filePath: The path to the file
                :param fileSize: The size of the file (may be deprecated soon)
                :param cached: T : F : None :: cached : not cached : mutably read
                :return: None
                '''
                with outer._CacheState.open(outer) as cacheInfo:
                    jobState = cls(cacheInfo.jobState[outer.hashedJobCommand])
                    jobState.addToJobSpecFiles(jobStoreFileID, filePath, fileSize, cached)
                    cacheInfo.jobState[outer.hashedJobCommand] = jobState.__dict__

            def addToJobSpecFiles(self, jobStoreFileID, filePath, fileSize, cached):
                '''
                This is the real method that actually does the updations.
                :param jobStoreFileID: job store Identifier for the file
                :param filePath: The path to the file
                :param fileSize: The size of the file (may be deprecated soon)
                :param cached: T : F : None :: cached : not cached : mutably read
                :return: None
                '''
                # If there is no entry for the jsfID, make one. self.jobSpecificFiles is a default
                # dict of default dicts and the absence of a key will return an empty dict
                # (equivalent to a None for the if)
                if not self.jobSpecificFiles[jobStoreFileID]:
                    self.jobSpecificFiles[jobStoreFileID][filePath] = fileSize
                else:
                    # If there's no entry for the filepath, create one
                    if not self.jobSpecificFiles[jobStoreFileID][filePath]:
                        self.jobSpecificFiles[jobStoreFileID][filePath] = fileSize
                    # This should never happen
                    else:
                        raise RuntimeError()
                # Now add the file to the reverse mapper. This will speed up cleanup and local file
                # deletion.
                self.filesToFSIDs[filePath].add(jobStoreFileID)
                if cached:
                    self.updateJobReqs(fileSize, 'add')

            def updateJobReqs(self, fileSize, actions):
                '''
                This method will update the current state of the disk required by the job after the
                most recent cache operation.
                :param fileSize: Size of the last file added/removed from the cache
                :param actions: 'add' or 'remove'
                :return:
                '''
                assert actions in ('add', 'remove')
                multiplier = 1 if actions == 'add' else -1
                # If the file was added to the cache, the value is subtracted from the requirements,
                # and it is added if the file was removed form the cache.
                self.jobReqs -= (fileSize * multiplier)

    class Service:
        """
        Abstract class used to define the interface to a service.
        """
        __metaclass__ = ABCMeta
        def __init__(self, memory=None, cores=None, disk=None, preemptable=None):
            """
            Memory, core and disk requirements are specified identically to as in \
            :func:`toil.job.Job.__init__`.
            """
            self.memory = memory
            self.cores = cores
            self.disk = disk
            self._childServices = []
            self._hasParent = False
            self.preemptable = preemptable

        @abstractmethod
        def start(self, fileStore):
            """
            Start the service.
            
            :param toil.job.Job.FileStore fileStore: A fileStore object to create temporary files with.

            :returns: An object describing how to access the service. The object must be pickleable \
            and will be used by jobs to access the service (see :func:`toil.job.Job.addService`).
            """
            pass

        @abstractmethod
        def stop(self, fileStore):
            """
            Stops the service. 
            
            :param toil.job.Job.FileStore fileStore: A fileStore object to create temporary files with.
            Function can block until complete.
            """
            pass

        def check(self):
            """
            Checks the service is still running.

            :raise RuntimeError: If the service failed, this will cause the service job to be labeled failed.
            :returns: True if the service is still running, else False. If False then the service job will be terminated,
            and considered a success.
            """
            pass

        def _addChild(self, service):
            """
            Add a child service to start up after this service has started. This should not be
            called by the user, instead use :func:`toil.job.Job.Service.addService` with the
            ``parentService`` option.

            :raises toil.job.JobException: If service has already been made the child of a job or another service.
            :param toil.job.Job.Service service: Service to add as a "child" of this service
            :return: a promise that will be replaced with the return value from \
            :func:`toil.job.Job.Service.start` of service after the service has started.
            :rtype: toil.job.Promise
            """
            if service._hasParent:
                raise JobException("The service already has a parent service")
            service._parent = True
            jobService = ServiceJob(service)
            self._childServices.append(jobService)
            return jobService.rv()

    ####################################################
    #Private functions
    ####################################################

    def _addPredecessor(self, predecessorJob):
        """
        Adds a predecessor job to the set of predecessor jobs. Raises a \
        RuntimeError if the job is already a predecessor.
        """
        if predecessorJob in self._directPredecessors:
            raise RuntimeError("The given job is already a predecessor of this job")
        self._directPredecessors.add(predecessorJob)

    @classmethod
    def _loadUserModule(cls, userModule):
        """
        Imports and returns the module object represented by the given module descriptor.

        :type userModule: ModuleDescriptor
        """
        if not userModule.belongsToToil:
            userModule = userModule.localize()
        if userModule.dirPath not in sys.path:
            sys.path.append(userModule.dirPath)
        try:
            return importlib.import_module(userModule.name)
        except ImportError:
            logger.error('Failed to import user module %r from sys.path=%r', userModule, sys.path)
            raise

    @classmethod
    def _loadJob(cls, command, jobStore):
        """
        Unpickles a :class:`toil.job.Job` instance by decoding command.

        The command is a reference to a jobStoreFileID containing the \
        pickle file for the job and a list of modules which must be imported so that \
        the Job can be successfully unpickled. \
        See :func:`toil.job.Job._serialiseFirstJob` and \
        :func:`toil.job.Job._makeJobWrappers` to see precisely how the Job is encoded \
        in the command.

        :param string command: encoding of the job in the job store.
        :param toil.jobStores.abstractJobStore.AbstractJobStore jobStore: The job store.
        :returns: The job referenced by the command.
        :rtype: toil.job.Job
        """
        commandTokens = command.split()
        assert "_toil" == commandTokens[0]
        userModule = ModuleDescriptor(*(commandTokens[2:]))
        userModule = cls._loadUserModule(userModule)
        pickleFile = commandTokens[1]
        if pickleFile == "firstJob":
            openFileStream = jobStore.readSharedFileStream(pickleFile)
        else:
            openFileStream = jobStore.readFileStream(pickleFile)
        with openFileStream as fileHandle:
            return cls._unpickle(userModule, fileHandle)

    @classmethod
    def _unpickle(cls, userModule, fileHandle):
        """
        Unpickles an object graph from the given file handle while loading symbols \
        referencing the __main__ module from the given userModule instead.

        :param userModule:
        :param fileHandle:
        :returns:
        """
        unpickler = cPickle.Unpickler(fileHandle)

        def filter_main(module_name, class_name):
            if module_name == '__main__':
                return getattr(userModule, class_name)
            else:
                return getattr(importlib.import_module(module_name), class_name)

        unpickler.find_global = filter_main
        return unpickler.load()

    def getUserScript(self):
        return self.userModule

    ####################################################
    #Functions to pass Job.run return values to the
    #input arguments of other Job instances
    ####################################################

    def _setReturnValuesForPromises(self, returnValues, jobStore):
        """
        Sets the values for promises using the return values from the job's run function.
        """
        for index, promiseFileStoreIDs in self._rvs.iteritems():
            promisedValue = returnValues if index is None else returnValues[index]
            for promiseFileStoreID in promiseFileStoreIDs:
                # File may be gone if the job is a service being re-run and the accessing job is
                # already complete
                if jobStore.fileExists(promiseFileStoreID):
                    with jobStore.updateFileStream(promiseFileStoreID) as fileHandle:
                        cPickle.dump(promisedValue, fileHandle, cPickle.HIGHEST_PROTOCOL)

    ####################################################
    #Functions associated with Job.checkJobGraphAcyclic to establish
    #that the job graph does not contain any cycles of dependencies.
    ####################################################

    def _dfs(self, visited):
        """Adds the job and all jobs reachable on a directed path from current \
        node to the set 'visited'.
        """
        if self not in visited:
            visited.add(self)
            for successor in self._children + self._followOns:
                successor._dfs(visited)

    def _checkJobGraphAcylicDFS(self, stack, visited, extraEdges):
        """
        DFS traversal to detect cycles in augmented job graph.
        """
        if self not in visited:
            visited.add(self)
            stack.append(self)
            for successor in self._children + self._followOns + extraEdges[self]:
                successor._checkJobGraphAcylicDFS(stack, visited, extraEdges)
            assert stack.pop() == self
        if self in stack:
            stack.append(self)
            raise JobGraphDeadlockException("A cycle of job dependencies has been detected '%s'" % stack)

    @staticmethod
    def _getImpliedEdges(roots):
        """
        Gets the set of implied edges. See Job.checkJobGraphAcylic
        """
        #Get nodes in job graph
        nodes = set()
        for root in roots:
            root._dfs(nodes)

        ##For each follow-on edge calculate the extra implied edges
        #Adjacency list of implied edges, i.e. map of jobs to lists of jobs
        #connected by an implied edge
        extraEdges = dict(map(lambda n : (n, []), nodes))
        for job in nodes:
            if len(job._followOns) > 0:
                #Get set of jobs connected by a directed path to job, starting
                #with a child edge
                reacheable = set()
                for child in job._children:
                    child._dfs(reacheable)
                #Now add extra edges
                for descendant in reacheable:
                    extraEdges[descendant] += job._followOns[:]
        return extraEdges

    ####################################################
    #The following functions are used to serialise
    #a job graph to the jobStore
    ####################################################

    def _createEmptyJobWrapperForJob(self, jobStore, command=None, predecessorNumber=0):
        """
        Create an empty job for the job.
        """
        requirements = self.effectiveRequirements(jobStore.config)
        if jobStore.config.disableSharedCache:
            del requirements.cache
        return jobStore.create(command=command, predecessorNumber=predecessorNumber, **requirements)

    def effectiveRequirements(self, config):
        """
        Determine and validate the effective requirements for this job, substituting a missing
        explict requirement with a default from the configuration.

        :rtype: Expando
        :return: a dictionary/object hybrid with one entry/attribute for each requirement
        """
        requirements = Expando(
            memory=float(config.defaultMemory) if self.memory is None else self.memory,
            cores=float(config.defaultCores) if self.cores is None else self.cores,
            disk=float(config.defaultDisk) if self.disk is None else self.disk,
            preemptable=float(config.defaultPreemptable) if self.preemptable is None else self.preemptable)
        if config.disableSharedCache:
            if self.cache is None:
                requirements.cache = min(requirements.disk, float(config.defaultCache))
            else:
                requirements.cache = self.cache
            if requirements.cache > requirements.disk:
                raise RuntimeError("Trying to allocate a cache ({cache}) larger than the disk "
                                   "requirement for the job ({disk})".format(**requirements))
        return requirements

    def _makeJobWrappers(self, jobWrapper, jobStore):
        """
        Creates a jobWrapper for each job in the job graph, recursively.
        """
        jobsToJobWrappers = { self:jobWrapper }
        for successors in (self._followOns, self._children):
            jobs = map(lambda successor:
                successor._makeJobWrappers2(jobStore, jobsToJobWrappers), successors)
            jobWrapper.stack.append(jobs)
        return jobsToJobWrappers

    def _makeJobWrappers2(self, jobStore, jobsToJobWrappers):
        #Make the jobWrapper for the job, if necessary
        if self not in jobsToJobWrappers:
            jobWrapper = self._createEmptyJobWrapperForJob(jobStore, predecessorNumber=len(self._directPredecessors))
            jobsToJobWrappers[self] = jobWrapper
            #Add followOns/children to be run after the current job.
            for successors in (self._followOns, self._children):
                jobs = map(lambda successor:
                    successor._makeJobWrappers2(jobStore, jobsToJobWrappers), successors)
                jobWrapper.stack.append(jobs)
        else:
            jobWrapper = jobsToJobWrappers[self]
        #The return is a tuple stored within a job.stack
        #The tuple is jobStoreID, memory, cores, disk, predecessorID
        #The predecessorID is used to establish which predecessors have been
        #completed before running the given Job - it is just a unique ID
        #per predecessor
        return (jobWrapper.jobStoreID, jobWrapper.memory, jobWrapper.cores,
                jobWrapper.disk, jobWrapper.preemptable,
                None if jobWrapper.predecessorNumber <= 1 else str(uuid.uuid4()))

    def getTopologicalOrderingOfJobs(self):
        """
        :returns: a list of jobs such that for all pairs of indices i, j for which i < j, \
        the job at index i can be run before the job at index j.
        :rtype: list
        """
        ordering = []
        visited = set()
        def getRunOrder(job):
            #Do not add the job to the ordering until all its predecessors have been
            #added to the ordering
            for p in job._directPredecessors:
                if p not in visited:
                    return
            if job not in visited:
                visited.add(job)
                ordering.append(job)
                map(getRunOrder, job._children + job._followOns)
        getRunOrder(self)
        return ordering

    def _serialiseJob(self, jobStore, jobsToJobWrappers, rootJobWrapper):
        """
        Pickle a job and its jobWrapper to disk.
        """
        # Pickle the job so that its run method can be run at a later time.
        # Drop out the children/followOns/predecessors/services - which are
        # all recorded within the jobStore and do not need to be stored within
        # the job
        self._children, self._followOns, self._services = [], [], []
        self._directPredecessors, self._promiseJobStore = set(), None
        # The pickled job is "run" as the command of the job, see worker
        # for the mechanism which unpickles the job and executes the Job.run
        # method.
        with jobStore.writeFileStream(rootJobWrapper.jobStoreID) as (fileHandle, fileStoreID):
            cPickle.dump(self, fileHandle, cPickle.HIGHEST_PROTOCOL)
        # Note that getUserScript() may have beeen overridden. This is intended. If we used
        # self.userModule directly, we'd be getting a reference to job.py if the job was
        # specified as a function (as opposed to a class) since that is where FunctionWrappingJob
        #  is defined. What we really want is the module that was loaded as __main__,
        # and FunctionWrappingJob overrides getUserScript() to give us just that. Only then can
        # filter_main() in _unpickle( ) do its job of resolveing any user-defined type or function.
        userScript = self.getUserScript().globalize()
        jobsToJobWrappers[self].command = ' '.join( ('_toil', fileStoreID) + userScript)
        #Update the status of the jobWrapper on disk
        jobStore.update(jobsToJobWrappers[self])

    def _serialiseServices(self, jobStore, jobWrapper, rootJobWrapper):
        """
        Serialises the services for a job.
        """
        def processService(serviceJob, depth):
            # Extend the depth of the services if necessary
            if depth == len(jobWrapper.services):
                jobWrapper.services.append([])

            # Recursively call to process child services
            for childServiceJob in serviceJob.service._childServices:
                processService(childServiceJob, depth+1)

            # Make a job wrapper
            serviceJobWrapper = serviceJob._createEmptyJobWrapperForJob(jobStore, predecessorNumber=1)

            # Create the start and terminate flags
            serviceJobWrapper.startJobStoreID = jobStore.getEmptyFileStoreID()
            serviceJobWrapper.terminateJobStoreID = jobStore.getEmptyFileStoreID()
            serviceJobWrapper.errorJobStoreID = jobStore.getEmptyFileStoreID()
            assert jobStore.fileExists(serviceJobWrapper.startJobStoreID)
            assert jobStore.fileExists(serviceJobWrapper.terminateJobStoreID)
            assert jobStore.fileExists(serviceJobWrapper.errorJobStoreID)

            # Create the service job tuple
            j = (serviceJobWrapper.jobStoreID, serviceJobWrapper.memory,
                 serviceJobWrapper.cores, serviceJobWrapper.disk,
                 serviceJobWrapper.startJobStoreID, serviceJobWrapper.terminateJobStoreID,
                 serviceJobWrapper.errorJobStoreID)

            # Add the service job tuple to the list of services to run
            jobWrapper.services[depth].append(j)

            # Break the links between the services to stop them being serialised together
            #childServices = serviceJob.service._childServices
            serviceJob.service._childServices = None
            assert serviceJob._services == []
            #service = serviceJob.service
            
            # Pickle the job
            serviceJob.pickledService = cPickle.dumps(serviceJob.service)
            serviceJob.service = None

            # Serialise the service job and job wrapper
            serviceJob._serialiseJob(jobStore, { serviceJob:serviceJobWrapper }, rootJobWrapper)

            # Restore values
            #serviceJob.service = service
            #serviceJob.service._childServices = childServices

        for serviceJob in self._services:
            processService(serviceJob, 0)

        self._services = []

    def _serialiseJobGraph(self, jobWrapper, jobStore, returnValues, firstJob):
        """
        Pickle the graph of jobs in the jobStore. The graph is not fully serialised \
        until the jobWrapper itself is written to disk, this is not performed by this \
        function because of the need to coordinate this operation with other updates. \
        """
        #Check if the job graph has created
        #any cycles of dependencies or has multiple roots
        self.checkJobGraphForDeadlocks()

        #Create the jobWrappers for followOns/children
        jobsToJobWrappers = self._makeJobWrappers(jobWrapper, jobStore)
        #Get an ordering on the jobs which we use for pickling the jobs in the
        #correct order to ensure the promises are properly established
        ordering = self.getTopologicalOrderingOfJobs()
        assert len(ordering) == len(jobsToJobWrappers)

        # Temporarily set the jobStore strings for the promise call back functions
        for job in ordering:
            job._promiseJobStore = jobStore
            def setForServices(serviceJob):
                serviceJob._promiseJobStore = jobStore
                for childServiceJob in serviceJob.service._childServices:
                    setForServices(childServiceJob)
            for serviceJob in self._services:
                setForServices(serviceJob)

        ordering.reverse()
        assert self == ordering[-1]
        if firstJob:
            #If the first job we serialise all the jobs, including the root job
            for job in ordering:
                # Pickle the services for the job
                job._serialiseServices(jobStore, jobsToJobWrappers[job], jobWrapper)
                # Now pickle the job
                job._serialiseJob(jobStore, jobsToJobWrappers, jobWrapper)
        else:
            #We store the return values at this point, because if a return value
            #is a promise from another job, we need to register the promise
            #before we serialise the other jobs
            self._setReturnValuesForPromises(returnValues, jobStore)
            #Pickle the non-root jobs
            for job in ordering[:-1]:
                # Pickle the services for the job
                job._serialiseServices(jobStore, jobsToJobWrappers[job], jobWrapper)
                # Pickle the job itself
                job._serialiseJob(jobStore, jobsToJobWrappers, jobWrapper)
            # Pickle any services for the job
            self._serialiseServices(jobStore, jobWrapper, jobWrapper)

    def _serialiseFirstJob(self, jobStore):
        """
        Serialises the root job. Returns the wrapping job.

        :param toil.jobStores.abstractJobStore.AbstractJobStore jobStore:
        """
        # Create first jobWrapper
        jobWrapper = self._createEmptyJobWrapperForJob(jobStore, None, predecessorNumber=0)
        # Write the graph of jobs to disk
        self._serialiseJobGraph(jobWrapper, jobStore, None, True)
        jobStore.update(jobWrapper)
        # Store the name of the first job in a file in case of restart. Up to this point the
        # root job is not recoverable. FIXME: "root job" or "first job", which one is it?
        jobStore.setRootJob(jobWrapper.jobStoreID)
        return jobWrapper

    def _serialiseExistingJob(self, jobWrapper, jobStore, returnValues):
        """
        Serialise an existing job.
        """
        self._serialiseJobGraph(jobWrapper, jobStore, returnValues, False)
        #Drop the completed command, if not dropped already
        jobWrapper.command = None
        #Merge any children (follow-ons) created in the initial serialisation
        #with children (follow-ons) created in the subsequent scale-up.
        assert len(jobWrapper.stack) >= 4
        combinedChildren = jobWrapper.stack[-1] + jobWrapper.stack[-3]
        combinedFollowOns = jobWrapper.stack[-2] + jobWrapper.stack[-4]
        jobWrapper.stack = jobWrapper.stack[:-4]
        if len(combinedFollowOns) > 0:
            jobWrapper.stack.append(combinedFollowOns)
        if len(combinedChildren) > 0:
            jobWrapper.stack.append(combinedChildren)

    ####################################################
    #Function which worker calls to ultimately invoke
    #a jobs Job.run method, and then handle created
    #children/followOn jobs
    ####################################################

    def _run(self, jobWrapper, fileStore):
        return self.run(fileStore)

    def _execute(self, jobWrapper, stats, localTempDir, jobStore, fileStore):
        """
        This is the core method for running the job within a worker.
        """
        if stats != None:
            startTime = time.time()
            startClock = getTotalCpuTime()
        baseDir = os.getcwd()
        #Run the job
        returnValues = self._run(jobWrapper, fileStore)
        #Serialize the new jobs defined by the run method to the jobStore
        self._serialiseExistingJob(jobWrapper, jobStore, returnValues)
        # If the job is not a checkpoint job, add the promise files to delete
        # to the list of jobStoreFileIDs to delete
        if not self.checkpoint:
            for jobStoreFileID in Promise.filesToDelete:
                fileStore.deleteGlobalFile(jobStoreFileID)
        else:
            # Else copy them to the job wrapper to delete later
            jobWrapper.checkpointFilesToDelete = list(Promise.filesToDelete)
        Promise.filesToDelete.clear()
        #Now indicate the asynchronous update of the job can happen
        fileStore._updateJobWhenDone()
        #Change dir back to cwd dir, if changed by job (this is a safety issue)
        if os.getcwd() != baseDir:
            os.chdir(baseDir)
        #Finish up the stats
        if stats != None:
            totalCpuTime, totalMemoryUsage = getTotalCpuTimeAndMemoryUsage()
            stats.jobs.append(
                Expando(
                    time=str(time.time() - startTime),
                    clock=str(totalCpuTime - startClock),
                    class_name=self._jobName(),
                    memory=str(totalMemoryUsage)
                )
            )

    def _jobName(self):
        """
        :rtype : string, used as identifier of the job class in the stats report.
        """
        return self.__class__.__name__

class JobException( Exception ):
    """
    General job exception.
    """
    def __init__( self, message ):
        super( JobException, self ).__init__( message )

class JobGraphDeadlockException( JobException ):
    """
    An exception raised in the event that a workflow contains an unresolvable \
    dependency, such as a cycle. See :func:`toil.job.Job.checkJobGraphForDeadlocks`.
    """
    def __init__( self, string ):
        super( JobGraphDeadlockException, self ).__init__( string )


class CacheError(Exception):
    '''
    Error Raised if the user attempts to add a non-local file to cache
    '''

    def __init__(self, message):
        super(CacheError, self).__init__(message)


class CacheInvalidSrcError(Exception):
    '''
    Error Raised if the user attempts to add a non-local file to cache
    '''

    def __init__(self, message):
        super(CacheInvalidSrcError, self).__init__(message)


class FunctionWrappingJob(Job):
    """
    Job used to wrap a function. In its run method the wrapped function is called.
    """
    def __init__(self, userFunction, *args, **kwargs):
        """
        :param userFunction: The function to wrap. The userFunction will be called \
        with the ``*args`` and ``**kwargs`` as arguments.

        The keywords "memory", "cores", "disk", "cache" are reserved keyword arguments \
        that if specified will be used to determine the resources for the job, \
        as :func:`toil.job.Job.__init__`. If they are keyword arguments to the function
        they will be extracted from the function definition, but may be overridden by
        the user (as you would expect).
        """
        # Use the user specified resource argument, if specified, else
        # grab the default argument from the function, if specified, else default to None
        argSpec = inspect.getargspec(userFunction)
        argDict = dict(zip(argSpec.args[-len(argSpec.defaults):],argSpec.defaults)) \
                        if argSpec.defaults != None else {}
        argFn = lambda x : kwargs.pop(x) if x in kwargs else \
                            (human2bytes(str(argDict[x])) if x in argDict.keys() else None)
        Job.__init__(self, memory=argFn("memory"), cores=argFn("cores"),
                     disk=argFn("disk"), cache=argFn("cache"),
                     preemptable=argFn("preemptable"),
                     checkpoint=kwargs.pop("checkpoint") if "checkpoint" in kwargs else False)
        #If dill is installed pickle the user function directly
        #TODO: Add dill support
        #else use indirect method
        self.userFunctionModule = ModuleDescriptor.forModule(userFunction.__module__).globalize()
        self.userFunctionName = str(userFunction.__name__)
        self._args=args
        self._kwargs=kwargs

    def _getUserFunction(self):
        userFunctionModule = self._loadUserModule(self.userFunctionModule)
        return getattr(userFunctionModule, self.userFunctionName)

    def run(self,fileStore):
        userFunction = self._getUserFunction( )
        return userFunction(*self._args, **self._kwargs)

    def getUserScript(self):
        return self.userFunctionModule

    def _jobName(self):
        return ".".join((self.__class__.__name__,self.userFunctionModule.name,self.userFunctionName))

class JobFunctionWrappingJob(FunctionWrappingJob):
    """
    A job function is a function whose first argument is a :class:`job.Job` \
    instance that is the wrapping job for the function. This can be used to \
    add successor jobs for the function and perform all the functions the \
    :class:`job.Job` class provides.

    To enable the job function to get access to the :class:`toil.job.Job.FileStore` \
    instance (see :func:`toil.job.Job.Run`), it is made a variable of the wrapping job \
    called fileStore.
    """
    def run(self, fileStore):
        userFunction = self._getUserFunction()
        self.fileStore = fileStore
        rValue = userFunction(*((self,) + tuple(self._args)), **self._kwargs)
        return rValue

class EncapsulatedJob(Job):
    """
    A convenience Job class used to make a job subgraph appear to be a single job.

    Let A be the root job of a job subgraph and B be another job we'd like to run after A
    and all its successors have completed, for this use encapsulate::

        A, B = A(), B() #Job A and subgraph, Job B
        A' = A.encapsulate()
        A'.addChild(B) #B will run after A and all its successors have
        # completed, A and its subgraph of successors in effect appear
        # to be just one job.

    The return value of an encapsulatd job (as accessed by the :func:`toil.job.Job.rv` function) \
    is the return value of the root job, e.g. A().encapsulate().rv() and A().rv() \
    will resolve to the same value after A or A.encapsulate() has been run.
    """
    def __init__(self, job):
        """
        :param toil.job.Job job: the job to encapsulate.
        """
        Job.__init__(self)
        self.encapsulatedJob = job
        Job.addChild(self, job)
        self.encapsulatedFollowOn = Job()
        Job.addFollowOn(self, self.encapsulatedFollowOn)

    def addChild(self, childJob):
        return Job.addChild(self.encapsulatedFollowOn, childJob)

    def addService(self, service):
        return Job.addService(self.encapsulatedFollowOn, service)

    def addFollowOn(self, followOnJob):
        return Job.addFollowOn(self.encapsulatedFollowOn, followOnJob)

    def rv(self, index=None):
        return self.encapsulatedJob.rv(index)

class ServiceJob(Job):
    """
    Job used to wrap a :class:`toil.job.Job.Service` instance.
    """
    def __init__(self, service):
        """
        This constructor should not be called by a user.

        :param service: The service to wrap in a job.
        :type service: toil.job.Job.Service
        """
        Job.__init__(self, memory=service.memory, cores=service.cores, disk=service.disk,
                     preemptable=service.preemptable)
        # service.__module__ is the module defining the class service is an instance of.
        self.serviceModule = ModuleDescriptor.forModule(service.__module__).globalize()

        #The service to run - this will be replace before serialization with a pickled version
        self.service = service
        self.pickledService = None

        # This references the parent job wrapper. It is initialised just before
        # the job is run. It is used to access the start and terminate flags.
        self.jobWrapper = None

    def run(self, fileStore):
        #Unpickle the service
        userModule = self._loadUserModule(self.serviceModule)
        service = self._unpickle( userModule, BytesIO( self.pickledService ) )
        #Start the service
        startCredentials = service.start(fileStore)
        try:
            #The start credentials  must be communicated to processes connecting to
            #the service, to do this while the run method is running we
            #cheat and set the return value promise within the run method
            self._setReturnValuesForPromises(startCredentials, fileStore.jobStore)
            self._rvs = {}  # Set this to avoid the return values being updated after the
            #run method has completed!

            #Now flag that the service is running jobs can connect to it
            logger.debug("Removing the start jobStoreID to indicate that establishment of the service")
            assert self.jobWrapper.startJobStoreID != None
            if fileStore.jobStore.fileExists(self.jobWrapper.startJobStoreID):
                fileStore.jobStore.deleteFile(self.jobWrapper.startJobStoreID)
            assert not fileStore.jobStore.fileExists(self.jobWrapper.startJobStoreID)

            #Now block until we are told to stop, which is indicated by the removal
            #of a file
            assert self.jobWrapper.terminateJobStoreID != None
            while True:
                # Check for the terminate signal
                if not fileStore.jobStore.fileExists(self.jobWrapper.terminateJobStoreID):
                    logger.debug("Detected that the terminate jobStoreID has been removed so exiting")
                    if not fileStore.jobStore.fileExists(self.jobWrapper.errorJobStoreID):
                        raise RuntimeError("Detected the error jobStoreID has been removed so exiting with an error")
                    break

                # Check the service's status and exit if failed or complete
                try:
                    if not service.check():
                        logger.debug("The service has finished okay, exiting")
                        break
                except RuntimeError:
                    logger.debug("Detected termination of the service")
                    raise

                time.sleep(fileStore.jobStore.config.servicePollingInterval) #Avoid excessive polling

            #Now kill the service
            #service.stop(fileStore)

            # Remove link to the jobWrapper
            self.jobWrapper = None

            logger.debug("Service is done")
        finally:
            # The stop function is always called
            service.stop(fileStore)

    def _run(self, jobWrapper, fileStore):
        # Set the jobWrapper for the job
        self.jobWrapper = jobWrapper
        #Run the job
        returnValues = self.run(fileStore)
        assert jobWrapper.stack == []
        assert jobWrapper.services == []
        # Unset the jobWrapper for the job
        self.jobWrapper = None
        # Set the stack to mimic what would be expected for a non-service job (this is a hack)
        jobWrapper.stack = [ [], [] ]
        return returnValues

    def getUserScript(self):
        return self.serviceModule


class Promise(object):
    """
    References a return value from a :meth:`toil.job.Job.run` or
    :meth:`toil.job.Job.Service.start` method as a *promise* before the method itself is run.

    Let T be a job. Instances of :class:`Promise` (termed a *promise*) are returned by T.rv(),
    which is used to reference the return value of T's run function. When the promise is passed
    to the constructor (or as an argument to a wrapped function) of a different, successor job
    the promise will be replaced by the actual referenced return value. This mechanism allows a
    return values from one job's run method to be input argument to job before the former job's
    run function has been executed.
    """
    _jobstore = None
    """
    Caches the job store instance used during unpickling to prevent it from being instantiated
    for each promise

    :type: toil.jobStores.abstractJobStore.AbstractJobStore
    """

    filesToDelete = set()
    """
    A set of IDs of files containing promised values when we know we won't need them anymore
    """
    def __init__(self, job, index):
        """
        :param Job job: the job whose return value this promise references
        """
        self.job = job
        self.index = index

    def __reduce__(self):
        """
        Called during pickling when a promise of this class is about to be be pickled. Returns
        the Promise class and construction arguments that will be evaluated during unpickling,
        namely the job store coordinates of a file that will hold the promised return value. By
        the time the promise is about to be unpickled, that file should be populated.
        """
        # The allocation of the file in the job store is intentionally lazy, we only allocate an
        # empty file in the job store if the promise is actually being pickled. This is done so
        # that we do not allocate files for promises that are never used.
        jobStoreString, jobStoreFileID = self.job.allocatePromiseFile(self.index)
        # Returning a class object here causes the pickling machinery to attempt to instantiate
        # the class. We will catch that with __new__ and return an the actual return value instead.
        return self.__class__, (jobStoreString, jobStoreFileID)

    @staticmethod
    def __new__(cls, *args):
        assert len(args) == 2
        if isinstance(args[0], Job):
            # Regular instantiation when promise is created, before it is being pickled
            return super(Promise, cls).__new__(cls, *args)
        else:
            # Attempted instantiation during unpickling, return promised value instead
            return cls._resolve(*args)

    @classmethod
    def _resolve(cls, jobStoreString, jobStoreFileID):
        # Initialize the cached job store if it was never initialized in the current process or
        # if it belongs to a different workflow that was run earlier in the current process.
        if cls._jobstore is None or cls._jobstore.config.jobStore != jobStoreString:
            cls._jobstore = Toil.loadOrCreateJobStore(jobStoreString)
        cls.filesToDelete.add(jobStoreFileID)
        with cls._jobstore.readFileStream(jobStoreFileID) as fileHandle:
            # If this doesn't work then the file containing the promise may not exist or be
            # corrupted
            value = cPickle.load(fileHandle)
            return value
