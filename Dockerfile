# OpenCode Agent — Sandboxed Docker Image for OpenWebUI
# 
# This image packages the opencode-ai CLI so it can run headless
# (opencode serve) inside a container. Each OpenWebUI chat gets its own
# isolated container with a dedicated workspace.
#
# Build:
#   docker build -t opencode-pipe:latest .
#
# Run manually (for testing):
#   docker run -it --rm -p 4096:4096 -v $(pwd)/workspace:/workspace opencode-pipe:latest

FROM node:20-slim

# Install common dependencies that coding agents often need
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    bash \
    python3 \
    python3-pip \
    make \
    g++ \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install opencode-ai globally
RUN npm install -g opencode-ai

# Set up workspace and config directories
RUN mkdir -p /workspace /root/.local/share/opencode /root/.config/opencode

WORKDIR /workspace

# Quick smoke test
RUN opencode --version

# Expose the default OpenCode server port
EXPOSE 4096

# Default: start the headless server binding to all interfaces
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
