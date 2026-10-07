"""Processes photos that photographers upload to incoming/<visibility>/<uploader>/<rand>__<name>.

For each new file: make thumb (480px) and web (2560px) versions, index faces for private
photos, write everything to <visibility>/{thumb,web,original}/<photoId>.jpg, then delete
the incoming copy. Uses the same photo IDs as scripts/upload_photos.py, so a photo sent
through both paths is only published once.
"""
import hashlib
import io
import os
import re
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 200_000_000  # allow very high-resolution camera files

COLLECTION_ID = os.environ["COLLECTION_ID"]
retry = Config(retries={"max_attempts": 10, "mode": "adaptive"})  # absorbs Rekognition throttling in big batches
s3 = boto3.client("s3", config=retry)
rekognition = boto3.client("rekognition", config=retry)


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        process(bucket, key)


def process(bucket, key):
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "incoming" or parts[1] not in ("public", "private"):
        print(f"Ignoring unexpected key {key}")
        return
    visibility, uploader, filename = parts[1], parts[2], parts[3]
    original_name = filename.split("__", 1)[-1]

    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    pid = photo_id(original_name, data)

    if exists(bucket, f"{visibility}/thumb/{pid}.jpg"):
        print(f"{key}: already published as {pid}, removing duplicate")
        s3.delete_object(Bucket=bucket, Key=key)
        return

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    thumb = resized_jpeg(img, 480, 78)
    web = resized_jpeg(img, 2560, 85)
    if original_name.lower().endswith((".jpg", ".jpeg")):
        original = data
    else:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95)
        original = buf.getvalue()

    faces = 0
    if visibility == "private":
        resp = index_faces(web, pid)
        faces = len(resp.get("FaceRecords", []))

    # Thumb last: its presence is what marks a photo as published.
    for size, body in (("original", original), ("web", web), ("thumb", thumb)):
        s3.put_object(Bucket=bucket, Key=f"{visibility}/{size}/{pid}.jpg", Body=body, ContentType="image/jpeg")
    s3.delete_object(Bucket=bucket, Key=key)
    print(f"{key}: published {visibility} photo {pid} from {uploader}, {faces} face(s) indexed")


def index_faces(image_bytes, pid):
    kwargs = dict(CollectionId=COLLECTION_ID, Image={"Bytes": image_bytes}, ExternalImageId=pid,
                  MaxFaces=100, QualityFilter="AUTO", DetectionAttributes=[])
    try:
        return rekognition.index_faces(**kwargs)
    except rekognition.exceptions.ResourceNotFoundException:
        try:
            rekognition.create_collection(CollectionId=COLLECTION_ID)
        except rekognition.exceptions.ResourceAlreadyExistsException:
            pass
        return rekognition.index_faces(**kwargs)


def photo_id(name, data):
    # Must match scripts/upload_photos.py: <sanitised file stem>_<first 8 hex of sha1>
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(name)[0])[:60]
    return f"{stem}_{hashlib.sha1(data).hexdigest()[:8]}"


def resized_jpeg(img, max_side, quality):
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    copy.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def exists(bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
