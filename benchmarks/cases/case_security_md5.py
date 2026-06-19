# BENCHMARK CASE: security_md5
# issue_type: security
# severity: high
# description: [B324] Use of weak MD5 hash
# expect_fixed: md5 replaced with sha256
# validate: "md5" not in fixed_code and "sha256" in fixed_code

import hashlib
import json

def hash_record(record):
    raw = json.dumps(record, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()

def verify_record(record, expected_hash):
    return hash_record(record) == expected_hash
