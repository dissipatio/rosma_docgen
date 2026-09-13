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
# collision, not a real conflict. In practice this clears within tens of
# seconds, not the couple of seconds originally assumed here: a real-world
# trace showed a DOCX upload 423-locked for the full length of the
# previous (much shorter) retry budget, immediately after the PDF upload
# to the same folder had just completed -- consistent with Yandex holding
# a brief folder-level lock while it settles a just-finished write, not
# with anything actually wrong. /generate responds to its caller
# immediately (202 Accepted) and does the real work afterward, so there's
# no live request being held open here -- affording a much longer budget
# than would be reasonable to block on.
_RETRY_DELAYS_SECONDS = [2, 4, 8, 15, 30]


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


def upload_and_publish(local_path, remote_filename, _fallback=False):
    """
    Uploads local_path to YANDEX_DISK_FOLDER/remote_filename, publishes it,
    and returns the public URL. Overwrites if a file with the same name
    already exists (so re-generating a document for the same inquiry
    replaces the old link rather than accumulating copies).

    If the upload step is STILL 423 LOCKED after the full retry budget
    above, that's no longer an ordinary transient collision -- in
    practice, this means one specific resource is stuck (most likely left
    over from an earlier interrupted attempt, from before this module had
    the folder-check and retry logic it has now), and no amount of
    retrying clears it; a real-world trace showed the exact same path
    still 423-locked after 60+ seconds of retries across multiple separate
    runs, while a different filename (the PDF, right next to it) uploaded
    fine every time. Rather than let one permanently stuck path block this
    file forever, falls back ONCE to a timestamp-suffixed filename so the
    caller still gets a working link. The stuck original is left alone --
    delete it by hand in the Yandex Disk web UI if you want the stable
    filename back; a fresh path won't inherit whatever state it's stuck
    in.
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
    if r.status_code == 423 and not _fallback:
        name, ext = os.path.splitext(remote_filename)
        fallback_filename = f"{name}_{int(time.time())}{ext}"
        return upload_and_publish(local_path, fallback_filename, _fallback=True)
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
    """Confirms YANDEX_DISK_FOLDER exists, creating it only if it genuinely
    doesn't. Only actually hits the network once per process (cached in
    _folder_confirmed).

    Checks with a plain GET first rather than unconditionally PUT-creating.
    A metadata GET is read-only and never locks the resource; PUT-create on
    a path that already exists is what was actually triggering the
    persistent 423s in practice -- it shows up when several generation
    runs overlap and each tries to "create" the same already-existing
    folder at the same moment. Since this folder has existed since the
    very first successful generation, that create call was pure
    contention with no upside; only take the PUT path (still through
    _request_with_retry, as a safety net) when the GET actually comes back
    404."""
    global _folder_confirmed
    if _folder_confirmed:
        return

    get_resp = _request_with_retry(
        "GET", DISK_API, headers=_headers(), params={"path": YANDEX_DISK_FOLDER}, timeout=30
    )
    if get_resp.status_code == 200:
        _folder_confirmed = True
        return

    # Not found (or some other non-200) -- actually try to create it.
    put_resp = _request_with_retry(
        "PUT", DISK_API, headers=_headers(), params={"path": YANDEX_DISK_FOLDER}, timeout=30
    )
    if put_resp.status_code not in (201, 409):
        put_resp.raise_for_status()
    _folder_confirmed = True
