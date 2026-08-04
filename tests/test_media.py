import datetime as dt
import urllib.parse

import pytest

from media import (
    MediaValidationError,
    R2MediaStore,
    create_upload_token,
    validate_upload,
    validate_upload_token,
)


def test_upload_token_is_scoped_and_expires():
    now = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.timezone.utc)
    token = create_upload_token("secret", 42, "LACC|2026-08-04|123", int(now.timestamp()) + 60)
    payload = validate_upload_token("secret", token, 42, "LACC|2026-08-04|123", now=now)
    assert payload["comment_id"] == 42

    with pytest.raises(MediaValidationError) as mismatched:
        validate_upload_token("secret", token, 43, "LACC|2026-08-04|123", now=now)
    assert mismatched.value.code == "invalid_upload_token"

    with pytest.raises(MediaValidationError):
        validate_upload_token(
            "secret", token, 42, "LACC|2026-08-04|123", now=now + dt.timedelta(minutes=2)
        )


def test_r2_presigned_url_uses_short_lived_sigv4_query():
    store = R2MediaStore("account", "access", "top-secret", "crestmap-media")
    url = store.presigned_url(
        "PUT",
        "comments/42/photo name.webp",
        expires=300,
        now=dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.timezone.utc),
        content_type="image/webp",
    )
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "account.r2.cloudflarestorage.com"
    assert parsed.path == "/crestmap-media/comments/42/photo%20name.webp"
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-SignedHeaders"] == ["content-type;host"]
    assert len(query["X-Amz-Signature"][0]) == 64
    assert "top-secret" not in url


def test_media_validation_rejects_large_or_long_uploads():
    photo = validate_upload(
        {"filename": "photo.jpg", "content_type": "image/jpeg", "size": 100},
        max_image_bytes=100,
        max_video_bytes=1000,
        max_video_seconds=60,
    )
    assert photo["kind"] == "image"

    with pytest.raises(MediaValidationError) as too_long:
        validate_upload(
            {
                "filename": "clip.mp4",
                "content_type": "video/mp4",
                "size": 500,
                "duration_seconds": 61,
            },
            max_image_bytes=100,
            max_video_bytes=1000,
            max_video_seconds=60,
        )
    assert too_long.value.code == "video_too_long"
