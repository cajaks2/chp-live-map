import base64
import binascii
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request


ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
ALLOWED_VIDEO_TYPES = {"video/mp4": ".mp4"}
MAX_ATTACHMENTS = 3


class MediaValidationError(ValueError):
    def __init__(self, message, code="invalid_media"):
        super().__init__(message)
        self.code = code


def is_postgres(conn):
    return conn.__class__.__module__.startswith("psycopg")


def placeholder(conn):
    return "%s" if is_postgres(conn) else "?"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _base64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def create_upload_token(secret, comment_id, event_key, expires_at):
    payload = json.dumps(
        {"comment_id": int(comment_id), "event_key": event_key, "exp": int(expires_at)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _base64url(payload)
    signature = _base64url(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def validate_upload_token(secret, token, comment_id, event_key, now=None):
    try:
        encoded, signature = token.split(".", 1)
        expected = _base64url(
            hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (AttributeError, ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        raise MediaValidationError("Upload authorization is invalid.", "invalid_upload_token") from None
    current = int((now or dt.datetime.now(dt.timezone.utc)).timestamp())
    if (
        int(payload.get("comment_id", -1)) != int(comment_id)
        or payload.get("event_key") != event_key
        or int(payload.get("exp", 0)) < current
    ):
        raise MediaValidationError("Upload authorization is invalid or expired.", "invalid_upload_token")
    return payload


def clean_filename(value):
    name = os.path.basename(str(value or "upload"))
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return (name or "upload")[:120]


def validate_upload(payload, max_image_bytes, max_video_bytes, max_video_seconds):
    content_type = str(payload.get("content_type") or "").lower().split(";", 1)[0].strip()
    try:
        size = int(payload.get("size"))
    except (TypeError, ValueError):
        raise MediaValidationError("File size is required.", "invalid_size") from None
    if size < 1:
        raise MediaValidationError("The file is empty.", "invalid_size")
    if content_type in ALLOWED_IMAGE_TYPES:
        if size > max_image_bytes:
            raise MediaValidationError("Image is too large after compression.", "image_too_large")
        kind = "image"
        duration = None
        extension = ALLOWED_IMAGE_TYPES[content_type]
    elif content_type in ALLOWED_VIDEO_TYPES:
        if size > max_video_bytes:
            raise MediaValidationError("Video is too large.", "video_too_large")
        try:
            duration = float(payload.get("duration_seconds"))
        except (TypeError, ValueError):
            raise MediaValidationError("Video duration is required.", "invalid_duration") from None
        if duration <= 0 or duration > max_video_seconds:
            raise MediaValidationError(
                f"Video must be {max_video_seconds} seconds or shorter.", "video_too_long"
            )
        kind = "video"
        extension = ALLOWED_VIDEO_TYPES[content_type]
    else:
        raise MediaValidationError("Use a JPEG, PNG, WebP, or MP4 file.", "unsupported_type")
    return {
        "kind": kind,
        "content_type": content_type,
        "size": size,
        "duration_seconds": duration,
        "filename": clean_filename(payload.get("filename")),
        "extension": extension,
    }


def comment_for_upload(conn, comment_id, event_key):
    ph = placeholder(conn)
    row = conn.execute(
        f"SELECT id, status FROM incident_comments WHERE id = {ph} AND event_key = {ph}",
        (comment_id, event_key),
    ).fetchone()
    return dict(row) if row else None


def create_media_row(conn, comment_id, event_key, upload):
    comment = comment_for_upload(conn, comment_id, event_key)
    if not comment or comment["status"] != "pending":
        raise MediaValidationError("The pending comment was not found.", "comment_not_pending")
    ph = placeholder(conn)
    existing = conn.execute(
        f"SELECT kind FROM incident_media WHERE comment_id = {ph} AND status IN ('uploading', 'pending')",
        (comment_id,),
    ).fetchall()
    existing_kinds = [dict(row)["kind"] for row in existing]
    if len(existing_kinds) >= MAX_ATTACHMENTS:
        raise MediaValidationError("A comment can have at most three photos.", "too_many_files")
    if upload["kind"] == "video" and existing_kinds:
        raise MediaValidationError("A video must be submitted by itself.", "mixed_media")
    if upload["kind"] == "image" and "video" in existing_kinds:
        raise MediaValidationError("Photos cannot be added with a video.", "mixed_media")
    object_key = f"comments/{int(comment_id)}/{secrets.token_hex(16)}{upload['extension']}"
    values = (
        comment_id,
        event_key,
        "uploading",
        upload["kind"],
        object_key,
        upload["filename"],
        upload["content_type"],
        upload["size"],
        upload["duration_seconds"],
        now_iso(),
    )
    if is_postgres(conn):
        row = conn.execute(
            """
            INSERT INTO incident_media (
                comment_id, event_key, status, kind, object_key, original_filename,
                content_type, expected_size, duration_seconds, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            values,
        ).fetchone()
        media_id = dict(row)["id"]
    else:
        cursor = conn.execute(
            """
            INSERT INTO incident_media (
                comment_id, event_key, status, kind, object_key, original_filename,
                content_type, expected_size, duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        media_id = cursor.lastrowid
    return {"id": media_id, "object_key": object_key, **upload}


def media_row(conn, media_id, comment_id=None):
    ph = placeholder(conn)
    query = f"SELECT * FROM incident_media WHERE id = {ph}"
    params = [media_id]
    if comment_id is not None:
        query += f" AND comment_id = {ph}"
        params.append(comment_id)
    row = conn.execute(query, tuple(params)).fetchone()
    return dict(row) if row else None


def finalize_media_row(conn, media_id, comment_id, actual_size, actual_content_type):
    row = media_row(conn, media_id, comment_id)
    if not row or row["status"] != "uploading":
        raise MediaValidationError("Upload was not found.", "upload_not_found")
    comment = comment_for_upload(conn, comment_id, row["event_key"])
    if not comment or comment["status"] != "pending":
        raise MediaValidationError("The comment is no longer pending.", "comment_not_pending")
    actual_type = str(actual_content_type or "").lower().split(";", 1)[0].strip()
    if int(actual_size) != int(row["expected_size"]) or actual_type != row["content_type"]:
        raise MediaValidationError("Uploaded file did not match its declaration.", "upload_mismatch")
    ph = placeholder(conn)
    conn.execute(
        f"UPDATE incident_media SET status = 'pending', actual_size = {ph}, uploaded_at = {ph} WHERE id = {ph}",
        (actual_size, now_iso(), media_id),
    )
    row["status"] = "pending"
    row["actual_size"] = actual_size
    return row


def media_for_comments(conn, comment_ids, statuses=None):
    if not comment_ids:
        return {}
    ph = placeholder(conn)
    id_marks = ", ".join([ph] * len(comment_ids))
    params = list(comment_ids)
    status_clause = ""
    if statuses:
        status_marks = ", ".join([ph] * len(statuses))
        status_clause = f" AND status IN ({status_marks})"
        params.extend(statuses)
    rows = conn.execute(
        f"SELECT * FROM incident_media WHERE comment_id IN ({id_marks}){status_clause} ORDER BY id",
        tuple(params),
    ).fetchall()
    result = {int(comment_id): [] for comment_id in comment_ids}
    for raw in rows:
        row = dict(raw)
        result.setdefault(int(row["comment_id"]), []).append(row)
    return result


def public_media(row, admin=False, admin_base="/admin/media"):
    prefix = admin_base.rstrip("/") if admin else "/api/v1/media"
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "content_type": row["content_type"],
        "filename": row["original_filename"],
        "size": int(row.get("actual_size") or row["expected_size"]),
        "duration_seconds": row.get("duration_seconds"),
        "url": f"{prefix}/{int(row['id'])}",
    }


def set_media_status(conn, comment_id, status):
    if status not in {"approved", "rejected"}:
        raise ValueError("status must be approved or rejected")
    ph = placeholder(conn)
    eligible = "('pending')" if status == "approved" else "('pending', 'approved')"
    conn.execute(
        f"UPDATE incident_media SET status = {ph} WHERE comment_id = {ph} AND status IN {eligible}",
        (status, comment_id),
    )


def media_keys_for_comment(conn, comment_id):
    ph = placeholder(conn)
    rows = conn.execute(
        f"SELECT object_key FROM incident_media WHERE comment_id = {ph}", (comment_id,)
    ).fetchall()
    return [dict(row)["object_key"] for row in rows]


def approved_media_row(conn, media_id):
    ph = placeholder(conn)
    row = conn.execute(
        f"""
        SELECT m.* FROM incident_media m
        JOIN incident_comments c ON c.id = m.comment_id
        WHERE m.id = {ph} AND m.status = 'approved' AND c.status = 'approved'
        """,
        (media_id,),
    ).fetchone()
    return dict(row) if row else None


class R2MediaStore:
    def __init__(self, account_id, access_key_id, secret_access_key, bucket):
        self.account_id = account_id
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket = bucket
        self.host = f"{account_id}.r2.cloudflarestorage.com"

    @staticmethod
    def _sign(key, message):
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    def presigned_url(self, method, object_key, expires=900, now=None, content_type=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = f"{date_stamp}/auto/s3/aws4_request"
        credential = f"{self.access_key_id}/{scope}"
        canonical_uri = "/" + urllib.parse.quote(self.bucket, safe="~") + "/" + urllib.parse.quote(
            object_key, safe="/~"
        )
        signed_header_values = {"host": self.host}
        if content_type:
            signed_header_values["content-type"] = content_type
        signed_headers = ";".join(sorted(signed_header_values))
        canonical_headers = "".join(
            f"{name}:{signed_header_values[name]}\n" for name in sorted(signed_header_values)
        )
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(int(expires)),
            "X-Amz-SignedHeaders": signed_headers,
        }
        canonical_query = urllib.parse.urlencode(sorted(query.items()), quote_via=urllib.parse.quote, safe="~")
        canonical_request = "\n".join(
            [method.upper(), canonical_uri, canonical_query, canonical_headers, signed_headers, "UNSIGNED-PAYLOAD"]
        )
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        date_key = self._sign(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp)
        region_key = self._sign(date_key, "auto")
        service_key = self._sign(region_key, "s3")
        signing_key = self._sign(service_key, "aws4_request")
        query["X-Amz-Signature"] = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        final_query = urllib.parse.urlencode(sorted(query.items()), quote_via=urllib.parse.quote, safe="~")
        return f"https://{self.host}{canonical_uri}?{final_query}"

    def head(self, object_key):
        request = urllib.request.Request(self.presigned_url("HEAD", object_key, expires=60), method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return {
                    "size": int(response.headers.get("Content-Length", "0")),
                    "content_type": response.headers.get("Content-Type", ""),
                }
        except urllib.error.HTTPError as exc:
            raise MediaValidationError("R2 could not verify the upload.", "upload_not_found") from exc

    def delete(self, object_key):
        request = urllib.request.Request(self.presigned_url("DELETE", object_key, expires=60), method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=15):
                return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
