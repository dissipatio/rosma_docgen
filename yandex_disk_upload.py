"""
Yandex Disk upload helper for ROSMA document generation.

Mirrors the upload_to_yandex_disk() flow already used in
gmail_airtable_sync.py (get-upload-URL -> PUT bytes -> publish -> get
public_url), so both services behave identically against the same Disk
account. Kept as a separate module rather than copy-pasted so both
services can eventually import a shared package if that gets set up.

Env vars (same names as gmail_airtable_sync.py):
    YANDEX_DISK_TOKEN    OAuth token, "y0_..." (see project notes: the
                         Yandex "Verification code" redirect page shows
                         this directly -- it's the final token already)
    YANDEX_DISK_FOLDER   Base folder on Disk, e.g. "/rosma_generated_documents"
                         (separate from /rosma_email_attachments)
"""

import os
import requests

YANDEX_DISK_TOKEN = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
YANDEX_DISK_FOLDER = os.environ.get("YANDEX_DISK_FOLDER", "/rosma_generated_documents").strip()

DISK_API = "https://cloud-api.yandex.net/v1/disk/resources"


def _headers():
    if not YANDEX_DISK_TOKEN:
        raise RuntimeError("YANDEX_DISK_TOKEN is not set")
    return {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}


def upload_and_publish(local_path, remote_filename):
    """
    Uploads local_path to YANDEX_DISK_FOLDER/remote_filename, publishes it,
    and returns the public URL. Overwrites if a file with the same name
    already exists (so re-generating a document for the same inquiry
    replaces the old link rather than accumulating copies).
    """
    remote_path = f"{YANDEX_DISK_FOLDER.rstrip('/')}/{remote_filename}"

    # 1. Get upload URL
    r = requests.get(
        f"{DISK_API}/upload",
        headers=_headers(),
        params={"path": remote_path, "overwrite": "true"},
        timeout=30,
    )
    r.raise_for_status()
    upload_url = r.json()["href"]

    # 2. Upload the file bytes
    with open(local_path, "rb") as f:
        put_resp = requests.put(upload_url, data=f, timeout=120)
    put_resp.raise_for_status()

    # 3. Publish it (makes it publicly accessible)
    pub_resp = requests.put(
        f"{DISK_API}/publish",
        headers=_headers(),
        params={"path": remote_path},
        timeout=30,
    )
    pub_resp.raise_for_status()

    # 4. Get the public link
    meta = requests.get(
        DISK_API,
        headers=_headers(),
        params={"path": remote_path, "fields": "public_url"},
        timeout=30,
    ).json()
    public_url = meta.get("public_url")
    if not public_url:
        raise RuntimeError(f"Yandex Disk did not return a public_url for {remote_path}: {meta}")
    return public_url


def ensure_folder_exists():
    """Creates YANDEX_DISK_FOLDER if it doesn't already exist. Safe to call
    every run -- Yandex returns 409 if it's already there, which we ignore."""
    r = requests.put(DISK_API, headers=_headers(), params={"path": YANDEX_DISK_FOLDER}, timeout=30)
    if r.status_code not in (201, 409):
        r.raise_for_status()
