import hashlib
import base64
import secrets
from typing import Tuple


class PKCEHelper:
    @staticmethod
    def generate(code_verifier_length: int = 64) -> Tuple[str, str]:
        code_verifier = PKCEHelper.generate_code_verifier(code_verifier_length)
        code_challenge = PKCEHelper.generate_code_challenge(code_verifier)
        return code_verifier, code_challenge

    @staticmethod
    def generate_code_verifier(length: int = 64) -> str:
        raw = secrets.token_bytes(length)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def generate_code_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")