#!/bin/bash
# Main pramana job: the pramana process, its LLM sessions (tmux) and LOCAL_SLOTS Vivado runs on
# this node; more runs go to the worker jobs named in PRAMANA_REMOTE (set by launch.sh).
# Args after the script: a pramana command, e.g.  pramana.incremental <c> --dcp-list ... --batches 1
#SBATCH -p fpga
#SBATCH -N 1
#SBATCH -c 48
#SBATCH --mem=0
#SBATCH -t 24:00:00
#SBATCH -J pr-train
#SBATCH -o /home/rsunketa/pramana/slurm/logs/train-%j.out
export XILINXD_LICENSE_FILE=2100@en4228283l.scai.dhcp.asu.edu
export PRAMANA_MAX_VIVADO=${LOCAL_SLOTS:-8}
cd /home/rsunketa/pramana
echo "host $(hostname) start $(date); local slots $PRAMANA_MAX_VIVADO; remote $PRAMANA_REMOTE"
/usr/bin/python3.12 -m "$@"
rc=$?
echo "end $(date) rc=$rc"
for j in ${PRAMANA_REMOTE//,/ }; do scancel "${j%%:*}"; done   # release the worker nodes
