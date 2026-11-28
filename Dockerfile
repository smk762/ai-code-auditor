# The interpreter validate_patch runs patches under. The test-env check reads this line to make sure
# its report describes the same interpreter (auditor/test_env_readiness.py, #136).
ARG PYTHON_VERSION=3.11

# GDAL, PDAL and PROJ: without them validate_patch skips every argus-atlas and argus-survey test that
# runs a geo tool (docker/geo/geo.yml). Pinned by exact URL and md5; the datum grids by sha256.
FROM debian:trixie-slim AS geo
ARG MICROMAMBA_SHA256=e7274528ceb9c20d048a428d6c22d7e02e268f8ffb762c4c365422347c8b8ba2
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates bzip2 curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL --retry 3 -o /tmp/mm.tar.bz2 https://micro.mamba.pm/api/micromamba/linux-64/2.3.3 \
    && echo "${MICROMAMBA_SHA256}  /tmp/mm.tar.bz2" | sha256sum -c - \
    && tar -xjf /tmp/mm.tar.bz2 -C /usr/local bin/micromamba
COPY docker/geo/geo-linux-64.lock docker/geo/proj-grids.sha256 docker/geo/wrap.sh /tmp/
RUN MAMBA_ROOT_PREFIX=/opt/mamba micromamba create -y -q -p /opt/geo -f /tmp/geo-linux-64.lock \
    && rm -rf /opt/geo/include /opt/geo/share/doc /opt/geo/share/man /opt/geo/man /opt/geo/pkgs \
    && find /opt/geo/lib -name '*.a' -delete \
    && for so in /opt/geo/lib/libpdal_plugin_*.so; do \
        if LD_LIBRARY_PATH=/opt/geo/lib ldd "$so" | grep -q 'not found'; then rm "$so"; fi; \
    done \
    && cd /opt/geo/share/proj \
    && cut -d' ' -f3 /tmp/proj-grids.sha256 | xargs -I{} curl -fsSL --retry 3 -o {} "https://cdn.proj.org/{}" \
    && sha256sum -c /tmp/proj-grids.sha256 \
    && cp /tmp/proj-grids.sha256 /opt/geo/share/proj/argus-survey-grids.sha256 \
    && bash /tmp/wrap.sh /opt/geo /opt/geo-bin

FROM python:${PYTHON_VERSION}-slim
ARG PYTHON_VERSION
ENV AI_AUDIT_VALIDATOR_PYTHON=${PYTHON_VERSION}

# git: ecosystem_audit / extraction metrics (subprocess git). wget: compose healthcheck for audit-api.
# graphviz: `dot` binary, used by service_topology renderer.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git wget ca-certificates graphviz \
    && rm -rf /var/lib/apt/lists/*

# node + npm: validate_patch runs TS/JS projects' typecheck, lint, *:check and tests (vitest 4 needs node 20+,
# Debian's nodejs is 18). Copied from the official image instead of apt.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# Where GitHub's runners have it: a test that runs `python3` with PATH cut to /usr/bin:/bin (argus-lightbox's
# lens-backend stubs) otherwise fails only here, with exit 127.
RUN ln -s /usr/local/bin/python3 /usr/bin/python3

# Wrappers only, so nothing else in the conda env (its python, curl, openssl) is ever on PATH.
COPY --from=geo /opt/geo /opt/geo
COPY --from=geo /opt/geo-bin/ /usr/local/bin/
RUN gdalinfo --version && pdal --version >/dev/null && projinfo EPSG:4326 >/dev/null && gdal2tiles.py --version

WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY auditor /app/auditor
COPY conductor /app/conductor
COPY ecosystem /app/ecosystem
COPY patterns /app/patterns
COPY semantic /app/semantic
COPY copilot /app/copilot
COPY pipelines /app/pipelines
COPY qa /app/qa
COPY wiki /app/wiki
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY config /app/config
COPY memory /app/memory
COPY scripts /app/scripts
COPY cli.py /app/cli.py
# argus-iris tenant manifest, the default ARGUS_GPU_TENANT_MANIFEST (auditor/gpu_lease.py)
COPY gpu-tenant.yaml /app/gpu-tenant.yaml

# ruff: fatal-only lint of patched files in validate_patch (per-project test venvs are built at runtime)
# uv: builds a test env on the interpreter a project's requires-python asks for when the image's
# doesn't satisfy it (validator.test_python, #141); its interpreters live in the venv cache.
RUN pip install --no-cache-dir . ruff uv==0.11.9

# The structure scan parses with this one: a file using syntax newer than the parser is unreadable to it
# (auditor/structure.py). Never below TEST_PYTHONS' newest. It runs no packages, and no patch's tests.
ARG SCAN_PYTHON_VERSION=3.14
RUN uv python install --no-cache --no-bin --install-dir /opt/scan-python ${SCAN_PYTHON_VERSION} \
    && ln -s /opt/scan-python/cpython-${SCAN_PYTHON_VERSION}-*/bin/python${SCAN_PYTHON_VERSION} /usr/local/bin/scan-python \
    && chmod -R a+rX /opt/scan-python && scan-python --version
ENV AI_AUDIT_SCAN_PYTHON=/usr/local/bin/scan-python

CMD ["python", "pipelines/nightly_repo_audit.py"]
