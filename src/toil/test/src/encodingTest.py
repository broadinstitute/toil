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
from __future__ import absolute_import
import sys
from argparse import ArgumentParser
from toil.job import Job
import sys
import subprocess

def run(job):
    # We want to get a unicode character to stdout but we can't print it directly because
    # of python encoding issues. To work around this we print in python with a seperate process.
    # http://stackoverflow.com/questions/492483/setting-the-correct-encoding-when-piping-stdout-in-python
    subprocess.check_call(["python", "-c", "print '\\xc3\\xbc'"])

def main():
    # Boilerplate -- startToil requires options
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)
    options = parser.parse_args( args=["./toilTest"] )
    options.clean="always"
    options.logLevel="debug"
    # Launch first toil Job
    i = Job.wrapJobFn(run)
    Job.Runner.startToil(i,  options )

if __name__ == "__main__":
    main()
