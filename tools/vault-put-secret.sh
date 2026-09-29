#!/bin/sh
# Copy UNIFI_HOST / UNIFI_USER / UNIFI_PASS from horn.env into Vault for the cluster's ExternalSecret.
# Values travel over stdin as JSON, never in argv.
set -eu
ENV_FILE=${1:-"$(dirname "$0")/../horn.env"}
python3 - "$ENV_FILE" <<'EOF' | vault kv put -mount=secret "Talos Cluster/apps/horn-player" -
import json, sys
keys = ("UNIFI_HOST", "UNIFI_USER", "UNIFI_PASS")
d = {}
for line in open(sys.argv[1]):
    k, _, v = line.strip().partition("=")
    if k in keys:
        d[k] = v.strip().strip("'\"")
missing = [k for k in keys if not d.get(k)]
if missing:
    sys.exit(f"missing in env file: {', '.join(missing)}")
print(json.dumps(d))
EOF
echo "stored keys: $(vault kv get -mount=secret -format=json 'Talos Cluster/apps/horn-player' | python3 -c 'import json,sys; print(", ".join(sorted(json.load(sys.stdin)["data"]["data"])))')"
