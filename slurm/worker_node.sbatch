#!/bin/bash
# Holds one node so a pramana job elsewhere can start Vivado runs in it:
#   PRAMANA_REMOTE="<this job id>:<slots>"  (runs start with `srun --jobid --overlap`).
# Nothing runs here by itself; cancel it when the training job is done.
#SBATCH -p fpga
#SBATCH -N 1
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH -t 24:00:00
#SBATCH -J pr-worker
#SBATCH -o /home/rsunketa/pramana/slurm/logs/worker-%j.out
echo "worker $(hostname) up $(date), $(nproc) CPUs"
sleep infinity
