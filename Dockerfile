# syntax=docker/dockerfile:1
# mcpgw container image: docker run -p 127.0.0.1:8765:8765 -e MCPGW_API_KEY=... ghcr.io/invictus2076/mcpgw

FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY mcpgw ./mcpgw
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /dist .

FROM python:3.13-slim
LABEL org.opencontainers.image.title="mcpgw" \
      org.opencontainers.image.description="Gateway for Streamable HTTP MCP servers with built-in OAuth 2.1" \
      org.opencontainers.image.source="https://github.com/Invictus2076/mcpgw" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /dist/ /tmp/dist/
RUN pip install --no-cache-dir /tmp/dist/*.whl && rm -rf /tmp/dist \
 && useradd --create-home --uid 10001 mcpgw \
 && mkdir -p /data && chown mcpgw:mcpgw /data && chmod 700 /data
COPY docker/mcpgw.toml /etc/mcpgw/mcpgw.toml
ENV MCPGW_CONFIG=/etc/mcpgw/mcpgw.toml \
    PYTHONUNBUFFERED=1
USER mcpgw
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,sys,urllib.request; u='http://127.0.0.1:%s/healthz' % os.environ.get('MCPGW_PORT', '8765'); sys.exit(urllib.request.urlopen(u, timeout=3).status != 200)"
ENTRYPOINT ["mcpgw"]
CMD ["serve"]
