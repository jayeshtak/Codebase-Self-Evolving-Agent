# issue_type: complexity
# severity: high
# description: 'evaluate_loan' has cyclomatic complexity 18 (very high, threshold=10)
# validate: len([l for l in fixed_code.splitlines() if l.strip().startswith('def ')]) > 1

def evaluate_loan(applicant):
    score = 0

    if applicant.get("age", 0) < 18:
        return {"approved": False, "reason": "Underage"}
    if applicant.get("age", 0) > 75:
        return {"approved": False, "reason": "Age limit exceeded"}

    income = applicant.get("income", 0)
    if income > 100000:
        score += 40
    elif income > 50000:
        score += 25
    elif income > 25000:
        score += 10
    else:
        score -= 10

    credit = applicant.get("credit_score", 0)
    if credit >= 750:
        score += 40
    elif credit >= 650:
        score += 20
    elif credit >= 550:
        score += 5
    else:
        score -= 20

    if applicant.get("employment") == "full_time":
        score += 15
    elif applicant.get("employment") == "part_time":
        score += 5
    elif applicant.get("employment") == "self_employed":
        score += 8
    else:
        score -= 15

    if applicant.get("existing_debt", 0) > income * 0.5:
        score -= 30
    elif applicant.get("existing_debt", 0) > income * 0.3:
        score -= 15

    if applicant.get("bankruptcies", 0) > 0:
        return {"approved": False, "reason": "Bankruptcy on record"}

    if applicant.get("collateral"):
        score += 10

    return {"approved": score >= 50, "score": score, "reason": "Evaluated"}
