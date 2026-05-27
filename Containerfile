FROM registry.opensuse.org/opensuse/tumbleweed

# Packman Essentials provides a full-featured ffmpeg build (libx264, libx265, etc.)
# which the standard OpenSUSE package omits due to patent restrictions.
RUN zypper addrepo -cfp 90 \
        'https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/Essentials' \
        packman-essentials \
    && zypper --gpg-auto-import-keys refresh \
    && zypper install -y \
        python3 \
        python3-pip \
        yt-dlp \
        ffmpeg \
        tesseract-ocr \
    && zypper clean -a

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

# Persistent data is bind-mounted from the host at runtime:
#   users.db, secret.key, lifts/
RUN mkdir -p lifts

EXPOSE 5000

CMD ["gunicorn", \
     "--workers", "1", \
     "--threads", "4", \
     "--bind", "0.0.0.0:5000", \
     "--timeout", "600", \
     "app:app"]
