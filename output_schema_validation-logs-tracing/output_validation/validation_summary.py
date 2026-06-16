class ValidationSummaryGenerator:
    @staticmethod
    def generate(
        total_checks,
        violations
    ):
        
        failed_checks = len({v["field"] for v in violations})

        passed_checks = max(0, total_checks - failed_checks)

        health_score = round((passed_checks / total_checks) * 100, 1) if total_checks > 0 else 100.0

        return {
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "failed_checks": failed_checks,
            "health_score": health_score
        }