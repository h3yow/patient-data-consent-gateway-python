def test_app_imports() -> None:
    from main import app

    assert app.title == "Patient Data Consent Gateway"
