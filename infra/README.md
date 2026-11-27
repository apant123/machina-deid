# Running the pipeline on AWS Batch

The reference run used one 16-vCPU EC2 instance (same code, `multiprocessing`). This folder
packages the same code for AWS Batch on Spot, for reruns:

1. Build and push the image (`infra/Dockerfile`; put the ODA File Converter `.deb` next to it).
2. Create a Spot compute environment (c7a / m7a, 16 vCPU, 200 GB gp3 scratch at `/scratch`).
3. Register `batch-job-definition.json`. The role needs read access to the staging bucket and
   write access to the output bucket. `DEID_SEED` comes from Secrets Manager, never from the image.
4. Submit one job per shard. `--formats` splits by format family, e.g. `step`, `pdf`, `ole2`,
   `solidworks-new`. Each job resumes from its status parts in `/data/rewrite/parts`.

The pseudonym map (`entities/map.parquet`) and the logo cache (`logos/logo_scan.parquet`) are read
from the data volume. Sync them from the private `_entities/` and `_logos/` prefixes of the staging
bucket. They contain PII, so they are never baked into the image.
