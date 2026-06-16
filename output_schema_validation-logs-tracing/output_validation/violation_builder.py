class ViolationBuilder:

    VIOLATION_MAPPING = {
        "type": "invalid_type",
        "required": "missing_required_field",
        "enum": "invalid_enum",
        "maximum": "range_violation",
        "minimum": "range_violation",
        "exclusiveMaximum": "range_violation",
        "exclusiveMinimum": "range_violation",
        "format": "format_validation_failed",
        "minLength": "length_violation",
        "maxLength": "length_violation",
        "minItems": "array_size_violation",
        "maxItems": "array_size_violation",
        "pattern": "pattern_mismatch",
        "additionalProperties": "unexpected_field"
    }

    SEVERITY_MAPPING = {
        "required": "ERROR",
        "type": "ERROR",
        "enum": "ERROR",
        "additionalProperties": "ERROR",
        "maximum": "WARNING",
        "minimum": "WARNING",
        "exclusiveMaximum": "WARNING",
        "exclusiveMinimum": "WARNING",
        "format": "WARNING",
        "minLength": "WARNING",
        "maxLength": "WARNING",
        "minItems": "WARNING",
        "maxItems": "WARNING",
        "pattern": "WARNING"
    }

    @classmethod
    def build(cls, error):
        violation_type = cls.VIOLATION_MAPPING.get(
                error.validator,
                error.validator
            )

        severity = cls.SEVERITY_MAPPING.get(error.validator, "ERROR")

        if error.validator == "required":
            field = error.message.split("'")[1]
        else:
            field = ".".join(map(str, error.path))

        return {
            "field": field,
            "violation_type": violation_type,
            "severity": severity,
            "message": error.message,
            "expected": error.validator_value,
            "actual": None if error.validator == "required" else error.instance
        }