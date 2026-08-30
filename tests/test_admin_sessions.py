import json
import re
import shutil
import subprocess
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import admin_sessions as sessions
from app import ADMIN_SESSION_COOKIE, WebSettings, create_app
from generate_live_map import admin_activity_script
from scrape_chp_traffic import connect_database


@pytest.fixture
def settings(tmp_path):
    return WebSettings(
        database=tmp_path / "sessions.sqlite",
        admin_username="admin", admin_password="test-password",
        admin_session_secret="test-signing-secret",
    )


def login(client, remembered=False):
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-password", "remember": "yes" if remembered else "no"},
        headers={"Origin": "http://testserver"}, follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def cookie_headers(token, **extra):
    return {"Cookie": f"{ADMIN_SESSION_COOKIE}={token}", **extra}


def test_unique_hashed_sessions_and_credential_invalidation(settings):
    with connect_database(settings.database) as conn:
        token, row = sessions.create_session(conn, settings, now=1000)
        token2, row2 = sessions.create_session(conn, settings, now=1000)
        assert token != token2
        assert row["session_id"] != row2["session_id"]
        assert token not in str(conn.execute("SELECT * FROM admin_sessions").fetchall())
        assert row["token_hash"] == sessions.token_hash(token)
        assert sessions.load_session(conn, settings, token, now=1001)
        assert not sessions.load_session(conn, settings, token + "tampered", now=1001)
        for changed in (
            replace(settings, admin_password="changed"),
            replace(settings, admin_username="changed"),
            replace(settings, admin_session_secret="changed"),
        ):
            assert not sessions.load_session(conn, changed, token, now=1001)
        # A valid signature alone is insufficient; old/unregistered tokens fail closed.
        unregistered = sessions.create_admin_session_token(settings, now=1000)
        assert sessions.valid_admin_session_token(settings, unregistered, now=1001)
        assert not sessions.load_session(conn, settings, unregistered, now=1001)


def test_standard_idle_renewal_stops_at_absolute_limit(settings):
    with connect_database(settings.database) as conn:
        token, row = sessions.create_session(conn, settings, now=1000)
        assert row["expires_at"] == 1000 + 8 * 3600
        assert row["absolute_expires_at"] == 1000 + 24 * 3600
        for hour in (7, 14, 21, 23):
            row = sessions.renew_session(conn, settings, token, now=1000 + hour * 3600)
            assert row["expires_at"] == 1000 + min(hour + 8, 24) * 3600
        assert not sessions.renew_session(conn, settings, token, now=1000 + 24 * 3600)
        idle_token, _ = sessions.create_session(conn, settings, now=2000)
        assert not sessions.renew_session(conn, settings, idle_token, now=2000 + 8 * 3600)


def test_remembered_session_is_fixed_30_days(settings):
    with connect_database(settings.database) as conn:
        token, row = sessions.create_session(conn, settings, remembered=True, now=1000)
        expiry = 1000 + 30 * 86400
        assert row["expires_at"] == row["absolute_expires_at"] == expiry
        row = sessions.renew_session(conn, settings, token, now=expiry - 1)
        assert row["expires_at"] == expiry
        assert not sessions.load_session(conn, settings, token, now=expiry)


def test_revocation_cannot_be_renewed_and_is_scoped_to_admin(settings):
    with connect_database(settings.database) as conn:
        token, row = sessions.create_session(conn, settings, now=1000)
        other_settings = replace(settings, admin_username="another-admin")
        other_token, other = sessions.create_session(conn, other_settings, now=1000)
        sessions.revoke_sessions(conn, settings, session_id=other["session_id"])
        assert sessions.load_session(conn, other_settings, other_token, now=1001)
        sessions.revoke_sessions(conn, settings, session_id=row["session_id"])
        assert not sessions.renew_session(conn, settings, token, now=1001)
        assert sessions.load_session(conn, other_settings, other_token, now=1001)


def test_login_checkbox_cookie_lifetimes_and_escaped_device(settings):
    with TestClient(create_app(settings)) as client:
        assert "X-Crestmap-Activity" not in client.get("/").text
        page = client.get("/admin/login").text
        assert 'name="remember"' in page
        assert "main { box-sizing: border-box;" in page
        assert "Remember this device for 30 days" in page
        assert "Max-Age=28800" in login(client).headers["set-cookie"]
        old_token = client.cookies.get(ADMIN_SESSION_COOKIE)
        client.headers["User-Agent"] = '<script>alert("device")</script>'
        response = login(client, remembered=True)
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        page = client.get("/admin/sessions")
        assert page.headers["cache-control"] == "no-store"
        assert "This device · Remembered" in page.text
        assert '&lt;script&gt;alert(&quot;device&quot;)&lt;/script&gt;' in page.text
        assert '<script>alert("device")</script>' not in page.text
        assert "token_hash" not in page.text
        assert not client.get("/admin/session", headers=cookie_headers(old_token)).json()["authenticated"]
        assert "Sessions / remembered devices" in client.get("/admin/comments").text


def test_polling_never_renews_but_activity_does(settings, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(sessions.time, "time", lambda: clock[0])
    with TestClient(create_app(settings)) as client:
        login(client)
        token = client.cookies.get(ADMIN_SESSION_COOKIE)
        start = clock[0]
        clock[0] += 7 * 3600
        for path in ("/", "/status.json", "/incidents.json", "/admin/session", "/admin/comments", "/admin/sessions"):
            response = client.get(path)
            assert response.status_code == 200
            assert "set-cookie" not in response.headers
        with connect_database(settings.database) as conn:
            row = sessions.load_session(conn, settings, token)
            assert row["expires_at"] == start + 8 * 3600
            assert row["last_activity_at"] == start
        response = client.post("/admin/session/activity", headers={
            "Origin": "http://testserver", "X-Crestmap-Activity": "1",
        })
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        with connect_database(settings.database) as conn:
            row = sessions.load_session(conn, settings, token)
            assert row["expires_at"] == start + 15 * 3600
        clock[0] = start + 15 * 3600
        response = client.post("/admin/session/activity", headers=cookie_headers(
            token, Origin="http://testserver", **{"X-Crestmap-Activity": "1"},
        ))
        assert response.status_code == 401


def test_sessions_survive_app_restart_and_logout_revokes_replay(settings):
    with TestClient(create_app(settings)) as client:
        login(client, remembered=True)
        token = client.cookies.get(ADMIN_SESSION_COOKIE)
    with TestClient(create_app(settings)) as client:
        headers = cookie_headers(token, Origin="http://testserver")
        assert client.get("/admin/session", headers=headers).json()["authenticated"]
        response = client.post("/admin/logout", headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert not client.get("/admin/session", headers=headers).json()["authenticated"]
        assert client.post("/admin/session/activity", headers={
            **headers, "X-Crestmap-Activity": "1",
        }).status_code == 401


def test_revoke_one_others_and_everywhere(settings):
    with TestClient(create_app(settings)) as client:
        login(client, remembered=True)
        own = client.cookies.get(ADMIN_SESSION_COOKIE)
        with connect_database(settings.database) as conn:
            other, row = sessions.create_session(conn, settings, remembered=True)
            third, _ = sessions.create_session(conn, settings)
        response = client.post("/admin/sessions", data={"action": "revoke", "session_id": row["session_id"]},
                               headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert response.status_code == 303
        assert not client.get("/admin/session", headers=cookie_headers(other)).json()["authenticated"]
        assert client.get("/admin/session", headers=cookie_headers(third)).json()["authenticated"]
        client.post("/admin/sessions", data={"action": "others"}, headers={"Origin": "http://testserver"})
        assert not client.get("/admin/session", headers=cookie_headers(third)).json()["authenticated"]
        assert client.get("/admin/session", headers=cookie_headers(own)).json()["authenticated"]
        response = client.post("/admin/sessions", data={"action": "all"},
                               headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert not client.get("/admin/session", headers=cookie_headers(own)).json()["authenticated"]


def test_session_actions_require_authentication_and_same_origin(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/admin/sessions", follow_redirects=False).status_code == 303
        assert client.post("/admin/sessions", data={"action": "all"}).status_code == 401
        login(client)
        assert client.post("/admin/session/activity").status_code == 403
        for origin in ("http://testserver.evil.example", "https://evil.example", "null"):
            assert client.post("/admin/session/activity", headers={
                "Origin": origin, "X-Crestmap-Activity": "1",
            }).status_code == 403
            assert client.post("/admin/sessions", data={"action": "all"}, headers={"Origin": origin}).status_code == 403
        assert client.post("/admin/sessions", data={"action": "all"}).status_code == 403
        assert client.get("/admin/session").json()["authenticated"]


def test_config_and_base_path(settings, monkeypatch):
    for name, value in {"ADMIN_SESSION_HOURS": "168", "ADMIN_SESSION_MAX_HOURS": "720", "ADMIN_REMEMBER_DAYS": "60"}.items():
        monkeypatch.setenv(name, value)
    configured = WebSettings.from_env()
    assert (configured.admin_session_hours, configured.admin_session_max_hours, configured.admin_remember_days) == (168, 720, 60)
    with TestClient(create_app(replace(settings, base_path="/crest"))) as client:
        assert client.get("/crest/admin/login").status_code == 200
        response = client.post("/crest/admin/login", data={"username": "admin", "password": "test-password"}, follow_redirects=False)
        assert response.headers["location"] == "/crest"
        assert 'fetch("/crest/admin/session/activity"' in client.get("/crest").text
        assert '/crest/admin/sessions' in client.get("/crest/admin/comments").text
        assert client.get("/crest/admin/sessions").status_code == 200


def test_activity_script_requires_real_visible_interaction():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the browser-script unit test")
    script = re.search(r"<script>(.*)</script>", admin_activity_script("/"), re.S).group(1)
    harness = r"""
      const vm = require('node:vm');
      const assert = require('node:assert/strict');
      const handlers = {};
      let now = 1000000;
      let calls = 0;
      let status = 200;
      const document = {visibilityState: 'visible', addEventListener: (name, fn) => handlers[name] = fn};
      vm.runInNewContext(SCRIPT, {
        document, Date: {now: () => now},
        fetch: async (url, options) => { calls++; assert.equal(url, '/admin/session/activity');
          assert.equal(options.method, 'POST'); return {status}; }
      });
      const flush = () => new Promise(resolve => setImmediate(resolve));
      (async () => {
        assert.equal(calls, 0); // Loading and polling have no renewal timer.
        handlers.pointerdown({isTrusted: false}); await flush(); assert.equal(calls, 0);
        document.visibilityState = 'hidden';
        handlers.pointerdown({isTrusted: true}); await flush(); assert.equal(calls, 0);
        document.visibilityState = 'visible';
        handlers.pointerdown({isTrusted: true}); await flush(); assert.equal(calls, 1);
        handlers.keydown({isTrusted: true}); await flush(); assert.equal(calls, 1);
        now += 300001; handlers.wheel({isTrusted: true}); await flush(); assert.equal(calls, 2);
        status = 401; now += 300001; handlers.touchstart({isTrusted: true}); await flush(); assert.equal(calls, 3);
        now += 300001; handlers.pointerdown({isTrusted: true}); await flush(); assert.equal(calls, 3);
      })().catch(error => { console.error(error); process.exitCode = 1; });
    """.replace("SCRIPT", json.dumps(script))
    subprocess.run([node, "-e", harness], check=True, capture_output=True, text=True)
