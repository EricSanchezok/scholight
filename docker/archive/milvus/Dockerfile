FROM docker.m.daocloud.io/milvusdb/milvus:v2.6.2

# Milvus v2.6.2 stable (via DaoCloud mirror)
# - embed etcd + local storage + GPFS persistence
# - user.yaml: MMAP (global ON), memory 0.85, localRPC, segment compaction 4096MB
# - env.sh: per-collection default MMAP env + memory watermarks
# - entrypoint.sh: IP registration + etcd snapshot guard + health polling

USER root

COPY embedEtcd.yaml /milvus/configs/embedEtcd.yaml
COPY user.yaml /milvus/configs/user.yaml
COPY env.sh /milvus/configs/env.sh
COPY --chmod=755 entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]