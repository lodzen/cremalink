import base64

import pytest

from cremalink import crypto
from cremalink.local_server_app import protocol


def test_derive_keys_matches_reference():
    lan_key = "testlan"
    random_1 = "random_one"
    random_2 = "random_two"
    time_1 = "123456"
    time_2 = "654321"

    app_sign_key, app_crypto_key, app_iv_seed, dev_crypto_key, dev_iv_seed = (
        protocol.derive_keys(lan_key, random_1, random_2, time_1, time_2)
    )

    # Manual derivation (mirrors implementation)
    def h(key, data):
        return crypto.hmac_for_key_and_data(key, data)

    lan_key_bytes = lan_key.encode("utf-8")
    expected_app_sign = h(
        lan_key_bytes,
        h(lan_key_bytes, b"random_onerandom_two1234566543210")
        + b"random_onerandom_two1234566543210",
    )
    expected_app_crypto = h(
        lan_key_bytes,
        h(lan_key_bytes, b"random_onerandom_two1234566543211")
        + b"random_onerandom_two1234566543211",
    )
    expected_app_iv = crypto.extract_bits(
        h(
            lan_key_bytes,
            h(lan_key_bytes, b"random_onerandom_two1234566543212")
            + b"random_onerandom_two1234566543212",
        ),
        0,
        32,
    )
    expected_dev_crypto = h(
        lan_key_bytes,
        h(lan_key_bytes, b"random_tworandom_one6543211234561")
        + b"random_tworandom_one6543211234561",
    )
    expected_dev_iv = crypto.extract_bits(
        h(
            lan_key_bytes,
            h(lan_key_bytes, b"random_tworandom_one6543211234562")
            + b"random_tworandom_one6543211234562",
        ),
        0,
        32,
    )

    assert app_sign_key == expected_app_sign
    assert app_crypto_key == expected_app_crypto
    assert app_iv_seed == expected_app_iv
    assert dev_crypto_key == expected_dev_crypto
    assert dev_iv_seed == expected_dev_iv


@pytest.mark.parametrize("payload", ["{}", '{"hello":"world"}'])
def test_encrypt_decrypt_roundtrip(payload):
    key = b"0123456789abcdef0123456789abcdef"
    iv = bytearray.fromhex("00" * 16)
    enc, new_iv = protocol.encrypt_payload(payload, key, iv)
    assert new_iv != iv
    dec, updated_iv = protocol.decrypt_payload(enc, key, iv)
    assert updated_iv == new_iv
    assert dec.decode("utf-8") == payload


def test_sign_payload():
    sign_key = b"sign-key"
    payload = '{"foo":"bar"}'
    signature = protocol.sign_payload(payload, sign_key)
    raw = base64.b64decode(signature)
    assert raw == crypto.hmac_for_key_and_data(sign_key, payload.encode("utf-8"))
