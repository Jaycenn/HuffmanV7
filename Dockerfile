# Image for the hosted demonstration instance. The primary deployment target is
# Azure Container Apps, but the image remains portable across Linux hosts.
#
# Three things this file exists to guarantee, in order of how badly each one
# hurts if it is wrong:
#
#   1. The C++ core is compiled INTO the image and verified before the image is
#      accepted.  afc_native.py degrades to pure Python when the core is
#      missing -- right for a laptop with no compiler, wrong here, where the
#      fallback is ~12x slower with byte-identical output and nothing on screen
#      says so.  A participant would rate that on the questionnaire as "the
#      compressor is slow".
#   2. Exactly one gunicorn worker with one thread.  Finished results live in a
#      per-process dict (app.py:113) and decompressed originals exist ONLY
#      there, so a second worker 404s downloads at random.  The single sync
#      worker is also the only thing enforcing MAX_CONCURRENT_JOBS == 1.
#   3. A worker timeout long enough for a real compression.  The work happens
#      inside the request and includes a full decompress-and-compare round trip
#      before anything is saved; gunicorn's 30 s default would kill it midway.
FROM python:3.11-slim

# g++ is a build-time dependency only -- it is used in the RUN below and never
# at runtime, because the library it produces is baked into the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged UID rather than root.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /home/user/app

# Dependencies first, so editing application code does not reinstall them.
COPY --chown=user:user requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user:user . ./

# Build the native core, then refuse the image unless it loads and every rung
# of the ladder is reachable.  A short ladder is not a slower build -- it is a
# different experiment, because ladder_for() decides which preset wins.
RUN g++ -O3 -std=c++17 -shared -fPIC -pthread afc_native.cpp -o afc_kernels.so \
 && python tools/verify_native.py

# Azure ingress targets port 7860. Other hosts may inject $PORT, so honour it
# when present and otherwise use 7860.
EXPOSE 7860

# Shell form, because ${PORT} has to be expanded.  `exec` hands PID 1 to
# gunicorn so the host's shutdown signal reaches the worker instead of dying at
# the shell -- without it, every redeploy is a hard kill mid-request.
CMD exec gunicorn \
      --workers 1 \
      --threads 1 \
      --timeout 600 \
      --graceful-timeout 30 \
      --keep-alive 5 \
      --bind "0.0.0.0:${PORT:-7860}" \
      --access-logfile - \
      --error-logfile - \
      app:app
