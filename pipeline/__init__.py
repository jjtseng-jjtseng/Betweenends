"""Between Ends data pipeline.

Collects archery tournament results from the Between Ends results API,
normalises them into analysis-ready tables, and builds per-archer profiles.

Stages (each is resumable and can be re-run independently):

    python -m pipeline.collect   ...   raw API responses  -> data/raw/
    python -m pipeline.normalize ...   data/raw/          -> data/processed/*.csv.gz
    python -m pipeline.archers   ...   data/processed/    -> archer identity + trends
    python -m pipeline.validate  ...   integrity checks over data/processed/
"""
