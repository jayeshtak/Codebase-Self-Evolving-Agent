# BENCHMARK CASE: dead_code
# issue_type: dead_code
# severity: low
# description: Unused function '_legacy_parse' never called
# expect_fixed: _legacy_parse should be removed
# validate: "_legacy_parse" not in fixed_code

import re

def parse(expr):
    return expr.strip().split()

def format_output(tokens):
    return " ".join(tokens)

# Dead — never called anywhere
def _legacy_parse(expr):
    tokens = expr.split(" ")
    return [t for t in tokens if t]
