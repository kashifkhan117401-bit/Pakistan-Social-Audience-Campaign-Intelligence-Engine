"""End-to-end verification for authentication, CAPTCHA, CSRF, and protected routes."""
import re
import tempfile
from pathlib import Path
 
from campaign_engine import auth, captcha, config
from campaign_engine.webapp import create_app


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "CSRF token input not found in form HTML"
    return m.group(1)


def _solve_session_captcha(client, mode="text", answer="TEST99"):
    """Inject a known salted CAPTCHA challenge into the test client's session."""
    with client.session_transaction() as sess:
        salt = "test-salt-123"
        sess["captcha"] = {
            "id": "test-id",
            "mode": mode,
            "salt": salt,
            "hash": captcha._hash_answer(answer, salt),
            "issued_at": __import__("time").time(),
        }
    return answer


def run_tests():
    # Use an isolated temporary auth DB so running tests never pollutes data/auth.db
    with tempfile.TemporaryDirectory() as tmp:
        orig_auth_db = config.AUTH_DB_PATH
        config.AUTH_DB_PATH = Path(tmp) / "test_auth.db"
        try:
            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            # 1. Public healthz, PWA manifest/sw & login/register/captcha accessible
            assert client.get("/healthz").status_code == 200
            r_man = client.get("/manifest.webmanifest")
            assert r_man.status_code == 200
            assert "Campaign Intelligence" in r_man.get_data(as_text=True)
            assert client.get("/sw.js").status_code == 200
            r_login = client.get("/login")
            assert r_login.status_code == 200
            assert "Human Verification" in r_login.get_data(as_text=True)

            r_reg = client.get("/register")
            assert r_reg.status_code == 200

            for mode in ("text", "math"):
                r_cap = client.get(f"/captcha.svg?mode={mode}")
                assert r_cap.status_code == 200
                assert r_cap.mimetype == "image/svg+xml"
                assert "<svg" in r_cap.get_data(as_text=True)

            # 2. Protected routes redirect unauthenticated users; API returns 401
            protected = [
                "/", "/budget", "/youtube", "/database", "/posts",
                "/comments", "/content", "/audience", "/anomalies",
                "/segments", "/recommendations", "/analyze", "/about", "/account",
            ]
            for route in protected:
                res = client.get(route)
                assert res.status_code == 302, f"{route} should redirect when unauthenticated, got {res.status_code}"
                assert "/login" in res.headers["Location"]

            assert client.post("/api/analyze", json={"text": "good"}).status_code == 401
            assert client.post("/api/sql", json={"sql": "SELECT 1"}).status_code == 401

            # 3. Wrong CAPTCHA rejected on login
            csrf = _extract_csrf(client.get("/login").get_data(as_text=True))
            _solve_session_captcha(client, answer="ABC234")
            res = client.post("/login", data={
                "csrf_token": csrf,
                "identifier": "admin",
                "password": "Admin@1234",
                "captcha": "WRONG1",
            })
            assert res.status_code == 200
            assert "Incorrect CAPTCHA" in res.get_data(as_text=True)

            # 4. CAPTCHA replay rejected (single-use)
            res = client.post("/login", data={
                "csrf_token": csrf,
                "identifier": "admin",
                "password": "Admin@1234",
                "captcha": "ABC234",
            })
            assert res.status_code == 200
            assert "expired or missing" in res.get_data(as_text=True)

            # 5. Registration with valid CAPTCHA & strong password
            csrf = _extract_csrf(client.get("/register").get_data(as_text=True))
            code = _solve_session_captcha(client, answer="REG789")
            res = client.post("/register", data={
                "csrf_token": csrf,
                "full_name": "Test Analyst",
                "username": "testanalyst",
                "email": "test.analyst@example.pk",
                "password": "StrongPass@123",
                "confirm_password": "StrongPass@123",
                "captcha": code.lower(),  # case-insensitive check
            }, follow_redirects=False)
            assert res.status_code == 302
            assert res.headers["Location"].endswith("/")

            # Logout new user
            acct_html = client.get("/account").get_data(as_text=True)
            assert "Test Analyst" in acct_html
            csrf = _extract_csrf(acct_html)
            res_out = client.post("/logout", data={"csrf_token": csrf})
            assert res_out.status_code == 302
            assert client.get("/").status_code == 302

            # 6. Sign in as seeded admin with valid CAPTCHA
            csrf = _extract_csrf(client.get("/login").get_data(as_text=True))
            code = _solve_session_captcha(client, answer="ADM456")
            res = client.post("/login", data={
                "csrf_token": csrf,
                "identifier": "admin",
                "password": "Admin@1234",
                "captcha": code,
                "remember": "1",
            })
            assert res.status_code == 302

            # 7. All protected routes now return 200 OK
            for route in protected:
                res = client.get(route)
                print(f"Route {route:20} -> Status: {res.status_code}")
                assert res.status_code == 200, f"{route} returned {res.status_code}"

            # 8. API endpoints work when authenticated & SQL console cannot access auth.db users
            res_an = client.post("/api/analyze", json={"text": "Yeh dress bht khubsurat hai!"})
            assert res_an.status_code == 200
            assert "sentiment" in res_an.get_json()

            res_sql_ok = client.post("/api/sql", json={"sql": "SELECT COUNT(*) AS n FROM post"})
            assert res_sql_ok.status_code == 200

            res_sql_iso = client.post("/api/sql", json={"sql": "SELECT * FROM user"})
            assert res_sql_iso.status_code == 400  # isolated in auth.db, does not exist in campaign.db

            # 8b. Privacy permission banner & consent persistence with reason
            dash_before = client.get("/").get_data(as_text=True)
            assert 'data-consent-open="true"' in dash_before
            csrf_admin = _extract_csrf(dash_before)

            res_rej = client.post(
                "/api/consent",
                json={"decision": "rejected_all", "reason": "Strict privacy preference"},
                headers={"X-CSRF-Token": csrf_admin},
            )
            assert res_rej.status_code == 200
            c_rej = res_rej.get_json()["consent"]
            assert c_rej["decision"] == "rejected_all"
            assert c_rej["essential"] == 1 and c_rej["analytics"] == 0 and c_rej["personalization"] == 0
            assert c_rej["reason"] == "Strict privacy preference"

            dash_after = client.get("/").get_data(as_text=True)
            assert 'data-consent-open="false"' in dash_after

            acct_after = client.get("/account").get_data(as_text=True)
            assert "Strict privacy preference" in acct_after
            assert "Rejected All" in acct_after

            res_acc = client.post(
                "/api/consent",
                json={"decision": "accepted_all", "reason": "Need full dashboard analytics"},
                headers={"X-CSRF-Token": csrf_admin},
            )
            assert res_acc.status_code == 200
            c_acc = res_acc.get_json()["consent"]
            assert c_acc["analytics"] == 1 and c_acc["personalization"] == 1 and c_acc["diagnostics"] == 1
            assert c_acc["reason"] == "Need full dashboard analytics"

            res_cust = client.post(
                "/api/consent",
                json={
                    "decision": "custom",
                    "analytics": True,
                    "personalization": False,
                    "diagnostics": True,
                    "reason": "Analytics and diagnostics only",
                },
                headers={"X-CSRF-Token": csrf_admin},
            )
            assert res_cust.status_code == 200
            c_cust = res_cust.get_json()["consent"]
            assert c_cust["analytics"] == 1 and c_cust["personalization"] == 0 and c_cust["diagnostics"] == 1
            assert c_cust["reason"] == "Analytics and diagnostics only"

            # 9. Lockout check on analyst account
            client2 = app.test_client()
            for i in range(config.AUTH_LOCKOUT_THRESHOLD):
                c2 = _extract_csrf(client2.get("/login").get_data(as_text=True))
                code = _solve_session_captcha(client2, answer=f"TRY{i}99")
                r_fail = client2.post("/login", data={
                    "csrf_token": c2,
                    "identifier": "analyst",
                    "password": "WrongPassword!1",
                    "captcha": code,
                })
                assert r_fail.status_code == 200
            assert "locked" in r_fail.get_data(as_text=True).lower()

            print("\n✅ All authentication, CAPTCHA, CSRF, lockout, isolation, and route checks passed!")
        finally:
            config.AUTH_DB_PATH = orig_auth_db


if __name__ == "__main__":
    run_tests()
