#!/bin/bash
# Start worker jobs on the given nodes, wait until they run, then the training job.
#   slurm/launch.sh "sfpga01i:12,sfpga01n:11" sg250 8 pramana.incremental <campaign> ...
# arg1: worker node:slots list   arg2: main node   arg3: local slots   rest: pramana command
set -e
workers=$1; main=$2; local_slots=$3; shift 3
cd "$(dirname "$0")"; mkdir -p logs
remote=""; ids=""
for ws in ${workers//,/ }; do
  node=${ws%%:*}; slots=${ws##*:}
  jid=$(env -u SLURM_JOB_ID -u SLURM_JOBID sbatch --parsable -w "$node" worker_node.sbatch)
  remote="${remote:+$remote,}$jid:$slots"; ids="$ids $jid"
  echo "worker $jid on $node ($slots slots)"
done
for jid in $ids; do
  until [ "$(squeue -j "$jid" -h -o %T)" = "RUNNING" ]; do sleep 10; done
done
echo "workers running: $remote"
env -u SLURM_JOB_ID -u SLURM_JOBID PRAMANA_REMOTE="$remote" LOCAL_SLOTS="$local_slots" \
  sbatch --parsable --export=ALL -w "$main" train.sbatch "$@" | sed 's/^/train job /'
echo "when training ends: scancel$ids"
