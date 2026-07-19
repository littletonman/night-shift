# night-shift agent runtime.
#
# Trust model (chosen): the AGENT runs as a NON-root user (node, UID 1000), while
# the enforcement (/opt/night-shift + /etc/claude-code/managed-settings.json) is
# root-owned and baked in — so the agent cannot disable its own gates, exactly as
# on the host. The container starts as root ONLY to run the entrypoint bootstrap
# (register the mounted repo, inject auth), then drops to `node` via gosu.
#
# Build from the repo root:  bash container/build.sh
FROM node:22-bookworm
# node:22-bookworm already ships a `node` user at UID 1000 → that is our agent.

# 1. OS deps: enforcement toolchain (jq, python3+yaml), git, gosu (privilege drop),
#    curl/ca-certs, and bubblewrap — Codex's sandbox prerequisite, used by the commit
#    gate's `codex review --sandbox read-only` (without it Codex falls back to a bundled
#    copy and warns on every gate run). Playwright's chromium system libs come with
#    `install --with-deps`.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git jq python3 python3-yaml curl ca-certificates gosu bubblewrap \
 && rm -rf /var/lib/apt/lists/*

# 2. The CLIs, pinned to the host versions.
RUN npm install -g @anthropic-ai/claude-code@2.1.215 @openai/codex@0.144.5

# 3. Bake Playwright + chromium at a world-readable path (UI verification works
#    immediately, no per-run download / npx-cache flakiness).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npx --yes playwright@1.61.1 install --with-deps chromium \
 && chmod -R a+rX /opt/ms-playwright

# 4. Root-owned enforcement. install.sh system copies the tree to /opt/night-shift
#    (root:root), installs gitleaks, writes /etc/claude-code/managed-settings.json
#    (allowManagedHooksOnly) + the integrity manifest. Build context is the repo root.
COPY enforcement/ /tmp/enforcement/
RUN bash /tmp/enforcement/install.sh system && rm -rf /tmp/enforcement

# 5. Entrypoint: root bootstrap -> drop to node.
COPY container/entrypoint.sh /usr/local/bin/ns-entrypoint
RUN chmod 755 /usr/local/bin/ns-entrypoint

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/ns-entrypoint"]
