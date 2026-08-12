FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# LibreOffice + fonts.
# fonts-liberation is the important one: Liberation Serif/Sans are METRIC-COMPATIBLE
# with Times New Roman / Arial, so LibreOffice substitutes them automatically and
# line breaks + page counts stay identical to the original Word file.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer \
      libreoffice-core \
      fonts-liberation \
      fonts-liberation2 \
      fonts-dejavu-core \
      fontconfig \
 && fc-cache -f \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# OPTIONAL: real Microsoft fonts (exact glyph shapes, not just matching metrics).
# Only needed if the client complains the PDF "looks slightly different".
# Requires the contrib repo and EULA preseeding.
#
# RUN sed -i 's/Components: main/Components: main contrib/' /etc/apt/sources.list.d/debian.sources \
#  && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
#  && apt-get update \
#  && apt-get install -y --no-install-recommends cabextract ttf-mscorefonts-installer \
#  && fc-cache -f \
#  && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Warm up the LibreOffice profile at build time so the first real request
# isn't paying the ~10s cold-start penalty.
RUN mkdir -p /tmp/lo_warmup \
 && soffice --headless --norestore \
      -env:UserInstallation=file:///tmp/lo_warmup \
      --convert-to pdf --outdir /tmp /app/warmup.txt || true

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
