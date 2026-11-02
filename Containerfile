# Fixed by digest so what enters the build is what a pull request reviewed.
# Dependabot proposes the new one. It must be the digest of the multi-architecture
# index of "latest", not the one of a single architecture, and "latest" has to stay:
# the dated tags of this registry only carry some architectures each, so one of
# them can lack arm64.
FROM registry.opensuse.org/opensuse/tumbleweed:latest@sha256:6fd5004406983816029c5cb235d10ebc15925cbd3288d795c3726257772d5ad4

# Tumbleweed is a rolling release: installing today's packages on top of an
# older base image leaves mismatched libraries (ffmpeg fails to start), so
# bring the base up to date first. This means the digest fixes the starting
# point, not the packages: this layer takes them from the repositories when it
# is built, and stays cached until the digest or this step changes. Updating the
# digest is therefore what brings a newer yt-dlp: YouTube answers 403 to stream
# URLs obtained with a yt-dlp that is a few months old.

# Packman Essentials provides a full-featured ffmpeg build (libx264, libx265, etc.)
# which the standard OpenSUSE package omits due to patent restrictions.
RUN zypper addrepo -cfp 90 \
        'https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/Essentials' \
        packman \
    && zypper --gpg-auto-import-keys refresh \
    && zypper dup -y \
    && zypper install -y \
        python3 \
        python3-pip \
        yt-dlp \
        tesseract-ocr \
        tesseract-ocr-traineddata-spa \
    && zypper install -y --from packman --allow-vendor-change \
        ffmpeg \
    && zypper clean -a

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

# Persistent data is bind-mounted from the host at runtime:
#   users.db, secret.key, lifts/
# The app runs as UID 1000 (podman --userns keep-id) and SQLite needs to create
# its journal next to users.db.
RUN mkdir -p lifts && chown 1000:1000 /app

EXPOSE 5000

CMD ["gunicorn", \
     "--workers", "1", \
     "--threads", "4", \
     "--bind", "0.0.0.0:5000", \
     "--timeout", "600", \
     "app:app"]
