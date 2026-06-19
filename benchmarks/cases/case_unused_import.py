# BENCHMARK CASE: unused_import
# issue_type: undefined_name
# severity: medium
# description: 'sys' imported but unused
# expect_fixed: import sys should be removed
# validate: "import sys" not in fixed_code

import os
import sys  # unused

def greet(name):
    return "Hello, " + name

def get_cwd():
    return os.getcwd()
