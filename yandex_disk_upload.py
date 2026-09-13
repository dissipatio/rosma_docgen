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
import time
import requests

YANDEX_DISK_TOKEN = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
YANDEX_DISK_FOLDER = os.environ.get("YANDEX_DISK_FOLDER", "/rosma_generated_documents").strip()

DISK_API = "https://cloud-api.yandex.net/v1/disk/resources"

# Yandex Disk returns 423 LOCKED when an async operation (publish, move,
# delete) is still finishing on the same path -- this is a transient
# collision, not a real conflict, and normally clears within a couple of
# seconds. It got more likely to show up once a generation run started
# uploading two files (PDF + DOCX) through the same folder instead of one,
# and gets worse still if multiple records generate back-to-back. Retried
# here rather than left to fail the whole run.
_RETRY_DELAYS_SECONDS = [1, 2, 4, 8]


def _headers():
    if not YANDEX_DISK_TOKEN:
        raise RuntimeError("YANDEX_DISK_TOKEN is not set")
    return {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}


def _request_with_retry(method, url, **kwargs):
    """Like requests.request(), but retries a few times (with backoff) if
    Yandex answers 423 LOCKED. Any other status -- including a real error
    -- is returned as-is on the first try, for the caller's own
    raise_for_status() to handle."""
    resp = requests.request(method, url, **kwargs)
    for delay in _RETRY_DELAYS_SECONDS:
        if resp.status_code != 423:
            return resp
        time.sleep(delay)
        resp = requests.request(method, url, **kwargs)
    return resp


def upload_and_publish(local_path, remote_filename):
    """
    Uploads local_path to YANDEX_DISK_FOLDER/remote_filename, publishes it,
    and returns the public URL. Overwrites if a file with the same name
    already exists (so re-generating a document for the same inquiry
    replaces the old link rather than accumulating copies).
    """
    remote_path = f"{YANDEX_DISK_FOLDER.rstrip('/')}/{remote_filename}"

    # 1. Get upload URL
    r = _request_with_retry(
        "GET",
        f"{DISK_API}/upload",
        headers=_headers(),
        params={"path": remote_path, "overwrite": "true"},
        timeout=30,
    )
    r.raise_for_status()
    upload_url = r.json()["href"]

    # 2. Upload the file bytes
    with open(local_path, "rb") as f:
        put_resp = _request_with_retry("PUT", upload_url, data=f, timeout=120)
    put_resp.raise_for_status()

    # 3. Publish it (makes it publicly accessible)
    pub_resp = _request_with_retry(
        "PUT",
        f"{DISK_API}/publish",
        headers=_headers(),
        params={"path": remote_path},
        timeout=30,
    )
    pub_resp.raise_for_status()

    # 4. Get the public link
    meta_resp = _request_with_retry(
        "GET",
        DISK_API,
        headers=_headers(),
        params={"path": remote_path, "fields": "public_url"},
        timeout=30,
    )
    meta_resp.raise_for_status()
    meta = meta_resp.json()
    public_url = meta.get("public_url")
    if not public_url:
        raise RuntimeError(f"Yandex Disk did not return a public_url for {remote_path}: {meta}")
    return public_url


_folder_confirmed = False  # module-level cache -- see ensure_folder_exists()


def ensure_folder_exists():
    """Creates YANDEX_DISK_FOLDER if it doesn't already exist. Only actually
    hits the network once per process (cached in _folder_confirmed) -- it
    was previously called on every single generation, which is almost
    always a wasted round-trip once the folder exists, and every extra hit
    on the same path is one more chance to collide with an in-progress
    publish/upload and get a 423 (see _request_with_retry above, which
    still covers this if the cache is ever wrong -- e.g. a fresh process
    that hasn't confirmed yet racing another that's mid-upload)."""
    global _folder_confirmed
    if _folder_confirmed:
        return
    r = _request_with_retry(
        "PUT", DISK_API, headers=_headers(), params={"path": YANDEX_DISK_FOLDER}, timeout=30
    )
    if r.status_code not in (201, 409):
        r.raise_for_status()
    _folder_confirmed = True
