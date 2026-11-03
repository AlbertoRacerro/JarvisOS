from app.modules.memory import jarvis_knowledge_actions as knowledge


def test_secret_screen_reuses_canonical_floor_for_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiJ1c2VyLTEyMyJ9."
        "c2lnbmF0dXJlMTIzNDU2"
    )

    assert knowledge._contains_secret_material([{"intent": f"Use {jwt} to clarify this requirement"}])


def test_secret_screen_blocks_standard_tokens_and_credential_urls() -> None:
    sensitive_values = [
        "xoxb-" + "1234567890-abcdefghijklmnop",
        "github_pat_" + "11AA22BB33CC44DD55EE66FF77GG88HH99",
        "postgresql://jarvis:supersecretpassword@db.internal/jarvis",
        "https://alice:supersecret@example.com/private",
        "http://alice:p@example.com/private",
        "sk_live_" + "abcdefghijklmnop",
        "glpat-" + "abcdefghijklmnop",
        "npm_abcdefghijklmnop",
        "AIzaabcdefghijklmnopqrstuv",
        "Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l",
        "Authorization: Basic dTpw",
        '{"password": "correcthorsebattery"}',
        '{"api_key":"abcdefghijklmnop"}',
        '"secret":"abcdefghijklmnop"',
    ]

    for value in sensitive_values:
        assert knowledge._contains_secret_material([{"content": value}])


def test_secret_screen_does_not_treat_basic_authentication_phrase_as_secret() -> None:
    assert not knowledge._contains_secret_material([{"intent": "Use basic authentication for this endpoint"}])
