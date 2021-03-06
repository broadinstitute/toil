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

import sys
from version import version
from setuptools import find_packages, setup

botoVersionRequired = 'boto==2.38.0'

kwargs = dict(
    name='toil',
    version=version,
    description='Pipeline management software for clusters.',
    author='Benedict Paten',
    author_email='benedict@soe.usc.edu',
    url="https://github.com/BD2KGenomics/toil",
    install_requires=[
        'bd2k-python-lib==1.13.dev14'],
    tests_require=[
        'mock==1.0.1',
        'pytest==2.8.3'],
    test_suite='toil',
    extras_require={
        'mesos': [
            'psutil==3.0.1'],
        'aws': [
            botoVersionRequired,
            'cgcloud-lib==1.4a1.dev195' ],
        'azure': [
            'azure==1.0.3'],
        'encryption': [
            'pynacl==0.3.0'],
        'google': [
            'gcs_oauth2_boto_plugin==1.9',
            botoVersionRequired],
        'cwl': [
            'cwltool==1.0.20160425140546']},
    package_dir={'': 'src'},
    packages=find_packages('src', exclude=['*.test']),
    entry_points={
        'console_scripts': [
            'toil = toil.utils.toilMain:main',
            '_toil_worker = toil.worker:main',
            'cwltoil = toil.cwl.cwltoil:main [cwl]',
            'cwl-runner = toil.cwl.cwltoil:main [cwl]',
            '_toil_mesos_executor = toil.batchSystems.mesos.executor:main [mesos]']})

from setuptools.command.test import test as TestCommand


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to py.test")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = []

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        import pytest
        # Sanitize command line arguments to avoid confusing Toil code attempting to parse them
        sys.argv[1:] = []
        errno = pytest.main(self.pytest_args)
        sys.exit(errno)


kwargs['cmdclass'] = {'test': PyTest}

setup(**kwargs)
