"""python -m rrchangelog [--host HOST] [--port PORT]"""

import argparse
import logging
import os

import uvicorn

parser = argparse.ArgumentParser(description="Run the RR Changelog web app.")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()

logging.basicConfig(level=os.environ.get("RR_LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(name)s: %(message)s")
logging.getLogger("asyncio").setLevel(logging.WARNING)
uvicorn.run("rrchangelog.api:app", host=args.host, port=args.port)
