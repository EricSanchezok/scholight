#!/bin/bash
set -e

DATA_ROOT="/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/milvus-data"
IP_FILE="$DATA_ROOT/milvus_ip.txt"

mkdir -p "$DATA_ROOT/etcd" "$DATA_ROOT/data" "$DATA_ROOT/logs" /var/lib/milvus/data /var/lib/milvus/rdb_data

# IP registration
CURRENT_IP=$(hostname -i 2>/dev/null | awk '{print $1}')
echo "$CURRENT_IP" > "$IP_FILE"
echo "[entrypoint] IP: $CURRENT_IP"

# ══════════════════════════════════════════════════════════════════════
# Embed etcd persistence guard
#
# If a previous etcd snapshot exists on GPFS, the cluster already
# exists.  Force ETCD_INITIAL_CLUSTER_STATE=existing to prevent the
# bug described in milvus-io/milvus#36648: embed etcd treating an
# existing data-dir as a brand-new cluster and overwriting all
# metadata (collections, indexes, schemas).
# ══════════════════════════════════════════════════════════════════════
if [ -f "$DATA_ROOT/etcd/member/snap/db" ]; then
    export ETCD_INITIAL_CLUSTER_STATE=existing
    echo "[entrypoint] Found existing etcd snapshot on GPFS — setting INITIAL_CLUSTER_STATE=existing"
else
    echo "[entrypoint] No existing etcd snapshot — first run, creating new cluster"
fi

# Start Milvus standalone with embed etcd + local storage
export MILVUSCONF=/milvus/configs
export ETCD_USE_EMBED=true
export ETCD_DATA_DIR="$DATA_ROOT/etcd"
export ETCD_CONFIG_PATH=/milvus/configs/embedEtcd.yaml
export COMMON_STORAGETYPE=local
export DEPLOY_MODE=STANDALONE

# ── MMAP + memory protection（env vars take priority over files）───────────────
if [ -f /milvus/configs/env.sh ]; then
    . /milvus/configs/env.sh
    echo "[entrypoint] MMAP env vars loaded"
fi

echo "[entrypoint] Starting Milvus standalone on :19530..."
/milvus/bin/milvus run standalone > "$DATA_ROOT/logs/milvus.log" 2>&1 &
MILVUS_PID=$!

echo "[entrypoint] Waiting for Milvus healthy (up to 120s)..."
for i in $(seq 1 60); do
    sleep 2
    if curl -sf http://localhost:9091/healthz > /dev/null 2>&1; then
        echo "[entrypoint] Milvus healthy at $CURRENT_IP:19530 ($((i*2))s)"
        exit 0
    fi
    kill -0 $MILVUS_PID 2>/dev/null || {
        echo "[entrypoint] Milvus died:"
        tail -20 "$DATA_ROOT/logs/milvus.log"
        exit 1
    }
done

echo "[entrypoint] Timeout after 120s:"
tail -20 "$DATA_ROOT/logs/milvus.log"
exit 1
