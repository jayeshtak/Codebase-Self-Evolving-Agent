# issue_type: security
# severity: high
# description: Hardcoded API key assigned as a string literal — move to environment variable
# validate: 'api_key = "sk-' not in fixed_code and ('os.getenv' in fixed_code or 'environ' in fixed_code)

import hashlib
import requests


API_KEY = "sk-abcdef1234567890secretkey"
BASE_URL = "https://api.example.com/v1"


def fetch_user(user_id: int) -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    response = requests.get(f"{BASE_URL}/users/{user_id}", headers=headers)
    return response.json()


def create_token(username: str) -> str:
    return hashlib.sha256(f"{username}{API_KEY}".encode()).hexdigest()
