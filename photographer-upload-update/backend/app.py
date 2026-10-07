"""Guest API.

POST /match   {code, image}  -> matches the selfie, starts a session, returns the gallery
GET  /gallery (Bearer token) -> returns the gallery for an existing session
POST /upload-urls {code, visibility, uploader, files} -> pre-signed S3 uploads for photographers

S3 layout (written by scripts/upload_photos.py):
  public/{thumb,web,original}/<photoId>.jpg   visible to every guest with the event code
  private/{thumb,web,original}/<photoId>.jpg  visible only to guests whose face is in it
Each face in a private photo is indexed in Rekognition with ExternalImageId = photoId,
so a face search returns photo IDs directly.
"""
import base64
import hmac
import json
import os
import re
import secrets
import time

import boto3
from botocore.config import Config

BUCKET = os.environ["BUCKET"]
TABLE = os.environ["TABLE"]
COLLECTION_ID = os.environ["COLLECTION_ID"]
EVENT_CODE = os.environ["EVENT_CODE"]
UPLOAD_CODE = os.environ.get("UPLOAD_CODE", "")
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "95"))
REGION = os.environ.get("AWS_REGION")

SESSION_DAYS = 30          # how long a guest stays signed in on their phone
URL_TTL = 2 * 60 * 60      # pre-signed image links last 2 hours
MAX_SELFIE_BYTES = 4_000_000
MAX_PHOTO_BYTES = 80 * 1024 * 1024   # largest single photo a photographer may upload
UPLOAD_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

s3 = boto3.client("s3", region_name=REGION,
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
rekognition = boto3.client("rekognition", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

_public_cache = {"ids": None, "at": 0}


def handler(event, _context):
    route = event.get("routeKey", "")
    try:
        if route == "POST /match":
            return match(event)
        if route == "GET /gallery":
            return gallery(event)
        if route == "POST /upload-urls":
            return upload_urls(event)
        return respond(404, {"error": "Not found."})
    except ClientFacingError as err:
        return respond(err.status, {"error": str(err)})


class ClientFacingError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def match(event):
    body = parse_body(event)

    code = str(body.get("code", "")).strip().upper()
    if not hmac.compare_digest(code.encode(), EVENT_CODE.strip().upper().encode()):
        raise ClientFacingError(403, "That event code isn't right. Check your invitation card.")

    image = body.get("image", "")
    if "," in image[:100]:  # strip a data: URL prefix if present
        image = image.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(image, validate=True)
    except Exception:
        raise ClientFacingError(400, "The selfie didn't upload correctly. Take it again.")
    if not image_bytes or len(image_bytes) > MAX_SELFIE_BYTES:
        raise ClientFacingError(400, "The selfie is too large. Take it again.")

    try:
        result = rekognition.search_faces_by_image(
            CollectionId=COLLECTION_ID,
            Image={"Bytes": image_bytes},
            FaceMatchThreshold=MATCH_THRESHOLD,
            MaxFaces=4096,
            QualityFilter="AUTO",
        )
    except rekognition.exceptions.InvalidParameterException:
        # Raised when no face is detected in the selfie.
        raise ClientFacingError(422, "No face found. Face the camera in good light and try again.")
    except rekognition.exceptions.ResourceNotFoundException:
        result = {"FaceMatches": []}  # no private photos uploaded yet

    private_ids = sorted({m["Face"]["ExternalImageId"] for m in result.get("FaceMatches", [])
                          if m["Face"].get("ExternalImageId")})

    session_id = secrets.token_urlsafe(32)
    table.put_item(Item={
        "sessionId": session_id,
        "privateIds": private_ids,
        "createdAt": int(time.time()),
        "expiresAt": int(time.time()) + SESSION_DAYS * 86400,
    })
    return respond(200, {"token": session_id, **build_gallery(private_ids)})


def gallery(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise ClientFacingError(401, "Take a selfie to see your photos.")

    item = table.get_item(Key={"sessionId": token}).get("Item")
    if not item or int(item.get("expiresAt", 0)) < time.time():
        raise ClientFacingError(401, "Your session ended. Take a new selfie to see your photos.")
    return respond(200, build_gallery(list(item.get("privateIds", []))))


def upload_urls(event):
    body = parse_body(event)
    code = str(body.get("code", "")).strip().upper()
    if not UPLOAD_CODE or not hmac.compare_digest(code.encode(), UPLOAD_CODE.strip().upper().encode()):
        raise ClientFacingError(403, "That upload code isn't right.")

    visibility = body.get("visibility")
    if visibility not in ("public", "private"):
        raise ClientFacingError(400, "Choose public or private.")
    uploader = re.sub(r"[^a-z0-9-]", "-", str(body.get("uploader", "photographer")).lower()).strip("-")[:40] or "photographer"

    files = body.get("files") or []
    if not isinstance(files, list) or not 1 <= len(files) <= 50:
        raise ClientFacingError(400, "Send between 1 and 50 files per request.")

    uploads = []
    for f in files:
        name = str(f.get("name", ""))
        ext = os.path.splitext(name)[1].lower()
        if ext not in UPLOAD_TYPES:
            uploads.append({"name": name, "error": "Only JPG and PNG files are accepted."})
            continue
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(name)[0])[:80] or "photo"
        # Random prefix stops two cameras' IMG_0001.jpg from overwriting each other before processing.
        key = f"incoming/{visibility}/{uploader}/{secrets.token_hex(4)}__{stem}{ext}"
        content_type = UPLOAD_TYPES[ext]
        post = s3.generate_presigned_post(
            Bucket=BUCKET, Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_PHOTO_BYTES]],
            ExpiresIn=3600,
        )
        uploads.append({"name": name, "url": post["url"], "fields": post["fields"]})
    return respond(200, {"uploads": uploads})


def build_gallery(private_ids):
    public_ids = list_public_ids()
    photos = [photo_entry("public", pid) for pid in public_ids]
    photos += [photo_entry("private", pid) for pid in private_ids]
    photos.sort(key=lambda p: p["id"])
    return {"photos": photos, "yourPhotoCount": len(private_ids), "publicCount": len(public_ids)}


def list_public_ids():
    # Cache for 60 seconds so a busy reception doesn't list the bucket on every request.
    if _public_cache["ids"] is not None and time.time() - _public_cache["at"] < 60:
        return _public_cache["ids"]
    ids = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="public/thumb/"):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if name.endswith(".jpg"):
                ids.append(name[:-4])
    _public_cache.update(ids=ids, at=time.time())
    return ids


def photo_entry(visibility, photo_id):
    def url(size, download=False):
        params = {"Bucket": BUCKET, "Key": f"{visibility}/{size}/{photo_id}.jpg"}
        if download:
            params["ResponseContentDisposition"] = f'attachment; filename="{photo_id}.jpg"'
        return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=URL_TTL)

    return {
        "id": photo_id,
        "yours": visibility == "private",
        "thumb": url("thumb"),
        "web": url("web"),
        "download": url("original", download=True),
    }


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ClientFacingError(400, "Request body must be JSON.")


def respond(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(payload),
    }
