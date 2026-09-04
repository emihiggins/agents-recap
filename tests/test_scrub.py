import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap.scrub import PLACEHOLDER, is_secret_key, scrub, scrub_obj

SECRETS = [
    "use sk-abcdefghijklmnopqrstuvwxyz123456 for openai",
    "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
    "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQdQw4w9WgXcQ",
    "AKIAIOSFODNN7EXAMPLE is the id",
    "ghp_16CharsMinimumHere1234",
    "password = hunter2000",
    "Authorization: Bearer abc123def456ghi789jkl",
    'my_api_key: "supersecretvalue"',
    "digest 5f4dcc3b5aa765d61d8327deb882cf99aa5f4dcc3b5aa765d61d8327deb882cf",
]

CLEAN = [
    "normal text about a graphql resolver bug in the rates API",
    "the file is at /Users/me/proj/src/index.ts line 42",
    "ran npm test and 3 specs failed in reservations.spec.ts",
    "branch feature/PROJ-874-error-leak is 2 commits ahead",
]


def test_secrets_are_redacted():
    for text in SECRETS:
        assert PLACEHOLDER in scrub(text), f"not redacted: {text}"


def test_no_secret_material_survives():
    """The point of the module: none of the raw values leak through."""
    leaks = ["sk-abcdefghij", "wJalrXUtnFEMIK7", "eyJhbGciOiJIUzI1NiJ9",
             "AKIAIOSFODNN7EXAMPLE", "ghp_16CharsMinimumHere1234",
             "hunter2000", "supersecretvalue"]
    joined = " ".join(scrub(t) for t in SECRETS)
    for leak in leaks:
        assert leak not in joined, f"leaked: {leak}"


def test_ordinary_text_is_untouched():
    for text in CLEAN:
        assert scrub(text) == text, f"over-redacted: {text}"


def test_field_names_survive_redaction():
    """Keep the name so the recap still reads sensibly."""
    assert "AWS_SECRET_ACCESS_KEY" in scrub("AWS_SECRET_ACCESS_KEY=abcdefghijkl")


def test_empty_and_none():
    assert scrub(None) is None
    assert scrub("") == ""


def test_secret_key_detection():
    assert is_secret_key("blobEncryptionKey")
    assert is_secret_key("speculativeSummarizationEncryptionKey")
    assert not is_secret_key("name")
    assert not is_secret_key("project_path")


def test_scrub_obj_drops_secret_keys_recursively():
    out = scrub_obj({
        "name": "session",
        "blobEncryptionKey": "AAAABBBBCCCCDDDD",
        "nested": [{"speculativeSummarizationEncryptionKey": "x", "text": "sk-aaaaaaaaaaaaaaaaaaaaaaaa"}],
    })
    assert "blobEncryptionKey" not in out
    assert "speculativeSummarizationEncryptionKey" not in out["nested"][0]
    assert out["name"] == "session"
    assert PLACEHOLDER in out["nested"][0]["text"]
