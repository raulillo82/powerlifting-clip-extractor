FROM registry.opensuse.org/opensuse/tumbleweed

# Tumbleweed is a rolling release: installing today's packages on top of an
# older base image leaves mismatched libraries (ffmpeg fails to start), so
# bring the base up to date first. Build with --pull=newer so the cached
# layers, and with them yt-dlp, do not go stale: YouTube answers 403 to
# stream URLs obtained with a yt-dlp that is a few months old.

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
