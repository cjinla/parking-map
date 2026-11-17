"""Print Web Push (VAPID) keys for the Render dashboard. Writes nothing to disk.

    python vapid_keys.py

Paste the three lines into the la-parking service environment, then redeploy.
Never commit the private key.
"""
import base64
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_key_material():
    v = Vapid()
    v.generate_keys()
    public_raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    private_pem = v.private_pem().decode()
    return {"public_raw": public_raw, "public_key": b64url(public_raw),
            "private_pem": private_pem, "private_key": private_pem.replace("\n", "\\n")}


def main():
    keys = new_key_material()
    print("VAPID_PUBLIC_KEY=" + keys["public_key"])
    print("VAPID_PRIVATE_KEY=" + keys["private_key"])
    print("VAPID_SUBJECT=mailto:you@example.com")
    print("\nSet VAPID_SUBJECT to a real mailto: address. Apple requires one.")


if __name__ == "__main__":
    main()
