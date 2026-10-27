"""Run the perturbation noise jobs for given designs of a campaign; print the noise floors."""
import sys
from pramana import baseline
from pramana.campaign import Campaign
from pramana.config import Config

c = Campaign(Config.load(sys.argv[1]))
ids = sys.argv[2].split(",")
jobs = [j for j in baseline.jobs(c) if j["purpose"] == "noise" and j["design"] in ids]
for r in c.store_runner.run_many(jobs, log=print):
    print("RESULT", r["design"], r["tag"], r["status"], r["valid"], r["result"] and r["result"]["wns"])
c2 = Campaign(Config.load(sys.argv[1]))
print("NOISE", {d: c2.noise_floor(d) for d in ids})
