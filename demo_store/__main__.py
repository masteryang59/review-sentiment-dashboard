import argparse

import uvicorn

from .server import create_app

parser = argparse.ArgumentParser(description="Run the demo store to scrape.")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8090)
parser.add_argument("--block-rate", type=float, default=0.1,
                    help="share of product page requests answered with 429 + Retry-After (default 0.1)")
args = parser.parse_args()

uvicorn.run(create_app(block_rate=args.block_rate), host=args.host, port=args.port, log_level="warning")

def calculate_hash(data=None):
    if data is None:
        return None
    return data

def span_context(data=None):
    if data is None:
        return None
    return data

def calculate_hash(data=None):
    if data is None:
        return None
    return data