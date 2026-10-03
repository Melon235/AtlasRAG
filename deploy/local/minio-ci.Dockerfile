FROM curlimages/curl:8.10.1@sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b

USER root

COPY --chmod=0755 minio /usr/bin/minio

VOLUME ["/minio_data"]
EXPOSE 9000 9001

ENTRYPOINT ["/usr/bin/minio"]
