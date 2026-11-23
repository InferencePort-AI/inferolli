FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates tzdata ffmpeg curl wget \
        fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 \
        libnss3 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
        xvfb pulseaudio chromium xdotool wmctrl scrot \
    && rm -rf /var/lib/apt/lists/*

# google chrome (for the browse/screen tools)
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/chrome.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb; rm -f /tmp/chrome.deb

# the desktop stack: XFCE4 + x11vnc (RO/RW instances) + novnc + cloudflared
RUN apt-get update && apt-get install -y --no-install-recommends \
        xfce4 xfce4-terminal x11vnc novnc websockify dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

# cloudflared — trycloudflare quick tunnels (public links, no account)
RUN wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# opencode — the bot's hands (headless agent: bash, files, git, installs)
# PINNED to the v2 binary (copied from the VPS — same version the tools were built against)
COPY --chmod=755 opencode-bin /usr/local/bin/opencode
RUN opencode --version

# the workspace where opencode does its computer work + its config (as the bot user)
COPY opencode.json /home/user/.config/opencode/opencode.json
RUN mkdir -p /workspace /home/user/.config/opencode \
    && chown -R 1000:1000 /workspace /home/user/.config

WORKDIR /bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# the sandbox runs as uid 1000 ("user") — give it ownership so the bot can
# write bot.log and the memory cortex files
RUN chown -R 1000:1000 /bot

# tonearm — the music studio, IN this sandbox (drives Apple Music through CDP
# on the bundled chromium). The Apple Music session profile is NOT baked in;
# start.sh restores it from the nightly S3 backup (cookies are credentials).
RUN git clone --depth 1 https://github.com/CloudCompile/tonearm.git /home/user/tonearm \
    && pip install --no-cache-dir -r /home/user/tonearm/requirements.txt \
    && chown -R 1000:1000 /home/user/tonearm

# start.sh: restores latest brain backup, then runs the bot under a
# crash-restarting supervisor loop (no systemd inside the microVM)
COPY start.sh /start.sh
# the template's start command is /bot/start.sh (that is the copy the bot
# actually runs), and `COPY . .` preserves the source mode — which was 0644, so
# the build failed with "start command failed: exit status 126". Chmod the one
# that is executed, not just the legacy root copy.
RUN chmod +x /start.sh /bot/start.sh

# the real bot is launched via exec (secure envs don't reach the start command)
CMD ["sleep", "infinity"]
