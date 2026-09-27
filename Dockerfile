# syntax=docker/dockerfile:1.7
# Review/build this on a branch before deploying. Targets Railway linux/amd64.
FROM python:3.14-slim-trixie AS base

RUN test "$(dpkg --print-architecture)" = amd64 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git tini xz-utils libatomic1 libgomp1 libstdc++6 \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 \
      libcups2t64 libdbus-1-3 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
      fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

FROM base AS build
ARG HERMES_COMMIT=41755854adfd96e2871badefdec7993f62324481
ARG GOG_VERSION=0.29.0
ENV HOME=/opt/build-home \
    HERMES_HOME=/opt/build-home/.hermes \
    HERMES_RUNTIME_DIR=/usr/local/lib/hermes-agent/tools \
    TMPDIR=/opt/build-tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CC=gcc CXX=g++ \
    npm_config_install_links=false

RUN mkdir -p /opt/build-home /opt/build-tmp /opt/products \
 && apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config libffi-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/local/lib/hermes-agent
RUN git init . \
 && git remote add origin https://github.com/NousResearch/hermes-agent.git \
 && git fetch --depth 1 origin "${HERMES_COMMIT}" \
 && git checkout --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${HERMES_COMMIT}"

# PM reads this checkout's pinned tools. Chromium and agent-browser land in the
# image's tool store (not the /data volume) for the Browser Use CLI backend.
# uv is internal PM tooling: ensured for dependency preparation, never on PATH.
RUN python - <<'PY'
import shutil
from pathlib import Path
from pm import ensure, env_for, installed_package, stage_manager_runtime
from scripts.bundles.payload import seal_pm_runtime
root = Path('/usr/local/lib/hermes-agent')
for name in ('python', 'uv', 'node', 'npm', 'ffmpeg', 'ripgrep', 'tirith', 'chromium', 'agent-browser'):
    ensure(name, explicit=True)
for command, package in (
    ('hermes-python', 'python'), ('node', 'node'), ('npm', 'npm'),
    ('ffmpeg', 'ffmpeg'), ('rg', 'ripgrep'), ('tirith', 'tirith'),
):
    Path('/usr/local/bin', command).symlink_to(installed_package(package).binary)
Path('/usr/local/bin/ffprobe').symlink_to(installed_package('ffmpeg').binary.with_name('ffprobe'))
Path('/usr/local/bin/npx').symlink_to(shutil.which('npx', path=env_for('npm', base_env={})['PATH']))
python = Path('/usr/local/bin/hermes-python').resolve()
stage_manager_runtime(python=python, destination=root / 'pm-runtime', project=root / 'pm')
seal_pm_runtime(root, python)
PY

ENV HERMES_PYTHON=/usr/local/bin/hermes-python
# Match the currently selected features, including Telegram at build time.
# This environment is part of the image, never hidden by the /data mount.
RUN hermes-python -m pm.build_env \
      --source /usr/local/lib/hermes-agent \
      --python /usr/local/bin/hermes-python \
      --out /usr/local/lib/hermes-agent/.venv \
      --no-install-project --sealed \
      --extra all --extra acp --extra computer-use --extra doc-extract \
      --extra google --extra telegram --extra tts-premium --extra vertex \
      --extra vision --extra voice --extra web --extra wecom --extra youtube

# Use the same frontend builders as this pinned version's official image.
RUN .venv/bin/python -I scripts/generate_icons.py \
      --source /usr/local/lib/hermes-agent --out /opt/products/icons \
 && node scripts/build/node-deps.mjs \
      --source /usr/local/lib/hermes-agent --workspace ui-tui --workspace web \
 && node scripts/build/tui.mjs \
      --source /usr/local/lib/hermes-agent --out /opt/products/tui \
 && node scripts/build/web.mjs \
      --source /usr/local/lib/hermes-agent --icons /opt/products/icons --out /opt/products/web

# Bind source, prepared dependencies, PM and UI products into a fixed payload.
RUN .venv/bin/python - <<'PY'
import json
import os
import shutil
import tomllib
from pathlib import Path
from docker.build_agent import assemble_image
from scripts.write_install_stamp import build_stamp
root = Path('/usr/local/lib/hermes-agent')
shutil.copytree('/opt/products/tui', root / 'ui-tui', dirs_exist_ok=True)
shutil.copytree('/opt/products/web', root / 'hermes_cli/web_dist', dirs_exist_ok=True)
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
stamp = build_stamp(commit=os.environ['HERMES_COMMIT'], base_version=version,
                    dirty=False, source='docker', distribution='docker',
                    update_mechanism='external')
stamp['pmRuntime'] = str(root / 'pm-runtime')
stamp['runtimeDir'] = str(root / 'tools')
(root / 'install-stamp.json').write_text(json.dumps(stamp) + '\n')
(root / '.install_method').write_text('docker\n')
assemble_image(root)
# Preserve the targets of existing /data/.local/bin/hermes{,-acp} wrappers.
(root / '.hermes/bin').mkdir(parents=True, exist_ok=True)
for command in ('hermes', 'hermes-acp'):
    (root / '.hermes/bin' / command).symlink_to(f'../../libexec/{command}')
# Fetch archives and build-only source metadata do not enter the final image.
for p in (root / 'tools').glob('fetch-*'):
    if p.is_dir():
        shutil.rmtree(p)
shutil.rmtree(root / '.git')
# Keep TypeScript, as upstream does; Vite/esbuild are build-only.
if (root / 'node_modules/typescript').is_dir():
    shutil.copytree(root / 'node_modules/typescript', '/opt/products/typescript', symlinks=True)
shutil.rmtree(root / 'node_modules')
if Path('/opt/products/typescript').is_dir():
    shutil.copytree('/opt/products/typescript', root / 'node_modules/typescript', symlinks=True)
    (root / 'node_modules/.bin').mkdir()
    (root / 'node_modules/.bin/tsc').symlink_to('../typescript/bin/tsc')
PY

RUN curl -fL --retry 3 \
      "https://github.com/openclaw/gogcli/releases/download/v${GOG_VERSION}/gogcli_${GOG_VERSION}_linux_amd64.tar.gz" \
      -o /opt/products/gog.tar.gz \
 && mkdir /opt/products/gog \
 && tar -xzf /opt/products/gog.tar.gz -C /opt/products/gog \
 && install -m 0755 /opt/products/gog/gog /usr/local/bin/gog

FROM base AS runtime
ENV HOME=/data \
    HERMES_HOME=/data/.hermes \
    HERMES_RUNTIME_DIR=/usr/local/lib/hermes-agent/tools \
    HERMES_PYTHON=/usr/local/bin/hermes-python \
    HERMES_BIN=/usr/local/bin/hermes \
    HERMES_WEB_DIST=/usr/local/lib/hermes-agent/hermes_cli/web_dist \
    HERMES_TUI_DIR=/usr/local/lib/hermes-agent/ui-tui \
    PLAYWRIGHT_BROWSERS_PATH=/usr/local/lib/hermes-agent/tools \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=build /usr/local/lib/hermes-agent /usr/local/lib/hermes-agent
COPY --from=build /usr/local/bin/gog /usr/local/bin/gog

# Re-create links rather than relying on COPY's symlink handling. PM is
# stdlib-only, so the base interpreter runs it before hermes-python exists.
WORKDIR /usr/local/lib/hermes-agent
RUN python - <<'PY'
import shutil
from pathlib import Path
from pm import env_for, installed_package
for command, package in (
    ('hermes-python', 'python'), ('node', 'node'), ('npm', 'npm'),
    ('ffmpeg', 'ffmpeg'), ('rg', 'ripgrep'), ('tirith', 'tirith'),
):
    Path('/usr/local/bin', command).symlink_to(installed_package(package).binary)
Path('/usr/local/bin/ffprobe').symlink_to(installed_package('ffmpeg').binary.with_name('ffprobe'))
Path('/usr/local/bin/npx').symlink_to(shutil.which('npx', path=env_for('npm', base_env={})['PATH']))
PY

COPY supervisor.py start.sh /app/
RUN ln -s /usr/local/lib/hermes-agent/libexec/hermes /usr/local/bin/hermes \
 && chmod +x /app/start.sh \
 && mkdir -p /data/.hermes

WORKDIR /usr/local/lib/hermes-agent
# Build-time smoke checks: no production config, bot token, or mounted volume.
RUN HERMES_HOME=/opt/build-check /usr/local/bin/hermes --version \
 && .venv/bin/python -c "import telegram, fastapi, uvicorn; print('Telegram and dashboard imports OK')" \
 && test -s hermes_cli/web_dist/index.html \
 && test -s ui-tui/dist/entry.js \
 && test -x .hermes/bin/hermes \
 && test -x .hermes/bin/hermes-acp \
 && gog --version \
 && python - <<'PY'
import subprocess
from pm import installed_package
agent_browser = installed_package('agent-browser')
chromium = installed_package('chromium')
assert agent_browser and agent_browser.binary.is_file(), 'agent-browser missing'
assert chromium and chromium.binary.is_file(), 'chromium missing'
dom = subprocess.run([str(chromium.binary), '--headless', '--no-sandbox', '--disable-gpu',
                      '--disable-dev-shm-usage', '--dump-dom',
                      'data:text/html,<title>hermes-ok</title>'],
                     capture_output=True, text=True, timeout=60)
assert 'hermes-ok' in dom.stdout, dom.stderr[-2000:]
print('Chromium headless OK:', chromium.binary)
PY

WORKDIR /data
ENTRYPOINT ["tini", "--"]
CMD ["/app/start.sh"]
