from app.main import _extract_oauth_callback


def test_extract_from_catcher_url():
    code, realm = _extract_oauth_callback(
        "https://nmarquettej.github.io/Vet-Margin-Dashboard/qbo-oauth-catcher/"
        "?code=AUTHCODE&state=x&realmId=9341457566470183"
    )
    assert code == "AUTHCODE"
    assert realm == "9341457566470183"


def test_extract_from_query_string():
    code, realm = _extract_oauth_callback("code=ABC&realm_id=999")
    assert code == "ABC"
    assert realm == "999"


if __name__ == "__main__":
    test_extract_from_catcher_url()
    test_extract_from_query_string()
    print("ok")
