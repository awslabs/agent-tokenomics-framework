"""Sign a support-runtime payload with the benchmark key, for local testing.

The support runtime only accepts synthetic `bench-*` users and pinned flags /
usage plans from KMS-signed requests (threat model M8; shared/caller_mac.py).
This adds the signature so you can curl a locally running container:

    echo '{"message": "What is your return policy?", "user_id": "bench-dev-1"}' \
      | BENCHMARK_MAC_KEY_ARN=<AppPlane BenchmarkMacKeyArn output> \
        python scripts/sign-agent-payload.py \
      | curl -s localhost:8080/invocations -H 'Content-Type: application/json' -d @-

Your credentials need kms:GenerateMac on the key. The container needs
BENCHMARK_MAC_KEY_ARN set and credentials with kms:VerifyMac on it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shared import caller_mac


def main() -> None:
    payload = json.load(sys.stdin)
    exp = int(time.time()) + caller_mac.TTL_SECONDS
    mac = boto3.client("kms").generate_mac(
        KeyId=os.environ["BENCHMARK_MAC_KEY_ARN"],
        Message=caller_mac.digest(payload, exp),
        MacAlgorithm=caller_mac.MAC_ALGORITHM,
    )["Mac"]
    payload[caller_mac.AUTH_FIELD] = {"exp": exp, "mac": base64.b64encode(mac).decode("ascii")}
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
