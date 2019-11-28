import os
import sys
import drmaa
from multiprocessing.pool import Pool
from argparse import ArgumentParser
from helpers import GDCIterator
import pickle

"""
This is a template for building a system that will list GDC files, download them and then submit jobs to the batch 
system process them. You need to
1. Edit the case and file filters to select the data and file types that you need 
2. Fill in the is_file_needed function (this determines whether the file should be downloaded
3. Adapt the get_file_list function for your use case
4. Provide a process.sh bash script that takes source endpoint file name as argument. You will need
   to know where the file is located based on your endpoint configuration.
Good luck!
"""

#-----------------------------------------------------------------------------
# Resources for your job in qstat format
RESOURCES = '-l nodes=1:ppn=2,mem=2gb,walltime=24:00:00'
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
"""
The file might have already been downloaded and processed, in which
return False and that will be skipped
"""
def are_files_needed(case_file_set):

  for (f, s) in zip(case_file_set.file_names, case_file_set.md5s):
    # If the output file doesn't exist we need to download it
    if not os.path.exists(f):
      print(f'no output file: {f}')
      return True

    sum_file = os.path.splitext(f)[0] + '.md5'

    # If the md5sum file doesn't exist, download is presumably incomplete
    if not os.path.exists(sum_file):
      print(f'no checksum file: {sum_file}')
      return True

    # If the md5sums don't match, download it again
    with open(sum_file, 'r') as fs:
      md5sum = fs.read().strip()

    if md5sum != s:
      print(f'Checksum mismatch for {f}. expected: {s}  got: {md5sum}')
      return True

  return False
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
'''
Edit these filters for your requirements (refer to the GDC documentation)
'''
case_filters = {
  'op': '=',
  'content': {
    'field': 'project.project_id',
    'value': 'TCGA-SKCM'
  }
}

file_filters = {
  'op': 'and',
  'content': [
    {
      'op': '=',
      'content': {
        'field': 'cases.submitter_id',
      }
    },
    {
      'op': '=',
      'content': {
        'field': 'data_format',
        'value': 'BAM'
      }
    },
    {
      'op': '=',
      'content': {
        'field': 'experimental_strategy',
        'value': 'WXS'
      }
    }
  ]
}
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
'''
Command line argument parser
'''
def build_parser():
  parser = ArgumentParser()
  parser.add_argument('--output-dir',
                      help='The directory where files will be downloaded to',
                      dest='output_dir',
                      required=True)
  parser.add_argument('--num-jobs',
                      dest='num_jobs',
                      help='Number of concurrent download jobs (each job downloads all files for a case in parallel.',
                      type=int,
                      default=1,
                      required=False)
  parser.add_argument('--start-after',
                      dest='start_after',
                      help='Start submitting jobs after this many queries (useful after using --stop-after).',
                      type=int,
                      default=0,
                      required=False)
  parser.add_argument('--stop-after',
                      dest='stop_after',
                      help='Stop after submitting this many jobs (useful when testing).',
                      type=int,
                      default=sys.maxsize,
                      required=False)
  parser.add_argument('--save-query-file',
                      dest='save_query_file',
                      help='If this file exists, unpickle it instead of redoing the query. ' + \
                           'If it does not exist save the query into this file.',
                      type=str,
                      default=None,
                      required=False)
  return parser
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
'''
A simple container for files associated with an individual patient
'''
class CaseFileSet:
  def __init__(self, output_dir):
    self.file_ids   = []
    self.file_names = []
    self.md5s       = []
    self.output_dir = output_dir

  def add(self, file_id, file_name, md5):
    self.md5s.append(md5)
    self.file_ids.append(file_id)
    self.file_names.append(os.path.join(self.output_dir, file_name))
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
"""
During testing, just return a single file, then scale up
"""
def get_file_list(output_dir, start_after, stop_after):
  print('Starting file listing')

  files = []
  cnt = 0
  for case in GDCIterator('cases', case_filters):

    cnt = cnt + 1
    if cnt > stop_after:
      break
    if cnt <= start_after:
      continue

    this_case = case['submitter_id']
    file_filters['content'][0]['content']['value'] = this_case

    cfs = CaseFileSet(output_dir)
    for fl in GDCIterator('files', file_filters):
      filename = fl['file_name']
      file_id  = fl['file_id']
      md5      = fl['md5sum']
      cfs.add(file_id, filename, md5)
      print(f'found {filename}')

    files.append(cfs)

  return files
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
"""
 This class builds and manages a batch job
"""
class Job:
  def __init__(self, case_file_set):
    self.cfs = case_file_set

  def __call__(self, *args, **kwargs):
    output_paths = self.cfs.file_names
    file_ids = self.cfs.file_ids
    md5sums = self.cfs.md5s

    print('Building job for {fn1}, {fn2}, etc'.format(fn1=output_paths[0], fn2=output_paths[1]))
    s = drmaa.Session()
    s.initialize()

    try:
      jt = s.createJobTemplate()
      jt.workingDirectory = os.getcwd()
      jt.outputPath = os.getcwd()
      jt.joinFiles = True
      jt.jobName = os.path.basename(output_paths[0])
      jt.remoteCommand = os.path.join(os.getcwd(), 'download-and-process.sh')
      jt.args = [','.join(output_paths), ','.join(file_ids), ','.join(md5sums)]
      jt.nativeSpecification = RESOURCES
      job_id = s.runJob(jt)

      print('Submitted job: {job_id}'.format(job_id=job_id))
      info = s.wait(job_id, drmaa.Session.TIMEOUT_WAIT_FOREVER)
      print('Completed job: {job_id}'.format(job_id=job_id))
      print("""\
      id:                        %(jobId)s
      exited:                    %(hasExited)s
      signaled:                  %(hasSignal)s
      with signal (id signaled): %(terminatedSignal)s
      dumped core:               %(hasCoreDump)s
      aborted:                   %(wasAborted)s
      resource usage:
      %(resourceUsage)s
      """ % info._asdict())
    finally:
      s.exit()
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
def main(argv):
  parser = build_parser()
  options = parser.parse_args(args=argv)

  num_jobs = options.num_jobs
  stop_after = options.stop_after
  start_after = options.start_after
  output_dir = options.output_dir
  save_query_file = options.save_query_file

  # Get the file list and filter for the ones we want to process
  if save_query_file is not None and os.path.exists(save_query_file):
    with open(save_query_file, 'rb') as f:
      case_files = pickle.load(f)
  else:
    case_files = get_file_list(output_dir, start_after, stop_after)

  if save_query_file is not None and not os.path.exists(save_query_file):
    with open(save_query_file, 'wb') as f:
      pickle.dump(case_files, f)

  #case_files = filter(are_files_needed, case_files)
  #cnt = 0
  #for _ in case_files:
  # cnt = cnt + 1
  #print(cnt)
  #quit()
  # A pool of workers. Each worker will manage a job in the batch system
  p = Pool(num_jobs)
  # Create jobs for each file
  jobs = [Job(fn) for fn in case_files]

  # Submit them to the pool
  submitted_jobs = [p.apply_async(job) for job in jobs]

  # Wait for them to finish
  for submitted_job in submitted_jobs:
    submitted_job.get()
#-----------------------------------------------------------------------------


#-----------------------------------------------------------------------------
if __name__ == '__main__':
  main(sys.argv[1:])
#-----------------------------------------------------------------------------
