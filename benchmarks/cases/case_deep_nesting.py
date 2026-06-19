# BENCHMARK CASE: deep_nesting
# issue_type: deep_nesting
# severity: medium
# description: Deeply nested block (depth 5)
# expect_fixed: nesting depth reduced below 5
# validate: fixed_code.count("        if ") < 3

def process(data, config):
    if data:
        if config:
            if config.get("enabled"):
                if data.get("items"):
                    if len(data["items"]) > 0:
                        return [x for x in data["items"] if x]
    return []
